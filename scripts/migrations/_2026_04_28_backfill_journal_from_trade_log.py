"""Backfill xenon.journal_entries from legacy data/trade_log.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from xenon.db.engine import get_sync_engine
from xenon.db.queries.journal import create_journal_entry
from xenon.db.schema import journal_entries
from xenon.execution.account_scope import AccountScope


def _configure_database_url(db_url: str) -> None:
    os.environ["DATABASE_URL"] = db_url
    import xenon.db.engine as engine_mod

    if engine_mod._sync_engine is not None:
        engine_mod._sync_engine.dispose()
    engine_mod._sync_engine = None


def _require_scope(*, broker: str, account_env: str, broker_account: str) -> AccountScope:
    if account_env == "legacy_unknown" or broker_account == "legacy_unknown":
        raise ValueError("backfill requires explicit account scope")
    if broker not in ("IB", "FUTU"):
        raise ValueError(f"unsupported broker: {broker}")
    return AccountScope(broker=broker, account_env=account_env, broker_account=broker_account)


def _load_entries(json_path: Path | str) -> list[dict[str, Any]]:
    path = Path(json_path)
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    entries = data.get("trades", data) if isinstance(data, dict) else data
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _entry_time(entry: dict[str, Any]) -> datetime:
    for key in ("authored_at", "opened_at", "closed_at", "filled_at", "timestamp", "datetime"):
        parsed = _parse_dt(entry.get(key))
        if parsed is not None:
            return parsed
    date_value = entry.get("date")
    if date_value:
        parsed_date = datetime.fromisoformat(str(date_value)).date()
        parsed_time = time.fromisoformat(str(entry.get("time") or "00:00:00"))
        return datetime.combine(parsed_date, parsed_time, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _stable_id(entry: dict[str, Any], *, ticker: str, authored_at: datetime) -> str:
    explicit = entry.get("id")
    if explicit is not None:
        raw = f"id|{explicit}|{ticker}|{authored_at.isoformat()}"
    else:
        raw = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _note(entry: dict[str, Any]) -> str | None:
    for key in ("notes", "note", "thesis", "rule_violation"):
        value = entry.get(key)
        if value:
            return str(value)
    return None


def _metadata(entry: dict[str, Any], *, legacy_id: str) -> dict[str, Any]:
    metadata = {
        "legacy_source": "trade_log_json",
        "legacy_id": legacy_id,
        "legacy_entry": entry,
    }
    for key in (
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
    ):
        if key in entry:
            metadata[key] = entry[key]
    return metadata


def _already_imported(conn, *, legacy_id: str, scope: AccountScope) -> bool:
    row = conn.execute(
        select(journal_entries.c.id).where(
            journal_entries.c.broker == scope.broker,
            journal_entries.c.account_env == scope.account_env,
            journal_entries.c.broker_account == scope.broker_account,
            journal_entries.c.metadata["legacy_id"].astext == legacy_id,
        )
    ).first()
    return row is not None


def run(
    *,
    json_path: Path | str,
    db_url: str,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
) -> int:
    scope = _require_scope(broker=broker, account_env=account_env, broker_account=broker_account)
    _configure_database_url(db_url)
    inserted = 0
    engine = get_sync_engine()
    with engine.begin() as conn:
        for entry in _load_entries(json_path):
            ticker = str(entry.get("ticker") or entry.get("symbol") or "").strip().upper()
            if not ticker:
                continue
            authored_at = _entry_time(entry)
            legacy_id = _stable_id(entry, ticker=ticker, authored_at=authored_at)
            if _already_imported(conn, legacy_id=legacy_id, scope=scope):
                continue
            create_journal_entry(
                conn,
                scope=scope,
                ticker=ticker,
                decision="LEGACY_IMPORT",
                note=_note(entry),
                authored_by="legacy_backfill",
                authored_at=authored_at,
                metadata=_metadata(entry, legacy_id=legacy_id),
            )
            inserted += 1
    return inserted


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default="data/trade_log.json")
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--broker", default=os.environ.get("XENON_BROKER", "IB"))
    parser.add_argument("--account-env", default=os.environ.get("XENON_TRADING_MODE", "legacy_unknown"))
    parser.add_argument("--broker-account", default=os.environ.get("XENON_BROKER_ACCOUNT", "legacy_unknown"))
    args = parser.parse_args()
    if not args.db_url:
        raise SystemExit("DATABASE_URL is required")
    count = run(
        json_path=args.json,
        db_url=args.db_url,
        broker=args.broker,
        account_env=args.account_env,
        broker_account=args.broker_account,
    )
    print(f"backfilled {count} journal_entries rows from trade_log.json")


if __name__ == "__main__":
    main()
