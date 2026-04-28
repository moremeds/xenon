"""Order read endpoints backed by Postgres."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select

from xenon.api.guards import get_account_scope
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_fills, order_submissions
from xenon.execution.account_scope import AccountScope

router = APIRouter()

ACTIVE_STATES = {"PENDING", "WORKING", "PARTIALLY_FILLED"}


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
        "tif": "DAY",
        "modifySequence": int(row.get("modify_sequence") or 0),
    }


def _executed_order(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") or {}
    sec_type = str(metadata.get("sec_type") or "STK")
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
        "quantity": int(row["qty"]),
        "avgPrice": _float_or_none(row.get("price")),
        "commission": _float_or_none(row.get("commission")),
        "realizedPNL": _float_or_none(metadata.get("realized_pnl")),
        "time": _iso(row.get("filled_at")),
        "exchange": str(metadata.get("exchange") or ""),
    }


def orders_payload_for_scope(scope: AccountScope, *, limit: int = 200) -> dict[str, Any]:
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
                select(order_fills)
                .where(
                    order_fills.c.broker == scope.broker,
                    order_fills.c.account_env == scope.account_env,
                    order_fills.c.broker_account == scope.broker_account,
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
    scope: AccountScope = Depends(get_account_scope),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    """Return scoped open order submissions plus recent execution fills."""
    return orders_payload_for_scope(scope, limit=limit)
