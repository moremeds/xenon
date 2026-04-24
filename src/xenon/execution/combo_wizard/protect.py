"""Combo wizard post-fill protection pipeline.

For V1 defined-risk spreads, after a combo FILLED we:

1. Attach a broker-side combo TP where the payoff is stable enough.
2. Arm a Risk Alert (assisted-exit) row — NOT a stop-loss per spec §9.2.

Retries TP attach with exponential backoff. On terminal failure we leave the
session in PROTECTION_PENDING and emit a session event; the monitor daemon
(rehydrate + wizard_stop_monitor) will re-drive.

Gate-4 guard: if the proposed TP would short an uncovered leg we refuse the
TP attach and route to Risk Alert only.

IMPORTANT: signed combo pricing is preserved end-to-end. Do NOT abs() any
combo price in this module — CREDIT spreads have negative net prices.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from xenon.execution import orders_store

logger = logging.getLogger(__name__)

_REHYDRATABLE = {"SUBMITTING", "WORKING", "REPRICE_PENDING", "PROTECTION_PENDING", "PROTECTED"}


def risk_alert_popup_copy() -> str:
    """Spec §9.2 — the popup must say Risk Alert → Assisted Exit, not stop-loss.

    Any UI surface that shows this message should call this helper so the
    wording stays centralized and the copy rule is grep-able.
    """
    return "Risk Alert triggered — Assisted Exit required. Confirm to close this combo; the system will not auto-exit."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db_path(db_path: Path | str | None) -> Path:
    return orders_store._resolve_path(db_path)


def _connect(db_path: Path | str | None):
    return orders_store._connect_utc(_db_path(db_path))


def _load_session(session_id: str, db_path: Path | str | None) -> dict[str, Any]:
    orders_store.init_store(_db_path(db_path))
    con = _connect(db_path)
    try:
        row = con.execute(
            """
            SELECT session_id, ticker, state, structure_name, intent, payload_json
              FROM wizard_sessions
             WHERE session_id = ?
            """,
            [session_id],
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise ValueError(f"Unknown wizard session {session_id}")
    payload = json.loads(row[5]) if row[5] else {}
    return {
        "session_id": row[0],
        "ticker": row[1],
        "state": row[2],
        "structure_name": row[3],
        "intent": row[4],
        "payload": payload,
    }


def _set_state(session_id: str, state: str, db_path: Path | str | None) -> None:
    con = _connect(db_path)
    try:
        con.execute(
            "UPDATE wizard_sessions SET state=?, updated_at=? WHERE session_id=?",
            [state, _now(), session_id],
        )
    finally:
        con.close()


def _record_event(session_id: str, kind: str, detail: dict, db_path: Path | str | None) -> None:
    con = _connect(db_path)
    try:
        con.execute(
            'INSERT INTO wizard_session_events (event_id, session_id, kind, detail, "at") VALUES (?, ?, ?, ?, ?)',
            [str(uuid.uuid4()), session_id, kind, json.dumps(detail, default=str), _now()],
        )
    finally:
        con.close()


def _upsert_protection(
    *,
    session_id: str,
    tp_enabled: bool,
    tp_target_price: Decimal | None,
    tp_ib_order_id: str | None,
    alert_enabled: bool,
    alert_threshold: Decimal | None,
    alert_virtual_id: str | None,
    db_path: Path | str | None,
) -> None:
    now = _now()
    con = _connect(db_path)
    try:
        existing = con.execute("SELECT session_id FROM wizard_protection WHERE session_id=?", [session_id]).fetchone()
        if existing is None:
            con.execute(
                """
                INSERT INTO wizard_protection (session_id, tp_enabled, tp_target_price,
                    tp_ib_order_id, alert_enabled, alert_net_mid_threshold,
                    alert_virtual_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    session_id,
                    tp_enabled,
                    str(tp_target_price) if tp_target_price is not None else None,
                    tp_ib_order_id,
                    alert_enabled,
                    str(alert_threshold) if alert_threshold is not None else None,
                    alert_virtual_id,
                    now,
                    now,
                ],
            )
        else:
            con.execute(
                """
                UPDATE wizard_protection
                   SET tp_enabled=?, tp_target_price=?, tp_ib_order_id=?,
                       alert_enabled=?, alert_net_mid_threshold=?,
                       alert_virtual_id=?, updated_at=?
                 WHERE session_id=?
                """,
                [
                    tp_enabled,
                    str(tp_target_price) if tp_target_price is not None else None,
                    tp_ib_order_id,
                    alert_enabled,
                    str(alert_threshold) if alert_threshold is not None else None,
                    alert_virtual_id,
                    now,
                    session_id,
                ],
            )
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Naked-short guard (Gate 4) — protect-time defensive check.
# ---------------------------------------------------------------------------


