from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import desc, func, insert, or_, select
from sqlalchemy.engine import Connection

from xenon.db.events import CHANNEL_TRADE_CLOSED
from xenon.db.schema import journal_entries, trades
from xenon.db.schema import outbox
from xenon.execution.account_scope import AccountScope

_METADATA_TOP_LEVEL_FIELDS = (
    "structure",
    "action",
    "contracts",
    "shares",
    "quantity",
    "fill_price",
    "entry_price",
    "total_cost",
    "entry_cost",
    "max_risk",
    "max_gain",
    "pct_of_bankroll",
    "gates_passed",
    "gates_failed",
    "edge_analysis",
    "realized_pnl",
    "return_on_risk",
    "outcome",
    "close_date",
    "rule_violation",
    "thesis",
    "legs",
)


def _json_compatible(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, list):
        return [_json_compatible(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_compatible(item) for key, item in value.items()}
    return value


def _as_mapping(row: Mapping[str, Any] | Any) -> Mapping[str, Any]:
    return row._mapping if hasattr(row, "_mapping") else row


def journal_entry_to_payload(row: Mapping[str, Any] | Any) -> dict[str, Any]:
    data = _as_mapping(row)
    metadata = data.get("metadata") or {}
    legacy_entry = metadata.get("legacy_entry")
    payload = dict(legacy_entry) if isinstance(legacy_entry, dict) else {}

    authored_at = data["authored_at"]
    if isinstance(authored_at, datetime):
        authored_at = authored_at.astimezone(timezone.utc)
        date_text = authored_at.date().isoformat()
        time_text = authored_at.time().isoformat(timespec="seconds")
    else:
        date_text = str(authored_at)
        time_text = None

    for key in _METADATA_TOP_LEVEL_FIELDS:
        if key in metadata and key not in payload:
            payload[key] = _json_compatible(metadata[key])

    payload["id"] = int(data["id"])
    payload["trade_id"] = data.get("trade_id")
    payload["date"] = date_text
    if time_text is not None:
        payload["time"] = time_text
    payload["ticker"] = data["ticker"]
    payload["structure"] = payload.get("structure") or "Journal Entry"
    payload["decision"] = data.get("decision") or payload.get("decision") or ""
    payload["notes"] = data.get("note") if data.get("note") is not None else payload.get("notes")
    payload["attachments"] = _json_compatible(data.get("attachments"))
    return payload


def list_journal_entries(
    conn: Connection,
    *,
    scope: AccountScope,
    cutoff: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(journal_entries)
        .where(
            journal_entries.c.broker == scope.broker,
            journal_entries.c.account_env == scope.account_env,
            journal_entries.c.broker_account == scope.broker_account,
            journal_entries.c.authored_at >= cutoff,
        )
        .order_by(desc(journal_entries.c.authored_at), desc(journal_entries.c.id))
        .limit(limit)
    ).all()
    return [journal_entry_to_payload(row) for row in rows]


def resolve_trade_ticker(conn: Connection, *, trade_id: int, scope: AccountScope) -> str | None:
    row = conn.execute(
        select(trades.c.ticker).where(
            trades.c.id == trade_id,
            trades.c.broker == scope.broker,
            trades.c.account_env == scope.account_env,
            trades.c.broker_account == scope.broker_account,
        )
    ).first()
    return str(row._mapping["ticker"]) if row else None


def create_journal_entry(
    conn: Connection,
    *,
    scope: AccountScope,
    ticker: str,
    trade_id: int | None = None,
    decision: str | None = None,
    note: str | None = None,
    attachments: Any = None,
    authored_by: str | None = None,
    authored_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "trade_id": trade_id,
        "ticker": ticker,
        "decision": decision,
        "note": note,
        "attachments": attachments,
        "authored_by": authored_by,
        "metadata": metadata,
        **scope.as_dict(),
    }
    if authored_at is not None:
        values["authored_at"] = authored_at
    row = conn.execute(insert(journal_entries).values(**values).returning(journal_entries)).one()
    return journal_entry_to_payload(row)


def pending_journal_outbox_count(conn: Connection, *, scope: AccountScope) -> int:
    result = conn.execute(
        select(func.count())
        .select_from(outbox)
        .where(
            outbox.c.channel == CHANNEL_TRADE_CLOSED,
            outbox.c.payload["broker"].astext == scope.broker,
            outbox.c.payload["account_env"].astext == scope.account_env,
            outbox.c.payload["broker_account"].astext == scope.broker_account,
            or_(outbox.c.consumed_by.is_(None), ~outbox.c.consumed_by.contains(["journal"])),
        )
    )
    return int(result.scalar_one())
