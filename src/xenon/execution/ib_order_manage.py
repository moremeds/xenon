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
from typing import Iterator, Literal, NoReturn, Optional

from xenon.clients.ib_client import (
    CLIENT_IDS,
    DEFAULT_GATEWAY_PORT,
    DEFAULT_HOST,
    IBClient,
    IBConnectionError,
)

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
        if isinstance(exc, IBConnectionError):
            # Typed client failure after retries — the gateway could not be
            # reached or stayed on. The 326 check above runs first so a
            # clientId-in-use wrapped in IBConnectionError keeps "ownership".
            return "connection"
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
# "order 3261 rejected". The canonical ib_async surface text is
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
        except (ConnectionError, OSError, TimeoutError, RuntimeError, IBConnectionError) as exc:
            # IBClient.connect surfaces exhausted retries as IBConnectionError
            # (which can wrap a raw IB 326) and 326 as RuntimeError. Socket /
            # gateway failures arrive as ConnectionError/OSError/TimeoutError.
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


def _reject_identity(message: str) -> NoReturn:
    """Emit an ownership-class identity rejection.

    A live order whose (account, owner clientId, orderId, permId) tuple
    disagrees with the caller's assertion is not ours to touch — same class
    as IB 326 (the order belongs to another session) → 409 OWNERSHIP.
    """
    output(
        "error",
        f"Order identity mismatch — refusing to act: {message}",
        classification="ownership",
    )


def _trade_account(trade) -> Optional[str]:
    """The account stamped on a live trade — empty when the open-order
    snapshot does not populate it (ib_async defaults Order.account to '')."""
    return getattr(trade.order, "account", None) or getattr(trade, "account", None)


def _identity_notes(trade, account: Optional[str]) -> dict:
    """Output extras for asserted identity fields IB left unverifiable.

    When the caller asserts a scope account but the located trade carries no
    account field, the account leg of the tuple could not be checked — that
    is not a mismatch, but the emitted JSON flags it so operators know the
    verification was partial.
    """
    if trade is not None and account and not _trade_account(trade):
        return {"account_unverified": True}
    return {}


def _verify_identity(
    trade,
    *,
    order_id: int = 0,
    account: Optional[str] = None,
    client_id: Optional[int] = None,
) -> None:
    """Reject when a located trade disagrees on an asserted tuple element.

    The element used for the match itself is already proven; this verifies
    the rest of the caller-supplied identity. A missing account field is
    NOT a mismatch — only reject when both sides carry a value and differ
    (cancel/modify must not be blocked by an unpopulated IB field).
    """
    problems = []
    if order_id > 0 and trade.order.orderId != order_id:
        problems.append(f"orderId={order_id} asserted but order has orderId={trade.order.orderId}")
    if account:
        trade_account = _trade_account(trade)
        if trade_account and trade_account != account:
            problems.append(f"account={account} asserted but order is on account={trade_account}")
    if client_id is not None and trade.order.clientId != client_id:
        problems.append(f"owner clientId={client_id} asserted but order is owned by clientId={trade.order.clientId}")
    if problems:
        _reject_identity("; ".join(problems))


def find_trade(
    client: IBClient,
    order_id: int,
    perm_id: int,
    account: Optional[str] = None,
    client_id: Optional[int] = None,
):
    """Locate the open trade for the asserted identity tuple.

    Identity is (scope account, owner clientId, orderId, permId) — spec
    §2.4. permId is globally unique across sessions and is the only trusted
    key when supplied: a missing permId NEVER falls back to orderId
    (orderIds collide across clientIds, and a vanished permId means the
    order changed identity — e.g. filled — before our request landed).

    orderId-only lookups stay supported for orders that have no permId yet
    (the permId=0 race right after placement), but a match shared by more
    than one client is ambiguous → reject unless ``client_id`` pins it.
    """
    trades = client.get_open_orders()

    if perm_id > 0:
        trade = next((t for t in trades if t.order.permId == perm_id), None)
        if trade is None:
            return None
        _verify_identity(trade, order_id=order_id, account=account, client_id=client_id)
        return trade

    if order_id > 0:
        matches = [t for t in trades if t.order.orderId == order_id]
        if client_id is not None:
            # (clientId, orderId) is a complete IB identity — orders owned
            # by other clients are simply not candidates.
            matches = [t for t in matches if t.order.clientId == client_id]
        if len(matches) > 1:
            _reject_identity(
                f"{len(matches)} open orders share orderId={order_id} on different clients — resubmit with permId"
            )
        if not matches:
            return None
        trade = matches[0]
        _verify_identity(trade, order_id=order_id, account=account, client_id=client_id)
        return trade

    return None


