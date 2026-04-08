"""Raw daily OI snapshot persistence — 5-day rolling per ticker.

Spec: docs/superpowers/specs/2026-04-08-uw-analyze-overhaul-design.md §"Daily OI tracker"
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Optional

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

logger = logging.getLogger("xenon.uw_analyze_oi_snapshots")

RETENTION_DAYS = 5
_DEFAULT_DIR = _SCRIPTS.parent / "data" / "uw_oi_snapshots"


def _path_for(ticker: str, data_dir: Optional[Path]) -> Path:
    base = Path(data_dir) if data_dir else _DEFAULT_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{ticker.upper()}.json"


def load_history(ticker: str, data_dir: Optional[Path] = None) -> list[dict]:
    p = _path_for(ticker, data_dir)
    if not p.exists():
        return []
    try:
        payload = json.loads(p.read_text())
        return list(payload.get("history") or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("oi snapshot load failed for %s: %s", ticker, exc)
        return []


def _atomic_write(path: Path, payload: dict) -> None:
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".oi_snap_", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def snapshot_oi(ticker: str, chain_client: Any, *, data_dir: Optional[Path] = None) -> dict:
    """Fetch full chain OI for `ticker`, persist as today's snapshot, retain last RETENTION_DAYS days."""
    today = date.today().isoformat()
    resp = chain_client.get_option_chain(ticker, expiry=None) or {}
    raw = resp.get("data") if isinstance(resp, dict) else None
    strikes: dict[str, dict] = {}
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        strike = r.get("strike")
        if strike is None:
            continue
        key = f"{float(strike):g}"
        existing = strikes.setdefault(key, {"call_oi": 0, "put_oi": 0})
        try:
            if r.get("call_oi") is not None:
                existing["call_oi"] = int(float(r["call_oi"]))
            if r.get("put_oi") is not None:
                existing["put_oi"] = int(float(r["put_oi"]))
        except (TypeError, ValueError):
            pass

    snap = {"data_date": today, "strikes": strikes}

    history = load_history(ticker, data_dir)
    history = [h for h in history if h.get("data_date") != today]
    history.append(snap)
    history.sort(key=lambda h: h.get("data_date") or "")
    history = history[-RETENTION_DAYS:]

    _atomic_write(_path_for(ticker, data_dir), {"updated_at": today, "history": history})
    return snap
