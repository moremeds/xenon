#!/usr/bin/env python3
"""
Interactive Brokers Orders Sync

Connects to TWS/IB Gateway and syncs open orders to Postgres.

Requirements:
  pip install ib_async

Usage:
  xenon-ib-orders              # Display orders
  xenon-ib-orders --sync       # Register open-order snapshots in Postgres
  xenon-ib-orders --port 4001  # Custom port
"""

import argparse
import math
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from xenon.clients.ib_client import CLIENT_IDS, DEFAULT_GATEWAY_PORT, DEFAULT_HOST, IBClient
from xenon.execution import gate4, orders_store
from xenon.execution.account_scope import AccountScope, resolve_from_env

DEFAULT_PORT = DEFAULT_GATEWAY_PORT
DEFAULT_CLIENT_ID = CLIENT_IDS["ib_orders"]

# IB uses this sentinel for "no value" on realizedPNL
IB_SENTINEL = 1.7976931348623157e308


def connect_ib(host: str, port: int, client_id="auto") -> IBClient:
    """Connect to TWS/IB Gateway, return an IBClient."""
    client = IBClient()
    try:
        client.connect(host=host, port=port, client_id=client_id)
        print(f"Connected to IB on {host}:{port}")
        return client
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)


def format_contract(contract) -> str:
    """Format contract into display string like 'ALAB C120'"""
    symbol = contract.symbol
    sec_type = contract.secType

    if sec_type == "STK":
        return symbol
    elif sec_type == "OPT":
        right = "C" if contract.right == "C" else "P"
        strike = int(contract.strike) if contract.strike == int(contract.strike) else contract.strike
        return f"{symbol} {right}{strike}"
    elif sec_type == "FUT":
        expiry = getattr(contract, "lastTradeDateOrContractMonth", "")
        return f"{symbol} {expiry}"
    elif sec_type == "BAG":
        return f"{symbol} Spread"
    else:
        return f"{symbol} ({sec_type})"


def _meta_fields(meta: dict | None) -> dict:
    """Qualified option metadata (contract_meta) in the adapter's key names."""
    if not meta:
        return {}
    return {"tradingClass": meta.get("tc"), "multiplier": meta.get("mult"), "underSecType": meta.get("und_sec")}


def serialize_contract(contract, resolved_legs: dict = None, meta: dict | None = None) -> dict:
    """Serialize contract fields for JSON.

    Args:
        contract: IB contract object.
        resolved_legs: Optional dict mapping conId -> qualified Contract for BAG legs.
        meta: Optional dict mapping conId -> contract_meta dict. OPT contracts
            and BAG option legs gain tradingClass / multiplier / underSecType
            ONLY from it (Gate 4 never infers them).
    """
    meta = meta or {}
    expiry = getattr(contract, "lastTradeDateOrContractMonth", "")
    if len(expiry) == 8:
        expiry = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:8]}"

    result = {
        "conId": getattr(contract, "conId", None),
        "symbol": contract.symbol,
        "secType": contract.secType,
        "strike": getattr(contract, "strike", None),
        "right": getattr(contract, "right", None),
        "expiry": expiry or None,
    }
    if contract.secType == "OPT":
        result.update(_meta_fields(meta.get(getattr(contract, "conId", None))))

    # Include resolved combo leg details for BAG orders
    combo_legs = getattr(contract, "comboLegs", None)
    if contract.secType == "BAG" and combo_legs and resolved_legs:
        legs = []
        for cl in combo_legs:
            leg_data = {
                "conId": cl.conId,
                "ratio": cl.ratio,
                "action": cl.action,
            }
            resolved = resolved_legs.get(cl.conId)
            if resolved:
                leg_expiry = getattr(resolved, "lastTradeDateOrContractMonth", "")
                if len(leg_expiry) == 8:
                    leg_expiry = f"{leg_expiry[:4]}-{leg_expiry[4:6]}-{leg_expiry[6:8]}"
                leg_data["symbol"] = resolved.symbol
                leg_data["strike"] = getattr(resolved, "strike", None)
                leg_data["right"] = getattr(resolved, "right", None)
                leg_data["expiry"] = leg_expiry or None
                # Coverage legs need the leg's own sec type (a BAG can carry
                # a stock leg next to option legs).
                leg_data["secType"] = getattr(resolved, "secType", None)
                if leg_data["secType"] == "OPT":
                    leg_data.update(_meta_fields(meta.get(cl.conId)))
            legs.append(leg_data)
        result["comboLegs"] = legs

    return result


