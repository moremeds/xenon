"""Postgres-backed orders_submissions / orders_events store.

Migrated from DuckDB. Preserves the same public API (function signatures,
dataclasses, return types).

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §12.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import Text, cast, func, insert, literal, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xenon.db.engine import get_sync_engine
from xenon.db.events import CHANNEL_FILL_COMMISSION_UPDATED, CHANNEL_FILL_RECORDED, emit_outbox_in_txn
from xenon.db.schema import order_events, order_fills, order_submissions, regime_overrides

# ── Read-only choke point ──


class ReadOnlyWriteError(RuntimeError):
    """Raised by every orders_store writer under XENON_READ_ONLY=1.

    Set by `scripts/infra/dev.sh live` for debugging sessions against live
    IB: reads stay open, but no live-derived order state may land in
    core_test. Real prod writes go through the macmini Docker stack, which
    never sets the flag.
    """


def ensure_writable() -> None:
    """Single choke point: refuse every orders_store write under XENON_READ_ONLY=1.

    First statement of every write function in this module so subprocess
    and in-process callers alike fail closed instead of silently persisting.
    """
    if os.environ.get("XENON_READ_ONLY") == "1":
        raise ReadOnlyWriteError(
            "XENON_READ_ONLY=1 — orders_store writes disabled (dev.sh live session)"
        )


# ── Schema init ──


def init_store(*_args, **_kwargs) -> None:
    """No-op — schema is managed by Alembic. Kept as a callable for legacy
    tests; any positional/keyword arguments (e.g. legacy ``db_path=``) are
    silently ignored.
    """
    return None


# ── Models ──


class RequestRow(BaseModel):
    ticker: str
    security_type: Literal["STK", "OPT", "BAG"]
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    expiry: str | None = None
    strike: Decimal | None = None
    right: Literal["C", "P"] | None = None
    multiplier: int
    con_id: int | None = None
    limit_price: Decimal


@dataclass
class ReservationOutcome:
    status: Literal["winner", "duplicate", "terminal", "body_mismatch"]
    submission_id: str | None
    state: str | None
    duplicate_of: str | None
    reason_code: str | None


_TERMINAL_STATES = {"REJECTED", "CANCELLED", "FAILED"}

ATTEMPT_BODY_MISMATCH = "ATTEMPT_BODY_MISMATCH"

# Sentinel for optional JSONB write-through params (register_from_snapshot):
# "not provided" must not overwrite a stored value the caller never computed.
_UNSET = object()


def gate4_lock_key(broker, account_env, broker_account) -> str:
    """Gate-4 account-scope advisory-lock key (PG `hashtext` of this string).

    Every writer of reservation state — place, modify begin/settle, and the
    snapshot mirror's legs/pending writes — serializes on this key."""
    return f"gate4:{broker or ''}:{account_env or ''}:{broker_account or ''}"


def _lock_gate4_scope(conn, broker, account_env, broker_account) -> None:
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": gate4_lock_key(broker, account_env, broker_account)},
    )


_PENDING_KEYS = ("pending_amend_qty", "pending_amend_id")


def normalize_legs_payload(raw) -> dict:
    """Stored `legs` column → dict shape {legs, guaranteed, pending_amend_qty,
    pending_amend_id[, account]}. Handles the PR-03b dict, the legacy bare
    leg list and NULL. `account` is present only when the payload recorded
    provenance."""
    if isinstance(raw, dict):
        out = dict(raw)
        out.setdefault("legs", None)
        out.setdefault("guaranteed", None)
        for k in _PENDING_KEYS:
            out.setdefault(k, None)
        return out
    if isinstance(raw, list):
        guaranteed = None
        if raw and all(isinstance(x, dict) for x in raw):
            vals = {x.get("guaranteed") for x in raw}
            vals.discard(None)
            guaranteed = vals.pop() if len(vals) == 1 else None
        return {"legs": list(raw), "guaranteed": guaranteed, "pending_amend_qty": None, "pending_amend_id": None}
    return {"legs": None, "guaranteed": None, "pending_amend_qty": None, "pending_amend_id": None}


def merge_observed_legs(stored_raw, observed) -> dict:
    """Merge a fresh IB observation into stored reservation state (item 6).

    Observation owns the leg list (when it resolved one) and the account
    provenance. Reservation state is never cleared by drift: pending amend
    fields always survive, and a known guarantee mode survives an
    observation whose routing tags are absent (guaranteed=None)."""
    obs = normalize_legs_payload(observed)
    if stored_raw is None:
        return obs
    merged = normalize_legs_payload(stored_raw)
    if obs["legs"] is not None:
        merged["legs"] = obs["legs"]
    if obs["guaranteed"] is not None:
        merged["guaranteed"] = obs["guaranteed"]
    if isinstance(observed, dict) and "account" in observed:
        merged["account"] = observed["account"]
    return merged


