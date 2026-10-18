#!/usr/bin/env python3
"""
IB Order Placement — JSON-in / JSON-out for web API.

Places a limit order via IB, waits briefly for acknowledgement, returns JSON.
Does NOT monitor fills or log trades (web layer handles that).

Usage:
  xenon-ib-place-order --json '{"type":"stock","symbol":"AAPL","action":"BUY","quantity":100,"limitPrice":214.50,"tif":"DAY"}'
  xenon-ib-place-order --json '{"type":"option","symbol":"GOOG","action":"BUY","quantity":10,"limitPrice":9.00,"tif":"GTC","expiry":"20260417","strike":315,"right":"C"}'
"""

import json
import os
import sys
from pathlib import Path

try:
    from ib_async import ComboLeg, Contract, LimitOrder, Option, OrderStatus, Stock, TagValue, util
except ImportError:
    print(json.dumps({"status": "error", "message": "ib_async not installed"}))
    sys.exit(1)

from xenon.clients.ib_client import CLIENT_IDS, DEFAULT_GATEWAY_PORT, DEFAULT_HOST, IBClient, IBOrderAccountError

CLIENT_ID = CLIENT_IDS.get("ib_place_order", 26)
PORT = DEFAULT_GATEWAY_PORT

# IB error 435 "You must specify an account": the gateway login manages more
# than one account and the order named none.
_IB_ACCOUNT_REQUIRED = 435

# Data-farm status chatter — dropped entirely (not even surfaced as notices).
_IB_FARM_STATUS_CODES = frozenset({2104, 2106, 2108, 2158, 10358})

# IB codes that are warnings / notices, never a rejection by themselves.
# Source: ib_async 2.1.0 ``Wrapper.error`` (ib_async/wrapper.py) warningCodes
# {105, 110, 165, 321, 329, 399, 404, 434, 492, 10167} plus the 2100-2199
# range that the same function also treats as warnings (its comments cite
# the IBKR message-code table,
# https://ibkrcampus.com/campus/ibkr-api-page/twsapi-doc/#api-error-codes).
# 110 is excluded on purpose: on a new
# (PendingSubmit) order ib_async itself turns 110 into a cancel, and the
# place route maps it to LIMIT_OFF_TICK.
_IB_NOTICE_CODES = frozenset({105, 165, 321, 329, 399, 404, 434, 492, 10167})

# ib_async OrderStatus.DoneStates minus Filled: the order is dead at IB.
_TERMINAL_STATUSES = OrderStatus.DoneStates - {OrderStatus.Filled}


def _is_notice(code: int) -> bool:
    return code in _IB_NOTICE_CODES or 2100 <= code < 2200


def _order_is_live(status: str, perm_id: int) -> bool:
    """True when IB holds the order: an ib_async active state (this includes
    ValidationError, which ib_async sets on a warning for a still-live order),
    or a permId assigned with no terminal status."""
    if status in OrderStatus.ActiveStates:
        return True
    return bool(perm_id) and perm_id > 0 and status not in _TERMINAL_STATUSES


def _build_stock_contract(params: dict) -> "Stock":
    """Build a Stock contract honoring body exchange/currency. Defaults to
    SMART/USD (US stocks); a foreign cash equity (e.g. 5016 TSEJ JPY,
    000660 KRX KRW) routes to its native venue + currency."""
    symbol = str(params["symbol"]).upper()
    exchange = str(params.get("exchange") or "SMART").upper()
    currency = str(params.get("currency") or "USD").upper()
    return Stock(symbol, exchange, currency)