class _ResolvedLeg:
    """Contract-like view of a contract_meta dict for serialize_contract."""

    def __init__(self, meta: dict):
        exp = meta.get("exp") or ""
        self.symbol = meta.get("sym")
        self.strike = meta.get("strike")
        self.right = meta.get("right")
        self.lastTradeDateOrContractMonth = exp
        self.secType = meta.get("sec")


def safe_float(value) -> Optional[float]:
    """Convert IB value to float, filtering sentinel values"""
    if value is None:
        return None
    try:
        f = float(value)
        if math.isnan(f) or math.isinf(f) or abs(f) >= IB_SENTINEL:
            return None
        return round(f, 4)
    except (TypeError, ValueError):
        return None


def _non_guaranteed_mode(order) -> int | None:
    """Parse the NonGuaranteed tag from order.smartComboRoutingParams.

    Returns 0 (guaranteed), 1 (non-guaranteed), or None when the order
    carries no tag / an unparseable value. None means "unknown" — callers
    must not assume a default when persisting reservation state.
    """
    for tag in getattr(order, "smartComboRoutingParams", None) or []:
        if getattr(tag, "tag", None) == "NonGuaranteed":
            try:
                value = int(str(tag.value))
            except (TypeError, ValueError):
                return None
            return value if value in (0, 1) else None
    return None


def fetch_open_orders(client: IBClient) -> list:
    """Fetch open orders from all clients (one ``reqAllOpenOrders`` read)."""
    return open_orders_from_trades(client, client.get_open_orders())


def open_orders_from_trades(client: IBClient, trades: list) -> list:
    """Shape IB open-order trades for display and the Postgres mirror.

    For BAG orders, resolves combo leg conIds to full contract details
    (symbol, strike, right, expiry) so the frontend can subscribe to
    real-time prices and compute BID/MID/ASK for spread modification.
    Every option contract (single-leg or combo leg) also gets its
    IB-qualified tradingClass / multiplier / underSecType via
    ``contract_meta`` — Gate 4 reads them from the mirrored row and fails
    closed when a lookup failed.
    """
    from xenon.execution import contract_meta

    # Collect all unique combo leg conIds that need resolving
    combo_con_ids: set = set()
    opt_con_ids: set = set()
    for trade in trades:
        contract = trade.contract
        combo_legs = getattr(contract, "comboLegs", None)
        if contract.secType == "BAG" and combo_legs:
            for cl in combo_legs:
                combo_con_ids.add(cl.conId)
        elif contract.secType == "OPT" and getattr(contract, "conId", None):
            opt_con_ids.add(contract.conId)

    # One reqContractDetails per distinct conId: the qualified contract for
    # combo legs plus the option metadata for every option contract.
    details = contract_meta.fetch_meta_by_con_ids(client, sorted(combo_con_ids | opt_con_ids))
    resolved_legs: dict = {}
    for con_id in combo_con_ids:
        m = details.get(con_id)
        if m is not None:
            resolved_legs[con_id] = _ResolvedLeg(m)

    orders = []
    for trade in trades:
        contract = trade.contract
        order = trade.order
        status = trade.orderStatus

        orders.append(
            {
                "orderId": order.orderId,
                "permId": order.permId,
                # Placing API clientId (IB reports it on every openOrder) —
                # matches a Xenon row whose permId was not known at placement.
                "clientId": getattr(order, "clientId", None),
                "symbol": format_contract(contract),
                "contract": serialize_contract(contract, resolved_legs, details),
                "action": order.action,  # BUY or SELL
                "orderType": order.orderType,  # LMT, MKT, STP, etc.
                "totalQuantity": float(order.totalQuantity),
                "limitPrice": safe_float(order.lmtPrice),
                "auxPrice": safe_float(order.auxPrice),  # stop price
                "status": status.status,  # Submitted, PreSubmitted, etc.
                # Account provenance (PR-03b): reqAllOpenOrders spans every
                # account the connection can see — consumers filter on this.
                "account": getattr(order, "account", None) or None,
                # BAG guarantee mode from smartComboRoutingParams — persisted
                # on the reservation row so modify can never silently change it.
                "nonGuaranteed": _non_guaranteed_mode(order),
                "filled": float(status.filled),
                "remaining": float(status.remaining),
                "avgFillPrice": safe_float(status.avgFillPrice),
                "tif": order.tif,  # DAY, GTC, IOC, etc.
                # Surface IB's orderRef so the naked-short audit can detect
                # leg_wizard-tagged orders and skip them (Wiz spec §11.1).
                "orderRef": getattr(order, "orderRef", None) or None,
            }
        )

    return orders


