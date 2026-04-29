from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import desc, func, or_, select
from sqlalchemy.engine import Connection

from xenon.db.schema import order_submissions, trades
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
        select(trades, order_submissions.c.perm_id.label("perm_id"))
        .select_from(
            trades.outerjoin(
                order_submissions,
                trades.c.submission_id == order_submissions.c.submission_id,
            )
        )
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
        "perm_id": row.get("perm_id"),
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


# ---------------------------------------------------------------------------
# PG ↔ Flex overlay merge (W3.4)
# ---------------------------------------------------------------------------

_DIVERGENCE_TOLERANCE = 0.01
_DIVERGENCE_FIELDS = ("realized_pnl", "total_quantity", "total_commission", "cost_basis", "proceeds")


def compare_blotter_rows(pg_row: Mapping[str, Any], flex_row: Mapping[str, Any]) -> list[str]:
    """Return list of fields that differ between PG and Flex by more than tolerance."""
    differing: list[str] = []
    for field in _DIVERGENCE_FIELDS:
        pg_val = pg_row.get(field)
        flex_val = flex_row.get(field)
        if pg_val is None or flex_val is None:
            continue
        try:
            if abs(float(pg_val) - float(flex_val)) > _DIVERGENCE_TOLERANCE:
                differing.append(field)
        except (TypeError, ValueError):
            if pg_val != flex_val:
                differing.append(field)
    return differing


def _trade_index(rows: list[dict]) -> dict[str, dict]:
    return {r["perm_id"]: r for r in rows if r.get("perm_id")}


def _merge_section(pg_rows: list[dict], flex_rows: list[dict]) -> list[dict]:
    pg_index = _trade_index(pg_rows)
    flex_index = _trade_index(flex_rows)
    merged: list[dict] = []
    for perm_id in sorted(set(pg_index) | set(flex_index)):
        pg_row = pg_index.get(perm_id)
        flex_row = flex_index.get(perm_id)
        if pg_row and flex_row:
            differing = compare_blotter_rows(pg_row, flex_row)
            merged.append({**pg_row, "divergence": bool(differing), "divergence_fields": differing})
        elif pg_row:
            merged.append({**pg_row, "divergence": False})
        else:
            merged.append({**flex_row, "divergence": False})
    merged.extend({**r, "divergence": False} for r in pg_rows if not r.get("perm_id"))
    return merged


def _summary_from_rows(closed: list[dict], open_: list[dict]) -> dict[str, Any]:
    total_commission = Decimal("0")
    realized_pnl = Decimal("0")
    for row in closed + open_:
        if row.get("total_commission") is not None:
            total_commission += Decimal(str(row["total_commission"]))
    for row in closed:
        if row.get("realized_pnl") is not None:
            realized_pnl += Decimal(str(row["realized_pnl"]))
    return {
        "closed_trades": len(closed),
        "open_trades": len(open_),
        "total_commissions": float(total_commission),
        "realized_pnl": float(realized_pnl),
    }


def merge_pg_and_flex(pg_payload: Mapping[str, Any], flex_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Merge PG and Flex blotter payloads by perm_id.

    Sets ``source`` based on which side contributed rows. Each merged row carries
    a ``divergence`` flag (True when both sides had the row and any tracked field
    differed by more than _DIVERGENCE_TOLERANCE). Recomputes ``summary`` from the
    merged row sets so totals match the visible rows.
    """
    pg_closed = list(pg_payload.get("closed_trades") or [])
    pg_open = list(pg_payload.get("open_trades") or [])
    flex_closed = list(flex_payload.get("closed_trades") or [])
    flex_open = list(flex_payload.get("open_trades") or [])

    closed = _merge_section(pg_closed, flex_closed)
    open_ = _merge_section(pg_open, flex_open)

    flex_contributed = bool(flex_closed or flex_open)
    pg_contributed = bool(pg_closed or pg_open)
    if flex_contributed and pg_contributed:
        source = "postgres+flex"
    elif pg_contributed:
        source = "postgres"
    elif flex_contributed:
        source = "flex"
    else:
        source = "none"

    return {
        **pg_payload,
        "source": source,
        "closed_trades": closed,
        "open_trades": open_,
        "summary": _summary_from_rows(closed, open_),
    }
