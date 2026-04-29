"""Backfill xenon.order_fills from legacy data/trade_log.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from xenon.execution.orders_store import record_fill
from xenon.execution.trade_aggregator import aggregate_trade_from_fills


def _configure_database_url(db_url: str) -> None:
    os.environ["DATABASE_URL"] = db_url
    import xenon.db.engine as engine_mod

    if engine_mod._sync_engine is not None:
        engine_mod._sync_engine.dispose()
    engine_mod._sync_engine = None


def _require_scope(*, broker: str, account_env: str, broker_account: str) -> dict[str, str]:
    if account_env == "legacy_unknown" or broker_account == "legacy_unknown":
        raise ValueError("backfill requires explicit account scope")
    return {"broker": broker, "account_env": account_env, "broker_account": broker_account}


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


def _entry_time(entry: dict[str, Any]) -> datetime | None:
    for key in ("opened_at", "filled_at", "timestamp", "datetime"):
        parsed = _parse_dt(entry.get(key))
        if parsed is not None:
            return parsed
    date_value = entry.get("date")
    if not date_value:
        return None
    time_value = entry.get("time") or "00:00:00"
    parsed_date = datetime.fromisoformat(str(date_value)).date()
    parsed_time = time.fromisoformat(str(time_value))
    return datetime.combine(parsed_date, parsed_time, tzinfo=timezone.utc)


def _entry_price(entry: dict[str, Any], qty: int) -> Decimal | None:
    for key in ("price", "fill_price", "avg_price", "avg_fill_price", "entry_price"):
        value = entry.get(key)
        if value is not None:
            return Decimal(str(value))
    entry_cost = entry.get("entry_cost")
    if entry_cost is not None and qty:
        return (Decimal(str(entry_cost)).copy_abs() / Decimal(qty)).quantize(Decimal("0.0001"))
    return None


def _stable_id(*, ticker: str, opened_at: datetime, qty: int, price: Decimal) -> str:
    raw = f"{ticker}|{opened_at.isoformat()}|{qty}|{price}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _entry_to_fill(entry: dict[str, Any]) -> dict[str, Any] | None:
    ticker = str(entry.get("ticker") or entry.get("symbol") or "").upper()
    if not ticker:
        return None
    qty_value = entry.get("qty", entry.get("quantity", entry.get("shares")))
    if qty_value is None:
        return None
    qty = abs(int(qty_value))
    if qty <= 0:
        return None
    opened_at = _entry_time(entry)
    if opened_at is None:
        return None
    price = _entry_price(entry, qty)
    if price is None:
        return None
    side = str(entry.get("side") or entry.get("action") or "BUY").upper()
    legacy_id = _stable_id(ticker=ticker, opened_at=opened_at, qty=qty, price=price)
    return {
        "exec_id": legacy_id,
        "legacy_id": legacy_id,
        "ticker": ticker,
        "side": side,
        "qty": qty,
        "price": price,
        "commission": Decimal(str(entry.get("commission") or 0)),
        "filled_at": opened_at,
        "metadata": {
            "legacy_source": "trade_log_json",
            "legacy_id": legacy_id,
            "original": entry,
        },
    }


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
    for entry in _load_entries(json_path):
        fill = _entry_to_fill(entry)
        if fill is None:
            continue
        did_insert = record_fill(
            exec_id=fill["exec_id"],
            submission_id=None,
            combo_attempt_id=None,
            perm_id=None,
            ib_order_id=str(entry.get("order_id")) if entry.get("order_id") is not None else None,
            con_id=entry.get("con_id") or entry.get("conId"),
            ticker=fill["ticker"],
            side=fill["side"],
            qty=fill["qty"],
            price=fill["price"],
            commission=fill["commission"],
            filled_at=fill["filled_at"],
            metadata=fill["metadata"],
            **scope,
        )
        if did_insert:
            inserted += 1
        aggregate_trade_from_fills(legacy_id=fill["legacy_id"])
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
    print(f"backfilled {count} order_fills rows from trade_log.json")


if __name__ == "__main__":
    main()