def fetch_executed_orders(client: IBClient) -> list:
    """Fetch executed fills via fills()"""
    fills = client.get_fills()
    executed = []

    for fill in fills:
        contract = fill.contract
        execution = fill.execution
        commission_report = fill.commissionReport

        realized_pnl = safe_float(getattr(commission_report, "realizedPNL", None))
        commission = safe_float(getattr(commission_report, "commission", None))

        executed.append(
            {
                "execId": execution.execId,
                "symbol": format_contract(contract),
                "contract": serialize_contract(contract),
                "side": execution.side,  # BOT or SLD
                "quantity": float(execution.shares),
                "avgPrice": safe_float(execution.avgPrice),
                "commission": commission,
                "realizedPNL": realized_pnl,
                "time": execution.time.isoformat() if hasattr(execution.time, "isoformat") else str(execution.time),
                "exchange": execution.exchange,
            }
        )

    # Sort by time descending (most recent first)
    executed.sort(key=lambda x: x["time"], reverse=True)
    return executed


def build_orders_data(open_orders: list, executed_orders: list) -> dict:
    """Build the display payload for open orders and session fills."""
    return {
        "last_sync": datetime.now().isoformat(),
        "open_orders": open_orders,
        "executed_orders": executed_orders,
        "open_count": len(open_orders),
        "executed_count": len(executed_orders),
    }


def sync_open_orders_to_postgres(
    open_orders: list[dict],
    *,
    scope: dict | AccountScope | None = None,
    observed_at: datetime | None = None,
) -> dict:
    """Register IB open-order snapshots in xenon.order_submissions.

    Returns counts per branch so a periodic poller can publish drift metrics:
      - registered: new rows INSERTed
      - updated:    snapshot-* rows UPDATEd because TWS values drifted
      - skipped:    UUID-authored rows left untouched (existing dedupe)
      - unidentified: open orders without a permId (0 or missing, even
        when orderId is set) — they cannot be keyed, so their demand is NOT
        persisted; a sync pass must not attest completeness while this is
        non-zero

    `observed_at` is when the caller started the IB open-order read; it
    gates which reservation changes the mirror may apply (see
    orders_store.register_from_snapshot).
    """
    if scope is None:
        resolved = resolve_from_env().as_dict()
    elif isinstance(scope, AccountScope):
        resolved = scope.as_dict()
    else:
        resolved = dict(scope)

    registered = 0
    updated = 0
    skipped = 0
    resurrected = 0
    foreign = 0
    unprovenanced = 0
    unidentified = 0
    scope_account = resolved.get("broker_account") or ""
    scoped = bool(scope_account and scope_account != "legacy_unknown")
    for order in open_orders:
        # PR-03b account provenance: reqAllOpenOrders spans every account the
        # connection can see. A row naming a DIFFERENT account must never
        # enter this scope's reservation view. An order WITHOUT account
        # provenance is still stored — `legs.account: null` marks it so the
        # gate treats the view as incomplete rather than hiding the demand.
        if scoped:
            raw_account = order.get("account")
            if raw_account and raw_account != scope_account:
                foreign += 1
                continue
            if not raw_account:
                unprovenanced += 1
        contract = order.get("contract") or {}
        sec_type = str(contract.get("secType") or "STK")
        multiplier = 100 if sec_type in {"OPT", "BAG"} else 1
        # permId is the only account-wide order key. orderId is unique only
        # per clientId (two clients can both have orderId 7), so an order
        # without a permId (the permId=0 race) is unidentified: not written,
        # and the pass is incomplete (round 5, item 4). ib_order_id falls
        # back to "0" (IB reports BAGs from another client with orderId=0).
        perm_id = str(order.get("permId") or "")
        ib_order_id = str(order.get("orderId") or "0")
        if not perm_id or perm_id == "0":
            unidentified += 1
            continue
        raw_client_id = order.get("clientId")
        strike_val = contract.get("strike")
        con_id_val = contract.get("conId")
        result = orders_store.register_from_snapshot(
            perm_id=perm_id,
            ib_order_id=ib_order_id,
            ticker=str(contract.get("symbol") or order.get("symbol") or ""),
            security_type=sec_type,
            action=str(order.get("action") or "BUY"),
            quantity=int(float(order.get("totalQuantity") or 0)),
            limit_price=float(order.get("limitPrice") or 0.0),
            multiplier=multiplier,
            strike=float(strike_val) if strike_val not in (None, 0, 0.0) else None,
            right=contract.get("right") or None,
            expiry=contract.get("expiry") or None,
            con_id=int(con_id_val) if con_id_val else None,
            tif=str(order.get("tif") or "DAY"),
            legs=gate4.legs_payload_from_snapshot_order(order),
            observed_at=observed_at,
            client_id=int(raw_client_id) if raw_client_id is not None else None,
            **resolved,
        )
        action = result.get("action") if isinstance(result, dict) else None
        if action == "INSERTED":
            registered += 1
        elif action == "UPDATED":
            updated += 1
        elif action == "RESURRECTED":
            resurrected += 1
        elif action == "SKIPPED_UUID":
            skipped += 1
    return {
        "registered": registered,
        "updated": updated,
        "resurrected": resurrected,
        "skipped": skipped,
        "skipped_foreign": foreign,
        "skipped_unprovenanced": unprovenanced,
        "unidentified": unidentified,
        "open_count": len(open_orders),
    }