def _uncovered_short_calls(legs: list[dict]) -> int:
    """Count uncovered short calls in the combo legs. Matches the leg-level
    BAG guard from `src/xenon/CLAUDE.md` — SELL calls not covered by BUY
    calls (any strike, any expiry) at the combo level."""
    buy_calls = 0
    sell_calls = 0
    for leg in legs:
        right = str(leg.get("right", "")).upper()
        action = str(leg.get("action", "")).upper()
        ratio = int(leg.get("ratio", 1))
        if right != "C":
            continue
        if action == "BUY":
            buy_calls += ratio
        elif action == "SELL":
            sell_calls += ratio
    return max(0, sell_calls - buy_calls)


def _tp_would_naked_short(session: dict) -> bool:
    legs = session.get("payload", {}).get("legs", [])
    return _uncovered_short_calls(legs) > 0


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def attach_protection(
    session_id: str,
    *,
    ib: Any,
    tp_target_price: Decimal,
    alert_net_mid_threshold: Decimal,
    polarity: str = "DEBIT",
    max_attempts: int = 3,
    base_backoff: float = 2.0,
    sleep: Callable[[float], None] | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Attach combo TP + Risk Alert after a filled wizard session.

    Args:
        session_id: wizard session id.
        ib: injected IB side — must expose `place_combo_tp(*, session_id, legs,
            target_price, quantity)` and `register_risk_alert(*, session_id,
            threshold, polarity)`.
        tp_target_price: SIGNED combo target (negative for CREDIT closes).
        alert_net_mid_threshold: SIGNED net-mid threshold for Risk Alert.
        polarity: "DEBIT" | "CREDIT" | "FLAT".
        max_attempts: TP retry count (default 3).
        base_backoff: base seconds for exponential backoff (default 2.0).
        sleep: injected sleeper for testability.

    Returns:
        dict with keys `state` (PROTECTED / PROTECTION_PENDING), `attempts`,
        `tp_attached`, `alert_armed`, `tp_order_id`, `alert_virtual_id`,
        `tp_refused_reason` (if naked-short-guarded), `noop` (if session was
        already PROTECTED).
    """
    if sleep is None:
        import time as _time

        sleep = _time.sleep  # pragma: no cover (not exercised in tests)

    session = _load_session(session_id, db_path)
    if session["state"].upper() == "PROTECTED":
        return {
            "state": "PROTECTED",
            "noop": True,
            "tp_attached": True,
            "alert_armed": True,
            "attempts": 0,
        }

    legs = session.get("payload", {}).get("legs", [])
    quantity = int(session.get("payload", {}).get("quantity", 1))

    # --- Gate-4: naked-short guard ------------------------------------------
    tp_refused_reason: str | None = None
    tp_attached = False
    tp_order_id: str | None = None
    attempts = 0

    if _tp_would_naked_short(session):
        tp_refused_reason = "NAKED_SHORT_GUARD"
        _record_event(
            session_id,
            "PROTECTION_TP_REFUSED",
            {"reason": tp_refused_reason, "legs": legs},
            db_path,
        )
    else:
        # Import lazily to avoid a top-level cycle — ib_adapter imports
        # _uncovered_short_calls from this module.
        from xenon.execution.combo_wizard.ib_adapter import NakedShortGuardError

        last_err: Exception | None = None
        for i in range(max_attempts):
            attempts = i + 1
            try:
                ack = ib.place_combo_tp(
                    session_id=session_id,
                    legs=legs,
                    target_price=tp_target_price,  # signed — no abs()
                    quantity=quantity,
                )
                tp_attached = True
                tp_order_id = str(ack.get("perm_id") or ack.get("order_id") or "")
                _record_event(
                    session_id,
                    "PROTECTION_TP_ATTACHED",
                    {
                        "attempt": attempts,
                        "tp_order_id": tp_order_id,
                        "target_price": str(tp_target_price),  # signed
                    },
                    db_path,
                )
                break
            except NakedShortGuardError as exc:
                # Gate-4 / IB-201 style terminal refusal. Retrying will not
                # change the outcome — short-circuit the retry loop and route
                # to the existing "tp refused, arm Risk Alert" path (same
                # idiom as the pre-check branch above).
                tp_refused_reason = "NAKED_SHORT_GUARD"
                _record_event(
                    session_id,
                    "PROTECTION_TP_REFUSED",
                    {
                        "reason": tp_refused_reason,
                        "attempt": attempts,
                        "error": str(exc),
                        "legs": legs,
                    },
                    db_path,
                )
                break
            except Exception as exc:  # noqa: BLE001 — retry everything
                last_err = exc
                _record_event(
                    session_id,
                    "PROTECTION_TP_ATTACH_FAILED",
                    {"attempt": attempts, "error": str(exc)},
                    db_path,
                )
                if i < max_attempts - 1:
                    sleep(base_backoff * (2**i))
        if not tp_attached and tp_refused_reason is None:
            # Terminal failure — do NOT arm the alert or mark PROTECTED. Leave
            # the session in PROTECTION_PENDING; the daemon will re-drive.
            _set_state(session_id, "PROTECTION_PENDING", db_path)
            _record_event(
                session_id,
                "PROTECTION_PENDING",
                {
                    "reason": "TP_ATTACH_FAILED",
                    "attempts": attempts,
                    "last_error": str(last_err) if last_err else None,
                },
                db_path,
            )
            _upsert_protection(
                session_id=session_id,
                tp_enabled=False,
                tp_target_price=tp_target_price,
                tp_ib_order_id=None,
                alert_enabled=False,
                alert_threshold=alert_net_mid_threshold,
                alert_virtual_id=None,
                db_path=db_path,
            )
            return {
                "state": "PROTECTION_PENDING",
                "attempts": attempts,
                "tp_attached": False,
                "alert_armed": False,
                "tp_order_id": None,
                "alert_virtual_id": None,
            }

    # --- Arm the Risk Alert (always, unless entire protect is aborted) ------
    alert_armed = False
    alert_virtual_id: str | None = None
    try:
        ack = ib.register_risk_alert(
            session_id=session_id,
            threshold=alert_net_mid_threshold,  # signed — no abs()
            polarity=polarity,
        )
        alert_armed = True
        alert_virtual_id = str(ack.get("virtual_id") or "")
        _record_event(
            session_id,
            "PROTECTION_RISK_ALERT_ARMED",
            {
                "threshold": str(alert_net_mid_threshold),  # signed
                "polarity": polarity,
                "alert_virtual_id": alert_virtual_id,
            },
            db_path,
        )
    except Exception as exc:  # noqa: BLE001
        _record_event(
            session_id,
            "PROTECTION_RISK_ALERT_FAILED",
            {"error": str(exc)},
            db_path,
        )

    _upsert_protection(
        session_id=session_id,
        tp_enabled=tp_attached,
        tp_target_price=tp_target_price,
        tp_ib_order_id=tp_order_id,
        alert_enabled=alert_armed,
        alert_threshold=alert_net_mid_threshold,
        alert_virtual_id=alert_virtual_id,
        db_path=db_path,
    )

    # Determine final state.
    if tp_refused_reason == "NAKED_SHORT_GUARD":
        # TP refused but alert armed → session is "PROTECTED" in the sense that
        # the operator has the Risk Alert safety net; controller flow labels
        # this as PROTECTED to close out the workflow, with the refusal
        # recorded as an event.
        final_state = "PROTECTED" if alert_armed else "PROTECTION_PENDING"
    else:
        final_state = "PROTECTED"
    _set_state(session_id, final_state, db_path)

    out: dict[str, Any] = {
        "state": final_state,
        "attempts": attempts,
        "tp_attached": tp_attached,
        "alert_armed": alert_armed,
        "tp_order_id": tp_order_id,
        "alert_virtual_id": alert_virtual_id,
    }
    if tp_refused_reason:
        out["tp_refused_reason"] = tp_refused_reason
    return out