def find_frozen_trade(
    client: IBClient,
    order_id: int,
    perm_id: int,
    client_id: int,
    account: Optional[str] = None,
):
    """Re-locate the SAME order by its frozen identity tuple.

    After the first verified lookup we know the order's true owner
    (clientId, orderId, and permId when non-zero). Every later snapshot
    must contain THAT order — a different permId or clientId occupying the
    same orderId is "not found", never an alternate match, and never a
    disappearance-ack we didn't cause.
    """
    for t in client.get_open_orders():
        if t.order.orderId != order_id or t.order.clientId != client_id:
            continue
        if perm_id > 0 and t.order.permId != perm_id:
            continue
        _verify_identity(t, order_id=order_id, account=account, client_id=client_id)
        return t
    return None


def _latch_perm_id(frozen: dict, trade) -> None:
    """Pin the first observed nonzero permId onto the frozen identity.

    While an order carries permId 0, (clientId, orderId) is the only
    matchable identity. Once a snapshot shows a nonzero permId on that
    frozen order the identity is complete — every later lookup must match
    it, so a different nonzero permId occupying the same slot is "not
    found", never a replacement we act on or an ack we didn't cause.
    """
    if trade is not None and frozen["perm_id"] <= 0 and (trade.order.permId or 0) > 0:
        frozen["perm_id"] = trade.order.permId


def _await_order_state(
    client: IBClient,
    frozen: dict,
    *,
    account: Optional[str] = None,
    initial_trade=None,
) -> Iterator[tuple]:
    """Poll refreshed open-order state — 10 ticks × 0.5s (unchanged).

    Yields ``(latest_trade, refreshed_trade)`` per tick. ``latest_trade`` is
    the most recent non-None snapshot (``initial_trade`` until the first
    refresh lands) so callers can report last-seen state after a timeout;
    ``refreshed_trade`` is this tick's raw ``find_frozen_trade`` result —
    ``None`` when the FROZEN order vanished from open orders (the
    disappearance-as-ack rule). The frozen (clientId, orderId, permId)
    tuple is matched on every poll so a different order sharing the same
    orderId can never be picked up mid-operation. A nonzero permId
    observed mid-poll is latched into ``frozen`` (see ``_latch_perm_id``).
    """
    latest = initial_trade
    for _ in range(10):
        client.sleep(0.5)
        refreshed = find_frozen_trade(
            client, frozen["order_id"], frozen["perm_id"], frozen["client_id"], account=account
        )
        if refreshed is not None:
            _latch_perm_id(frozen, refreshed)
            latest = refreshed
        yield latest, refreshed


