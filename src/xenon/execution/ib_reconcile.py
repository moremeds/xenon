#!/usr/bin/env python3
"""
IB Trade Reconciliation Script

Fetches trading history from IB, writes execution fills to Postgres, and reconciles
against the latest scoped portfolio snapshot.
Designed to run asynchronously at startup without blocking the UI.

Actions detected:
- BUY: Opening long stock/option position
- SELL: Closing long position (realized P&L)
- SHORT: Opening short stock position
- COVER: Closing short position (realized P&L)
- New positions missing from the latest snapshot
- Closed positions still present in the latest snapshot
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select

from xenon.clients.ib_client import CLIENT_IDS, DEFAULT_GATEWAY_PORT, DEFAULT_HOST, IBClient
from xenon.db.engine import get_sync_engine
from xenon.db.schema import account_snapshots
from xenon.execution.account_scope import AccountScope, resolve_from_env
from xenon.execution.orders_store import record_fill
from xenon.execution.trade_aggregator import aggregate_trade_from_fills


def log(msg: str, level: str = "info"):
    """Print log message with timestamp."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    prefix = {"info": "ℹ", "warn": "⚠", "error": "✗", "success": "✓"}.get(level, "•")
    print(f"[{timestamp}] {prefix} {msg}")


def connect_ib(port: int = DEFAULT_GATEWAY_PORT, client_id: int = CLIENT_IDS["ib_reconcile"]) -> Optional[IBClient]:
    """Connect to IB Gateway/TWS."""
    try:
        client = IBClient()
        client.connect(host=DEFAULT_HOST, port=port, client_id=client_id)
        return client
    except Exception as e:
        log(f"IB connection failed: {e}", "error")
        return None


def load_portfolio_snapshot(*, scope: AccountScope | None = None) -> dict:
    """Load latest scoped portfolio payload from xenon.account_snapshots."""
    resolved = scope or resolve_from_env()
    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(account_snapshots.c.payload)
            .where(
                account_snapshots.c.broker == resolved.broker,
                account_snapshots.c.account_env == resolved.account_env,
                account_snapshots.c.broker_account == resolved.broker_account,
            )
            .order_by(account_snapshots.c.snapshot_at.desc(), account_snapshots.c.id.desc())
            .limit(1)
        ).first()
    if row is None:
        return {}
    payload = row._mapping["payload"]
    return dict(payload) if isinstance(payload, dict) else {}


def load_json(filepath: str) -> dict:
    """Load JSON file, return empty dict if not found."""
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(filepath: str, data: dict):
    """Save data to JSON file."""
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, default=str)


def get_trade_log_trades(trade_log: dict) -> set:
    """Extract set of (ticker, date, structure_type) tuples from a legacy trade log dict."""
    trades = set()
    for trade in trade_log.get("trades", []):
        ticker = trade.get("ticker")
        date = trade.get("date") or trade.get("close_date")
        structure = trade.get("structure", "")
        trades.add((ticker, date, structure))
    return trades


def fetch_ib_executions(client: IBClient, lookback_days: int = 7) -> list:
    """Fetch executions from IB for the last N days."""
    executions = []
    fills = client.get_fills()

    for fill in fills:
        e = fill.execution
        c = fill.contract
        cr = fill.commissionReport

        executions.append(
            {
                "exec_id": getattr(e, "execId", None),
                "perm_id": getattr(e, "permId", None),
                "ib_order_id": getattr(e, "orderId", None),
                "con_id": getattr(c, "conId", None),
                "time": e.time,
                "symbol": c.symbol,
                "sec_type": c.secType,
                "side": e.side,  # BOT or SLD
                "shares": e.shares,
                "price": e.price,
                "exchange": e.exchange,
                "commission": cr.commission if cr else 0,
                "realized_pnl": cr.realizedPNL if cr and cr.realizedPNL else 0,
                "strike": c.strike if c.secType == "OPT" else None,
                "expiry": c.lastTradeDateOrContractMonth if c.secType == "OPT" else None,
                "right": c.right if c.secType == "OPT" else None,
            }
        )

    return executions


