"""Order read endpoints backed by Postgres."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select

from xenon.api.guards import get_broker_scope
from xenon.db.engine import get_sync_engine
from xenon.db.schema import futu_order_fees, futu_orders, futu_trades, order_fills, order_submissions
from xenon.execution.account_scope import AccountScope

router = APIRouter()

ACTIVE_STATES = {"PENDING", "WORKING", "PARTIALLY_FILLED"}

_ET_ZONE = ZoneInfo("America/New_York")


def _today_et_start_utc(now: datetime | None = None) -> datetime:
    """Start of the current ET calendar day, as a UTC instant.

    The "Today's Executed Orders" panel and the Realized P&L card both mean the
    current Eastern trading day; this mirrors web/lib/realized-pnl.ts
    (fillDateET/todayET) so the executed-fills query agrees with them.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(_ET_ZONE)
    start_et = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_et.astimezone(timezone.utc)


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value) if value is not None else ""


def _date_iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _display_symbol(ticker: str, sec_type: str, right: str | None, strike: Any) -> str:
    if sec_type == "OPT" and right and strike is not None:
        strike_value = _float_or_none(strike)
        strike_text = f"{strike_value:g}" if strike_value is not None else str(strike)
        return f"{ticker} {right}{strike_text}"
    if sec_type == "BAG":
        return f"{ticker} Spread"
    return ticker


def _contract(
    *,
    ticker: str,
    sec_type: str,
    con_id: Any,
    strike: Any = None,
    right: str | None = None,
    expiry: Any = None,
) -> dict[str, Any]:
    return {
        "conId": _int_or_zero(con_id) if con_id is not None else None,
        "symbol": ticker,
        "secType": sec_type,
        "strike": _float_or_none(strike),
        "right": right,
        "expiry": _date_iso(expiry),
    }


def _status_from_state(state: str) -> str:
    return {
        "PENDING": "PendingSubmit",
        "WORKING": "Submitted",
        "PARTIALLY_FILLED": "PartiallyFilled",
    }.get(state, state)


def _open_order(row: dict[str, Any]) -> dict[str, Any]:
    filled = int(row.get("filled_qty") or 0)
    total = int(row["quantity"])
    sec_type = str(row["security_type"])
    ticker = str(row["ticker"])
    return {
        "submissionId": row["submission_id"],
        "orderId": _int_or_zero(row.get("ib_order_id")),
        "permId": _int_or_zero(row.get("perm_id")),
        "symbol": _display_symbol(ticker, sec_type, row.get("right"), row.get("strike")),
        "contract": _contract(
            ticker=ticker,
            sec_type=sec_type,
            con_id=row.get("con_id"),
            strike=row.get("strike"),
            right=row.get("right"),
            expiry=row.get("expiry"),
        ),
        "action": row["action"],
        "orderType": "LMT" if row.get("limit_price") is not None else "MKT",
        "totalQuantity": total,
        "limitPrice": _float_or_none(row.get("limit_price")),
        "auxPrice": None,
        "status": _status_from_state(str(row["state"])),
        "filled": filled,
        "remaining": max(total - filled, 0),
        "avgFillPrice": _float_or_none(row.get("avg_fill_price")),
        "tif": str(row.get("tif") or "DAY"),
        "modifySequence": int(row.get("modify_sequence") or 0),
    }


