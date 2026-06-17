from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import and_, desc, func, insert, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from xenon.db.events import CHANNEL_TRADE_CLOSED
from xenon.db.schema import journal_entries, outbox, trades
from xenon.execution.account_scope import AccountScope

AUTO_IMPORT_CONSUMER_ID = "journal_auto_import"
LEGACY_JOURNAL_CONSUMER_ID = "journal"

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
    cutoff: datetime | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    conditions = [
        journal_entries.c.broker == scope.broker,
        journal_entries.c.account_env == scope.account_env,
        journal_entries.c.broker_account == scope.broker_account,
    ]
    if cutoff is not None:
        conditions.append(journal_entries.c.authored_at >= cutoff)
    rows = conn.execute(
        select(journal_entries)
        .where(*conditions)
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


def upsert_auto_import_entry(
    conn: Connection,
    *,
    trade_id: int,
) -> dict[str, Any] | None:
    """Idempotently create an IB_AUTO_IMPORT journal entry for a closed trade.

    Resolves scope from the `trades` row (not from a caller-supplied scope) so
    the listener does not need scope-bearing payloads. Relies on the partial
    unique index `uq_journal_auto_import` for concurrent safety.

    Returns the row payload, or None if the trade does not exist.
    """
    trade_row = conn.execute(
        select(
            trades.c.ticker,
            trades.c.broker,
            trades.c.account_env,
            trades.c.broker_account,
        ).where(trades.c.id == trade_id)
    ).first()
    if trade_row is None:
        return None

    stmt = (
        pg_insert(journal_entries)
        .values(
            trade_id=trade_id,
            ticker=trade_row.ticker,
            decision="IB_AUTO_IMPORT",
            authored_by="system",
            metadata={"source": "trade_closed_outbox"},
            broker=trade_row.broker,
            account_env=trade_row.account_env,
            broker_account=trade_row.broker_account,
        )
        .on_conflict_do_nothing(
            index_elements=["broker", "account_env", "broker_account", "trade_id"],
            index_where=text("decision = 'IB_AUTO_IMPORT' AND trade_id IS NOT NULL"),
        )
        .returning(journal_entries)
    )
    inserted = conn.execute(stmt).first()
    if inserted is not None:
        return journal_entry_to_payload(inserted)

    existing = conn.execute(
        select(journal_entries)
        .where(
            journal_entries.c.trade_id == trade_id,
            journal_entries.c.decision == "IB_AUTO_IMPORT",
            journal_entries.c.broker == trade_row.broker,
            journal_entries.c.account_env == trade_row.account_env,
            journal_entries.c.broker_account == trade_row.broker_account,
        )
        .limit(1)
    ).first()
    return journal_entry_to_payload(existing) if existing is not None else None


FUTU_AUTO_IMPORT_CONFLICT = dict(
    index_elements=["broker", "account_env", "broker_account", "futu_close_id"],
    index_where=text("decision = 'FUTU_AUTO_IMPORT' AND futu_close_id IS NOT NULL"),
)


def build_futu_auto_import_values(scope: AccountScope, closed_trade: Mapping[str, Any]) -> dict[str, Any]:
    """Pure builder for a FUTU_AUTO_IMPORT journal_entries INSERT values dict.

    Shared by the sync upsert (`upsert_futu_auto_import_entry`) and the async batch
    insert (`xenon.db.queries.futu_history.insert_futu_journal_entries`) so the
    metadata contract lives in one place. Metadata keys mirror those
    `journal_entry_to_payload` lifts top-level (quantity / entry_cost / realized_pnl /
    return_on_risk) so the journal table renders for Futu rows. `trade_id` stays NULL.
    """
    cost_basis = closed_trade.get("cost_basis")
    realized = closed_trade.get("realized_pnl")
    ror = None
    if cost_basis not in (None, 0) and realized is not None:
        ror = float(realized) / float(cost_basis)
    opened_at = closed_trade.get("opened_at")
    closed_at = closed_trade["closed_at"]
    meta = {
        "source": "futu_closed_trade",
        "futu_close_id": closed_trade["futu_close_id"],
        # Structure name (e.g. "Bull Call Spread · 07/17/26 $560/$590") so the
        # journal STRUCTURE column matches the HISTORICAL blotter; journal_entry_to_payload
        # lifts this top-level. Falls back to the table-level "Journal Entry" when absent.
        "structure": closed_trade.get("structure"),
        "quantity": float(closed_trade["quantity"]),
        "entry_cost": float(cost_basis) if cost_basis is not None else None,
        "cost_basis": float(cost_basis) if cost_basis is not None else None,
        "proceeds": float(closed_trade["proceeds"]) if closed_trade.get("proceeds") is not None else None,
        "realized_pnl": float(realized) if realized is not None else None,
        "return_on_risk": ror,
        "opened_at": opened_at.astimezone(timezone.utc).isoformat() if isinstance(opened_at, datetime) else None,
        "close_date": closed_at.astimezone(timezone.utc).isoformat()
        if isinstance(closed_at, datetime)
        else str(closed_at),
        "outcome": "WIN" if (realized is not None and float(realized) >= 0) else "LOSS",
    }
    return {
        "trade_id": None,
        "ticker": closed_trade["ticker"],
        "decision": "FUTU_AUTO_IMPORT",
        "authored_by": "system",
        "metadata": meta,
        "futu_close_id": closed_trade["futu_close_id"],
        "broker": scope.broker,
        "account_env": scope.account_env,
        "broker_account": scope.broker_account,
        "authored_at": closed_at,
    }


def upsert_futu_auto_import_entry(
    conn: Connection,
    *,
    scope: AccountScope,
    closed_trade: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Idempotently create a FUTU_AUTO_IMPORT journal entry for a closed Futu lot.

    Futu has no `trades` row to resolve scope from (the trades table is IB-locked),
    so scope + closed-trade detail come from the caller. `trade_id` stays NULL;
    dedup is on the dedicated `futu_close_id` column via the partial unique index
    `uq_journal_futu_auto_import`.
    """
    stmt = (
        pg_insert(journal_entries)
        .values(**build_futu_auto_import_values(scope, closed_trade))
        .on_conflict_do_nothing(**FUTU_AUTO_IMPORT_CONFLICT)
        .returning(journal_entries)
    )
    inserted = conn.execute(stmt).first()
    if inserted is not None:
        return journal_entry_to_payload(inserted)
    existing = conn.execute(
        select(journal_entries)
        .where(
            journal_entries.c.decision == "FUTU_AUTO_IMPORT",
            journal_entries.c.broker == scope.broker,
            journal_entries.c.account_env == scope.account_env,
            journal_entries.c.broker_account == scope.broker_account,
            journal_entries.c.futu_close_id == closed_trade["futu_close_id"],
        )
        .limit(1)
    ).first()
    return journal_entry_to_payload(existing) if existing is not None else None


def pending_journal_outbox_count(conn: Connection, *, scope: AccountScope) -> int:
    result = conn.execute(
        select(func.count())
        .select_from(outbox)
        .where(
            outbox.c.channel == CHANNEL_TRADE_CLOSED,
            outbox.c.payload["broker"].astext == scope.broker,
            outbox.c.payload["account_env"].astext == scope.account_env,
            outbox.c.payload["broker_account"].astext == scope.broker_account,
            or_(
                outbox.c.consumed_by.is_(None),
                and_(
                    ~outbox.c.consumed_by.contains([AUTO_IMPORT_CONSUMER_ID]),
                    ~outbox.c.consumed_by.contains([LEGACY_JOURNAL_CONSUMER_ID]),
                ),
            ),
        )
    )
    return int(result.scalar_one())
