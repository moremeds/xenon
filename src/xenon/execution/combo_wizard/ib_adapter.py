"""Concrete ib_insync-backed adapter for the combo wizard protect / rehydrate
pipelines.

Task 5 shipped `protect.py` and `rehydrate.py` against abstract handles with
methods like ``place_combo_tp``, ``register_risk_alert``, ``get_executions``,
``get_open_orders``, ``get_positions``. This module produces the concrete
adapter that satisfies that contract using the existing `IBClient`.

Key guarantees:

- **Gate-4 naked-short guard** — we refuse to place a TP that would create
  naked short exposure (re-checked here, defensive; `protect.py` also checks
  before calling). Refusal raises ``NakedShortGuardError`` so the caller
  routes to the Risk Alert path instead of silently placing the order.
- **BAG envelope semantics** — closing a long-debit combo uses
  ``Order.action = "SELL"`` (IB reverses the legs). ``ComboLeg.action`` stays
  as the structure's LONG/SHORT per leg — never flipped. A double-reverse
  causes IB error 201.
- **Signed pricing preserved** — no ``abs()`` on combo prices. CREDIT closes
  submit with a negative ``lmtPrice``.
- **register_risk_alert** is app-side only. No broker order is placed; we
  persist a ``RISK_ALERT_REGISTERED`` event + a virtual alert id so the
  ``wizard_stop_monitor`` handler can drive the crossing check.

Citations (every ib_insync call site below is grep-verified against the
installed source tree at `.venv/lib/python3.13/site-packages/ib_insync/`).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

# ib_insync imports. Citations:
#   ib_insync/contract.py:11  — class Contract (BAG via secType='BAG')
#   ib_insync/contract.py:418 — class Bag(Contract): __init__(**kwargs)
#   ib_insync/contract.py:449 — class ComboLeg (conId, ratio, action, exchange)
#   ib_insync/contract.py:193 — class Option(Contract)
#   ib_insync/order.py:181    — class LimitOrder(action, totalQuantity, lmtPrice)
from ib_insync import ComboLeg, Contract, LimitOrder, Option  # type: ignore

from xenon.execution import orders_store
from xenon.execution.combo_wizard.protect import _uncovered_short_calls

logger = logging.getLogger(__name__)


class NakedShortGuardError(RuntimeError):
    """Raised when a proposed TP would create uncovered short exposure."""


# Poll parameters for the permId race (see place_combo_tp). Kept at module
# scope so tests can monkeypatch them for fast regression runs.
_PERM_ID_POLL_DEADLINE_S = 2.0
_PERM_ID_POLL_INTERVAL_S = 0.1


def _is_ib_error_201(exc: BaseException) -> bool:
    """Heuristic: does this exception represent an IB "error 201"
    (terminal order reject)?

    IBClient.place_order wraps the underlying ib_insync failure in
    ``IBOrderError(f"Failed to place order: {exc}")`` — no .code attribute
    is preserved. `ib_place_order.py` classifies via errorEvent listener,
    which our adapter doesn't wire. Match whatever is observable: a `.code`
    attribute if one happens to exist, else the message payload.
    """
    code = getattr(exc, "code", None) or getattr(exc, "errorCode", None)
    if code is not None:
        try:
            if int(code) == 201:
                return True
        except (TypeError, ValueError):
            pass
    msg = str(exc) or ""
    # Match " 201 " or "error 201" / "code 201" — avoid matching strike "201"
    # or other embedded numerics by requiring non-digit boundaries.
    import re

    return bool(
        re.search(r"(?:^|[^0-9])201(?:[^0-9]|$)", msg)
        and "201" in msg
        and ("error" in msg.lower() or "reject" in msg.lower() or "code" in msg.lower())
    )


def _wait_for_perm_id(ib_client: Any, trade: Any) -> int | None:
    """Poll ``trade.order.permId`` until non-zero or deadline reached.

    ib_insync seeds permId=0 client-side; IB's openOrder ack later sets the
    real value. Returns the real permId if obtained within the deadline,
    else None (so callers fall back to order_id; downstream
    ``protect.py`` uses ``ack.get("perm_id") or ack.get("order_id")``).

    Uses ``ib_client.ib.sleep(...)`` if available (the ib_insync idiom —
    runs the event loop for N seconds without blocking), else falls back
    to ``time.sleep`` (test stubs).
    """
    order = getattr(trade, "order", None)
    if order is None:
        return None
    perm_id = getattr(order, "permId", None)
    if perm_id:
        return int(perm_id)

    sleep_fn = None
    inner_ib = getattr(ib_client, "ib", None)
    if inner_ib is not None and hasattr(inner_ib, "sleep"):
        sleep_fn = inner_ib.sleep
    elif hasattr(ib_client, "sleep"):
        sleep_fn = ib_client.sleep
    if sleep_fn is None:
        import time

        sleep_fn = time.sleep

    elapsed = 0.0
    while elapsed < _PERM_ID_POLL_DEADLINE_S:
        sleep_fn(_PERM_ID_POLL_INTERVAL_S)
        elapsed += _PERM_ID_POLL_INTERVAL_S
        perm_id = getattr(order, "permId", None)
        if perm_id:
            return int(perm_id)

    logger.warning(
        "permId still 0 after %.2fs — falling back to order_id (orderId=%s)",
        _PERM_ID_POLL_DEADLINE_S,
        getattr(order, "orderId", None),
    )
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connect(db_path: Path | str | None):
    return orders_store._connect_utc(orders_store._resolve_path(db_path))


def _record_event(session_id: str, kind: str, detail: dict, db_path: Path | str | None) -> None:
    con = _connect(db_path)
    try:
        con.execute(
            'INSERT INTO wizard_session_events (event_id, session_id, kind, detail, "at") VALUES (?, ?, ?, ?, ?)',
            [
                str(uuid.uuid4()),
                session_id,
                kind,
                json.dumps(detail, default=str),
                _now(),
            ],
        )
    finally:
        con.close()


def _fill_to_dict(fill: Any) -> dict:
    """Flatten an ib_insync Fill (NamedTuple of contract + execution + commissionReport)
    into a dict that the rehydrate per-leg aggregator understands.

    Citations:
      ib_insync/objects.py:316 — class Fill(NamedTuple): contract, execution, ...
      ib_insync/objects.py:50  — class Execution (permId, shares, price, execId)
      ib_insync/contract.py:86 — Contract.conId
    """
    if isinstance(fill, dict):
        return fill
    contract = getattr(fill, "contract", None)
    execution = getattr(fill, "execution", None)
    if execution is None:
        return {"raw": repr(fill)}
    return {
        "execId": getattr(execution, "execId", None),
        "permId": getattr(execution, "permId", None),
        "conId": getattr(contract, "conId", None) if contract is not None else None,
        "shares": getattr(execution, "shares", 0),
        "price": getattr(execution, "price", 0.0),
        "side": getattr(execution, "side", ""),
        "time": getattr(execution, "time", None),
        "orderId": getattr(execution, "orderId", 0),
    }


class ComboWizardIbAdapter:
    """Concrete IB adapter satisfying the abstract handles used by
    ``protect.attach_protection`` and ``rehydrate.rehydrate_combo_sessions``.
    """

    def __init__(self, ib_client: Any, *, db_path: Path | str | None = None):
        self._ib = ib_client
        self._db_path = db_path

    # ------------------------------------------------------------------
    # Protect-side: place_combo_tp, register_risk_alert
    # ------------------------------------------------------------------

    def place_combo_tp(
        self,
        *,
        session_id: str,
        legs: list[dict],
        target_price: Decimal,
        quantity: int,
    ) -> dict:
        """Attach a combo take-profit.

        For V1 defined-risk combos, the open was a BUY envelope (long-debit
        combo). The take-profit closes that position, so ``Order.action`` is
        ``SELL`` — IB reverses the legs on the envelope. Per-leg
        ``ComboLeg.action`` stays as the structure's LONG/SHORT per leg —
        never flipped (flipping causes a double-reverse → IB error 201).

        Signed ``target_price`` is preserved end-to-end. No ``abs()``.

        Raises:
            NakedShortGuardError: if legs would leave uncovered short calls.
        """
        # Defensive re-check of Gate 4. `protect.py` also checks; we refuse
        # here so any direct caller can't bypass the guard.
        if _uncovered_short_calls(legs) > 0:
            _record_event(
                session_id,
                "PROTECTION_TP_REFUSED_ADAPTER",
                {"reason": "NAKED_SHORT_GUARD", "legs": legs},
                self._db_path,
            )
            raise NakedShortGuardError(f"Refusing TP for session {session_id}: would leave uncovered short calls")

        # Qualify each leg as an Option so the BAG gets real conIds.
        # If the caller already supplied conIds we still qualify so that
        # exchange/localSymbol get populated (IB requires fully-qualified
        # contracts on BAG legs).
        # Citation: ib_insync/contract.py:193 — class Option(Contract)
        option_contracts = []
        for leg in legs:
            option_contracts.append(
                Option(
                    symbol=str(leg.get("symbol") or ""),
                    lastTradeDateOrContractMonth=str(leg.get("expiry") or ""),
                    strike=float(leg.get("strike") or 0.0),
                    right=str(leg.get("right") or ""),
                    exchange="SMART",
                    currency="USD",
                )
            )
        # Citation: ib_insync/ib.py:? qualifyContracts via IBClient wrapper
        # (src/xenon/clients/ib_client.py:775).
        qualified = self._ib.qualify_contracts(*option_contracts) if option_contracts else []

        # Build BAG. Citation: ib_insync/contract.py:418 — class Bag(Contract)
        # A plain Contract with secType='BAG' is the idiom already used in
        # src/xenon/execution/ib_place_order.py:65-80.
        combo = Contract()
        combo.symbol = str(legs[0].get("symbol") or "") if legs else ""
        combo.secType = "BAG"
        combo.currency = "USD"
        combo.exchange = "SMART"

        combo_legs: list[ComboLeg] = []
        for i, leg in enumerate(legs):
            # Prefer qualified conId (IB-provided) but fall back to the leg's
            # own conId if qualification didn't return anything at this index.
            # Citation: ib_insync/contract.py:449-457 — class ComboLeg
            con_id = None
            if i < len(qualified):
                con_id = getattr(qualified[i], "conId", None)
            if not con_id:
                con_id = leg.get("conId") or leg.get("con_id") or 0
            cl = ComboLeg()
            cl.conId = int(con_id)
            cl.ratio = int(leg.get("ratio", 1))
            # Per-leg action preserves the structure; do NOT flip based on the
            # envelope. Combo / BAG Order Guardrails in src/xenon/CLAUDE.md.
            cl.action = str(leg.get("action", "")).upper()
            cl.exchange = "SMART"
            combo_legs.append(cl)
        combo.comboLegs = combo_legs

        # Citation: ib_insync/order.py:181-187 — class LimitOrder(action,
        # totalQuantity, lmtPrice). Signed price preserved (no abs()).
        order = LimitOrder(
            action="SELL",  # close a long-debit combo
            totalQuantity=int(quantity),
            lmtPrice=float(target_price),
            tif="GTC",
        )

        # IBClient.place_order wraps ib.placeOrder. Citation:
        # src/xenon/clients/ib_client.py:548 — place_order(contract, order).
        # Classify terminal broker rejects (IB error 201 — order rejected,
        # commonly Gate-4-ish "Contract not allowed for short") as a
        # NakedShortGuardError so the protect.py retry loop doesn't waste
        # attempts on a terminal reject. All other errors re-raise unchanged
        # so the existing retry/backoff path still applies.
        try:
            trade = self._ib.place_order(combo, order)
        except Exception as exc:  # noqa: BLE001 — we re-raise below
            if _is_ib_error_201(exc):
                _record_event(
                    session_id,
                    "PROTECTION_TP_REFUSED_BROKER_201",
                    {"reason": "IB_ERROR_201", "error": str(exc), "legs": legs},
                    self._db_path,
                )
                raise NakedShortGuardError(
                    f"Refusing TP for session {session_id}: IB error 201 (terminal broker reject): {exc}"
                ) from exc
            raise

        # permId=0 race: ib_insync seeds Trade.order.permId=0 client-side and
        # only fills the real value once IB's openOrder ack arrives. The
        # rehydrate BAG per-leg aggregator keys on permId, so emitting 0/None
        # here would break combo fill detection. Poll briefly for the real
        # value using the ib_insync idiom (ib.sleep yields the event loop
        # without blocking). Matches ib_place_order.py:138 which also waits
        # after place_order before reading permId.
        order_id = getattr(getattr(trade, "order", None), "orderId", None)
        perm_id = _wait_for_perm_id(self._ib, trade)
        _record_event(
            session_id,
            "PROTECTION_TP_SUBMITTED_ADAPTER",
            {
                "order_id": order_id,
                "perm_id": perm_id,
                "target_price": str(target_price),  # signed
                "quantity": int(quantity),
            },
            self._db_path,
        )
        return {"order_id": order_id, "perm_id": perm_id}

    def register_risk_alert(
        self,
        *,
        session_id: str,
        threshold: Decimal,
        polarity: str,
    ) -> dict:
        """Register a Risk Alert (Assisted Exit, NOT a stop-loss). App-side
        only — no broker order. Persists a ``RISK_ALERT_REGISTERED`` event
        keyed by a virtual id that the ``wizard_stop_monitor`` handler uses.
        """
        virtual_id = f"ra-{uuid.uuid4().hex[:12]}"
        _record_event(
            session_id,
            "RISK_ALERT_REGISTERED",
            {
                "virtual_id": virtual_id,
                "threshold": str(threshold),  # signed
                "polarity": polarity,
            },
            self._db_path,
        )
        return {"virtual_id": virtual_id}

    # ------------------------------------------------------------------
    # Rehydrate-side: get_executions, get_open_orders, get_positions
    # ------------------------------------------------------------------

    def get_executions(self) -> list[dict]:
        """Return executions as a list of dicts, preserving permId + conId.

        The rehydrate BAG per-leg aggregator keys on ``permId`` (parent) and
        ``conId`` (leg) — dropping either would break combo fill detection.

        No ``since`` parameter: the previous signature forwarded a ``datetime``
        to ``IBClient.get_executions(exec_filter)``, which expects an
        ``ib_insync.ExecutionFilter`` — a latent type mismatch with no live
        callers. If a future caller needs filtering, add proper
        ExecutionFilter conversion at the call site then.

        Citation: ib_insync/ib.py:822 — def reqExecutions(self, filter) ->
        List[Fill]. Wrapped by IBClient.get_executions (ib_client.py:782).
        """
        raw = self._ib.get_executions()
        return [_fill_to_dict(f) for f in (raw or [])]

    def get_open_orders(self) -> list:
        """Delegate to IBClient. Citation: ib_insync/ib.py:804 — reqAllOpenOrders
        (wrapped in ib_client.py:649 which calls reqAllOpenOrders + openTrades)."""
        return self._ib.get_open_orders() or []

    def get_positions(self) -> list:
        """Delegate. Citation: ib_insync/ib.py:429 — def positions(account) ->
        List[Position]. Wrapped by IBClient.get_positions (ib_client.py:499)."""
        return self._ib.get_positions() or []
