"""
IB order management cores — cancel and modify an open order on the API's
``orders`` connection (run by ``api/order_owner.py`` on its worker) and
return the structured reply. There is no standalone CLI: every order write
goes through the API.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterator, Literal, NoReturn, Optional

from xenon.clients.ib_client import (
    ClientIdInUse,
    ClientIdNotAllowed,
    IBClient,
    IBConnectionError,
)


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
        if isinstance(exc, ClientIdNotAllowed):
            # The owner id is outside this environment's allowed zone (pool
            # role id, relay id, another base): never reached, not ours.
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
    """True iff *exc* is IB Error 326 (clientId already in use).

    The typed ``ClientIdInUse`` (raised by ``IBClient.connect`` from the
    ``errorEvent`` code) wins; ib_async's own timeout carries no text, so the
    text match is only a fallback.
    """
    if isinstance(exc, ClientIdInUse):
        return True
    msg = str(exc).lower()
    if "client id is already in use" in msg:
        return True
    return bool(_CLIENTID_IN_USE_RE.search(msg)) and "client id" in msg


# Disposition of an amend, stamped on every modify error reply as
# ``amend_outcome`` (PR-03b round 3). ``not_sent``: failed before
# placeOrder; ``rejected``: IB explicitly rejected the amend (103/201/202);
# ``unconfirmed``: placeOrder was sent but no refreshed state proved either
# outcome. Only the first two prove the amend did not apply — the route
# keeps the max(old,new) reservation for anything else. Tracked per
# invocation (``_Invocation``), never in module state, so one connected
# client can run many cores in a row.
_FATAL_MODIFY_CODES = (103, 201, 202)

# ---------------------------------------------------------------------------
# Structured replies. The cores (``cancel_order_on_client``,
# ``modify_order_on_client``) never print, exit or disconnect: helpers raise
# ``_Reply`` to unwind and the core returns the reply as a dict.
# ---------------------------------------------------------------------------


class _Reply(Exception):
    """One structured reply, raised to unwind out of a core."""

    def __init__(self, status: str, message: str, extra: dict) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


@dataclass
class _Invocation:
    """Invocation-local state of one cancel / modify."""

    amend_stage: Optional[str] = None


def _reply(status: str, message: str, **extra) -> NoReturn:
    raise _Reply(status, message, extra)


def _result(status: str, message: str, extra: dict, amend_stage: Optional[str] = None) -> dict:
    """The reply dict — ``{"status", "message", **extra}`` (+ amend_outcome)."""
    extra = dict(extra)
    if status != "ok" and amend_stage is not None:
        extra.setdefault("amend_outcome", amend_stage)
    return {"status": status, "message": message, **extra}


def _refuse_foreign_owner(client: "IBClient", original_client_id: int) -> NoReturn:
    """The order belongs to another clientId; IB scopes cancel/modify by
    clientId, so this connection cannot act on it → ownership (409). A
    connected client never re-identifies.
    """
    _reply(
        "error",
        f"Order is owned by clientId {original_client_id}; this connection is "
        f"clientId {client.ib.client.clientId} — not ours to cancel or modify",
        classification="ownership",
        originalClientId=original_client_id,
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
    _reply(
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


def _locate_trade(
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


def _locate_frozen_trade(
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


# CLI-semantics adapters (an identity rejection prints JSON and exits).


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
        refreshed = _locate_frozen_trade(
            client, frozen["order_id"], frozen["perm_id"], frozen["client_id"], account=account
        )
        if refreshed is not None:
            _latch_perm_id(frozen, refreshed)
            latest = refreshed
        yield latest, refreshed


def _run_core(inv: _Invocation, body: Callable[[], None]) -> dict:
    """Run one core body and turn its terminal ``_Reply`` into the result dict.

    A connection drop mid-operation (stale socket, gateway restart) is the
    ``connection`` reply, carrying the amend stage reached so far. Any other
    exception propagates (the CLI dies with a traceback, as before).
    """
    try:
        body()
    except _Reply as r:
        return _result(r.status, r.message, r.extra, inv.amend_stage)
    except IBConnectionError as exc:
        return _result("error", f"IB connection lost: {exc}", {"classification": "connection"}, inv.amend_stage)
    raise RuntimeError("order-manage core ended without a reply")  # pragma: no cover — every path replies


def _freeze_and_own(client: IBClient, trade) -> dict:
    """Freeze the observed identity; refuse when *client* is not its owner.

    Every later lookup must find THIS order, not another sharing the same
    orderId. A different permId or clientId in a later snapshot is "not
    found", never a match. permId 0 keeps the (clientId, orderId) fallback
    only until a snapshot shows a nonzero permId, which _latch_perm_id pins
    into `frozen` — a later different nonzero permId is a different order.

    IB scopes cancelOrder / placeOrder by (clientId, orderId): from another
    clientId a cancel fails with 10147 and a modify with 103, so an order
    owned by another clientId is refused (ownership).
    """
    if client.ib.client.clientId != trade.order.clientId:
        _refuse_foreign_owner(client, trade.order.clientId)
    return {
        "order_id": trade.order.orderId,
        "perm_id": trade.order.permId or 0,
        "client_id": trade.order.clientId,
    }


def cancel_order_on_client(
    client: IBClient,
    order_id: int,
    perm_id: int,
    *,
    account: Optional[str] = None,
    client_id: Optional[int] = None,
) -> dict:
    """Cancel an open order on an already-connected client; return the reply.

    No print, exit, watchdog or disconnect; the error listener is removed in
    ``finally``. ``account``/``client_id`` assert the rest of the identity
    tuple (spec §2.4): scope account and recorded placing clientId. An order
    owned by another clientId is refused (ownership).
    """
    inv = _Invocation(amend_stage="not_sent")
    return _run_core(
        inv,
        lambda: _cancel_body(client, inv, order_id, perm_id, account, client_id),
    )


def _cancel_body(client, inv, order_id, perm_id, account, client_id) -> NoReturn:
    trade = _locate_trade(client, order_id, perm_id, account=account, client_id=client_id)
    if trade is None:
        _reply(
            "error",
            f"Trade not found (orderId={order_id}, permId={perm_id})",
            classification="ib_reject",
            upstream={"code": 10147, "message": "Order not found (local lookup)"},
            **_identity_notes(trade, account),
        )

    status = trade.orderStatus.status
    if status in ("Filled", "Cancelled", "ApiCancelled"):
        _reply(
            "error",
            f"Order already {status} — cannot cancel",
            classification="ib_reject",
            **_identity_notes(trade, account),
        )

    frozen = _freeze_and_own(client, trade)

    # Capture IB error events during the cancel attempt
    error_msgs = []

    def on_error(reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if reqId == trade.order.orderId or reqId == -1:
            error_msgs.append((errorCode, errorString))

    client.ib.errorEvent += on_error
    try:
        # From here on the cancel may have reached IB: only an explicit IB
        # reject code proves it was not applied.
        inv.amend_stage = "unconfirmed"
        client.cancel_order(trade.order)
        latest_trade = trade

        # Wait for refreshed open-order state to acknowledge the cancel.
        for latest_trade, refreshed_trade in _await_order_state(client, frozen, account=account, initial_trade=trade):
            if refreshed_trade is None:
                _reply(
                    "ok",
                    f"Order cancelled (orderId={trade.order.orderId})",
                    orderId=trade.order.orderId,
                    finalStatus="Cancelled",
                    **_identity_notes(latest_trade or trade, account),
                )

            if refreshed_trade.orderStatus.status in ("Cancelled", "ApiCancelled"):
                _reply(
                    "ok",
                    f"Order cancelled (orderId={refreshed_trade.order.orderId})",
                    orderId=refreshed_trade.order.orderId,
                    finalStatus=refreshed_trade.orderStatus.status,
                    **_identity_notes(refreshed_trade, account),
                )

            # Check for fatal errors (10147=order not found, 201=rejected)
            fatal = [e for e in error_msgs if e[0] in (10147, 201)]
            if fatal:
                _reply(
                    "error",
                    f"IB rejected cancel: {fatal[0][1]}",
                    classification=classify_failure(fatal[0][0]),
                    upstream={"code": fatal[0][0], "message": fatal[0][1]},
                    amend_outcome="rejected",
                    **_identity_notes(refreshed_trade, account),
                )

        final_status = latest_trade.orderStatus.status if latest_trade is not None else trade.orderStatus.status
        _reply(
            "error",
            f"Cancel not confirmed — order still {final_status}; check working orders",
            orderId=trade.order.orderId,
            finalStatus=final_status,
            classification="unconfirmed",
            **_identity_notes(latest_trade or trade, account),
        )
    finally:
        client.ib.errorEvent -= on_error


def modify_order_on_client(
    client: IBClient,
    order_id: int,
    perm_id: int,
    new_price: Optional[float],
    *,
    outside_rth=None,
    new_quantity: Optional[int] = None,
    account: Optional[str] = None,
    client_id: Optional[int] = None,
    non_guaranteed: Optional[int] = None,
    send_by: Optional[datetime] = None,
) -> dict:
    """Modify an open order on an already-connected client; return the reply.

    No print, exit, watchdog or disconnect; the error listener is removed in
    ``finally``; the amend stage (``amend_outcome``) is invocation-local.
    ``send_by`` is checked immediately before the only placeOrder. An order
    owned by another clientId is refused (ownership).
    """
    inv = _Invocation(amend_stage="not_sent")
    return _run_core(
        inv,
        lambda: _modify_body(
            client,
            inv,
            order_id,
            perm_id,
            new_price,
            outside_rth,
            new_quantity,
            account,
            client_id,
            non_guaranteed,
            send_by,
        ),
    )


def _modify_body(
    client,
    inv,
    order_id,
    perm_id,
    new_price,
    outside_rth,
    new_quantity,
    account,
    client_id,
    non_guaranteed,
    send_by,
) -> NoReturn:
    trade = _locate_trade(client, order_id, perm_id, account=account, client_id=client_id)
    if trade is None:
        _reply(
            "error",
            f"Trade not found (orderId={order_id}, permId={perm_id})",
            classification="ib_reject",
            upstream={"code": 10147, "message": "Order not found (local lookup)"},
            **_identity_notes(trade, account),
        )

    status = trade.orderStatus.status
    if status in ("Filled", "Cancelled", "ApiCancelled"):
        _reply(
            "error",
            f"Order already {status} — cannot modify",
            classification="ib_reject",
            **_identity_notes(trade, account),
        )

    order_type = trade.order.orderType
    if order_type not in ("LMT", "STP LMT"):
        _reply(
            "error",
            f"Cannot modify price on {order_type} order — only LMT and STP LMT supported",
            classification="ib_reject",
            **_identity_notes(trade, account),
        )

    if new_price is None and new_quantity is None and outside_rth is None:
        _reply(
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
            _reply(
                "error",
                "New price must be non-zero" if is_bag else "New price must be > 0",
                classification="ib_reject",
                **_identity_notes(trade, account),
            )
    if new_quantity is not None and new_quantity <= 0:
        _reply("error", "New quantity must be > 0", classification="ib_reject", **_identity_notes(trade, account))

    frozen = _freeze_and_own(client, trade)

    # Capture IB error events during the modify attempt
    error_msgs = []

    def on_error(reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if reqId == trade.order.orderId or reqId == -1:
            error_msgs.append((errorCode, errorString))

    client.ib.errorEvent += on_error
    try:
        old_price = trade.order.lmtPrice
        old_quantity = trade.order.totalQuantity
        if new_price is not None:
            trade.order.lmtPrice = new_price
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

        # Send-by fence (round 5, item 2): Gate 4 expires the amend at
        # send_by + IB grace, so nothing may be transmitted after send_by —
        # checked immediately before the (only) placeOrder, after any reconnect.
        if send_by is not None and datetime.now(timezone.utc) >= send_by:
            _reply(
                "error",
                f"Modify send window closed at {send_by.isoformat()}; nothing was sent",
                classification="send_window_closed",
                **_identity_notes(trade, account),
            )

        # From here on the amend may have reached IB: any failure without an
        # explicit rejection is "unconfirmed", never proof it did not apply.
        inv.amend_stage = "unconfirmed"
        client.place_order(trade.contract, trade.order)

        # Wait for refreshed IB state to reflect the requested modify.
        confirmed_trade = None
        latest_trade = trade
        for latest_trade, refreshed_trade in _await_order_state(client, frozen, account=account, initial_trade=trade):
            if modify_is_confirmed(refreshed_trade, new_price, new_quantity, outside_rth):
                confirmed_trade = refreshed_trade
                break
            # Check for fatal errors (103=duplicate id, 201=rejected, 202=cancelled)
            fatal = [e for e in error_msgs if e[0] in _FATAL_MODIFY_CODES]
            if fatal:
                _reply(
                    "error",
                    f"IB rejected modify: {fatal[0][1]}",
                    classification=classify_failure(fatal[0][0]),
                    upstream={"code": fatal[0][0], "message": fatal[0][1]},
                    amend_outcome="rejected",
                    **_identity_notes(refreshed_trade, account),
                )
    finally:
        client.ib.errorEvent -= on_error

    if confirmed_trade is None:
        fatal = [e for e in error_msgs if e[0] in _FATAL_MODIFY_CODES]
        if fatal:
            _reply(
                "error",
                f"IB rejected modify: {fatal[0][1]}",
                classification=classify_failure(fatal[0][0]),
                upstream={"code": fatal[0][0], "message": fatal[0][1]},
                amend_outcome="rejected",
                **_identity_notes(latest_trade or trade, account),
            )
        latest_status = latest_trade.orderStatus.status if latest_trade is not None else "Unknown"
        latest_price = latest_trade.order.lmtPrice if latest_trade is not None else None
        latest_quantity = json_number(latest_trade.order.totalQuantity) if latest_trade is not None else None
        # Surface ALL IB error events captured during the attempt. The "fatal"
        # filter above only catches 103/201/202; non-fatal codes like 10147
        # (order not found), 326 (clientId in use), or warnings would be
        # swallowed otherwise — but they're often the actual rejection reason.
        captured_errors = [{"code": code, "message": msg} for (code, msg) in error_msgs]
        _reply(
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
    # Report what the broker holds, not the request: confirmation allows a
    # <0.001 price tolerance, and the API persists these values (PR-08).
    confirmed_price = json_number(confirmed_trade.order.lmtPrice)
    confirmed_quantity = json_number(confirmed_trade.order.totalQuantity)
    changes = []
    if new_price is not None and old_price != confirmed_price:
        changes.append(f"${old_price} → ${confirmed_price}")
    if new_quantity is not None and old_quantity != confirmed_trade.order.totalQuantity:
        changes.append(f"qty {old_quantity} → {confirmed_trade.order.totalQuantity}")
    if outside_rth is not None:
        changes.append("outside RTH updated")
    suffix = f": {', '.join(changes)}" if changes else ""
    _reply(
        "ok",
        f"Order modified{suffix}",
        orderId=trade.order.orderId,
        oldPrice=old_price,
        newPrice=confirmed_price,
        oldQuantity=json_number(old_quantity),
        newQuantity=confirmed_quantity,
        confirmedPrice=confirmed_price,
        confirmedQuantity=confirmed_quantity,
        finalStatus=final_status,
        **_identity_notes(confirmed_trade, account),
    )
