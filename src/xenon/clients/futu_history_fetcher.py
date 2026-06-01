"""Read-only history fetcher for Futu OpenSecTradeContext.

Wraps history_deal_list_query / history_order_list_query / get_acc_cash_flow
with 90-day paging, US-market filter, rate-limit retry, malformed-row
tolerance. Records per-call stats via FutuApiStats.

**SDK-probe caveat (C23)**: this module assumes Futu's `create_time` field
is emitted as naive `YYYY-MM-DD HH:MM:SS` strings interpreted in
America/New_York time. Step 0 of plan Task 5 documents how to verify this
against a live OpenD instance. If the operator probe shows UTC instead,
patch `_parse_ny_dt` per the plan's alternative implementation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterator

from xenon.utils.futu_api_stats import FutuApiStats

log = logging.getLogger(__name__)
_PAGE_DAYS = 90
_RATE_LIMIT_KEYWORDS = ("rate limit", "frequency control", "too frequent")

# Sum ALL fee components rather than picking one; Futu may return commission +
# platform fee + clearing fee on the same row. Update via Step 0 probe if
# Futu has added/renamed fee columns in newer releases.
_FEE_COLUMNS = (
    "counter_broker_commission",
    "platform_fee",
    "clearing_fee",
    "commission",
    "fee",
)


def _sum_fees(row: dict[str, Any]) -> Decimal:
    total = Decimal("0")
    for col in _FEE_COLUMNS:
        v = row.get(col)
        if v in (None, ""):
            continue
        try:
            total += Decimal(str(v))
        except Exception:
            continue
    return total


@dataclass
class FutuFill:
    futu_deal_id: str
    futu_order_id: str
    ticker: str
    futu_code: str
    market: str
    action: str
    quantity: int
    price: Decimal
    fees: Decimal
    filled_at: datetime
    raw: dict[str, Any]


@dataclass
class FutuCashFlowRow:
    futu_flow_id: str
    futu_type_raw: str
    amount_original: Decimal
    original_currency: str
    description: str | None
    ticker: str | None
    flow_at: datetime
    raw: dict[str, Any]


def _normalize_code(code: str) -> tuple[str, str]:
    if not code or "." not in code:
        return "", code
    market, sym = code.split(".", 1)
    return market.upper(), sym.upper()


def _parse_ny_dt(s: str) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("America/New_York"))


def _is_rate_limit_error(message: str) -> bool:
    msg = (message or "").lower()
    return any(k in msg for k in _RATE_LIMIT_KEYWORDS)


class FutuHistoryFetcher:
    def __init__(
        self,
        trade_ctx,
        trd_env: str = "REAL",
        max_retries: int = 3,
        retry_sleep_s: float = 2.0,
    ) -> None:
        self._ctx = trade_ctx
        self._trd_env = trd_env
        self._max_retries = max_retries
        self._retry_sleep_s = retry_sleep_s
        self._stats = FutuApiStats.instance()

    def _chunks(self, start: date, end: date) -> Iterator[tuple[date, date]]:
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=_PAGE_DAYS - 1), end)
            yield cursor, chunk_end
            cursor = chunk_end + timedelta(days=1)

    def _call_with_retry(self, fn_name: str, fn, *args, **kwargs):
        for attempt in range(self._max_retries):
            t0 = time.monotonic()
            ret = fn(*args, **kwargs)
            latency = time.monotonic() - t0
            if isinstance(ret, tuple) and len(ret) == 2:
                code, payload = ret
            else:
                code, payload = -1, str(ret)
            if code == 0:
                self._stats.record_call(fn_name, latency, ok=True)
                return payload
            if _is_rate_limit_error(str(payload)):
                self._stats.record_call(fn_name, latency, ok=False, error_code="rate_limit")
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_sleep_s * (2**attempt))
                    continue
                raise RuntimeError(f"{fn_name}: rate limit after retries")
            self._stats.record_call(fn_name, latency, ok=False, error_code=f"code_{code}")
            raise RuntimeError(f"{fn_name} failed: code={code} payload={payload!r}")
        raise RuntimeError(f"{fn_name}: max retries exhausted")

    def fetch_deals(self, start: date, end: date, acc_id: str) -> Iterator[FutuFill]:
        for s, e in self._chunks(start, end):
            df = self._call_with_retry(
                "history_deal_list_query",
                self._ctx.history_deal_list_query,
                start=s.strftime("%Y-%m-%d 00:00:00"),
                end=e.strftime("%Y-%m-%d 23:59:59"),
                acc_id=acc_id,
                trd_env=self._trd_env,
            )
            if df is None or df.empty:
                continue
            for row in df.to_dict(orient="records"):
                fill = self._row_to_fill(row)
                if fill is None or fill.market != "US":
                    continue
                yield fill

    def _row_to_fill(self, row: dict[str, Any]) -> FutuFill | None:
        deal_id = row.get("deal_id")
        if not deal_id:
            log.warning("dropping malformed Futu deal row: %s", row)
            return None
        market, ticker = _normalize_code(row.get("code", ""))
        try:
            return FutuFill(
                futu_deal_id=str(deal_id),
                futu_order_id=str(row.get("order_id", "")),
                ticker=ticker,
                futu_code=row.get("code", ""),
                market=market,
                action=str(row.get("trd_side", "")).upper(),
                quantity=int(row.get("qty", 0)),
                price=Decimal(str(row.get("price", "0"))),
                fees=_sum_fees(row),
                filled_at=_parse_ny_dt(str(row.get("create_time"))),
                raw=row,
            )
        except Exception:
            log.exception("malformed Futu deal row: %s", row)
            return None

    def fetch_cash_flow(self, start: date, end: date, acc_id: str) -> Iterator[FutuCashFlowRow]:
        for s, e in self._chunks(start, end):
            df = self._call_with_retry(
                "get_acc_cash_flow",
                self._ctx.get_acc_cash_flow,
                clearing_date_start=s.strftime("%Y-%m-%d"),
                clearing_date_end=e.strftime("%Y-%m-%d"),
                acc_id=acc_id,
                trd_env=self._trd_env,
            )
            if df is None or df.empty:
                continue
            for row in df.to_dict(orient="records"):
                cf = self._row_to_cash_flow(row)
                if cf is not None:
                    yield cf

    def _row_to_cash_flow(self, row: dict[str, Any]) -> FutuCashFlowRow | None:
        fid = row.get("flow_id") or row.get("cashflow_id")
        if not fid:
            log.warning("dropping malformed Futu cash flow row: %s", row)
            return None
        try:
            return FutuCashFlowRow(
                futu_flow_id=str(fid),
                futu_type_raw=str(row.get("cashflow_type", row.get("type", ""))),
                amount_original=Decimal(str(row.get("amount", "0"))),
                original_currency=str(row.get("currency", "USD")).upper(),
                description=row.get("remark") or row.get("cashflow_remark"),
                ticker=row.get("stock_code") or None,
                flow_at=_parse_ny_dt(str(row.get("create_time", row.get("cashflow_time")))),
                raw=row,
            )
        except Exception:
            log.exception("malformed Futu cash flow row: %s", row)
            return None
