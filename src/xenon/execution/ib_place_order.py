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
import re
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

# Codes that do not reject an order by themselves. This is ib_async's warning
# set, not a verified broker-semantics list. Source: ib_async 2.1.0 ``Wrapper.error`` (ib_async/wrapper.py) warningCodes
# {105, 110, 165, 321, 329, 399, 404, 434, 492, 10167} plus the 2100-2199
# range that the same function also treats as warnings (its comments cite
# the IBKR message-code table,
# https://ibkrcampus.com/campus/ibkr-api-page/twsapi-doc/#api-error-codes).
# 110 is excluded on purpose: on a new
# (PendingSubmit) order ib_async itself turns 110 into a cancel, and the
# place route maps it to LIMIT_OFF_TICK.
_IB_NOTICE_CODES = frozenset({105, 165, 321, 329, 399, 404, 434, 492, 10167})

# Warning codes that, on a NEW order, mean IB did not accept it. Source: the
# same ib_async 2.1.0 ``Wrapper.error`` comments — 434 "The order size cannot
# be zero." and 321 "Server error when validating an API client request."
# (with its TODO that 321 on new placement may need the order deleted). The
# broker semantics of the other warning codes on a new order are unverified.
_NEW_ORDER_VALIDATION_FAILURES = frozenset({321, 434})

# ib_async OrderStatus.DoneStates minus Filled: the order is dead at IB.
TERMINAL_STATUSES = OrderStatus.DoneStates - {OrderStatus.Filled}


def _is_notice(code: int) -> bool:
    return code in _IB_NOTICE_CODES or 2100 <= code < 2200


def is_hard_error(code: object) -> bool:
    """An order-scoped code that rejects a new order: any non-notice code, or
    a new-order validation failure (321 / 434)."""
    try:
        value = int(code)
    except (TypeError, ValueError):
        return False
    return value in _NEW_ORDER_VALIDATION_FAILURES or not _is_notice(value)


# ib_async 2.1.0 ``Wrapper.execDetails`` (ib_async/wrapper.py line 833, the
# ``TradeLogEntry(time, trade.orderStatus.status, f"Fill {execution.shares}@{execution.price}")``
# call) logs every new execution with errorCode 0 and a COPIED status.
_FILL_ENTRY = re.compile(r"^Fill \S+@\S+$")


def decide_outcome(log: list, perm_id: int) -> tuple[str, int | None, str | None, str, int]:
    """Classify a new order from its ordered ib_async ``Trade.log``.

    ib_async ``Wrapper.error`` overwrites ``trade.orderStatus.status`` on a
    warning (ValidationError) and on a hard error (Cancelled), so the final
    status cannot be trusted. Its log can: ``TradeLogEntry.errorCode`` is 0
    for status entries (``placeOrder``'s PendingSubmit, ``Wrapper.orderStatus``)
    and the IB code for error / warning entries.

    Returns ``(outcome, code, message, broker_status, fill_count)``; outcome
    is ``accepted`` | ``rejected`` | ``unconfirmed``:

    - any fill entry → accepted, whatever the status or permId says. A fill
      is the strongest evidence that IB accepted AND executed the order;
      reporting a traded order as rejected invites a duplicate. Fill entries
      (``Fill <shares>@<price>``, errorCode 0) copy whatever status is
      current, so they never count as broker_status and never reset a
      hard error;
    - broker_status := status of the LAST non-fill errorCode == 0 entry
      (warning and error entries never count);
    - rejected: broker_status terminal (Cancelled / ApiCancelled / Inactive),
      or a hard error after the last broker_status. An earlier permId never
      clears a later rejection;
    - accepted: broker_status Filled, or a live broker_status with permId > 0
      and no hard error after it;
    - unconfirmed: otherwise (no ack / no permId yet, only notices) — e.g. a
      399 off-hours, where IB sends the permId after the wait.
    """
    broker_status = ""
    hard_after: tuple[int, str] | None = None
    first_hard: tuple[int, str] | None = None
    fill_count = 0
    for entry in log:
        code = int(getattr(entry, "errorCode", 0) or 0)
        message = str(getattr(entry, "message", "") or "")
        if code == 0 and _FILL_ENTRY.match(message):
            fill_count += 1
        elif code == 0:
            broker_status = str(getattr(entry, "status", "") or "")
            hard_after = None
        elif is_hard_error(code):
            first_hard = first_hard or (code, message)
            hard_after = hard_after or (code, message)
    if fill_count:
        return "accepted", None, None, broker_status, fill_count
    if broker_status in TERMINAL_STATUSES or hard_after:
        code, message = hard_after or first_hard or (None, f"order {broker_status}")
        return "rejected", code, message, broker_status, 0
    live = broker_status in OrderStatus.ActiveStates and perm_id > 0
    if broker_status == OrderStatus.Filled or live:
        return "accepted", None, None, broker_status, 0
    return "unconfirmed", None, None, broker_status, 0