def cancel_order(
    client: IBClient,
    order_id: int,
    perm_id: int,
    host: str,
    port: int,
    account: Optional[str] = None,
    client_id: Optional[int] = None,
):
    """Cancel an open order.

    IB's cancelOrder is scoped by (clientId, orderId) -- a cancel from a
    different clientId than the one that placed the order fails with
    Error 10147 (order not found).  We detect the original clientId
    from trade.order.clientId and reconnect as that client before cancelling.

    ``account``/``client_id`` assert the rest of the identity tuple
    (spec §2.4): scope account and recorded placing clientId.
    """
    trade = find_trade(client, order_id, perm_id, account=account, client_id=client_id)
    if trade is None:
        output(
            "error",
            f"Trade not found (orderId={order_id}, permId={perm_id})",
            classification="ib_reject",
            upstream={"code": 10147, "message": "Order not found (local lookup)"},
            **_identity_notes(trade, account),
        )

    status = trade.orderStatus.status
    if status in ("Filled", "Cancelled", "ApiCancelled"):
        output(
            "error",
            f"Order already {status} — cannot cancel",
            classification="ib_reject",
            **_identity_notes(trade, account),
        )

    # Freeze the observed identity — every later lookup must find THIS
    # order, not another sharing the same orderId. A different permId or
    # clientId in a later snapshot is "not found", never a match. permId 0
    # keeps the (clientId, orderId) fallback only until a snapshot shows a
    # nonzero permId, which _latch_perm_id pins into `frozen` — a later
    # different nonzero permId is a different order.
    frozen = {
        "order_id": trade.order.orderId,
        "perm_id": trade.order.permId or 0,
        "client_id": trade.order.clientId,
    }

    # Reconnect as the original placer if needed (fixes Error 10147).
    # _reconnect_as_owner handles 326-retry + emits classified output on failure.
    original_client_id = frozen["client_id"]
    if client.ib.client.clientId != original_client_id:
        _reconnect_as_owner(client, host, port, original_client_id)
        trade = find_frozen_trade(
            client, frozen["order_id"], frozen["perm_id"], frozen["client_id"], account=account
        )
        if trade is None:
            output(
                "error",
                "Trade not found after reconnect as original clientId",
                classification="ib_reject",
                upstream={"code": 10147, "message": "Order not found (local lookup)"},
                **_identity_notes(trade, account),
            )
        _latch_perm_id(frozen, trade)

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
    for latest_trade, refreshed_trade in _await_order_state(
        client, frozen, account=account, initial_trade=trade
    ):
        if refreshed_trade is None:
            finish(
                "ok",
                f"Order cancelled (orderId={trade.order.orderId})",
                orderId=trade.order.orderId,
                finalStatus="Cancelled",
                **_identity_notes(latest_trade or trade, account),
            )

        if refreshed_trade.orderStatus.status in ("Cancelled", "ApiCancelled"):
            finish(
                "ok",
                f"Order cancelled (orderId={refreshed_trade.order.orderId})",
                orderId=refreshed_trade.order.orderId,
                finalStatus=refreshed_trade.orderStatus.status,
                **_identity_notes(refreshed_trade, account),
            )

        # Check for fatal errors (10147=order not found, 201=rejected)
        fatal = [e for e in error_msgs if e[0] in (10147, 201)]
        if fatal:
            finish(
                "error",
                f"IB rejected cancel: {fatal[0][1]}",
                classification=classify_failure(fatal[0][0]),
                upstream={"code": fatal[0][0], "message": fatal[0][1]},
                **_identity_notes(refreshed_trade, account),
            )

    final_status = latest_trade.orderStatus.status if latest_trade is not None else trade.orderStatus.status
    finish(
        "error",
        f"Cancel failed — order still {final_status}",
        orderId=trade.order.orderId,
        finalStatus=final_status,
        classification="ib_reject",
        **_identity_notes(latest_trade or trade, account),
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
    account: Optional[str] = None,
    client_id: Optional[int] = None,
    non_guaranteed: Optional[int] = None,
):
    """Modify an open order.

    IB's placeOrder is scoped by (clientId, orderId) -- a modify from a
    different clientId than the one that placed the order silently fails
    with Error 103 (Duplicate order id).  We detect the original clientId
    from trade.order.clientId and reconnect as that client before modifying.

    ``account``/``client_id`` assert the rest of the identity tuple
    (spec §2.4): scope account and recorded placing clientId.
    """
    trade = find_trade(client, order_id, perm_id, account=account, client_id=client_id)
    if trade is None:
        output(
            "error",
            f"Trade not found (orderId={order_id}, permId={perm_id})",
            classification="ib_reject",
            upstream={"code": 10147, "message": "Order not found (local lookup)"},
            **_identity_notes(trade, account),
        )

    status = trade.orderStatus.status
    if status in ("Filled", "Cancelled", "ApiCancelled"):
        output(
            "error",
            f"Order already {status} — cannot modify",
            classification="ib_reject",
            **_identity_notes(trade, account),
        )

    order_type = trade.order.orderType
    if order_type not in ("LMT", "STP LMT"):
        output(
            "error",
            f"Cannot modify price on {order_type} order — only LMT and STP LMT supported",
            classification="ib_reject",
            **_identity_notes(trade, account),
        )

    if new_price is None and new_quantity is None and outside_rth is None:
        output(
            "error",
            "Must provide at least one modify field",
            classification="ib_reject",
            **_identity_notes(trade, account),
        )

    if new_price is not None:
        # Credit combos carry a NEGATIVE BAG limit — a signed price is legal
        # on BAG orders. Zero is meaningless on anything; negative on a
        # single-leg order is an error.
        is_bag = trade.contract.secType == "BAG"
        if new_price == 0 or (new_price < 0 and not is_bag):
            output(
                "error",
                "New price must be non-zero" if is_bag else "New price must be > 0",
                classification="ib_reject",
                **_identity_notes(trade, account),
            )
    if new_quantity is not None and new_quantity <= 0:
        output("error", "New quantity must be > 0", classification="ib_reject", **_identity_notes(trade, account))

    # Freeze the observed identity — every later lookup must find THIS
    # order, not another sharing the same orderId. A different permId or
    # clientId in a later snapshot is "not found", never a match. permId 0
    # keeps the (clientId, orderId) fallback only until a snapshot shows a
    # nonzero permId, which _latch_perm_id pins into `frozen` — a later
    # different nonzero permId is a different order.
    frozen = {
        "order_id": trade.order.orderId,
        "perm_id": trade.order.permId or 0,
        "client_id": trade.order.clientId,
    }

    # Reconnect as the original placer if needed (fixes Error 103).
    # _reconnect_as_owner handles 326-retry + emits classified output on failure.
    original_client_id = frozen["client_id"]
    if client.ib.client.clientId != original_client_id:
        _reconnect_as_owner(client, host, port, original_client_id)
        trade = find_frozen_trade(
            client, frozen["order_id"], frozen["perm_id"], frozen["client_id"], account=account
        )
        if trade is None:
            output(
                "error",
                "Trade not found after reconnect as original clientId",
                classification="ib_reject",
                upstream={"code": 10147, "message": "Order not found (local lookup)"},
                **_identity_notes(trade, account),
            )
        _latch_perm_id(frozen, trade)

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
    # The mode is always sent explicitly from the API route's persisted
    # value (--non-guaranteed); the "1" fallback only covers direct CLI use
    # without the flag — never a silent mode change.
    if trade.contract.secType == "BAG":
        from ib_async import TagValue

        trade.order.smartComboRoutingParams = [
            TagValue("NonGuaranteed", str(non_guaranteed) if non_guaranteed is not None else "1")
        ]

    client.place_order(trade.contract, trade.order)

    # Wait for refreshed IB state to reflect the requested modify.
    confirmed_trade = None
    latest_trade = trade
    for latest_trade, refreshed_trade in _await_order_state(
        client, frozen, account=account, initial_trade=trade
    ):
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
                **_identity_notes(refreshed_trade, account),
            )

    client.ib.errorEvent -= on_error

    if confirmed_trade is None:
        latest_status = latest_trade.orderStatus.status if latest_trade is not None else "Unknown"
        latest_price = latest_trade.order.lmtPrice if latest_trade is not None else None
        latest_quantity = json_number(latest_trade.order.totalQuantity) if latest_trade is not None else None
        # Surface ALL IB error events captured during the attempt. The "fatal"
        # filter above only catches 103/201/202; non-fatal codes like 10147
        # (order not found), 326 (clientId in use), or warnings would be
        # swallowed otherwise — but they're often the actual rejection reason.
        captured_errors = [{"code": code, "message": msg} for (code, msg) in error_msgs]
        output(
            "error",
            "Modify not confirmed by refreshed IB open orders",
            orderId=trade.order.orderId,
            requestedPrice=new_price,
            requestedQuantity=new_quantity,
            currentPrice=latest_price,
            currentQuantity=latest_quantity,
            finalStatus=latest_status,
            originalClientId=trade.order.clientId,
            currentClientId=client.ib.client.clientId,
            ibErrors=captured_errors,
            classification="ib_reject",
            **_identity_notes(latest_trade or trade, account),
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
        **_identity_notes(confirmed_trade, account),
    )


