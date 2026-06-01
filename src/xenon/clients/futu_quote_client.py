"""Read-only OpenQuoteContext wrapper for get_rehab corporate-action lookups.

Only `get_rehab` is used. Caller owns OpenQuoteContext lifecycle.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterator

from xenon.utils.futu_api_stats import FutuApiStats

log = logging.getLogger(__name__)

# Futu company_act_flag bitmask (from Futu OpenAPI docs).
_SPLIT_FLAG = 0x04
_REVERSE_SPLIT_FLAG = 0x08


@dataclass
class CorporateAction:
    ticker: str
    action_type: str
    effective_date: date
    ratio_numerator: Decimal
    ratio_denominator: Decimal
    new_ticker: str | None
    raw: dict[str, Any]


def _normalize(code: str) -> str:
    return code.split(".", 1)[-1].upper() if code else code


class FutuQuoteClient:
    def __init__(self, quote_ctx) -> None:
        self._ctx = quote_ctx
        self._stats = FutuApiStats.instance()

    def fetch_corporate_actions(self, code: str) -> Iterator[CorporateAction]:
        t0 = time.monotonic()
        ret = self._ctx.get_rehab(code)
        latency = time.monotonic() - t0
        if not isinstance(ret, tuple) or ret[0] != 0:
            self._stats.record_call(
                "get_rehab",
                latency,
                ok=False,
                error_code=str(ret[0] if isinstance(ret, tuple) else "?"),
            )
            raise RuntimeError(f"get_rehab({code}) failed: {ret!r}")
        self._stats.record_call("get_rehab", latency, ok=True)
        df = ret[1]
        if df is None or df.empty:
            return
        ticker = _normalize(code)
        for row in df.to_dict(orient="records"):
            flag = int(row.get("company_act_flag", 0) or 0)
            split = Decimal(str(row.get("split_ratio", "1") or "1"))
            join = Decimal(str(row.get("join_ratio", "1") or "1"))
            if flag & _SPLIT_FLAG:
                yield CorporateAction(
                    ticker=ticker,
                    action_type="split",
                    effective_date=date.fromisoformat(str(row["ex_div_date"])),
                    ratio_numerator=split,
                    ratio_denominator=join,
                    new_ticker=None,
                    raw=row,
                )
            elif flag & _REVERSE_SPLIT_FLAG:
                yield CorporateAction(
                    ticker=ticker,
                    action_type="reverse_split",
                    effective_date=date.fromisoformat(str(row["ex_div_date"])),
                    ratio_numerator=split,
                    ratio_denominator=join,
                    new_ticker=None,
                    raw=row,
                )