def _as_aware(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def is_fresh_observation(observed_at, row_updated_at) -> bool:
    """True when the IB read began after the row's last local write.

    Only a fresh observation may release reservation state. A read that
    began before a local write (amend opened, settle, cancel) may show the
    broker state from before that write.
    """
    obs = _as_aware(observed_at)
    upd = _as_aware(row_updated_at)
    return obs is not None and (upd is None or obs > upd)


def _clear_pending(payload: dict) -> dict:
    out = dict(payload)
    out["pending_amend_qty"] = None
    out["pending_amend_id"] = None
    out.pop("pending_amend_deadline", None)
    return out


def _reconcile_pending(payload: dict, observed_qty: int, *, observed_at=None, fresh: bool = False) -> tuple[dict, str | None]:
    """Resolve an open amend generation against one IB observation.

    Returns (payload, outcome) with outcome None (unchanged), "confirmed"
    (IB reports exactly the pending quantity) or "expired".

    Every outcome needs a FRESH observation (read began after the amend
    token was written — round 4 item 1), so a delayed read can never confirm
    a newer generation. "expired" additionally needs the read to begin after
    `pending_amend_deadline`: the instant after which the original sender
    can no longer apply the amend (route returned, or the modify CLI's
    hard wall-clock bound passed — this also covers an API restart). The
    broker order is then observed as it really is, so the token is released
    and demand follows the durable/observed quantity again (item 4).
    A pending amend without a deadline never expires.
    """
    if payload.get("pending_amend_id") is None and payload.get("pending_amend_qty") is None:
        return payload, None
    if not fresh:
        return payload, None
    pending = payload.get("pending_amend_qty")
    if pending is not None and int(pending) == int(observed_qty):
        return _clear_pending(payload), "confirmed"
    deadline = _as_aware(payload.get("pending_amend_deadline"))
    obs = _as_aware(observed_at)
    if deadline is not None and obs is not None and obs > deadline:
        return _clear_pending(payload), "expired"
    return payload, None


def _mirror_uuid_row(
    conn,
    submission_id: str,
    *,
    existing_state: str,
    existing_quantity: int,
    existing_legs,
    observed_qty: int,
    observed_legs,
    observed_at,
    fresh: bool,
    now: datetime,
) -> dict:
    """Mirror one observation onto a Xenon-authored (UUID) row.

    Request identity (quantity column, modify_sequence, attempt keys) stays
    owned by the place/modify path. The mirror writes only reservation
    state in the `legs` payload (PR-03b round 4, item 2):

    - qualified leg metadata from the adapter (re-stamps legacy rows whose
      legs are NULL or unqualified);
    - `observed_qty`: the broker's quantity when it differs from the
      durable one (TWS-side edit). Coverage reserves max(durable, observed)
      so the pass persists conservative demand instead of skipping it. A
      stale read can only raise it;
    - pending-amend confirmation / expiry (fresh reads only).

    A terminal row the broker still reports open is resurrected only by a
    fresh read (a read that began before the cancel proves nothing).
    """
    if existing_state in _TERMINAL_STATES | {"FILLED"}:
        if not fresh:
            return {"action": "SKIPPED_UUID", "drift": None}
        conn.execute(
            update(order_submissions)
            .where(order_submissions.c.submission_id == submission_id)
            .values(state="WORKING", reason_code=None, updated_at=now)
        )
        conn.execute(
            insert(order_events).values(
                submission_id=submission_id,
                kind="IB_MIRROR_RESURRECT",
                detail={"from_state": existing_state, "to_state": "WORKING", "reason": "ib_still_reports_open"},
                at=now,
            )
        )
        return {"action": "RESURRECTED", "drift": None}

    stored = normalize_legs_payload(existing_legs)
    payload = merge_observed_legs(existing_legs, observed_legs) if observed_legs is not _UNSET else dict(stored)
    payload, outcome = _reconcile_pending(payload, observed_qty, observed_at=observed_at, fresh=fresh)
    values: dict = {}
    if outcome == "confirmed":
        values["quantity"] = observed_qty
    durable = values.get("quantity", existing_quantity)
    prev_obs = payload.get("observed_qty")
    if fresh:
        target = observed_qty if observed_qty != durable else None
    else:
        raised = [v for v in (prev_obs, observed_qty if observed_qty > durable else None) if v is not None]
        target = max(raised) if raised else None
    if target is None:
        payload.pop("observed_qty", None)
    else:
        payload["observed_qty"] = int(target)

    if payload == stored and not values:
        return {"action": "SKIPPED_UUID", "drift": None}
    conn.execute(
        update(order_submissions)
        .where(order_submissions.c.submission_id == submission_id)
        .values(legs=payload, updated_at=now, **values)
    )
    kind = {"confirmed": "AMEND_RECONCILED", "expired": "AMEND_EXPIRED"}.get(outcome, "IB_MIRROR_OBSERVED")
    conn.execute(
        insert(order_events).values(
            submission_id=submission_id,
            kind=kind,
            detail={"quantity": observed_qty, "observed_qty": payload.get("observed_qty")},
            at=now,
        )
    )
    out = {"action": "SKIPPED_UUID", "drift": None}
    if outcome == "confirmed":
        out["reconciled"] = True
    elif outcome == "expired":
        out["expired"] = True
    else:
        out["observed"] = True
    return out


def _canon_json(value):
    """Normalize a JSON-shaped value for body equality.

    Numbers become Decimals so int/float/Decimal round-trips through jsonb
    compare equal; dicts are key-sorted by construction of dict comparison.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)).normalize()
    if isinstance(value, dict):
        return {k: _canon_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canon_json(v) for v in value]
    return value


def _norm_expiry(value):
    """Dates and 'YYYYMMDD'/'YYYY-MM-DD' strings compare as ISO text."""
    if value is None:
        return None
    if isinstance(value, str):
        digits = value.replace("-", "")
        if len(digits) == 8 and digits.isdigit():
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        return value
    return value.isoformat()


def _request_body_matches(row, request: RequestRow, legs) -> bool:
    """True iff the stored row's request columns equal the incoming body.

    Compared fields are the reservation payload: the RequestRow columns plus
    `legs`. `user_id` is deliberately absent — idempotency is scoped to the
    account, not the caller identity.
    """

    def _num(v):
        return None if v is None else Decimal(str(v)).normalize()

    return (
        row.ticker == request.ticker
        and row.security_type == request.security_type
        and row.action == request.action
        and int(row.quantity) == int(request.quantity)
        and _norm_expiry(row.expiry) == _norm_expiry(request.expiry)
        and _num(row.strike) == _num(request.strike)
        and row.right == request.right
        and (None if row.multiplier is None else int(row.multiplier))
        == (None if request.multiplier is None else int(request.multiplier))
        and (None if row.con_id is None else int(row.con_id))
        == (None if request.con_id is None else int(request.con_id))
        and _num(row.limit_price) == _num(request.limit_price)
        and _canon_json(row.legs) == _canon_json(legs)
    )


@dataclass
class SubmissionRow:
    submission_id: str
    user_id: str
    ticker: str
    state: str
    ib_order_id: str | None
    perm_id: str | None
    placing_client_id: int | None
    reason_code: str | None
    quantity: int
    action: str
    security_type: str
    right: str | None
    expiry: str | None


# ── Core functions (now Postgres-backed) ──


def reserve_attempt(
    user_id: str,
    client_attempt_id: str,
    request: RequestRow,
    *,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
    legs: list | dict | None = None,
    override_audit: dict | None = None,
) -> ReservationOutcome:
    """Atomically reserve a submission slot keyed by
    (broker, account_env, broker_account, client_attempt_id).

    `legs` (optional) is the durable effective-leg payload for Gate-4
    coverage — a list of effective-leg dicts (action, sym, sec, ratio,
    right, exp, mult, tc, con_id, guaranteed) or a JSON object carrying
    them; stored verbatim in `order_submissions.legs`.

    Conflict on the account-scoped key replays idempotently: same request
    body returns the existing row (`duplicate`/`terminal`); a different
    body returns `body_mismatch` with reason_code ATTEMPT_BODY_MISMATCH
    (the API maps it to 409). Caller identity (`user_id`) is still written
    but no longer participates in the key.

    `override_audit` (when provided) writes a regime_overrides row in
    the same transaction as the submission reservation. The deferred
    composite FK on regime_overrides means both rows commit atomically
    or both roll back. Schema: dict with keys
    `route, vcg_tier, cri_tier, binding_side, block_reason, user_reason,
    order_payload`.
    """
    ensure_writable()
    sid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    engine = get_sync_engine()
    with engine.begin() as conn:
        stmt = pg_insert(order_submissions).values(
            submission_id=sid,
            user_id=user_id,
            client_attempt_id=client_attempt_id,
            ticker=request.ticker,
            security_type=request.security_type,
            action=request.action,
            quantity=request.quantity,
            expiry=request.expiry,
            strike=request.strike,
            right=request.right,
            multiplier=request.multiplier,
            con_id=request.con_id,
            limit_price=request.limit_price,
            state="PENDING",
            submitted_at=now,
            updated_at=now,
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
            legs=legs,
        )
        # EXPAND keeps both attempt keys live (legacy user-scoped +
        # account-scoped). Postgres speculative insertion only honors the
        # named arbiter index — naming one key still raises UniqueViolation
        # when a concurrent insert collides on the other. Bare DO NOTHING
        # makes every unique index an arbiter; the scope/attempt lookup
        # below then classifies which conflict actually happened.
        stmt = stmt.on_conflict_do_nothing()
        stmt = stmt.returning(order_submissions.c.submission_id)
        inserted = conn.execute(stmt).first()

        if inserted is not None:
            if override_audit is not None:
                conn.execute(
                    insert(regime_overrides).values(
                        user_id=user_id,
                        account_env=account_env,
                        broker=broker,
                        broker_account=broker_account,
                        submission_id=sid,
                        client_attempt_id=client_attempt_id,
                        route=override_audit["route"],
                        vcg_tier=override_audit.get("vcg_tier"),
                        cri_tier=override_audit.get("cri_tier"),
                        binding_side=override_audit["binding_side"],
                        block_reason=override_audit["block_reason"],
                        user_reason=override_audit["user_reason"],
                        order_payload=override_audit["order_payload"],
                    )
                )
            return ReservationOutcome(
                status="winner",
                submission_id=sid,
                state="PENDING",
                duplicate_of=None,
                reason_code=None,
            )

        row = conn.execute(
            select(order_submissions).where(
                order_submissions.c.broker == broker,
                order_submissions.c.account_env == account_env,
                order_submissions.c.broker_account == broker_account,
                order_submissions.c.client_attempt_id == client_attempt_id,
            )
        ).first()
        if row is None:
            # The conflict resolved on a key this lookup cannot see — e.g.
            # a submission_id collision, or the conflicting row vanished
            # between the insert and this read. Never a second insert and
            # never an exception: fail closed as a non-winner; the API maps
            # body_mismatch / ATTEMPT_BODY_MISMATCH to 409.
            return ReservationOutcome(
                status="body_mismatch",
                submission_id=None,
                state=None,
                duplicate_of=None,
                reason_code=ATTEMPT_BODY_MISMATCH,
            )
        if not _request_body_matches(row, request, legs):
            return ReservationOutcome(
                status="body_mismatch",
                submission_id=row.submission_id,
                state=row.state,
                duplicate_of=None,
                reason_code=ATTEMPT_BODY_MISMATCH,
            )
        existing_sid, state, ib_order_id, reason_code = (
            row.submission_id,
            row.state,
            row.ib_order_id,
            row.reason_code,
        )
        if state in _TERMINAL_STATES:
            return ReservationOutcome(
                status="terminal",
                submission_id=existing_sid,
                state=state,
                duplicate_of=None,
                reason_code=reason_code,
            )
        return ReservationOutcome(
            status="duplicate",
            submission_id=existing_sid,
            state=state,
            duplicate_of=ib_order_id,
            reason_code=None,
        )


def apply_modify(
    order_id: str,
    sequence: int,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> dict:
    """Monotonic modify_sequence gate keyed by ib_order_id.

    `ib_order_id` is unique only within an IB account session, so when scope
    is provided we filter on it to avoid colliding with rows from a different
    paper/live account.
    """
    ensure_writable()
    scope_conds: list = []
    if broker is not None:
        scope_conds.append(order_submissions.c.broker == broker)
    if account_env is not None:
        scope_conds.append(order_submissions.c.account_env == account_env)
    if broker_account is not None:
        scope_conds.append(order_submissions.c.broker_account == broker_account)
    engine = get_sync_engine()
    with engine.begin() as conn:
        result = conn.execute(
            update(order_submissions)
            .where(
                order_submissions.c.ib_order_id == str(order_id),
                order_submissions.c.modify_sequence < sequence,
                *scope_conds,
            )
            .values(
                modify_sequence=sequence,
                updated_at=datetime.now(timezone.utc),
            )
            .returning(order_submissions.c.modify_sequence)
        )
        updated = result.first()
        if updated is not None:
            return {"applied": True, "current_sequence": int(updated[0])}

        row = conn.execute(
            select(order_submissions.c.modify_sequence).where(
                order_submissions.c.ib_order_id == str(order_id),
                *scope_conds,
            )
        ).first()
        if row is None:
            return {"applied": False, "current_sequence": -1}
        return {"applied": False, "current_sequence": int(row[0])}


def register_from_snapshot(
    perm_id: str,
    ib_order_id: str,
    ticker: str,
    security_type: str,
    action: str,
    quantity: int,
    limit_price: float,
    multiplier: int = 1,
    user_id: str = "snapshot",
    *,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
    strike: float | None = None,
    right: str | None = None,
    expiry: str | None = None,
    con_id: int | None = None,
    tif: str = "DAY",
    legs=_UNSET,
    observed_at: datetime | None = None,
) -> dict:
    """Mirror an IB-side open order into `xenon.order_submissions`.

    Three branches, one transaction:

    1. **No existing row** — INSERT a `snapshot-<perm_id>` row in state
       `WORKING`. Returns ``{"action": "INSERTED", "drift": None}``.
    2. **Existing snapshot-* row** — compare ``limit_price`` and ``quantity``
       against incoming. On drift, UPDATE the row + write an
       ``IB_MIRROR_UPDATE`` order_event recording before/after. On no drift,
       no-op. Returns ``{"action": "UPDATED"|"NOOP", "drift": {...}|None}``.
    3. **Existing UUID-authored row** — keep dedupe semantics: this is a
       Xenon-placed order with `modify_sequence` invariants we won't
       silently violate from a TWS-side change. Returns
       ``{"action": "SKIPPED_UUID", "drift": None}``.

    Branch (2) closes the bug where TWS price/qty edits never reached our
    DB: the previous insert-only behavior left snapshot rows frozen at
    their first-seen values.

    `observed_at` is the instant the caller STARTED the IB read that produced
    this observation (PR-03b round 4). An observation is *fresh* only when
    it began after the row's last local write (`updated_at`). Freshness is
    required to release reservation state (confirm or expire a pending
    amend). A stale observation applies only conservative changes (it can
    raise demand, never lower or release it). `observed_at=None` (legacy
    callers) keeps the old drift behavior but never touches a pending amend.
    """
    ensure_writable()
    submission_id = f"snapshot-{perm_id}"
    client_attempt_id = f"snapshot-{perm_id}"
    now = datetime.now(timezone.utc)
    engine = get_sync_engine()
    with engine.begin() as conn:
        # Reservation state (legs / pending amend / quantity) is written
        # under the same account-scope lock as the gate's check+insert.
        _lock_gate4_scope(conn, broker, account_env, broker_account)
        existing = conn.execute(
            select(
                order_submissions.c.submission_id,
                order_submissions.c.limit_price,
                order_submissions.c.quantity,
                order_submissions.c.state,
                order_submissions.c.legs,
                order_submissions.c.updated_at,
            ).where(
                order_submissions.c.perm_id == str(perm_id),
                order_submissions.c.broker == broker,
                order_submissions.c.account_env == account_env,
                order_submissions.c.broker_account == broker_account,
            )
        ).first()

        # Pull existing tif separately so we don't break the positional unpack
        # of older callers if this query expands further.
        existing_tif: str | None = None
        if existing is not None:
            existing_tif_row = conn.execute(
                select(order_submissions.c.tif).where(order_submissions.c.submission_id == existing[0])
            ).first()
            existing_tif = str(existing_tif_row[0]) if existing_tif_row and existing_tif_row[0] is not None else None

        if existing is not None:
            existing_submission_id = existing[0]
            existing_limit_price = float(existing[1]) if existing[1] is not None else None
            existing_quantity = int(existing[2]) if existing[2] is not None else None
            existing_state = str(existing[3]) if existing[3] is not None else ""
            existing_legs = existing[4]
            fresh = is_fresh_observation(observed_at, existing[5])
            # Legacy callers (observed_at=None) keep the pre-round-4 drift
            # semantics for price/qty/tif; a read that provably began before
            # the row's last local write applies only conservative changes.
            drift_ok = fresh or observed_at is None
            observed_qty = int(quantity)

            if not existing_submission_id.startswith("snapshot-"):
                return _mirror_uuid_row(
                    conn,
                    existing_submission_id,
                    existing_state=existing_state,
                    existing_quantity=existing_quantity or 0,
                    existing_legs=existing_legs,
                    observed_qty=observed_qty,
                    observed_legs=legs,
                    observed_at=observed_at,
                    fresh=fresh,
                    now=now,
                )

            # IB still reports this order as open while we have it in a
            # terminal state — restore it. Operator error, race in the
            # cancel route, or out-of-band TWS action can drive this. A read
            # that began before the terminal write proves nothing.
            if existing_state in {"CANCELLED", "FILLED", "REJECTED", "FAILED", "UNKNOWN"}:
                if not drift_ok:
                    return {"action": "NOOP", "drift": None, "stale": True}
                resurrect_values = dict(
                    state="WORKING",
                    reason_code=None,
                    limit_price=float(limit_price),
                    quantity=int(quantity),
                    tif=tif,
                    updated_at=now,
                )
                if legs is not _UNSET:
                    resurrect_values["legs"] = merge_observed_legs(existing_legs, legs)
                conn.execute(
                    update(order_submissions)
                    .where(order_submissions.c.submission_id == existing_submission_id)
                    .values(**resurrect_values)
                )
                conn.execute(
                    insert(order_events).values(
                        submission_id=existing_submission_id,
                        kind="IB_MIRROR_RESURRECT",
                        detail={
                            "from_state": existing_state,
                            "to_state": "WORKING",
                            "reason": "ib_still_reports_open",
                        },
                        at=now,
                    )
                )
                return {"action": "RESURRECTED", "drift": None}

            # Round to 4dp before comparing — the DB column is numeric(12,4)
            # and incoming `limit_price` is a raw float that can carry round-trip
            # noise (e.g. 1.4500000001). Without rounding, every poll tick would
            # spuriously detect drift and emit a redundant IB_MIRROR_UPDATE event.
            new_price = round(float(limit_price), 4)
            stored_price = round(existing_limit_price, 4) if existing_limit_price is not None else None
            new_quantity = observed_qty if drift_ok else max(existing_quantity or 0, observed_qty)
            drift: dict = {}
            if drift_ok and stored_price != new_price:
                drift["limit_price"] = {"from": stored_price, "to": new_price}
            if existing_quantity != new_quantity:
                drift["quantity"] = {
                    "from": existing_quantity,
                    "to": new_quantity,
                }
            if drift_ok and existing_tif is not None and existing_tif != tif:
                drift["tif"] = {"from": existing_tif, "to": tif}
            merged_legs = _UNSET
            if legs is not _UNSET or existing_legs is not None:
                merged_legs = (
                    merge_observed_legs(existing_legs, legs)
                    if legs is not _UNSET
                    else normalize_legs_payload(existing_legs)
                )
                merged_legs, outcome = _reconcile_pending(
                    merged_legs, observed_qty, observed_at=observed_at, fresh=fresh
                )
                if outcome is not None:
                    drift["pending_amend_qty"] = {outcome: observed_qty}
                if existing_legs is None or merged_legs != normalize_legs_payload(existing_legs):
                    drift.setdefault("legs", "updated")
                elif legs is _UNSET:
                    merged_legs = _UNSET
            if not drift:
                return {"action": "NOOP", "drift": None}

            drift_values = dict(
                quantity=new_quantity,
                updated_at=now,
            )
            if drift_ok:
                drift_values["limit_price"] = float(limit_price)
                drift_values["tif"] = tif
            if merged_legs is not _UNSET:
                drift_values["legs"] = merged_legs
            conn.execute(
                update(order_submissions)
                .where(order_submissions.c.submission_id == existing_submission_id)
                .values(**drift_values)
            )
            conn.execute(
                insert(order_events).values(
                    submission_id=existing_submission_id,
                    kind="IB_MIRROR_UPDATE",
                    detail=drift,
                    at=now,
                )
            )
            return {"action": "UPDATED", "drift": drift}

        result = conn.execute(
            pg_insert(order_submissions)
            .values(
                submission_id=submission_id,
                user_id=user_id,
                client_attempt_id=client_attempt_id,
                ticker=ticker,
                security_type=security_type,
                action=action,
                quantity=quantity,
                multiplier=multiplier,
                ib_order_id=str(ib_order_id),
                perm_id=str(perm_id),
                limit_price=limit_price,
                state="WORKING",
                submitted_at=now,
                updated_at=now,
                modify_sequence=0,
                broker=broker,
                account_env=account_env,
                broker_account=broker_account,
                strike=strike,
                right=right,
                expiry=expiry,
                con_id=con_id,
                tif=tif,
                legs=None if legs is _UNSET else legs,
            )
            # client_attempt_id is non-null here (snapshot-<perm_id>), so a
            # racing sibling conflicts on the attempt keys too — a PK-only
            # arbiter would raise UniqueViolation instead of no-op'ing.
            .on_conflict_do_nothing()
            .returning(order_submissions.c.submission_id)
        )
        if result.first() is None:
            # Race: another writer slipped in between the SELECT and INSERT.
            # Fine to no-op — the next poll tick will see and reconcile.
            return {"action": "NOOP", "drift": None}
        return {"action": "INSERTED", "drift": None}


def apply_modify_by_perm_id(
    perm_id: str,
    sequence: int,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> dict:
    """Variant of apply_modify keyed by perm_id.

    Resolves ib_order_id from perm_id then delegates to apply_modify,
    both within the same engine session to avoid TOCTOU. Scope filters
    isolate paper/live rows that may share a perm_id namespace.
    """
    ensure_writable()
    scope_conds: list = []
    if broker is not None:
        scope_conds.append(order_submissions.c.broker == broker)
    if account_env is not None:
        scope_conds.append(order_submissions.c.account_env == account_env)
    if broker_account is not None:
        scope_conds.append(order_submissions.c.broker_account == broker_account)
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(
            select(order_submissions.c.ib_order_id).where(
                order_submissions.c.perm_id == str(perm_id),
                *scope_conds,
            )
        ).first()
        if row is None or not row[0]:
            return {"applied": False, "current_sequence": -1}
        ib_order_id = str(row[0])
        result = conn.execute(
            update(order_submissions)
            .where(
                order_submissions.c.ib_order_id == ib_order_id,
                order_submissions.c.modify_sequence < sequence,
                *scope_conds,
            )
            .values(modify_sequence=sequence, updated_at=datetime.now(timezone.utc))
            .returning(order_submissions.c.modify_sequence)
        )
        updated = result.first()
        if updated is not None:
            return {"applied": True, "current_sequence": int(updated[0])}
        cur = conn.execute(
            select(order_submissions.c.modify_sequence).where(
                order_submissions.c.ib_order_id == ib_order_id,
                *scope_conds,
            )
        ).first()
        return {"applied": False, "current_sequence": int(cur[0]) if cur else -1}


def mark_submitted(
    *,
    submission_id: str,
    ib_order_id: str,
    perm_id: str | None,
    placing_client_id: int | None,
) -> None:
    ensure_writable()

    def _int_or_none(value: str | None) -> int | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    now = datetime.now(timezone.utc)
    perm_id_int = _int_or_none(perm_id)
    ib_order_id_int = _int_or_none(ib_order_id)
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            update(order_submissions)
            .where(order_submissions.c.submission_id == submission_id)
            .values(
                ib_order_id=str(ib_order_id),
                perm_id=str(perm_id) if perm_id is not None else None,
                placing_client_id=placing_client_id,
                state="WORKING",
                updated_at=now,
            )
        )
        conn.execute(
            update(regime_overrides)
            .where(regime_overrides.c.submission_id == submission_id)
            .values(
                perm_id=perm_id_int,
                ib_order_id=ib_order_id_int,
            )
        )


def mark_terminal(
    *,
    submission_id: str,
    state: Literal["FILLED", "REJECTED", "CANCELLED", "FAILED", "PARTIALLY_FILLED"],
    reason_code: str | None,
    filled_qty: int | None,
    avg_fill_price: Decimal | None,
    expected_states: tuple[str, ...] | None = None,
) -> int:
    """Transition a submission to a terminal state. Returns the affected
    rowcount.

    ``expected_states`` is an optional optimistic-concurrency guard: when
    provided, the UPDATE only applies if the row is still in one of those
    states. A concurrent transition (e.g. a user cancel or a fill event
    landing between a caller's read and this write) leaves the row untouched
    and returns 0, so the caller can avoid clobbering a newer terminal state.
    """
    ensure_writable()
    now = datetime.now(timezone.utc)
    engine = get_sync_engine()
    stmt = update(order_submissions).where(order_submissions.c.submission_id == submission_id)
    if expected_states is not None:
        stmt = stmt.where(order_submissions.c.state.in_(expected_states))
    values = dict(state=state, reason_code=reason_code, updated_at=now)
    if filled_qty is not None:
        # filled_qty=None keeps the stored fill state: a cancel releases only
        # the unfilled remainder and must never erase executed quantity.
        values.update(filled_qty=filled_qty, avg_fill_price=avg_fill_price)
    with engine.begin() as conn:
        result = conn.execute(stmt.values(**values))
    return result.rowcount


def record_fill(
    *,
    exec_id: str,
    submission_id: str | None,
    combo_attempt_id: str | None = None,
    perm_id: str | None,
    ib_order_id: str | None = None,
    con_id: int | None,
    ticker: str,
    side: str,
    qty: Decimal,
    price: Decimal,
    commission: Decimal = Decimal(0),
    filled_at: datetime,
    metadata: dict | None = None,
    broker: str,
    account_env: str,
    broker_account: str,
) -> bool:
    """Idempotently record one execution-grain fill and emit fill.recorded."""
    ensure_writable()
    if account_env == "legacy_unknown" or broker_account == "legacy_unknown":
        raise ValueError("record_fill requires explicit account scope")

    engine = get_sync_engine()
    with engine.begin() as conn:
        stmt = (
            pg_insert(order_fills)
            .values(
                exec_id=exec_id,
                submission_id=submission_id,
                combo_attempt_id=combo_attempt_id,
                perm_id=perm_id,
                ib_order_id=str(ib_order_id) if ib_order_id is not None else None,
                con_id=con_id,
                ticker=ticker,
                side=side,
                qty=qty,
                price=price,
                commission=commission,
                filled_at=filled_at,
                metadata=metadata,
                broker=broker,
                account_env=account_env,
                broker_account=broker_account,
            )
            .on_conflict_do_nothing(index_elements=["exec_id"])
            .returning(order_fills.c.exec_id)
        )
        inserted = conn.execute(stmt).first()
        if inserted is None:
            return False

        emit_outbox_in_txn(
            conn,
            channel=CHANNEL_FILL_RECORDED,
            source="record_fill",
            payload={
                "exec_id": exec_id,
                "submission_id": submission_id,
                "combo_attempt_id": combo_attempt_id,
                "perm_id": perm_id,
                "ib_order_id": str(ib_order_id) if ib_order_id is not None else None,
                "con_id": con_id,
                "ticker": ticker,
                "side": side,
                "qty": str(qty),
                "price": str(price),
                "commission": str(commission),
                "filled_at": filled_at.isoformat(),
                "metadata": metadata,
                "broker": broker,
                "account_env": account_env,
                "broker_account": broker_account,
            },
        )
        return True


def update_fill_commission(
    *,
    exec_id: str,
    commission: Decimal,
    realized_pnl: Decimal | None,
) -> bool:
    """Apply a late-arriving CommissionReport to an existing order_fills row.

    Execution-grain fill fields remain insert-only. IB delivers commission
    and realizedPNL on a separate message, so this writer only patches those
    post-fill report fields and emits an outbox event when anything changed.
    """
    ensure_writable()
    incoming_commission = Decimal(str(commission))
    realized_pnl_text = str(realized_pnl) if realized_pnl is not None else None

    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(
            select(
                order_fills.c.exec_id,
                order_fills.c.submission_id,
                order_fills.c.combo_attempt_id,
                order_fills.c.commission,
                order_fills.c.metadata,
            )
            .where(order_fills.c.exec_id == exec_id)
            .with_for_update()
        ).first()
        if row is None:
            return False

        fill = row._mapping
        stored_commission = Decimal(fill["commission"] or 0)
        metadata = fill["metadata"] if isinstance(fill["metadata"], dict) else {}
        existing_realized_pnl = metadata.get("realized_pnl")
        existing_realized_pnl_text = str(existing_realized_pnl) if existing_realized_pnl is not None else None

        update_commission = incoming_commission > 0 and stored_commission == 0
        update_realized_pnl = realized_pnl is not None and existing_realized_pnl_text != realized_pnl_text
        if not update_commission and not update_realized_pnl:
            return False

        values = {}
        if update_commission:
            values["commission"] = incoming_commission
        if update_realized_pnl:
            values["metadata"] = func.jsonb_set(
                func.coalesce(order_fills.c.metadata, cast(literal("{}"), JSONB)),
                cast(literal(["realized_pnl"]), ARRAY(Text)),
                cast(literal(json.dumps(realized_pnl_text)), JSONB),
                True,
            )

        conn.execute(update(order_fills).where(order_fills.c.exec_id == exec_id).values(**values))
        emit_outbox_in_txn(
            conn,
            channel=CHANNEL_FILL_COMMISSION_UPDATED,
            source="update_fill_commission",
            payload={
                "exec_id": exec_id,
                "submission_id": fill["submission_id"],
                "combo_attempt_id": fill["combo_attempt_id"],
                "legacy_id": metadata.get("legacy_id"),
                "commission": str(incoming_commission),
                "realized_pnl": realized_pnl_text,
            },
        )
        return True


def record_event(
    submission_id: str,
    kind: str,
    detail: dict,
) -> None:
    ensure_writable()
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_events).values(
                submission_id=submission_id,
                kind=kind,
                detail=detail,
            )
        )


def load_submission_for_modify(
    *,
    order_id: str = "",
    perm_id: str = "",
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
) -> dict | None:
    """Return the scoped submission row for a cancel/modify identity.

    permId wins when supplied — it is globally unique, and the resolved
    row's ``ib_order_id`` is verified against the request's orderId.
    orderId-only requests must resolve to exactly ONE scoped row: zero or
    several rows means the stored owner is ambiguous (two clients can share
    an orderId), so None is returned and the CLI's own ambiguity rule
    decides — never an arbitrary LIMIT 1 owner.

    Used by the cancel/modify routes to assert the recorded placing
    clientId. Scope-filtered so a paper-account modify cannot resolve a
    live row.
    """
    conditions = [
        order_submissions.c.broker == broker,
        order_submissions.c.account_env == account_env,
        order_submissions.c.broker_account == broker_account,
    ]
    if perm_id:
        conditions.append(order_submissions.c.perm_id == str(perm_id))
    elif order_id:
        conditions.append(order_submissions.c.ib_order_id == str(order_id))
    else:
        return None
    engine = get_sync_engine()
    with engine.connect() as conn:
        rows = conn.execute(select(order_submissions).where(*conditions)).all()
    if len(rows) != 1:
        return None
    row = dict(rows[0]._mapping)
    if perm_id and order_id and str(row.get("ib_order_id") or "") != str(order_id):
        return None
    return row


def lookup_submission_id_by_ib_order_id(
    ib_order_id: str,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> str | None:
    if not ib_order_id:
        return None
    conditions = [order_submissions.c.ib_order_id == str(ib_order_id)]
    if broker is not None:
        conditions.append(order_submissions.c.broker == broker)
    if account_env is not None:
        conditions.append(order_submissions.c.account_env == account_env)
    if broker_account is not None:
        conditions.append(order_submissions.c.broker_account == broker_account)
    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(select(order_submissions.c.submission_id).where(*conditions)).first()
    return row[0] if row else None


def lookup_submission_id_by_perm_id(
    perm_id: str,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> str | None:
    if not perm_id:
        return None
    conditions = [order_submissions.c.perm_id == str(perm_id)]
    if broker is not None:
        conditions.append(order_submissions.c.broker == broker)
    if account_env is not None:
        conditions.append(order_submissions.c.account_env == account_env)
    if broker_account is not None:
        conditions.append(order_submissions.c.broker_account == broker_account)
    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(select(order_submissions.c.submission_id).where(*conditions).limit(1)).first()
    return row[0] if row else None


def lookup_by_attempt(
    user_id: str,
    client_attempt_id: str,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> SubmissionRow | None:
    engine = get_sync_engine()
    with engine.connect() as conn:
        conditions = [
            order_submissions.c.user_id == user_id,
            order_submissions.c.client_attempt_id == client_attempt_id,
        ]
        if broker is not None:
            conditions.append(order_submissions.c.broker == broker)
        if account_env is not None:
            conditions.append(order_submissions.c.account_env == account_env)
        if broker_account is not None:
            conditions.append(order_submissions.c.broker_account == broker_account)
        row = conn.execute(
            select(
                order_submissions.c.submission_id,
                order_submissions.c.user_id,
                order_submissions.c.ticker,
                order_submissions.c.state,
                order_submissions.c.ib_order_id,
                order_submissions.c.perm_id,
                order_submissions.c.placing_client_id,
                order_submissions.c.reason_code,
                order_submissions.c.quantity,
                order_submissions.c.action,
                order_submissions.c.security_type,
                order_submissions.c.right,
                order_submissions.c.expiry,
            ).where(*conditions)
        ).first()
    if row is None:
        return None
    vals = list(row)
    if vals[12] is not None and not isinstance(vals[12], str):
        vals[12] = str(vals[12])
    return SubmissionRow(*vals)


from xenon.execution.preflight import WorkingReservations

_ACTIVE_STATES = ("PENDING", "WORKING", "PARTIALLY_FILLED")


def working_reservations_for(
    user_id: str,
    ticker: str,
    *,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> WorkingReservations:
    """Aggregate active working-order quantities for preflight.

    Scope kwargs are critical here: cross-account aggregation produces
    incorrect naked-short / short-call coverage decisions in shared DB.
    """
    scope_conds: list = []
    if broker is not None:
        scope_conds.append(order_submissions.c.broker == broker)
    if account_env is not None:
        scope_conds.append(order_submissions.c.account_env == account_env)
    if broker_account is not None:
        scope_conds.append(order_submissions.c.broker_account == broker_account)
    engine = get_sync_engine()
    with engine.connect() as conn:
        stock_sell = conn.execute(
            select(func.coalesce(func.sum(order_submissions.c.quantity - order_submissions.c.filled_qty), 0)).where(
                order_submissions.c.user_id == user_id,
                order_submissions.c.ticker == ticker,
                order_submissions.c.security_type == "STK",
                order_submissions.c.action == "SELL",
                order_submissions.c.state.in_(_ACTIVE_STATES),
                *scope_conds,
            )
        ).scalar()
        short_call = conn.execute(
            select(func.coalesce(func.sum(order_submissions.c.quantity - order_submissions.c.filled_qty), 0)).where(
                order_submissions.c.user_id == user_id,
                order_submissions.c.ticker == ticker,
                order_submissions.c.security_type == "OPT",
                order_submissions.c.action == "SELL",
                order_submissions.c.right == "C",
                order_submissions.c.state.in_(_ACTIVE_STATES),
                *scope_conds,
            )
        ).scalar()
    return WorkingReservations(
        stock_sell_qty=int(stock_sell),
        short_call_qty=int(short_call),
        short_put_cash_required=Decimal("0"),
        long_call_close_qty_same_exp=0,
    )
