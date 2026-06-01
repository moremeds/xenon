"""Read-only history fetcher for Futu OpenSecTradeContext.

Wraps history_deal_list_query / get_acc_cash_flow with rate-limit retry and
malformed-row tolerance. Records per-call stats via FutuApiStats.

SDK contract pinned against futu==10.03.6308 + Futu official OpenAPI docs
(checked 2026-06-01 during the C23 verification pass). Key facts:

  - history_deal_list_query(code='', start='YYYY-MM-DD HH:MM:SS',
        end='YYYY-MM-DD HH:MM:SS', trd_env='REAL', acc_id=0, ...).
    Returned columns: code, stock_name, deal_market, deal_id, order_id,
    qty, price, trd_side, create_time, counter_broker_id,
    counter_broker_name, status, jp_acc_type. **No fee columns** — fees
    are reconciled at sync-time from get_acc_cash_flow rows with
    cashflow_type in {'COMMISSION', 'PLATFORM_FEE', 'CLEARING_FEE'}.
    Range supported (max 90 days per call).

  - get_acc_cash_flow(clearing_date='YYYY-MM-DD', trd_env='REAL',
        acc_id=0, acc_index=0, cashflow_direction='N/A').
    Takes a SINGLE clearing date — to backfill a date range we iterate
    day-by-day. Returned columns: cashflow_id, clearing_date,
    settlement_date, currency, cashflow_type, cashflow_direction,
    cashflow_amount, cashflow_remark.

`create_time` timezone caveat (C23): Futu emits the deal create_time
exactly as configured in OpenD's settings — the SDK does not transform
it. OpenD ships with no fixed default; operators set the timezone
explicitly. `_parse_ny_dt` assumes America/New_York; an OpenD configured
to UTC or HK will silently bucket fills to the wrong calendar date.
Plan Task 5 Step 0 describes the operator probe. The sync orchestrator
SHOULD persist the detected OpenD TZ on first run and refuse to run on
mismatch — that automation is a follow-up.
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
    # Fees are ALWAYS Decimal('0') on deal rows: the deal_list endpoint does
    # not return any fee column. Real fees are reconciled in the sync
    # orchestrator by joining cash-flow rows whose cashflow_type is a fee
    # type and whose row references the deal via cashflow_remark.
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
    """Parse a Futu 'YYYY-MM-DD HH:MM:SS[.ms]' string as America/New_York.

    Tolerates an optional millisecond fragment that the SDK passes through
    when OpenD reports sub-second precision.
    """
    from zoneinfo import ZoneInfo

    s = s.strip()
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in s else "%Y-%m-%d %H:%M:%S"
    return datetime.strptime(s, fmt).replace(tzinfo=ZoneInfo("America/New_York"))


def _parse_ny_date_to_dt(s: str) -> datetime:
    """Coerce a 'YYYY-MM-DD' date string to 00:00:00 America/New_York.

    get_acc_cash_flow returns clearing_date as a date only; the schema's
    flow_at column is TIMESTAMP(timezone=True), so we anchor at midnight ET.
    """
    from zoneinfo import ZoneInfo

    d = date.fromisoformat(s.strip())
    return datetime(d.year, d.month, d.day, tzinfo=ZoneInfo("America/New_York"))


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

    def _iter_days(self, start: date, end: date) -> Iterator[date]:
        cursor = start
        while cursor <= end:
            yield cursor
            cursor += timedelta(days=1)

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
                # No fee columns on deal rows — see module docstring.
                fees=Decimal("0"),
                filled_at=_parse_ny_dt(str(row.get("create_time"))),
                raw=row,
            )
        except Exception:
            log.exception("malformed Futu deal row: %s", row)
            return None

    def fetch_cash_flow(self, start: date, end: date, acc_id: str) -> Iterator[FutuCashFlowRow]:
        """Iterate day-by-day — get_acc_cash_flow takes a SINGLE clearing_date.

        The Futu SDK signature is `get_acc_cash_flow(clearing_date='', ...)`
        with one date per call; there is no range form. To backfill a
        window we call once per day and concatenate.
        """
        for d in self._iter_days(start, end):
            df = self._call_with_retry(
                "get_acc_cash_flow",
                self._ctx.get_acc_cash_flow,
                clearing_date=d.strftime("%Y-%m-%d"),
                acc_id=acc_id,
                trd_env=self._trd_env,
            )
            if df is None or df.empty:
                continue
            for row in df.to_dict(orient="records"):
                cf = self._row_to_cash_flow(row, fallback_date=d)
                if cf is not None:
                    yield cf

    def _row_to_cash_flow(self, row: dict[str, Any], fallback_date: date | None = None) -> FutuCashFlowRow | None:
        # SDK column is `cashflow_id`; legacy fallback to `flow_id` kept
        # in case Futu releases an older OpenD with the older name.
        fid = row.get("cashflow_id") or row.get("flow_id")
        if not fid:
            log.warning("dropping malformed Futu cash flow row: %s", row)
            return None
        try:
            # SDK keys: cashflow_type, cashflow_amount, clearing_date,
            # currency, cashflow_remark, cashflow_direction, settlement_date.
            cf_type = row.get("cashflow_type") or row.get("type", "")
            amount = row.get("cashflow_amount")
            if amount is None:
                # Legacy mock fallback only — real SDK always emits cashflow_amount.
                amount = row.get("amount", "0")
            currency = row.get("currency", "USD")
            description = row.get("cashflow_remark") or row.get("remark")
            ticker = row.get("stock_code") or None

            clearing = row.get("clearing_date")
            if clearing:
                flow_at = _parse_ny_date_to_dt(str(clearing))
            elif fallback_date is not None:
                flow_at = _parse_ny_date_to_dt(fallback_date.isoformat())
            else:
                # Last-resort legacy fallback for mocks emitting full timestamps.
                flow_at = _parse_ny_dt(str(row.get("create_time", row.get("cashflow_time"))))

            return FutuCashFlowRow(
                futu_flow_id=str(fid),
                futu_type_raw=str(cf_type),
                amount_original=Decimal(str(amount)),
                original_currency=str(currency).upper(),
                description=description,
                ticker=ticker,
                flow_at=flow_at,
                raw=row,
            )
        except Exception:
            log.exception("malformed Futu cash flow row: %s", row)
            return None
