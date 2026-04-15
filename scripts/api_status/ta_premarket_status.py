"""Typed read/write for data/ta_premarket_status.json.

Records outcome of the 6 AM ET premarket refresh so trend_scan can
surface staleness at 8:30 AM ET.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger(__name__)

_Status = Literal["ok", "aborted", "partial"]


@dataclass
class TaPremarketStatus:
    last_run_ts: datetime
    status: _Status
    refreshed_tickers: int
    failed_tickers: int
    failure_ratio: float
    failure_reason: Optional[str]


def save_status(path: Path, status: TaPremarketStatus) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(status)
    payload["last_run_ts"] = status.last_run_ts.isoformat()
    path.write_text(json.dumps(payload, indent=2))


def load_status(path: Path) -> Optional[TaPremarketStatus]:
    path = Path(path)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        return TaPremarketStatus(
            last_run_ts=datetime.fromisoformat(raw["last_run_ts"]),
            status=raw["status"],
            refreshed_tickers=int(raw["refreshed_tickers"]),
            failed_tickers=int(raw["failed_tickers"]),
            failure_ratio=float(raw["failure_ratio"]),
            failure_reason=raw.get("failure_reason"),
        )
    except (ValueError, KeyError) as exc:
        logger.warning("Failed to parse ta_premarket_status.json: %s", exc)
        return None