def place_order(params: dict) -> dict:
    """Place a limit order and return result as dict."""
    order_type = params.get("type", "stock")
    symbol = params["symbol"].upper()
    action = params["action"].upper()
    quantity = int(params["quantity"])
    limit_price = float(params["limitPrice"])
    tif = params.get("tif", "DAY").upper()
    # FastAPI forwards the scope account it stamped on the reservation row.
    # Direct CLI use falls back to the sync-subprocess scope env var. An empty
    # value is rejected by IBClient.place_order (fail closed).
    account = str(params.get("account") or os.environ.get("XENON_BROKER_ACCOUNT", "")).strip()

    client = IBClient()

    try:
        client.connect(host=DEFAULT_HOST, port=PORT, client_id="auto", timeout=10)
    except Exception as e:
        return {"status": "error", "message": f"Connection failed: {e}"}

    try:
        # Build contract
        if order_type == "combo":
            legs_data = params["legs"]
            options = []
            for leg in legs_data:
                opt = Option(
                    symbol=symbol,
                    lastTradeDateOrContractMonth=leg["expiry"],
                    strike=float(leg["strike"]),
                    right=leg["right"],
                    exchange="SMART",
                    currency="USD",
                )
                options.append(opt)

            qualified = client.qualify_contracts(*options)
            if len(qualified) != len(options):
                return {"status": "error", "message": f"Could not qualify all combo legs for {symbol}"}

            combo = Contract()
            combo.symbol = symbol
            combo.secType = "BAG"
            combo.currency = "USD"
            combo.exchange = "SMART"

            combo_legs = []
            for i, leg in enumerate(legs_data):
                cl = ComboLeg()
                cl.conId = qualified[i].conId
                cl.ratio = int(leg.get("ratio", 1))
                cl.action = leg["action"].upper()
                cl.exchange = "SMART"
                combo_legs.append(cl)

            combo.comboLegs = combo_legs
            contract = combo

        elif order_type == "option":
            expiry = params["expiry"]
            strike = float(params["strike"])
            right = params["right"]
            contract = Option(
                symbol=symbol,
                lastTradeDateOrContractMonth=expiry,
                strike=strike,
                right=right,
                exchange="SMART",
                currency="USD",
            )
            qualified = client.qualify_contracts(contract)
            if not qualified:
                return {"status": "error", "message": f"Could not qualify contract: {symbol}"}
            contract = qualified[0]

        else:
            contract = _build_stock_contract(params)
            qualified = client.qualify_contracts(contract)
            if not qualified:
                return {"status": "error", "message": f"Could not qualify contract: {symbol}"}
            contract = qualified[0]

        # Capture IB error events during the ack wait. Only events whose
        # reqId is THIS order's orderId can reject it; reqId -1 is a
        # connection-wide message and other reqIds belong to other requests.
        ib_events: list[tuple[int, int, str]] = []

        def _on_error(reqId, errorCode, errorString, contract=None):
            if errorCode not in _IB_FARM_STATUS_CODES:
                ib_events.append((reqId, errorCode, errorString))

        client._ib.errorEvent += _on_error

        # Build order
        order = LimitOrder(
            action=action,
            totalQuantity=quantity,
            lmtPrice=limit_price,
            tif=tif,
            outsideRth=False,
        )
        order.account = account

        if order_type == "combo":
            order.smartComboRoutingParams = [TagValue("NonGuaranteed", "1")]
            print(
                f"  Combo order: {len(legs_data)} legs, NonGuaranteed=1, ratios={[int(l.get('ratio', 1)) for l in legs_data]}"
            )

        # Place
        trade = client.place_order(contract, order)

        # Combo orders need extra time: IB routes each leg independently and
        # risk checks take longer — 2 s is not enough to get an ack.
        wait_secs = 5 if order_type == "combo" else 2
        client.sleep(wait_secs)

        order_id = trade.order.orderId
        perm_id = trade.order.permId
        accepted_tif = getattr(trade.order, "tif", None) or tif
        status = trade.orderStatus.status if trade.orderStatus else "Unknown"

        # Decide by order state, not by "any errorEvent arrived".
        order_events = [(c, m) for r, c, m in ib_events if r == order_id]
        hard_errors = [(c, m) for c, m in order_events if not _is_notice(c)]
        notices = [{"reqId": r, "code": c, "message": m} for r, c, m in ib_events]
        live = _order_is_live(status, perm_id or 0)
        if status in _TERMINAL_STATUSES or (hard_errors and not live):
            code, msg = (hard_errors or order_events or [(None, f"order {status}")])[0]
            # Code 110 = "The price does not conform to the minimum price
            # variation for this contract." Means our 0.01 tick stub
            # under-approximated the real tick rule for this instrument.
            # Log structured so we can grep and measure how often it bites.
            if code == 110:
                print(
                    json.dumps(
                        {
                            "level": "warning",
                            "event": "ib_tick_rejection",
                            "ib_code": 110,
                            "ib_message": msg,
                            "symbol": symbol,
                            "limit_price": limit_price,
                            "order_type": order_type,
                            "note": "tick-rule stub returned 0.01; IB enforced true rule",
                        }
                    ),
                    file=sys.stderr,
                )
            reason = {"reason_code": "ACCOUNT_UNRESOLVED"} if code == _IB_ACCOUNT_REQUIRED else {}
            return {
                "status": "error",
                "code": code,
                **reason,
                "message": f"IB error {code}: {msg}" if code is not None else f"IB {msg}",
                "orderId": order_id,
                "permId": perm_id,
                "tif": accepted_tif,
                "initialStatus": status,
                "ibNotices": notices,
            }

        return {
            "status": "ok",
            "orderId": order_id,
            "permId": perm_id,
            "tif": accepted_tif,
            "initialStatus": status,
            "message": f"{action} {quantity} {symbol} @ ${limit_price:.2f} {accepted_tif} — {status}",
            "ibNotices": notices,
        }

    except IBOrderAccountError as e:
        return {"status": "error", "reason_code": e.reason_code, "message": str(e)}

    except Exception as e:
        return {"status": "error", "message": str(e)}

    finally:
        client.disconnect()


def main():
    if "--json" not in sys.argv:
        print(json.dumps({"status": "error", "message": "Usage: --json '{...}'"}))
        sys.exit(1)

    json_idx = sys.argv.index("--json")
    if json_idx + 1 >= len(sys.argv):
        print(json.dumps({"status": "error", "message": "Missing JSON argument after --json"}))
        sys.exit(1)

    try:
        params = json.loads(sys.argv[json_idx + 1])
    except json.JSONDecodeError as e:
        print(json.dumps({"status": "error", "message": f"Invalid JSON: {e}"}))
        sys.exit(1)

    # Validate required fields
    required = ["symbol", "action", "quantity", "limitPrice"]
    missing = [f for f in required if f not in params]
    if missing:
        print(json.dumps({"status": "error", "message": f"Missing fields: {', '.join(missing)}"}))
        sys.exit(1)

    if params.get("type") == "option":
        opt_required = ["expiry", "strike", "right"]
        opt_missing = [f for f in opt_required if f not in params]
        if opt_missing:
            print(json.dumps({"status": "error", "message": f"Option missing: {', '.join(opt_missing)}"}))
            sys.exit(1)

    if params.get("type") == "combo":
        legs = params.get("legs")
        if not legs or not isinstance(legs, list) or len(legs) < 2:
            print(json.dumps({"status": "error", "message": "Combo requires 'legs' array with 2+ entries"}))
            sys.exit(1)
        leg_required = ["expiry", "strike", "right", "action"]
        for i, leg in enumerate(legs):
            leg_missing = [f for f in leg_required if f not in leg]
            if leg_missing:
                print(json.dumps({"status": "error", "message": f"Leg {i} missing: {', '.join(leg_missing)}"}))
                sys.exit(1)

    result = place_order(params)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
