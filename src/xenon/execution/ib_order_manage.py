#!/usr/bin/env python3
"""
Interactive Brokers Order Management — Cancel & Modify

Connects to TWS/IB Gateway to cancel or modify open orders.
All output is JSON to stdout for API route parsing.

Usage:
  xenon-ib-order-manage cancel --order-id 10 --perm-id 12345
  xenon-ib-order-manage modify --order-id 10 --perm-id 12345 --new-price 22.50
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Literal, NoReturn, Optional

from xenon.clients.ib_client import CLIENT_IDS, DEFAULT_GATEWAY_PORT, DEFAULT_HOST, IBClient

DEFAULT_PORT = DEFAULT_GATEWAY_PORT
DEFAULT_CLIENT_ID = CLIENT_IDS["ib_order_manage"]

# IB semantic reject codes: 201 (rejected), 202 (cancelled by exchange),
# 103 (duplicate order id), 321 (VOL field invalid), 10147 (order not found),
# 10148 (cannot modify). 326 is "client id in use" — ownership, not reject.
_IB_REJECT_CODES = {103, 201, 202, 321, 10147, 10148}


def classify_failure(
    error_code: Optional[int] = None,
    exc: Optional[BaseException] = None,
) -> Literal["connection", "ownership", "ib_reject"]:
    """Classify an IB failure per SL §8.

    - connection: no socket / gateway down / handshake reject -> HTTP 503
    - ownership:  IB 326 clientId in use -> HTTP 409
    - ib_reject:  semantic reject (201/202/103/321/10147/10148/...) -> HTTP 4xx

    Unknown failures fall back to ``connection`` (fail-safe to 503).
    """
    if exc is not None:
        if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
            return "connection"
        if _is_clientid_in_use(exc):
            return "ownership"
    if error_code == 326:
        return "ownership"
    if error_code in _IB_REJECT_CODES:
        return "ib_reject"
    if error_code is not None:
        # Unknown IB code but we have a code — treat as semantic reject
        # rather than connection (code implies the socket was alive).
        return "ib_reject"
    return "connection"


# IB Error 326 detection. Must match standalone "326" (word boundary) AND
# the phrase "client id" to avoid false positives on messages like
# "order 3261 rejected". The canonical ib_insync surface text is
# "client id is already in use"; we keep compatibility with that phrasing.
_CLIENTID_IN_USE_RE = re.compile(r"\b326\b")


def _is_clientid_in_use(exc: BaseException) -> bool:
    """True iff *exc* looks like IB Error 326 (clientId already in use)."""
    msg = str(exc).lower()
    if "client id is already in use" in msg:
        return True
    return bool(_CLIENTID_IN_USE_RE.search(msg)) and "client id" in msg


def output(status: str, message: str, **extra) -> NoReturn:
    """Print JSON result and exit.

    ``classification`` (when present in *extra*) is one of
    ``connection`` | ``ownership`` | ``ib_reject`` — consumed by the
    FastAPI route to map to HTTP 503 / 409 / 4xx (F5.4).
    """
    print(json.dumps({"status": status, "message": message, **extra}))
    sys.exit(0 if status == "ok" else 1)


def _reconnect_as_owner(client: "IBClient", host: str, port: int, original_client_id: int) -> None:
    """Reconnect the subprocess as *original_client_id*.

    Retries up to 3 times on IB Error 326 with a 0.5s backoff, always using
    the same ``original_client_id`` (never rotated — ownership invariant
    from src/xenon/api/CLAUDE.md §"Cancel / Modify Failure Propagation").

    On persistent 326, emits JSON with ``classification="ownership"``.
    On other connection failures, emits ``classification="connection"``.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(3):
        try:
            client.disconnect()
            client.connect(host=host, port=port, client_id=original_client_id)
            return
        except (ConnectionError, OSError, TimeoutError, RuntimeError) as exc:
            # IBClient.connect surfaces 326 as RuntimeError (string-wrapped).
            # Socket / gateway failures arrive as ConnectionError/OSError/TimeoutError.
            # Programmer errors (AttributeError, TypeError) deliberately propagate.
            last_exc = exc
            if _is_clientid_in_use(exc) and attempt < 2:
                time.sleep(0.5)
                continue
            # Not retryable (or retries exhausted) — classify & exit.
            if _is_clientid_in_use(exc):
                output(
                    "error",
                    f"IB clientId {original_client_id} still in use after 3 retries: {exc}",
                    classification="ownership",
                    originalClientId=original_client_id,
                )
                return  # unreachable: output() is NoReturn
            output(
                "error",
                f"IB reconnect as clientId {original_client_id} failed: {exc}",
                classification=classify_failure(None, exc),
            )
            return  # unreachable: output() is NoReturn
    # Unreachable — the loop always either returns (success) or falls into output().
    output(
        "error",
        f"IB reconnect exhausted: {last_exc}",
        classification="ownership",
    )


