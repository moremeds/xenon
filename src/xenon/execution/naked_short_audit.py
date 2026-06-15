#!/usr/bin/env python3
"""
Naked Short Audit — scan open orders and cancel naked short violations.

Detects orders that would create naked short positions (stock or call options)
and optionally cancels them via IB Gateway.

Usage:
  xenon-naked-short-audit --dry-run
  xenon-naked-short-audit
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from xenon.api.ib_pool import ClientIdBusy, acquire_owner
from xenon.clients.ib_client import CLIENT_IDS, DEFAULT_GATEWAY_PORT, DEFAULT_HOST, IBClient

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = {"Submitted", "PreSubmitted"}
DATA_DIR = Path(__file__).resolve().parents[3] / "data"

WIZARD_TAG_PREFIX = "leg_wizard:"


def _order_is_wizard_tagged(order: dict) -> bool:
    """True if the order is owned by the leg-wizard and should be skipped by the audit.

    The wizard applies server-side Gate 4 per-leg (see Wiz spec §11.1); the post-sync
    audit must not race the wizard by cancelling in-flight wizard legs.
    """
    ref = order.get("orderRef") or ""
    return isinstance(ref, str) and ref.startswith(WIZARD_TAG_PREFIX)


def _get_stock_shares(positions: list, ticker: str) -> int:
    """Sum shares held for a ticker across all Stock positions."""
    total = 0
    for pos in positions:
        if pos.get("ticker", pos.get("symbol", "")).upper() != ticker.upper():
            continue
        if pos.get("structure_type") != "Stock":
            continue
        total += int(pos.get("contracts", 0))
    return total


def _get_short_call_contracts(positions: list, ticker: str) -> int:
    """Sum existing SHORT call contracts for a ticker across all positions."""
    total = 0
    for pos in positions:
        if pos.get("ticker", pos.get("symbol", "")).upper() != ticker.upper():
            continue
        for leg in pos.get("legs", []):
            if leg.get("direction") == "SHORT" and leg.get("type") == "Call":
                total += int(leg.get("contracts", 0))
    return total


def _normalize_expiry(expiry: str | None) -> str | None:
    """Canonicalize expiry to YYYYMMDD. Returns None if missing or wrong shape."""
    if not expiry:
        return None
    clean = expiry.replace("-", "")
    return clean if len(clean) == 8 and clean.isdigit() else None


def _count_long_calls_at_expiry(positions: list, ticker: str, expiry: str | None) -> int:
    """Sum LONG call contracts for ticker at the given expiry (any strike).

    Matches web/lib/nakedShortGuard.ts countLongCallsAtExpiry() for parity.
    """
    normalized = _normalize_expiry(expiry)
    if normalized is None:
        return 0

    total = 0
    for pos in positions:
        if pos.get("ticker", pos.get("symbol", "")).upper() != ticker.upper():
            continue
        if _normalize_expiry(pos.get("expiry")) != normalized:
            continue
        for leg in pos.get("legs", []):
            if leg.get("direction") == "LONG" and leg.get("type") == "Call":
                total += int(leg.get("contracts", 0))
    return total


def _count_matching_long_options(
    positions: list, ticker: str, expiry: str | None, strike: float | None, right: str
) -> int:
    """Sum LONG option contracts for the exact (expiry, strike, right) — selling-to-close detector.

    Matches web/lib/nakedShortGuard.ts countMatchingLongOptionContracts() for parity.
    `right` is the IB single-letter: "C" or "P".
    """
    normalized = _normalize_expiry(expiry)
    if normalized is None or strike is None or right not in ("C", "P"):
        return 0

    expected_type = "Call" if right == "C" else "Put"
    total = 0
    for pos in positions:
        if pos.get("ticker", pos.get("symbol", "")).upper() != ticker.upper():
            continue
        if _normalize_expiry(pos.get("expiry")) != normalized:
            continue
        for leg in pos.get("legs", []):
            if (
                leg.get("direction") == "LONG"
                and leg.get("type") == expected_type
                and float(leg.get("strike", 0.0)) == float(strike)
            ):
                total += int(leg.get("contracts", 0))
    return total


def find_naked_short_violations(orders: list, positions: list) -> list:
    """Pure function: detect naked short violations in open orders.

    Args:
        orders: list of order dicts (from orders.json open_orders)
        positions: list of position dicts (from portfolio.json positions)

    Returns:
        List of violation dicts: [{"order_id", "perm_id", "reason", "symbol"}]
    """
    violations = []

    for order in orders:
        # Only check active orders
        if order.get("status") not in ACTIVE_STATUSES:
            continue

        if _order_is_wizard_tagged(order):
            continue

        action = order.get("action", "").upper()
        if action != "SELL":
            continue

        contract = order.get("contract", {})
        sec_type = contract.get("secType", "")
        symbol = contract.get("symbol", "")
        qty = int(order.get("totalQuantity", 0))
        order_id = order.get("orderId")
        perm_id = order.get("permId")

        # BAG/combo orders are spreads — never a violation
        if sec_type == "BAG":
            continue

        # SELL stock
        if sec_type == "STK":
            shares_held = _get_stock_shares(positions, symbol)
            if shares_held == 0:
                violations.append(
                    {
                        "order_id": order_id,
                        "perm_id": perm_id,
                        "symbol": symbol,
                        "reason": f"SELL {qty} shares of {symbol}: no LONG stock position exists",
                    }
                )
            elif qty > shares_held:
                violations.append(
                    {
                        "order_id": order_id,
                        "perm_id": perm_id,
                        "symbol": symbol,
                        "reason": (f"SELL {qty} shares of {symbol} exceeds {shares_held} shares held"),
                    }
                )
            continue

        # SELL option
        if sec_type == "OPT":
            right = contract.get("right", "").upper()
            expiry = contract.get("expiry")
            strike = contract.get("strike")

            # SELL put is cash-secured — never a violation
            if right == "P":
                continue

            # SELL call — parity with web/lib/nakedShortGuard.ts
            if right == "C":
                # 1. Sell-to-close exact match → allowed
                closing_long = _count_matching_long_options(positions, symbol, expiry, strike, "C")
                remaining_after_close = max(qty - closing_long, 0)
                if remaining_after_close == 0:
                    continue

                # 2. Vertical spread cover: long calls at same expiry, any strike
                long_calls_at_expiry = _count_long_calls_at_expiry(positions, symbol, expiry)
                spread_cover = max(long_calls_at_expiry - closing_long, 0)
                remaining_after_spread = max(remaining_after_close - spread_cover, 0)
                if remaining_after_spread == 0:
                    continue

                # 3. Fall back to stock cover
                shares_held = _get_stock_shares(positions, symbol)
                if shares_held == 0 and spread_cover == 0:
                    expiry_label = expiry or "<unknown>"
                    violations.append(
                        {
                            "order_id": order_id,
                            "perm_id": perm_id,
                            "symbol": symbol,
                            "reason": (
                                f"SELL {qty} call(s) on {symbol}: no long stock or "
                                f"vertical-spread cover at expiry {expiry_label} — naked short call"
                            ),
                        }
                    )
                    continue

                existing_short_calls = _get_short_call_contracts(positions, symbol)
                total_short_contracts = existing_short_calls + remaining_after_spread
                covered_contracts = shares_held // 100

                if total_short_contracts > covered_contracts:
                    violations.append(
                        {
                            "order_id": order_id,
                            "perm_id": perm_id,
                            "symbol": symbol,
                            "reason": (
                                f"SELL {qty} call(s) on {symbol}: total short "
                                f"({total_short_contracts}) exceeds stock cover "
                                f"({covered_contracts}) after vertical-spread accounting — "
                                f"under-covered"
                            ),
                        }
                    )
            continue

    return violations


def cancel_violations(client, violations: list) -> int:
    """Cancel each violating order via IBClient.

    Args:
        client: connected IBClient instance
        violations: list of violation dicts from find_naked_short_violations

    Returns:
        Count of orders cancelled.
    """
    if not violations:
        return 0

    cancelled = 0
    for v in violations:
        order_id = v["order_id"]
        perm_id = v["perm_id"]
        symbol = v["symbol"]
        try:
            trades = client.get_open_orders()
            trade = None
            # Find by permId first
            if perm_id and perm_id > 0:
                for t in trades:
                    if t.order.permId == perm_id:
                        trade = t
                        break
            # Fallback to orderId
            if trade is None and order_id and order_id > 0:
                for t in trades:
                    if t.order.orderId == order_id:
                        trade = t
                        break

            if trade is None:
                logger.warning(
                    "Order not found for cancellation: %s (orderId=%s, permId=%s)", symbol, order_id, perm_id
                )
                continue

            client.cancel_order(trade.order)
            client.sleep(1)
            cancelled += 1
            logger.info("Cancelled naked short violation: %s orderId=%s — %s", symbol, order_id, v["reason"])
        except Exception as e:
            logger.error("Failed to cancel order %s (orderId=%s): %s", symbol, order_id, e)

    return cancelled


def _shape_open_order_for_audit(trade) -> dict | None:
    """Map an ib_async Trade into the dict shape `find_naked_short_violations`
    consumes. Mirrors `ib_sync._open_orders_for_audit`."""
    order = getattr(trade, "order", None)
    contract = getattr(trade, "contract", None)
    status_obj = getattr(trade, "orderStatus", None)
    if order is None or contract is None:
        return None
    return {
        "status": getattr(status_obj, "status", "") if status_obj else "",
        "action": getattr(order, "action", "") or "",
        "totalQuantity": int(getattr(order, "totalQuantity", 0) or 0),
        "orderId": getattr(order, "orderId", None),
        "permId": getattr(order, "permId", None),
        "orderRef": getattr(order, "orderRef", "") or "",
        "contract": {
            "secType": getattr(contract, "secType", "") or "",
            "symbol": getattr(contract, "symbol", "") or "",
            "right": getattr(contract, "right", "") or "",
            "expiry": getattr(contract, "lastTradeDateOrContractMonth", "") or "",
            "strike": getattr(contract, "strike", 0) or 0,
        },
    }


def main(argv=None):
    """CLI entry point. Returns summary dict (for testing).

    Default sources are PG (positions via account_snapshots) + live IB
    (open orders). The legacy ``--portfolio FILE`` / ``--orders FILE``
    flags remain for offline test fixtures and dry-run forensics.
    """
    parser = argparse.ArgumentParser(description="Naked short audit — detect and cancel violations")
    parser.add_argument("--dry-run", action="store_true", help="Print violations without cancelling")
    parser.add_argument(
        "--portfolio",
        type=str,
        default=None,
        help="Optional path to portfolio.json fixture (default: PG account_snapshots for current scope)",
    )
    parser.add_argument(
        "--orders",
        type=str,
        default=None,
        help="Optional path to orders.json fixture (default: live IB open orders)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_GATEWAY_PORT)

    args = parser.parse_args(argv)

    # Positions: PG by default, optional file override.
    if args.portfolio is not None:
        with open(args.portfolio) as f:
            portfolio_data = json.load(f)
        positions = portfolio_data.get("positions", [])
    else:
        from xenon.execution.account_scope import resolve_from_env
        from xenon.utils.portfolio_loader import load_portfolio_payload_sync

        payload = load_portfolio_payload_sync(scope=resolve_from_env())
        if payload is None:
            print(json.dumps({"error": "no portfolio snapshot in PG for current scope"}, indent=2))
            return {"violations_found": 0, "violations": [], "cancelled": 0, "error": "no portfolio snapshot"}
        positions = payload.get("positions", [])

    # Orders + cancel: fold both into a single IB session so we don't acquire
    # the audit client_id twice.
    audit_client_id = CLIENT_IDS.get("ib_order_manage", 25)
    needs_ib = args.orders is None or not args.dry_run
    summary: dict
    if args.orders is not None:
        with open(args.orders) as f:
            orders_data = json.load(f)
        orders = orders_data.get("open_orders", [])
        violations = find_naked_short_violations(orders, positions)
        summary = {
            "violations_found": len(violations),
            "violations": violations,
            "cancelled": 0,
            "dry_run": args.dry_run,
        }
        if not violations or args.dry_run:
            print(json.dumps(summary, indent=2))
            return summary
        # Live cancel of violations from file-supplied orders.
        try:
            with acquire_owner(audit_client_id, timeout_ms=5000):
                client = IBClient()
                try:
                    client.connect(host=args.host, port=args.port, client_id=audit_client_id)
                    summary["cancelled"] = cancel_violations(client, violations)
                except Exception as e:
                    logger.error("IB connection failed: %s", e)
                    summary["error"] = str(e)
                finally:
                    try:
                        client.disconnect()
                    except Exception:
                        pass
        except ClientIdBusy as e:
            logger.error("audit deferred: clientId %d busy", e.client_id)
            summary["error"] = f"audit deferred: clientId {e.client_id} busy"
            print(json.dumps(summary, indent=2))
            sys.exit(2)
        print(json.dumps(summary, indent=2))
        return summary

    # Default path: pull orders from IB and cancel inside the same session.
    try:
        with acquire_owner(audit_client_id, timeout_ms=5000):
            client = IBClient()
            try:
                client.connect(host=args.host, port=args.port, client_id=audit_client_id)
                trades = client.get_open_orders()
                orders = [o for o in (_shape_open_order_for_audit(t) for t in trades) if o is not None]
                violations = find_naked_short_violations(orders, positions)
                summary = {
                    "violations_found": len(violations),
                    "violations": violations,
                    "cancelled": 0,
                    "dry_run": args.dry_run,
                }
                if violations and not args.dry_run:
                    summary["cancelled"] = cancel_violations(client, violations)
            except Exception as e:
                logger.error("IB session failed: %s", e)
                summary = {
                    "violations_found": 0,
                    "violations": [],
                    "cancelled": 0,
                    "dry_run": args.dry_run,
                    "error": str(e),
                }
            finally:
                try:
                    client.disconnect()
                except Exception:
                    pass
    except ClientIdBusy as e:
        logger.error("audit deferred: clientId %d busy", e.client_id)
        summary = {
            "violations_found": 0,
            "violations": [],
            "cancelled": 0,
            "dry_run": args.dry_run,
            "error": f"audit deferred: clientId {e.client_id} busy",
        }
        print(json.dumps(summary, indent=2))
        sys.exit(2)

    # Operator heartbeat for the monitored (default) audit path. The forensic
    # --portfolio/--orders/--dry-run branches return earlier and intentionally
    # don't heartbeat — they're not the monitored writer. No-ops under
    # XENON_READ_ONLY. Scope resolves from XENON_TRADING_MODE/XENON_BROKER_ACCOUNT.
    from datetime import datetime, timezone

    from xenon.db.service_health import record_service_health

    record_service_health(
        "naked_short_audit",
        "error" if summary.get("error") else "ok",
        error={"msg": summary["error"]} if summary.get("error") else None,
        finished_at=datetime.now(timezone.utc),
    )
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
