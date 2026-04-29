from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import desc, func, or_, select
from sqlalchemy.engine import Connection

from xenon.db.schema import trades
from xenon.execution.account_scope import AccountScope


def fetch_blotter_pg(
    conn: Connection,
    *,
    scope: AccountScope,
    days: int = 30,
) -> dict[str, Any]:
    """Return the Historical Trades payload derived from xenon.trades."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    trade_time = func.coalesce(trades.c.closed_at, trades.c.opened_at)
    rows = conn.execute(
        select(trades)
        .where(
            trades.c.broker == scope.broker,
            trades.c.account_env == scope.account_env,
            trades.c.broker_account == scope.broker_account,
            or_(trade_time >= cutoff, trade_time.is_(None)),
        )
        .order_by(desc(trade_time), desc(trades.c.id))
    ).all()

    closed_trades = []
    open_trades = []
    total_commissions = Decimal("0")
    realized_pnl = Decimal("0")
    as_of: datetime | None = None

    for row in rows:
        payload = _trade_to_payload(row._mapping)
        total_commissions += Decimal(str(payload["total_commission"]))
        if payload["is_closed"] and payload["realized_pnl"] is not None:
            realized_pnl += Decimal(str(payload["realized_pnl"]))
        latest = _latest_time(row._mapping)
        if latest is not None and (as_of is None or latest > as_of):
            as_of = latest
        if payload["is_closed"]:
            closed_trades.append(payload)
        else:
            open_trades.append(payload)

    return {
        "configured": True,
        "source": "postgres",
        "as_of": _iso(as_of) if as_of else None,
        "summary": {
            "closed_trades": len(closed_trades),
            "open_trades": len(open_trades),
            "total_commissions": _number(total_commissions),
            "realized_pnl": _number(realized_pnl),
        },
        "closed_trades": closed_trades,
        "open_trades": open_trades,
    }


def blotter_has_trades(payload: Mapping[str, Any]) -> bool:
    summary = payload.get("summary") or {}
    return int(summary.get("closed_trades") or 0) + int(summary.get("open_trades") or 0) > 0


def _trade_to_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") or {}
    executions = _executions(row, metadata)
    is_closed = row.get("state") == "CLOSED"
    total_commission = sum(Decimal(str(item["commission"])) for item in executions)
    entry_cost = row.get("entry_cost")
    exit_cost = row.get("exit_cost")
    realized_pnl = row.get("realized_pnl")

    return {
        "symbol": row["ticker"],
        "contract_desc": str(metadata.get("contract_desc") or row.get("structure") or row["ticker"]),
        "sec_type": str(metadata.get("sec_type") or _sec_type(metadata, row)),
        "is_closed": is_closed,
        "net_quantity": 0 if is_closed else int(row["quantity"]),
        "total_quantity": int(row["quantity"]),
        "total_commission": _number(total_commission),
        "realized_pnl": _number(realized_pnl) if realized_pnl is not None else None,
        "cost_basis": _number(entry_cost) if entry_cost is not None else None,
        "proceeds": _number(exit_cost) if exit_cost is not None else None,
        "total_cash_flow": _number(realized_pnl) if realized_pnl is not None else _number(entry_cost or 0),
        "executions": executions,
    }


def _executions(row: Mapping[str, Any], metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    legs = metadata.get("legs")
    if isinstance(legs, list) and legs:
        return [_execution_from_leg(row, leg, index) for index, leg in enumerate(legs)]

    timestamp = _latest_time(row)
    return [
        {
            "exec_id": f"trade-{row['id']}",
            "time": _iso(timestamp) if timestamp else "",
            "side": str(row.get("action") or ""),
            "quantity": int(row["quantity"]),
            "price": _number(row.get("entry_cost") or 0),
            "commission": 0,
            "notional_value": _number(row.get("entry_cost") or 0),
            "net_cash_flow": _number(row.get("realized_pnl") or row.get("entry_cost") or 0),
        }
    ]


def _execution_from_leg(row: Mapping[str, Any], leg: Any, index: int) -> dict[str, Any]:
    data = leg if isinstance(leg, Mapping) else {}
    qty = int(data.get("qty") or data.get("quantity") or 0)
    price = Decimal(str(data.get("price") or 0))
    commission = Decimal(str(data.get("commission") or 0))
    side = str(data.get("side") or "").upper()
    notional = Decimal(qty) * price
    net_cash_flow = notional - commission if side in {"SELL", "SLD", "SOLD"} else -notional - commission
    return {
        "exec_id": str(data.get("exec_id") or f"trade-{row['id']}-{index}"),
        "time": _iso_value(data.get("filled_at") or _latest_time(row)),
        "side": side,
        "quantity": qty,
        "price": _number(price),
        "commission": _number(commission),
        "notional_value": _number(notional),
        "net_cash_flow": _number(net_cash_flow),
    }


def _latest_time(row: Mapping[str, Any]) -> datetime | None:
    value = row.get("closed_at") or row.get("opened_at")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return None


def _sec_type(metadata: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    if metadata.get("sec_type"):
        return str(metadata["sec_type"])
    structure = str(row.get("structure") or "")
    if "option" in structure.lower() or "spread" in structure.lower() or metadata.get("legs"):
        return "OPT"
    return "STK"


def _iso_value(value: Any) -> str:
    if isinstance(value, datetime):
        return _iso(value)
    return str(value or "")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _number(value: Decimal | int | float | str) -> float:
    return float(Decimal(str(value)))
