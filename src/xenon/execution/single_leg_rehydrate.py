"""F7.1 — Three-source reconcile for single-leg orders.

On boot (and reusable by the wizard per SL §11), reconcile rows in
{PENDING, WORKING, PARTIALLY_FILLED} against three IB sources:

    1. Open orders (by perm_id)
    2. Executions (by perm_id)
    3. Positions snapshot (by (ticker, con_id) change flag)

The outer ``rehydrate_on_boot`` performs all side-effects (DB writes +
orders_events). The inner ``_reconcile_from_three_sources`` is a pure
function consumed by both the boot path and the order wizard.

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §11.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Literal

from xenon.db.engine import get_sync_engine
from xenon.db.queries import combo_wizard
from xenon.execution import orders_store as _orders_store_mod

PENDING_TIMEOUT_SECONDS = 60

ReconcileState = Literal[
    "WORKING",
    "FILLED",
    "CANCELLED",
    "UNKNOWN",
    "FAILED",
    "PARTIALLY_FILLED",
    "PENDING",
]


@dataclass
class ReconcileDecision:
    to_state: ReconcileState
    filled_qty: int | None = None
    avg_fill_price: Decimal | None = None
    reason_code: str | None = None
    event_kind: Literal["REHYDRATE_RECONCILED", "REHYDRATE_UNCERTAIN"] = "REHYDRATE_RECONCILED"
    detail: dict = field(default_factory=dict)
    # If True, the caller should write nothing — nothing changed.
    noop: bool = False


def _submitted_at_epoch(val: Any) -> float:
    """Return epoch seconds for a submitted_at value.

    Postgres TIMESTAMP WITH TIME ZONE columns return timezone-aware datetimes.
    For backwards compat, also handles naive datetimes (treated as UTC).
    """
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc).timestamp()
        return val.timestamp()
    dt = datetime.fromisoformat(str(val))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).timestamp()
    return dt.timestamp()


def _reconcile_from_three_sources(
    row: dict,
    *,
    open_orders_by_perm: dict,
    execs_by_perm: dict,
    positions_changed: bool | None,
    now: float,
) -> ReconcileDecision:
    """Pure reconcile decision for a single row.

    Parameters
    ----------
    row: dict with keys state, perm_id, ib_order_id, submitted_at, ...
    open_orders_by_perm: {perm_id: {...}} from IB open orders snapshot.
    execs_by_perm: {perm_id: {"shares": int, "avg_price": Decimal|float}}.
    positions_changed: True/False/None — the caller's judgement on whether the
        (ticker, con_id) position row has changed since the order was submitted.
        None means "unknown / not checked" and is treated the same as True
        (err on the side of UNKNOWN rather than auto-CANCELLED).
    now: epoch seconds; used only for PENDING-timeout comparison.
    """
    state = row["state"]
    perm_id = row.get("perm_id")
    ib_order_id = row.get("ib_order_id")
    submitted_at_epoch = _submitted_at_epoch(row["submitted_at"])
    sources = {
        "open_orders": perm_id is not None and perm_id in open_orders_by_perm,
        "executions": perm_id is not None and perm_id in execs_by_perm,
        "positions_changed": positions_changed,
    }

    # --- PENDING with no ib_order_id: timeout branch -----------------------
    if state == "PENDING" and not ib_order_id:
        age_s = now - submitted_at_epoch
        if age_s > PENDING_TIMEOUT_SECONDS:
            return ReconcileDecision(
                to_state="FAILED",
                reason_code="PENDING_TIMEOUT",
                event_kind="REHYDRATE_RECONCILED",
                detail={
                    "from_state": state,
                    "to_state": "FAILED",
                    "reason_code": "PENDING_TIMEOUT",
                    "age_seconds": round(age_s, 2),
                    "sources": sources,
                },
            )
        # Young PENDING → no-op
        return ReconcileDecision(
            to_state="PENDING",
            noop=True,
            detail={"from_state": state, "age_seconds": round(age_s, 2)},
        )

    # --- State is WORKING / PARTIALLY_FILLED (or PENDING w/ ib_order_id) ---
    if perm_id is not None and perm_id in open_orders_by_perm:
        oo = open_orders_by_perm[perm_id] or {}
        oo_status = str(oo.get("status", "")).lower()
        to_state: ReconcileState = "PARTIALLY_FILLED" if "partial" in oo_status else "WORKING"
        filled_qty_out: int | None = None
        avg_dec_out: Decimal | None = None
        if to_state == "PARTIALLY_FILLED" and perm_id in execs_by_perm:
            ex = execs_by_perm[perm_id]
            filled_qty_out = int(ex.get("shares") or ex.get("filled_qty") or 0)
            avg = ex.get("avg_price") or ex.get("avg_fill_price")
            avg_dec_out = Decimal(str(avg)) if avg is not None else None
        return ReconcileDecision(
            to_state=to_state,
            filled_qty=filled_qty_out,
            avg_fill_price=avg_dec_out,
            event_kind="REHYDRATE_RECONCILED",
            detail={
                "from_state": state,
                "to_state": to_state,
                "sources": sources,
                **({"filled_qty": filled_qty_out} if filled_qty_out is not None else {}),
                **({"avg_fill_price": str(avg_dec_out)} if avg_dec_out is not None else {}),
            },
        )

    if perm_id is not None and perm_id in execs_by_perm:
        ex = execs_by_perm[perm_id]
        shares = int(ex.get("shares") or ex.get("filled_qty") or 0)
        avg = ex.get("avg_price") or ex.get("avg_fill_price")
        avg_dec = Decimal(str(avg)) if avg is not None else None
        return ReconcileDecision(
            to_state="FILLED",
            filled_qty=shares,
            avg_fill_price=avg_dec,
            event_kind="REHYDRATE_RECONCILED",
            detail={
                "from_state": state,
                "to_state": "FILLED",
                "filled_qty": shares,
                "avg_fill_price": str(avg_dec) if avg_dec is not None else None,
                "sources": sources,
            },
        )

    # Not in open orders, no executions. Positions disambiguation.
    if positions_changed is False:
        # Positions unchanged → safe to infer CANCELLED.
        return ReconcileDecision(
            to_state="CANCELLED",
            event_kind="REHYDRATE_RECONCILED",
            detail={
                "from_state": state,
                "to_state": "CANCELLED",
                "sources": sources,
            },
        )

    # Positions changed OR unknown → never auto-CANCELLED.
    return ReconcileDecision(
        to_state="UNKNOWN",
        event_kind="REHYDRATE_UNCERTAIN",
        detail={
            "from_state": state,
            "to_state": "UNKNOWN",
            "sources": sources,
        },
    )


# ---------------------------------------------------------------------------
# Side-effect-ful boot entrypoint
# ---------------------------------------------------------------------------


def _list_unresolved(
    db_path: Path | str | None = None,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[dict]:
    engine = get_sync_engine()
    with engine.connect() as conn:
        rows = combo_wizard.list_unresolved_orders(
            conn,
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
        )
    # Postgres returns `expiry` as a date object; convert to str for compat.
    for r in rows:
        if isinstance(r.get("expiry"), date):
            r["expiry"] = r["expiry"].isoformat()
    return rows


def _index_open_orders(open_orders: list) -> dict:
    out: dict = {}
    for oo in open_orders or []:
        if isinstance(oo, dict):
            pid = oo.get("perm_id") or oo.get("permId")
        else:
            pid = getattr(oo, "perm_id", None) or getattr(oo, "permId", None)
        if pid is None:
            continue
        out[str(pid)] = oo if isinstance(oo, dict) else {"perm_id": str(pid)}
    return out


def _index_executions(execs: list) -> dict:
    """Aggregate execution fills by perm_id.

    Takes shares-weighted average price across fills for the same perm_id.
    """
    agg: dict = {}
    for ex in execs or []:
        if isinstance(ex, dict):
            pid = ex.get("perm_id") or ex.get("permId")
            shares = float(ex.get("shares") or ex.get("filled_qty") or 0)
            avg = ex.get("avg_price") or ex.get("avg_fill_price") or ex.get("price")
        else:
            pid = getattr(ex, "perm_id", None) or getattr(ex, "permId", None)
            shares = float(getattr(ex, "shares", 0) or 0)
            avg = getattr(ex, "avg_price", None) or getattr(ex, "price", None)
        if pid is None or shares <= 0 or avg is None:
            continue
        key = str(pid)
        prior = agg.get(key)
        if prior is None:
            agg[key] = {"shares": shares, "avg_price": float(avg)}
        else:
            tot = prior["shares"] + shares
            prior["avg_price"] = (prior["avg_price"] * prior["shares"] + float(avg) * shares) / tot
            prior["shares"] = tot
    # Normalize integer shares
    for v in agg.values():
        v["shares"] = int(round(v["shares"]))
    return agg


def _build_positions_snapshot(
    ib_positions: list | dict | None,
    rows: list[dict],
) -> dict[tuple[str, int | None], dict]:
    """Normalize ``ib.positions()`` output to the snapshot dict shape the
    reconcile helper consumes.

    ``IBClient.get_positions()`` returns ib_insync ``Position`` objects in a
    list; older call sites pass a preformed dict. We accept either form.

    v1 has no baseline to diff against, so every row's (ticker, con_id) is
    marked ``{"changed": None}`` — this routes to UNKNOWN (REHYDRATE_UNCERTAIN)
    rather than auto-CANCELLED, which is the safe bias per SL §11.
    """
    if isinstance(ib_positions, dict):
        return ib_positions  # back-compat for tests supplying a dict
    # List form (or None) → build an "unknown per row" map so unresolved rows
    # don't accidentally match an empty dict and infer CANCELLED.
    snapshot: dict[tuple[str, int | None], dict] = {}
    for row in rows:
        key = (row.get("ticker"), row.get("con_id"))
        snapshot[key] = {"changed": None}
    return snapshot


def _positions_changed(positions_snapshot: dict, ticker: str, con_id: int | None) -> bool | None:
    """v1 heuristic: the snapshot is a dict keyed by (ticker, con_id) with a
    per-entry ``{"changed": bool}`` marker. In production, the caller builds
    this by diffing the current positions against any persisted baseline; for
    the boot path without a baseline, unknown entries return ``None`` (treated
    as "uncertain" → UNKNOWN rather than auto-CANCELLED).
    """
    if not positions_snapshot:
        return None
    key = (ticker, con_id)
    entry = positions_snapshot.get(key)
    if entry is None:
        return None
    if isinstance(entry, dict):
        ch = entry.get("changed")
        return bool(ch) if ch is not None else None
    return bool(entry)


def rehydrate_on_boot(
    ib_client_factory: Callable[[], Any],
    orders_store,
    now: Callable[[], float] = time.time,
    db_path: Path | str | None = None,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[ReconcileDecision]:
    """Reconcile all unresolved orders against IB state. Returns decisions made.

    ``orders_store`` is passed in (rather than imported) so tests can inject a
    stub if needed; we default to the real module in ``__init__`` semantics.

    Scope filters (broker/account_env/broker_account) limit reconciliation to
    rows owned by this broker account. Pass None to skip filtering (backward-
    compatible with legacy callers).
    """
    rows = _list_unresolved(
        db_path=db_path,
        broker=broker,
        account_env=account_env,
        broker_account=broker_account,
    )
    if not rows:
        return []

    ib = ib_client_factory()
    open_orders = ib.get_open_orders() or []
    execs = ib.get_executions() or []
    positions_raw = ib.get_positions() or []
    positions = _build_positions_snapshot(positions_raw, rows)

    open_idx = _index_open_orders(open_orders)
    exec_idx = _index_executions(execs)
    now_ts = now() if callable(now) else float(now)

    decisions: list[ReconcileDecision] = []
    for row in rows:
        pchanged = _positions_changed(positions, row["ticker"], row.get("con_id"))
        decision = _reconcile_from_three_sources(
            row,
            open_orders_by_perm=open_idx,
            execs_by_perm=exec_idx,
            positions_changed=pchanged,
            now=now_ts,
        )
        decisions.append(decision)

        if decision.noop:
            continue

        # Apply side effects. WORKING→WORKING is still recorded as an event
        # for observability but does not need a DB state change.
        if decision.to_state != row["state"]:
            if decision.to_state == "WORKING":
                # No dedicated helper; use a direct UPDATE via Postgres for state shift.
                _update_state_only(row["submission_id"], decision.to_state, db_path=db_path)
            elif decision.to_state in (
                "FILLED",
                "CANCELLED",
                "FAILED",
                "PARTIALLY_FILLED",
            ):
                orders_store.mark_terminal(
                    submission_id=row["submission_id"],
                    state=decision.to_state,
                    reason_code=decision.reason_code,
                    filled_qty=decision.filled_qty
                    if decision.filled_qty is not None
                    else int(row.get("filled_qty") or 0),
                    avg_fill_price=decision.avg_fill_price,
                    db_path=db_path,
                )
            elif decision.to_state == "UNKNOWN":
                _update_state_only(row["submission_id"], "UNKNOWN", db_path=db_path)

        orders_store.record_event(
            row["submission_id"],
            decision.event_kind,
            decision.detail,
            db_path=db_path,
        )

    return decisions


def _update_state_only(submission_id: str, state: str, db_path: Path | str | None = None) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        combo_wizard.update_order_state(conn, submission_id, state)