def json_number(value):
    """Return JSON-safe numeric primitives, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def modify_is_confirmed(trade, new_price, new_quantity, outside_rth):
    """Check refreshed IB open-order state for the requested modify fields."""
    if trade is None:
        return False

    status = trade.orderStatus.status
    if status not in ("Submitted", "PreSubmitted"):
        return False

    if new_price is not None:
        current_price = trade.order.lmtPrice
        if current_price is None or abs(current_price - new_price) >= 0.001:
            return False

    if new_quantity is not None and trade.order.totalQuantity != new_quantity:
        return False

    if outside_rth is not None and getattr(trade.order, "outsideRth", None) is not outside_rth:
        return False

    return True


def find_trade(client: IBClient, order_id: int, perm_id: int):
    """Find an open trade by permId (preferred) or orderId."""
    trades = client.get_open_orders()

    # Prefer permId (globally unique across IB sessions)
    if perm_id > 0:
        for trade in trades:
            if trade.order.permId == perm_id:
                return trade

    # Fallback to orderId
    if order_id > 0:
        for trade in trades:
            if trade.order.orderId == order_id:
                return trade

    return None


def cancel_order(client: IBClient, order_id: int, perm_id: int, host: str, port: int):
    """Cancel an open order.

    IB's cancelOrder is scoped by (clientId, orderId) -- a cancel from a
    different clientId than the one that placed the order fails with
    Error 10147 (order not found).  We detect the original clientId
    from trade.order.clientId and reconnect as that client before cancelling.
    """
    trade = find_trade(client, order_id, perm_id)
    if trade is None:
        output(
            "error",
            f"Trade not found (orderId={order_id}, permId={perm_id})",
            classification="ib_reject",
            upstream={"code": 10147, "message": "Order not found (local lookup)"},
        )

    status = trade.orderStatus.status
    if status in ("Filled", "Cancelled", "ApiCancelled"):
        output(
            "error",
            f"Order already {status} — cannot cancel",
            classification="ib_reject",
        )

    # Reconnect as the original placer if needed (fixes Error 10147).
    # _reconnect_as_owner handles 326-retry + emits classified output on failure.
    original_client_id = trade.order.clientId
    if client.ib.client.clientId != original_client_id:
        _reconnect_as_owner(client, host, port, original_client_id)
        trade = find_trade(client, order_id, perm_id)
        if trade is None:
            output(
                "error",
                "Trade not found after reconnect as original clientId",
                classification="ib_reject",
                upstream={"code": 10147, "message": "Order not found (local lookup)"},
            )

    # Capture IB error events during the cancel attempt
    error_msgs = []

    def on_error(reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if reqId == trade.order.orderId or reqId == -1:
            error_msgs.append((errorCode, errorString))

    client.ib.errorEvent += on_error

    client.cancel_order(trade.order)
    latest_trade = trade

    def finish(status_text: str, message: str, **extra):
        client.ib.errorEvent -= on_error
        output(status_text, message, **extra)

    # Wait for refreshed open-order state to acknowledge the cancel.
    for _ in range(10):
        client.sleep(0.5)
        refreshed_trade = find_trade(client, order_id, perm_id)
        if refreshed_trade is None:
            finish(
                "ok",
                f"Order cancelled (orderId={trade.order.orderId})",
                orderId=trade.order.orderId,
                finalStatus="Cancelled",
            )

        latest_trade = refreshed_trade
        if refreshed_trade.orderStatus.status in ("Cancelled", "ApiCancelled"):
            finish(
                "ok",
                f"Order cancelled (orderId={refreshed_trade.order.orderId})",
                orderId=refreshed_trade.order.orderId,
                finalStatus=refreshed_trade.orderStatus.status,
            )

        # Check for fatal errors (10147=order not found, 201=rejected)
        fatal = [e for e in error_msgs if e[0] in (10147, 201)]
        if fatal:
            finish(
                "error",
                f"IB rejected cancel: {fatal[0][1]}",
                classification=classify_failure(fatal[0][0]),
                upstream={"code": fatal[0][0], "message": fatal[0][1]},
            )

    final_status = latest_trade.orderStatus.status if latest_trade is not None else trade.orderStatus.status
    finish(
        "error",
        f"Cancel failed — order still {final_status}",
        orderId=trade.order.orderId,
        finalStatus=final_status,
        classification="ib_reject",
    )


def modify_order(
    client: IBClient,
    order_id: int,
    perm_id: int,
    new_price: Optional[float],
    host: str,
    port: int,
    outside_rth=None,
    new_quantity: Optional[int] = None,
):
    """Modify an open order.

    IB's placeOrder is scoped by (clientId, orderId) -- a modify from a
    different clientId than the one that placed the order silently fails
    with Error 103 (Duplicate order id).  We detect the original clientId
    from trade.order.clientId and reconnect as that client before modifying.
    """
    trade = find_trade(client, order_id, perm_id)
    if trade is None:
        output(
            "error",
            f"Trade not found (orderId={order_id}, permId={perm_id})",
            classification="ib_reject",
            upstream={"code": 10147, "message": "Order not found (local lookup)"},
        )

    status = trade.orderStatus.status
    if status in ("Filled", "Cancelled", "ApiCancelled"):
        output(
            "error",
            f"Order already {status} — cannot modify",
            classification="ib_reject",
        )

    order_type = trade.order.orderType
    if order_type not in ("LMT", "STP LMT"):
        output(
            "error",
            f"Cannot modify price on {order_type} order — only LMT and STP LMT supported",
            classification="ib_reject",
        )

    if new_price is None and new_quantity is None and outside_rth is None:
        output("error", "Must provide at least one modify field", classification="ib_reject")

    if new_price is not None and new_price <= 0:
        output("error", "New price must be > 0", classification="ib_reject")
    if new_quantity is not None and new_quantity <= 0:
        output("error", "New quantity must be > 0", classification="ib_reject")

    # Reconnect as the original placer if needed (fixes Error 103).
    # _reconnect_as_owner handles 326-retry + emits classified output on failure.
    original_client_id = trade.order.clientId
    if client.ib.client.clientId != original_client_id:
        _reconnect_as_owner(client, host, port, original_client_id)
        trade = find_trade(client, order_id, perm_id)
        if trade is None:
            output(
                "error",
                "Trade not found after reconnect as original clientId",
                classification="ib_reject",
                upstream={"code": 10147, "message": "Order not found (local lookup)"},
            )

    # Capture IB error events during the modify attempt
    error_msgs = []

    def on_error(reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if reqId == trade.order.orderId or reqId == -1:
            error_msgs.append((errorCode, errorString))

    client.ib.errorEvent += on_error

    old_price = trade.order.lmtPrice
    old_quantity = trade.order.totalQuantity
    applied_price = old_price
    if new_price is not None:
        trade.order.lmtPrice = new_price
        applied_price = new_price
    if new_quantity is not None:
        trade.order.totalQuantity = new_quantity
    if outside_rth is not None:
        trade.order.outsideRth = outside_rth

    # Clear VOL-related fields to prevent IB error 321 on non-VOL orders.
    # IB populates these on open order snapshots; re-submitting them on a
    # LMT order causes "VOL order requires non-negative floating point value".
    if order_type not in ("VOL",):
        trade.order.volatility = 1.7976931348623157e308  # IB sentinel = unset
        trade.order.volatilityType = 2147483647  # IB sentinel = unset

    # Ensure NonGuaranteed is set for combo/BAG orders.
    # IB's open order snapshot may strip smartComboRoutingParams;
    # re-submitting without it causes "Missing or invalid NonGuaranteed value".
    if trade.contract.secType == "BAG":
        from ib_insync import TagValue

        trade.order.smartComboRoutingParams = [TagValue("NonGuaranteed", "1")]

    client.place_order(trade.contract, trade.order)

    # Wait for refreshed IB state to reflect the requested modify.
    confirmed_trade = None
    latest_trade = trade
    for _ in range(10):
        client.sleep(0.5)
        refreshed_trade = find_trade(client, order_id, perm_id)
        if refreshed_trade is not None:
            latest_trade = refreshed_trade
        if modify_is_confirmed(refreshed_trade, new_price, new_quantity, outside_rth):
            confirmed_trade = refreshed_trade
            break
        # Check for fatal errors (103=duplicate id, 201=rejected, 202=cancelled)
        fatal = [e for e in error_msgs if e[0] in (103, 201, 202)]
        if fatal:
            client.ib.errorEvent -= on_error
            output(
                "error",
                f"IB rejected modify: {fatal[0][1]}",
                classification=classify_failure(fatal[0][0]),
                upstream={"code": fatal[0][0], "message": fatal[0][1]},
            )

    client.ib.errorEvent -= on_error

    if confirmed_trade is None:
        latest_status = latest_trade.orderStatus.status if latest_trade is not None else "Unknown"
        latest_price = latest_trade.order.lmtPrice if latest_trade is not None else None
        latest_quantity = json_number(latest_trade.order.totalQuantity) if latest_trade is not None else None
        output(
            "error",
            "Modify not confirmed by refreshed IB open orders",
            orderId=trade.order.orderId,
            requestedPrice=new_price,
            requestedQuantity=new_quantity,
            currentPrice=latest_price,
            currentQuantity=latest_quantity,
            finalStatus=latest_status,
            classification="ib_reject",
        )

    final_status = confirmed_trade.orderStatus.status
    changes = []
    if new_price is not None and old_price != applied_price:
        changes.append(f"${old_price} → ${applied_price}")
    if new_quantity is not None and old_quantity != confirmed_trade.order.totalQuantity:
        changes.append(f"qty {old_quantity} → {confirmed_trade.order.totalQuantity}")
    if outside_rth is not None:
        changes.append("outside RTH updated")
    suffix = f": {', '.join(changes)}" if changes else ""
    output(
        "ok",
        f"Order modified{suffix}",
        orderId=trade.order.orderId,
        oldPrice=old_price,
        newPrice=applied_price,
        oldQuantity=json_number(old_quantity),
        newQuantity=json_number(confirmed_trade.order.totalQuantity),
        finalStatus=final_status,
    )


def main():
    parser = argparse.ArgumentParser(description="Cancel or modify IB orders")
    sub = parser.add_subparsers(dest="action", required=True)

    cancel_p = sub.add_parser("cancel")
    cancel_p.add_argument("--order-id", type=int, default=0)
    cancel_p.add_argument("--perm-id", type=int, default=0)
    cancel_p.add_argument("--host", default=DEFAULT_HOST)
    cancel_p.add_argument("--port", type=int, default=DEFAULT_PORT)

    modify_p = sub.add_parser("modify")
    modify_p.add_argument("--order-id", type=int, default=0)
    modify_p.add_argument("--perm-id", type=int, default=0)
    modify_p.add_argument("--new-price", type=float)
    modify_p.add_argument("--new-quantity", type=int)
    modify_p.add_argument(
        "--outside-rth", action="store_true", default=None, help="Allow order to fill outside regular trading hours"
    )
    modify_p.add_argument(
        "--no-outside-rth",
        dest="outside_rth",
        action="store_false",
        help="Restrict order to regular trading hours only",
    )
    modify_p.add_argument("--host", default=DEFAULT_HOST)
    modify_p.add_argument("--port", type=int, default=DEFAULT_PORT)

    args = parser.parse_args()

    if args.order_id == 0 and args.perm_id == 0:
        output("error", "Must provide --order-id or --perm-id", classification="ib_reject")

    client = IBClient()
    try:
        client.connect(host=args.host, port=args.port, client_id="auto")
    except Exception as e:
        output(
            "error",
            f"IB connection failed: {e}",
            classification=classify_failure(None, e),
        )

    try:
        if args.action == "cancel":
            cancel_order(client, args.order_id, args.perm_id, args.host, args.port)
        elif args.action == "modify":
            modify_order(
                client,
                args.order_id,
                args.perm_id,
                args.new_price,
                args.host,
                args.port,
                outside_rth=args.outside_rth,
                new_quantity=args.new_quantity,
            )
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