def main():
    parser = argparse.ArgumentParser(description="Cancel or modify IB orders")
    sub = parser.add_subparsers(dest="action", required=True)

    cancel_p = sub.add_parser("cancel")
    cancel_p.add_argument("--order-id", type=int, default=0)
    cancel_p.add_argument("--perm-id", type=int, default=0)
    cancel_p.add_argument("--account", default=None, help="Asserted scope account (identity tuple)")
    cancel_p.add_argument("--client-id", type=int, default=None, help="Asserted owner clientId (identity tuple)")
    cancel_p.add_argument("--host", default=DEFAULT_HOST)
    cancel_p.add_argument("--port", type=int, default=DEFAULT_PORT)

    modify_p = sub.add_parser("modify")
    modify_p.add_argument("--order-id", type=int, default=0)
    modify_p.add_argument("--perm-id", type=int, default=0)
    modify_p.add_argument("--account", default=None, help="Asserted scope account (identity tuple)")
    modify_p.add_argument("--client-id", type=int, default=None, help="Asserted owner clientId (identity tuple)")
    modify_p.add_argument("--new-price", type=float)
    modify_p.add_argument("--new-quantity", type=int)
    modify_p.add_argument(
        "--non-guaranteed",
        type=int,
        choices=[0, 1],
        default=None,
        help="Explicit NonGuaranteed mode for BAG modifies (0=guaranteed, 1=non-guaranteed)",
    )
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
            cancel_order(
                client,
                args.order_id,
                args.perm_id,
                args.host,
                args.port,
                account=args.account,
                client_id=args.client_id,
            )
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
                account=args.account,
                client_id=args.client_id,
                non_guaranteed=args.non_guaranteed,
            )
    except IBConnectionError as exc:
        # Connection dropped mid-operation (stale socket, gateway restart) —
        # emit the JSON error contract instead of dying with a traceback.
        output(
            "error",
            f"IB connection lost: {exc}",
            classification="connection",
        )
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