def _parse_quantity(raw: object) -> int | None:
    """Positive whole-number quantity, else None."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0 or not value.is_integer():
        return None
    return int(value)


def _build_stock_contract(params: dict) -> "Stock":
    """Build a Stock contract honoring body exchange/currency. Defaults to
    SMART/USD (US stocks); a foreign cash equity (e.g. 5016 TSEJ JPY,
    000660 KRX KRW) routes to its native venue + currency."""
    symbol = str(params["symbol"]).upper()
    exchange = str(params.get("exchange") or "SMART").upper()
    currency = str(params.get("currency") or "USD").upper()
    return Stock(symbol, exchange, currency)


def place_order(params: dict) -> dict:
    """Place a limit order and return result as dict.

    After the ack wait the result carries an explicit ``outcome`` from
    ``decide_outcome`` (the ordered ``Trade.log`` timeline, never the final
    status): ``accepted`` / ``unconfirmed`` → ``status: ok``; ``rejected`` →
    ``status: error`` with the hard error's code (110 / 435 keep their route
    mappings). Only this order's log can reject it; every non-farm
    errorEvent (any reqId, including -1) is returned in ``ibNotices``.

    Caveats: an ``unconfirmed`` order (no permId inside the wait) can still be
    one IB later rejects; its WORKING row then waits for reconciliation. OP-1 is NOT fixed here: if the
    route's 15 s subprocess timeout fires after IB accepted the order, the
    reservation goes FAILED and a retry with a new attempt id can duplicate.
    """
    order_type = params.get("type", "stock")
    symbol = params["symbol"].upper()
    action = params["action"].upper()
    quantity = _parse_quantity(params.get("quantity"))
    if quantity is None:
        return {
            "status": "error",
            "reason_code": "INVALID_ORDER_BODY",
            "message": f"quantity must be a positive whole number, got {params.get('quantity')!r}",
        }
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
            # NonGuaranteed is always sent explicitly — 0=guaranteed,
            # 1=non-guaranteed. The persisted reservation records the same
            # mode (gate4 legs payload) so modify can never silently change it.
            try:
                non_guaranteed = int(params.get("nonGuaranteed", 1))
            except (TypeError, ValueError):
                non_guaranteed = 1
            if non_guaranteed not in (0, 1):
                non_guaranteed = 1
            order.smartComboRoutingParams = [TagValue("NonGuaranteed", str(non_guaranteed))]
            print(
                f"  Combo order: {len(legs_data)} legs, NonGuaranteed={non_guaranteed}, ratios={[int(l.get('ratio', 1)) for l in legs_data]}"
            )

        # Place
        trade = client.place_order(contract, order)

        # Combo orders need extra time: IB routes each leg independently and
        # risk checks take longer — 2 s is not enough to get an ack.
        wait_secs = 5 if order_type == "combo" else 2
        client.sleep(wait_secs)

        order_id = trade.order.orderId
        # openOrder fills order.permId, orderStatus fills orderStatus.permId;
        # either one proves IB accepted the order.
        perm_id = max(
            int(getattr(trade.order, "permId", 0) or 0),
            int(getattr(trade.orderStatus, "permId", 0) or 0) if trade.orderStatus else 0,
        )
        # Cancel / modify must reuse the placing clientId ("auto" allocates one).
        client_id = getattr(trade.order, "clientId", 0) or getattr(client, "_last_client_id", 0) or None
        accepted_tif = getattr(trade.order, "tif", None) or tif
        status = trade.orderStatus.status if trade.orderStatus else "Unknown"

        notices = [{"reqId": r, "code": c, "message": m} for r, c, m in ib_events]
        outcome, code, msg, broker_status, fill_count = decide_outcome(list(getattr(trade, "log", None) or []), perm_id)
        if outcome == "rejected":
            # Prefer IB's raw text over ib_async's "Error 201, reqId 7: ..." log line.
            msg = next((m for r, c, m in ib_events if r == order_id and c == code), msg)
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
                "brokerStatus": broker_status,
                "outcome": outcome,
                "filled": False,
                "fillCount": 0,
                "clientId": client_id,
                "ibNotices": notices,
            }

        return {
            "status": "ok",
            "orderId": order_id,
            "permId": perm_id,
            "tif": accepted_tif,
            "initialStatus": status,
            "message": f"{action} {quantity} {symbol} @ ${limit_price:.2f} {accepted_tif} — {status}",
            "brokerStatus": broker_status,
            "outcome": outcome,
            "filled": fill_count > 0,
            "fillCount": fill_count,
            "clientId": client_id,
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