def display_orders(open_orders: list, executed_orders: list):
    """Pretty print orders"""
    print(f"\n{'=' * 60}")
    print("OPEN ORDERS")
    print(f"{'=' * 60}")

    if not open_orders:
        print("  No open orders")
    else:
        for o in open_orders:
            lmt = f" @ ${o['limitPrice']}" if o["limitPrice"] else ""
            print(f"  {o['action']} {o['totalQuantity']:.0f}x {o['symbol']}{lmt} [{o['orderType']}] — {o['status']}")

    print(f"\n{'=' * 60}")
    print("EXECUTED ORDERS")
    print(f"{'=' * 60}")

    if not executed_orders:
        print("  No fills this session")
    else:
        for e in executed_orders:
            side = "BUY" if e["side"] == "BOT" else "SELL"
            pnl_str = f" P&L: ${e['realizedPNL']:,.2f}" if e["realizedPNL"] is not None else ""
            print(f"  {side} {e['quantity']:.0f}x {e['symbol']} @ ${e['avgPrice']}{pnl_str} — {e['time']}")

    print(f"\nSummary: {len(open_orders)} open, {len(executed_orders)} executed")


def main():
    parser = argparse.ArgumentParser(description="Sync orders from Interactive Brokers")
    parser.add_argument("--host", default=DEFAULT_HOST, help="TWS/Gateway host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TWS/Gateway port")
    parser.add_argument("--client-id", type=int, default=None, help="Client ID (omit for auto-allocation)")
    parser.add_argument("--sync", action="store_true", help="Register open-order snapshots in Postgres")

    args = parser.parse_args()

    client = connect_ib(args.host, args.port, args.client_id or "auto")

    try:
        print("Fetching open orders...")
        observed_at = datetime.now(timezone.utc)
        open_orders = fetch_open_orders(client)

        print("Fetching executed fills...")
        executed_orders = fetch_executed_orders(client)

        display_orders(open_orders, executed_orders)

        if args.sync:
            accounts = client.ib.managedAccounts()
            if accounts:
                os.environ["XENON_BROKER_ACCOUNT"] = accounts[0]
            result = sync_open_orders_to_postgres(open_orders, observed_at=observed_at)
            print(f"Registered {result['registered']} open-order snapshots in Postgres")
        else:
            print("\nRun with --sync to register open-order snapshots in Postgres")

    finally:
        client.disconnect()
        print("Disconnected from IB")


if __name__ == "__main__":
    main()
