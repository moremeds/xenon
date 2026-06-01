"""Thread-safe per-call latency / error counter for Futu SDK calls.

Mirrors `xenon.utils.uw_api_stats` but in-memory only (no Postgres persistence
yet — the sync runs are one-shot CLIs so process-local stats are sufficient).
The /futu-stats FastAPI route reads `FutuApiStats.instance().snapshot()`.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any


class FutuApiStats:
    _instance: "FutuApiStats | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "calls": 0,
                "errors": 0,
                "latency_sum_s": 0.0,
                "error_codes": defaultdict(int),
            }
        )

    @classmethod
    def instance(cls) -> "FutuApiStats":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def record_call(
        self,
        fn: str,
        latency_s: float,
        ok: bool,
        error_code: str | None = None,
    ) -> None:
        with self._lock:
            row = self._calls[fn]
            row["calls"] += 1
            row["latency_sum_s"] += latency_s
            if not ok:
                row["errors"] += 1
                if error_code:
                    row["error_codes"][error_code] += 1

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            out: dict[str, dict[str, Any]] = {}
            for fn, row in self._calls.items():
                calls = row["calls"]
                out[fn] = {
                    "calls": calls,
                    "errors": row["errors"],
                    "mean_latency_s": ((row["latency_sum_s"] / calls) if calls else 0.0),
                    "error_codes": dict(row["error_codes"]),
                }
            return out

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()
