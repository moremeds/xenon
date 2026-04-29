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
from xenon.execution.trade_aggregator import aggregate_trade_from_fills

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

    if positions_changed is True:
        # Positions genuinely moved but we couldn't reconcile to a fill →
        # UNKNOWN is the right "we know something happened, can't say what" state.
        return ReconcileDecision(
            to_state="UNKNOWN",
            event_kind="REHYDRATE_UNCERTAIN",
            detail={
                "from_state": state,
                "to_state": "UNKNOWN",
                "sources": sources,
            },
        )

    # positions_changed is None → no baseline (boot path) or no signal at all.
    # We have NO authoritative evidence the row's state has changed, so leave
    # it alone. Demoting to UNKNOWN here was the bug behind the post-restart
    # "open orders disappeared from the UI" regression — `snapshot-*` rows
    # imported in WORKING got nuked to UNKNOWN by every boot, hiding real
    # IB-side open orders that the importer had just captured. The next sync
    # cycle (with a real positions baseline) is the correct place to act.
    return ReconcileDecision(
        to_state=state,
        noop=True,
        detail={
            "from_state": state,
            "reason_code": "REHYDRATE_NO_BASELINE",
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
    states: tuple[str, ...] = combo_wizard.DEFAULT_UNRESOLVED_STATES,
) -> list[dict]:
    engine = get_sync_engine()
    with engine.connect() as conn:
        rows = combo_wizard.list_unresolved_orders(
            conn,
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
            states=states,
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
    records_by_perm: dict[str, list[dict]] = {}
    for ex in execs or []:
        record = _normalize_execution_record(ex)
        if record is None:
            continue
        records_by_perm.setdefault(str(record["perm_id"]), []).append(record)

    agg: dict = {}
    for key, records in records_by_perm.items():
        bag_records = [record for record in records if record.get("sec_type") == "BAG"]
        summary_records = bag_records or records
        for record in summary_records:
            shares = float(record["qty"])
            avg = record["price"]
            if shares <= 0:
                continue
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


def _record_sec_type_from_row(row: dict, record: dict) -> str | None:
    sec_type = record.get("sec_type")
    if sec_type:
        return str(sec_type)
    row_sec_type = row.get("security_type")
    if row_sec_type == "BAG":
        return "BAG" if record.get("con_id") == row.get("con_id") else "OPT"
    return str(row_sec_type) if row_sec_type else None


def _record_metadata_for_order(row: dict, record: dict) -> dict:
    metadata = {"source": "single_leg_rehydrate"}
    sec_type = _record_sec_type_from_row(row, record)
    optional_fields = {
        "sec_type": sec_type,
        "exchange": record.get("exchange"),
        "strike": record.get("strike"),
        "expiry": record.get("expiry"),
        "right": record.get("right"),
        "realized_pnl": record.get("realized_pnl"),
    }
    for key, value in optional_fields.items():
        if value is not None and value != "":
            metadata[key] = str(value)
    return metadata


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if value is None:
        return datetime.now(timezone.utc)
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _normalize_execution_side(side: Any) -> str:
    normalized = str(side or "").upper()
    if normalized in {"BOT", "BOUGHT"}:
        return "BUY"
    if normalized in {"SLD", "SOLD"}:
        return "SELL"
    return normalized or "BUY"


def _normalize_execution_record(ex: Any) -> dict | None:
    if isinstance(ex, dict):
        pid = ex.get("perm_id") or ex.get("permId")
        exec_id = ex.get("exec_id") or ex.get("execId")
        shares = ex.get("shares") or ex.get("filled_qty") or ex.get("qty")
        price = ex.get("avg_price") or ex.get("avg_fill_price") or ex.get("avgPrice") or ex.get("price")
        if pid is None or shares is None or price is None:
            return None
        return {
            "perm_id": str(pid),
            "exec_id": str(exec_id) if exec_id is not None else None,
            "ib_order_id": str(ex.get("ib_order_id") or ex.get("order_id") or ex.get("orderId") or "") or None,
            "con_id": ex.get("con_id") or ex.get("conId"),
            "ticker": ex.get("ticker") or ex.get("symbol"),
            "sec_type": ex.get("sec_type") or ex.get("secType"),
            "exchange": ex.get("exchange"),
            "strike": ex.get("strike"),
            "expiry": ex.get("expiry") or ex.get("lastTradeDateOrContractMonth"),
            "right": ex.get("right"),
            "side": _normalize_execution_side(ex.get("side") or ex.get("action")),
            "qty": int(shares),
            "price": Decimal(str(price)),
            "commission": Decimal(str(ex.get("commission") or 0)),
            "realized_pnl": ex.get("realized_pnl") if ex.get("realized_pnl") is not None else ex.get("realizedPNL"),
            "filled_at": _coerce_datetime(ex.get("filled_at") or ex.get("time")),
        }

    execution = getattr(ex, "execution", ex)
    contract = getattr(ex, "contract", None)
    report = getattr(ex, "commissionReport", None)
    pid = getattr(execution, "permId", None) or getattr(execution, "perm_id", None)
    shares = getattr(execution, "shares", None)
    price = getattr(execution, "avgPrice", None) or getattr(execution, "price", None)
    if pid is None or shares is None or price is None:
        return None
    return {
        "perm_id": str(pid),
        "exec_id": str(getattr(execution, "execId", "") or "") or None,
        "ib_order_id": str(getattr(execution, "orderId", "") or "") or None,
        "con_id": getattr(contract, "conId", None),
        "ticker": getattr(contract, "symbol", None),
        "sec_type": getattr(contract, "secType", None),
        "exchange": getattr(execution, "exchange", None),
        "strike": getattr(contract, "strike", None),
        "expiry": getattr(contract, "lastTradeDateOrContractMonth", None),
        "right": getattr(contract, "right", None),
        "side": _normalize_execution_side(getattr(execution, "side", None)),
        "qty": int(shares),
        "price": Decimal(str(price)),
        "commission": Decimal(str(getattr(report, "commission", 0) or 0)),
        "realized_pnl": getattr(report, "realizedPNL", None) if report is not None else None,
        "filled_at": _coerce_datetime(getattr(execution, "time", None)),
    }


def _index_execution_records(execs: list) -> dict[str, list[dict]]:
    by_perm: dict[str, list[dict]] = {}
    for ex in execs or []:
        record = _normalize_execution_record(ex)
        if record is None:
            continue
        by_perm.setdefault(record["perm_id"], []).append(record)
    return by_perm


def _has_explicit_scope(row: dict) -> bool:
    return bool(row.get("account_env") != "legacy_unknown" and row.get("broker_account") != "legacy_unknown")


def _record_fill_records_for_order(row: dict, records: list[dict], orders_store) -> None:
    if not records or not _has_explicit_scope(row):
        return
    for record in records:
        if not record.get("exec_id"):
            continue
        orders_store.record_fill(
            exec_id=record["exec_id"],
            submission_id=row["submission_id"],
            combo_attempt_id=None,
            perm_id=str(row.get("perm_id") or record["perm_id"]),
            ib_order_id=str(row.get("ib_order_id") or record.get("ib_order_id") or "") or None,
            con_id=record.get("con_id") if record.get("con_id") is not None else row.get("con_id"),
            ticker=record.get("ticker") or row["ticker"],
            side=record["side"],
            qty=record["qty"],
            price=record["price"],
            commission=record["commission"],
            filled_at=record["filled_at"],
            metadata=_record_metadata_for_order(row, record),
            broker=row.get("broker", "IB"),
            account_env=row["account_env"],
            broker_account=row["broker_account"],
        )
    aggregate_trade_from_fills(submission_id=row["submission_id"])


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
    states: tuple[str, ...] = combo_wizard.DEFAULT_UNRESOLVED_STATES,
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
        states=states,
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
    exec_records_by_perm = _index_execution_records(execs)
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

        if decision.to_state in ("FILLED", "PARTIALLY_FILLED"):
            _record_fill_records_for_order(
                row,
                exec_records_by_perm.get(str(row.get("perm_id")), []),
                orders_store,
            )

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