def _executed_order(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") or {}
    sec_type = _fill_sec_type(row, metadata)
    side = str(row["side"]).upper()
    return {
        "execId": row["exec_id"],
        "symbol": _display_symbol(str(row["ticker"]), sec_type, metadata.get("right"), metadata.get("strike")),
        "contract": _contract(
            ticker=str(row["ticker"]),
            sec_type=sec_type,
            con_id=row.get("con_id"),
            strike=metadata.get("strike"),
            right=metadata.get("right"),
            expiry=metadata.get("expiry"),
        ),
        "side": "BOT" if side == "BUY" else "SLD" if side == "SELL" else side,
        "quantity": float(row["qty"]),
        "avgPrice": _float_or_none(row.get("price")),
        "commission": _float_or_none(row.get("commission")),
        "realizedPNL": _float_or_none(metadata.get("realized_pnl")),
        "currency": str(metadata.get("currency") or "USD"),
        "time": _iso(row.get("filled_at")),
        "exchange": str(metadata.get("exchange") or ""),
    }


def _fill_sec_type(row: dict[str, Any], metadata: dict[str, Any]) -> str:
    explicit = metadata.get("sec_type")
    if explicit:
        return str(explicit)
    submission_sec_type = row.get("submission_security_type")
    if submission_sec_type == "BAG":
        submission_con_id = row.get("submission_con_id")
        if submission_con_id is not None and row.get("con_id") == submission_con_id:
            return "BAG"
        return "OPT"
    return str(submission_sec_type or "STK")


# --- Futu read path: shape futu_orders/futu_trades into the IB OpenOrder/ExecutedOrder contract ---

_FUTU_STATUS = {
    "SUBMITTING": "PendingSubmit",
    "WAITING_SUBMIT": "PendingSubmit",
    "SUBMITTED": "Submitted",
    "FILLED_PART": "PartiallyFilled",
    "FILLED_ALL": "Filled",
    "CANCELLED_ALL": "Cancelled",
    "CANCELLED_PART": "Cancelled",
    "CANCELLING_PART": "Submitted",
    "CANCELLING_ALL": "Submitted",
}
_FUTU_TYPE = {"NORMAL": "LMT", "MARKET": "MKT"}  # else passthrough raw Futu label
_FUTU_OPEN_STATUSES = ("SUBMITTING", "SUBMITTED", "WAITING_SUBMIT", "FILLED_PART", "CANCELLING_PART", "CANCELLING_ALL")
# OCC option symbol: <underlier><YYMMDD><C|P><strike*1000>.
_OCC = re.compile(r"^(?P<u>[A-Z]+)(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})(?P<cp>[CP])(?P<strike>\d+)$")


def _futu_contract(ticker: str) -> dict[str, Any]:
    m = _OCC.match(ticker)
    if not m:
        return {"conId": None, "symbol": ticker, "secType": "STK", "strike": None, "right": None, "expiry": None}
    return {
        "conId": None,
        "symbol": m["u"],
        "secType": "OPT",
        "strike": int(m["strike"]) / 1000.0,
        "right": m["cp"],
        "expiry": f"20{m['y']}-{m['m']}-{m['d']}",
    }


def _futu_surrogate_id(futu_order_id: str) -> int:
    """Stable JS-safe (< 2^48) surrogate so frontend `${orderId}-${permId}` keys don't collide.

    Futu order ids are ~19-digit strings — using them as JS numbers loses precision past
    2^53. Frontend prefers the string submissionId for the key; this is the numeric fallback.
    """
    return int(hashlib.sha1(str(futu_order_id).encode()).hexdigest()[:12], 16)


def _futu_open_order(row: dict[str, Any]) -> dict[str, Any]:
    contract = _futu_contract(str(row["ticker"]))
    total = int(row["quantity"])
    filled = int(row.get("filled_qty") or 0)
    oid = _futu_surrogate_id(row["futu_order_id"])
    return {
        "submissionId": str(row["futu_order_id"]),
        "orderId": oid,
        "permId": oid,
        "symbol": _display_symbol(contract["symbol"], contract["secType"], contract["right"], contract["strike"]),
        "contract": contract,
        "action": row["action"],
        "orderType": _FUTU_TYPE.get(row["order_type"], row["order_type"]),
        "totalQuantity": total,
        "limitPrice": _float_or_none(row.get("limit_price")),
        "auxPrice": _float_or_none(row.get("aux_price")),
        "status": _FUTU_STATUS.get(row["status"], row["status"]),
        "filled": filled,
        "remaining": max(total - filled, 0),
        "avgFillPrice": _float_or_none(row.get("avg_fill_price")),
        "tif": str(row.get("tif") or "DAY"),
        "modifySequence": 0,
    }


def _futu_executed_order(row: dict[str, Any]) -> dict[str, Any]:
    contract = _futu_contract(str(row["ticker"]))
    side = str(row["action"]).upper()
    return {
        "execId": str(row["futu_deal_id"]),
        "symbol": _display_symbol(contract["symbol"], contract["secType"], contract["right"], contract["strike"]),
        "contract": contract,
        "side": "BOT" if side == "BUY" else "SLD" if side == "SELL" else side,
        "quantity": float(row["quantity"]),
        "avgPrice": _float_or_none(row.get("price")),
        "commission": _float_or_none(row.get("fees")),
        "realizedPNL": None,
        "time": _iso(row.get("filled_at")),
        "exchange": "FUTU",
    }


def _futu_orders_payload(scope: AccountScope, *, limit: int) -> dict[str, Any]:
    engine = get_sync_engine()
    with engine.connect() as conn:
        open_rows = [
            dict(r._mapping)
            for r in conn.execute(
                select(futu_orders)
                .where(
                    futu_orders.c.broker == scope.broker,
                    futu_orders.c.account_env == scope.account_env,
                    futu_orders.c.broker_account == scope.broker_account,
                    futu_orders.c.status.in_(_FUTU_OPEN_STATUSES),
                )
                .order_by(desc(futu_orders.c.updated_at))
                .limit(limit)
            ).all()
        ]
        fill_rows = [
            dict(r._mapping)
            for r in conn.execute(
                select(futu_trades)
                .where(
                    futu_trades.c.broker == scope.broker,
                    futu_trades.c.account_env == scope.account_env,
                    futu_trades.c.broker_account == scope.broker_account,
                    futu_trades.c.filled_at >= _today_et_start_utc(),
                )
                .order_by(desc(futu_trades.c.filled_at), desc(futu_trades.c.futu_deal_id))
                .limit(limit)
            ).all()
        ]
    open_orders = [_futu_open_order(row) for row in open_rows]
    executed_orders = [_futu_executed_order(row) for row in fill_rows]
    return {
        "last_sync": datetime.now(timezone.utc).isoformat(),
        "open_orders": open_orders,
        "executed_orders": executed_orders,
        "open_count": len(open_orders),
        "executed_count": len(executed_orders),
    }


def orders_payload_for_scope(scope: AccountScope, *, limit: int = 200) -> dict[str, Any]:
    if scope.broker == "FUTU":
        return _futu_orders_payload(scope, limit=limit)
    engine = get_sync_engine()
    with engine.connect() as conn:
        open_rows = [
            dict(row._mapping)
            for row in conn.execute(
                select(order_submissions)
                .where(
                    order_submissions.c.broker == scope.broker,
                    order_submissions.c.account_env == scope.account_env,
                    order_submissions.c.broker_account == scope.broker_account,
                    order_submissions.c.state.in_(ACTIVE_STATES),
                )
                .order_by(desc(order_submissions.c.updated_at))
                .limit(limit)
            ).all()
        ]
        fill_rows = [
            dict(row._mapping)
            for row in conn.execute(
                select(
                    order_fills,
                    order_submissions.c.security_type.label("submission_security_type"),
                    order_submissions.c.con_id.label("submission_con_id"),
                )
                .select_from(
                    order_fills.outerjoin(
                        order_submissions,
                        order_fills.c.submission_id == order_submissions.c.submission_id,
                    )
                )
                .where(
                    order_fills.c.broker == scope.broker,
                    order_fills.c.account_env == scope.account_env,
                    order_fills.c.broker_account == scope.broker_account,
                    order_fills.c.filled_at >= _today_et_start_utc(),
                )
                .order_by(desc(order_fills.c.filled_at), desc(order_fills.c.exec_id))
                .limit(limit)
            ).all()
        ]

    open_orders = [_open_order(row) for row in open_rows]
    executed_orders = [_executed_order(row) for row in fill_rows]
    return {
        "last_sync": datetime.now(timezone.utc).isoformat(),
        "open_orders": open_orders,
        "executed_orders": executed_orders,
        "open_count": len(open_orders),
        "executed_count": len(executed_orders),
    }


@router.get("/orders")
async def orders_list(
    scope: AccountScope = Depends(get_broker_scope),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    """Return scoped open order submissions plus recent execution fills (IB or FUTU)."""
    return orders_payload_for_scope(scope, limit=limit)