def _field(execution: Any, *names: str) -> Any:
    for name in names:
        if isinstance(execution, dict):
            value = execution.get(name)
        else:
            value = getattr(execution, name, None)
        if value is not None and value != "":
            return value
    return None


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError(f"Cannot coerce execution time {value!r} to datetime")


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def _normalize_fill_side(side: Any) -> str:
    normalized = str(side or "").upper()
    if normalized in {"BOT", "BOUGHT"}:
        return "BUY"
    if normalized in {"SLD", "SOLD"}:
        return "SELL"
    return normalized


def _execution_exec_id(execution: dict) -> str:
    explicit = _field(execution, "exec_id", "execId")
    if explicit is not None:
        return str(explicit)
    fingerprint = "|".join(
        str(_field(execution, name) or "")
        for name in ("time", "symbol", "sec_type", "side", "shares", "price", "con_id")
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
    return f"ib_reconcile:hash:{digest}"


def _legacy_group_id(execution: dict) -> str:
    perm_id = _field(execution, "perm_id", "permId")
    if perm_id is not None:
        return f"ib_reconcile:perm:{perm_id}"
    ib_order_id = _field(execution, "ib_order_id", "order_id", "orderId")
    if ib_order_id is not None:
        return f"ib_reconcile:order:{ib_order_id}"
    return f"ib_reconcile:exec:{_execution_exec_id(execution)}"


def _fill_metadata(execution: dict, *, legacy_id: str) -> dict[str, Any]:
    return {
        "legacy_source": "ib_reconcile",
        "legacy_id": legacy_id,
        "sec_type": _field(execution, "sec_type", "secType"),
        "exchange": _field(execution, "exchange"),
        "strike": _field(execution, "strike"),
        "expiry": _field(execution, "expiry"),
        "right": _field(execution, "right"),
        "raw_side": _field(execution, "side"),
        "realized_pnl": str(_decimal(_field(execution, "realized_pnl", "realizedPNL"))),
    }


def record_external_fills(
    executions: list[dict],
    *,
    scope: AccountScope | None = None,
) -> dict[str, Any]:
    """Record external IB executions into order_fills and aggregate affected groups."""
    resolved = scope or resolve_from_env()
    inserted = 0
    replayed = 0
    affected_legacy_ids: set[str] = set()

    for execution in executions:
        legacy_id = _legacy_group_id(execution)
        did_insert = record_fill(
            exec_id=_execution_exec_id(execution),
            submission_id=None,
            combo_attempt_id=None,
            perm_id=str(_field(execution, "perm_id", "permId") or "") or None,
            ib_order_id=str(_field(execution, "ib_order_id", "order_id", "orderId") or "") or None,
            con_id=_field(execution, "con_id", "conId"),
            ticker=str(_field(execution, "symbol", "ticker")),
            side=_normalize_fill_side(_field(execution, "side")),
            qty=int(_field(execution, "shares", "qty")),
            price=_decimal(_field(execution, "price")),
            commission=_decimal(_field(execution, "commission")),
            filled_at=_coerce_datetime(_field(execution, "time", "filled_at")),
            metadata=_fill_metadata(execution, legacy_id=legacy_id),
            broker=resolved.broker,
            account_env=resolved.account_env,
            broker_account=resolved.broker_account,
        )
        if did_insert:
            inserted += 1
            affected_legacy_ids.add(legacy_id)
        else:
            replayed += 1

    ordered_legacy_ids = sorted(affected_legacy_ids)
    for legacy_id in ordered_legacy_ids:
        aggregate_trade_from_fills(legacy_id=legacy_id)

    return {
        "inserted": inserted,
        "replayed": replayed,
        "affected_legacy_ids": ordered_legacy_ids,
    }


def fetch_ib_positions(client: IBClient) -> list:
    """Fetch current positions from IB."""
    positions = []
    for p in client.get_positions():
        positions.append(
            {
                "symbol": p.contract.symbol,
                "sec_type": p.contract.secType,
                "quantity": p.position,
                "avg_cost": p.avgCost,
                "strike": p.contract.strike if p.contract.secType == "OPT" else None,
                "expiry": p.contract.lastTradeDateOrContractMonth if p.contract.secType == "OPT" else None,
                "right": p.contract.right if p.contract.secType == "OPT" else None,
            }
        )
    return positions


def _contract_key(e: dict) -> str:
    """Build a grouping key: symbol only for stocks, symbol+strike+expiry+right for options."""
    if e["sec_type"] in ("OPT", "BAG"):
        return f"{e['symbol']}|{e['sec_type']}|{e.get('strike')}|{e.get('expiry')}|{e.get('right')}"
    return f"{e['symbol']}|{e['sec_type']}"


def group_executions_by_symbol(executions: list) -> dict:
    """Group executions by contract (symbol + strike/expiry/right for options) and determine net action."""
    grouped = {}

    for e in executions:
        key = _contract_key(e)
        if key not in grouped:
            grouped[key] = {
                "symbol": e["symbol"],
                "sec_type": e["sec_type"],
                "strike": e.get("strike"),
                "expiry": e.get("expiry"),
                "right": e.get("right"),
                "executions": [],
                "net_quantity": 0,
                "total_value": 0,
                "total_commission": 0,
                "total_realized_pnl": 0,
            }

        g = grouped[key]
        g["executions"].append(e)

        qty = e["shares"] if e["side"] == "BOT" else -e["shares"]
        g["net_quantity"] += qty
        g["total_value"] += e["shares"] * e["price"]
        g["total_commission"] += e["commission"]
        g["total_realized_pnl"] += e["realized_pnl"]

    # Determine action for each group
    for key, g in grouped.items():
        if g["net_quantity"] > 0:
            g["action"] = "BUY" if g["sec_type"] == "STK" else "BUY_OPTION"
        elif g["net_quantity"] < 0:
            # Check if it's a close (has realized P&L) or short opening
            if g["total_realized_pnl"] != 0:
                g["action"] = "SELL" if g["sec_type"] == "STK" else "SELL_OPTION"
            else:
                g["action"] = "SHORT" if g["sec_type"] == "STK" else "SELL_TO_OPEN"
        else:
            # Net zero - could be a day trade or covered
            if g["total_realized_pnl"] != 0:
                g["action"] = "CLOSED"
            else:
                g["action"] = "NEUTRAL"

    return grouped


def find_new_trades(executions: list, trade_log: dict) -> list:
    """Find executions that aren't in the trade log."""
    existing = get_trade_log_trades(trade_log)
    grouped = group_executions_by_symbol(executions)

    new_trades = []
    for key, g in grouped.items():
        symbol = g["symbol"]
        # Get the date from first execution
        if g["executions"]:
            trade_date = g["executions"][0]["time"].strftime("%Y-%m-%d")

            # Check if this trade exists in log
            found = False
            for ticker, date, structure in existing:
                if ticker == symbol and date == trade_date:
                    found = True
                    break

            if not found and g["action"] not in ["NEUTRAL"]:
                entry = {
                    "symbol": symbol,
                    "date": trade_date,
                    "action": g["action"],
                    "net_quantity": g["net_quantity"],
                    "avg_price": g["total_value"] / sum(e["shares"] for e in g["executions"]) if g["executions"] else 0,
                    "commission": g["total_commission"],
                    "realized_pnl": g["total_realized_pnl"],
                    "sec_type": g["sec_type"],
                }
                # Include contract details for options
                if g["sec_type"] in ("OPT", "BAG") and g.get("strike"):
                    entry["strike"] = g["strike"]
                    entry["expiry"] = g.get("expiry")
                    entry["right"] = g.get("right")
                new_trades.append(entry)

    return new_trades


def find_position_discrepancies(ib_positions: list, portfolio: dict) -> dict:
    """Find positions that differ between IB and local portfolio."""
    discrepancies = {
        "missing_locally": [],  # In IB but not in latest snapshot
        "missing_in_ib": [],  # In latest snapshot but not in IB (closed)
        "quantity_mismatch": [],
    }

    # Build lookup of local tickers (just by symbol for simplicity)
    local_tickers = set()
    local_positions = {}
    for pos in portfolio.get("positions", []):
        ticker = pos.get("ticker")
        if ticker:
            local_tickers.add(ticker)
            # Use ticker as key, store the position
            if ticker not in local_positions:
                local_positions[ticker] = []
            local_positions[ticker].append(pos)

    # Build lookup of IB tickers
    ib_tickers = set()
    ib_positions_by_symbol = {}
    for p in ib_positions:
        symbol = p["symbol"]
        ib_tickers.add(symbol)
        if symbol not in ib_positions_by_symbol:
            ib_positions_by_symbol[symbol] = []
        ib_positions_by_symbol[symbol].append(p)

    # Find positions in IB not locally (new positions)
    for symbol in ib_tickers - local_tickers:
        for p in ib_positions_by_symbol[symbol]:
            discrepancies["missing_locally"].append(p)

    # Find positions locally not in IB (closed positions)
    for ticker in local_tickers - ib_tickers:
        for p in local_positions[ticker]:
            discrepancies["missing_in_ib"].append(p)

    return discrepancies


def generate_reconciliation_report(new_trades: list, discrepancies: dict) -> dict:
    """Generate a report of what needs to be reconciled."""
    report = {
        "timestamp": datetime.now().isoformat(),
        "new_trades": new_trades,
        "positions_missing_locally": discrepancies["missing_locally"],
        "positions_closed": discrepancies["missing_in_ib"],
        "needs_attention": len(new_trades) > 0
        or len(discrepancies["missing_locally"]) > 0
        or len(discrepancies["missing_in_ib"]) > 0,
    }
    return report


def main():
    """Main reconciliation routine."""
    log("Starting IB trade reconciliation...")

    # Paths
    project_root = Path(__file__).resolve().parents[3]
    reconcile_path = project_root / "data" / "reconciliation.json"

    try:
        scope = resolve_from_env()
    except ValueError as e:
        log(f"Cannot resolve account scope: {e}", "error")
        return

    # Connect to IB
    client = connect_ib()
    if not client:
        log("Cannot connect to IB Gateway - skipping reconciliation", "warn")
        return

    try:
        # Load latest scoped portfolio snapshot from Postgres.
        portfolio = load_portfolio_snapshot(scope=scope)

        # Fetch IB data
        log("Fetching executions from IB...")
        executions = fetch_ib_executions(client)
        log(f"Found {len(executions)} executions")

        log("Recording external executions to Postgres...")
        fill_result = record_external_fills(executions, scope=scope)
        log(
            f"Recorded {fill_result['inserted']} new fills "
            f"({fill_result['replayed']} already present)"
        )

        log("Fetching positions from IB...")
        positions = fetch_ib_positions(client)
        log(f"Found {len(positions)} positions")

        # Find discrepancies
        log("Checking for new trades...")
        affected = set(fill_result["affected_legacy_ids"])
        new_trades = find_new_trades([e for e in executions if _legacy_group_id(e) in affected], {"trades": []})

        log("Checking position discrepancies...")
        discrepancies = find_position_discrepancies(positions, portfolio)

        # Generate report
        report = generate_reconciliation_report(new_trades, discrepancies)
        report["fills_inserted"] = fill_result["inserted"]
        report["fills_replayed"] = fill_result["replayed"]
        report["affected_fill_groups"] = fill_result["affected_legacy_ids"]

        # Save reconciliation report
        save_json(str(reconcile_path), report)

        # Log summary
        if report["needs_attention"]:
            log(f"⚠️  Reconciliation needed:", "warn")
            if new_trades:
                log(f"   • {len(new_trades)} new trades to log", "warn")
                for t in new_trades:
                    log(f"     - {t['action']} {t['symbol']}: {t['net_quantity']} @ ${t['avg_price']:.2f}", "info")
            if discrepancies["missing_locally"]:
                log(f"   • {len(discrepancies['missing_locally'])} positions missing locally", "warn")
            if discrepancies["missing_in_ib"]:
                log(f"   • {len(discrepancies['missing_in_ib'])} positions may be closed", "warn")
        else:
            log("✓ Trade log and portfolio are in sync", "success")

    finally:
        client.disconnect()
        log("Disconnected from IB")


if __name__ == "__main__":
    main()
