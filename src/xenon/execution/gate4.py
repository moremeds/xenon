"""Gate-4 atomic reservations — coverage check + durable insert in one txn.

PR-03b (tmp/repo-review/spec.md §2.3): `/orders/place` and the quantity leg of
`/orders/modify` must evaluate `xenon.execution.coverage` and write the
reservation row atomically — inside one Postgres transaction serialized by a
deterministic account-scope advisory lock so two processes racing for the
last unit of cover can never both win.

Data sources (all read *inside* the locked transaction):

- holdings  — latest `account_snapshots.payload` for the scope. The sync pass
  stamps `payload["observation"] = {"as_of", "complete", ...}`; as_of is the
  pass-start observation boundary, complete=False when any read in the pass
  (positions / all-account open orders / executions) failed.
- orders    — every `order_submissions` row in scope (any state). Demand =
  fills after `as_of` + outstanding remaining; terminal rows keep
  remaining=0 but still consume via post-snapshot fills.
- fills     — `order_fills` in scope, attributed to orders by submission_id
  (or perm_id fallback). Post-as_of fills that resolve to no in-scope order
  mark the view incomplete — an unattributed fill is demand we cannot
  model, so new risk must fail closed.

Durable reservation payload (`order_submissions.legs` JSONB):

    {"legs": [ {action, sym, sec, ratio, right, exp, mult, tc, strike,
                con_id, und_sec} ... ] | null,
     "guaranteed": bool | null,          # BAG guarantee mode (NonGuaranteed=0)
     "pending_amend_qty": int | null}    # uncertain modify keeps max(old,new)

A pre-PR-03b row stores a bare leg list (or NULL); non-BAG rows without
stored legs are re-synthesized from row columns, unresolved BAG rows fail
closed — `coverage` treats legs=None/() as unclassifiable.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xenon.db.engine import get_sync_engine
from xenon.db.schema import account_snapshots, order_fills, order_submissions, regime_overrides
from xenon.execution import coverage
from xenon.execution.orders_store import (
    ATTEMPT_BODY_MISMATCH,
    RequestRow,
    _canon_json,
    _request_body_matches,
    ensure_writable,
)
from xenon.execution.universe import INDEX_UNIVERSE, UNIVERSE, get_multiplier

TERMINAL_STATES = frozenset({"FILLED", "CANCELLED", "REJECTED", "FAILED"})

# Sentinel: a legs payload that never recorded account provenance vs one
# that recorded it as null — the latter must fail closed, not fall back.
_UNSET = object()

_NETTING_ENV = "XENON_GATE4_NETTING"


def netting_enabled() -> bool:
    """Spec §2.3: guaranteed-BAG netting is off unless explicitly enabled."""
    return os.environ.get(_NETTING_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def scope_lock_key(scope: dict) -> str:
    """Deterministic advisory-lock key — stable across processes (no hash())."""
    return f"gate4:{scope.get('broker') or ''}:{scope.get('account_env') or ''}:{scope.get('broker_account') or ''}"


def _norm_expiry(value: Any) -> str | None:
    """Expiry → YYYYMMDD. Dates, 'YYYY-MM-DD', 'YYYYMMDD' all normalize."""
    if value is None:
        return None
    if isinstance(value, str):
        digits = value.replace("-", "")
        return digits if len(digits) == 8 and digits.isdigit() else None
    return value.strftime("%Y%m%d") if hasattr(value, "strftime") else None


def _und_sec(sym: str) -> str:
    """Underlying sec-type fallback for rows/snapshots that predate sync-side
    stamping. Xenon's option universe is only US equity and cash-settled
    index underlyings, so und_sec ∈ {STK, IND}."""
    return "IND" if sym in INDEX_UNIVERSE else "STK"


def _mult_for(sym: str) -> int:
    return get_multiplier(sym) if sym in UNIVERSE else 100


def server_multiplier(sym: str) -> int:
    """Server-authoritative contract multiplier for the request/durable row."""
    return _mult_for(sym)


def _as_utc(dt: Any) -> datetime | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return None
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _market_open_now() -> bool:
    from zoneinfo import ZoneInfo

    et = datetime.now(tz=ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return False
    return 9 * 60 + 30 <= et.hour * 60 + et.minute <= 16 * 60


def _snapshot_stale(as_of: datetime | None) -> bool:
    """Same thresholds as the legacy route-level check."""
    if as_of is None:
        return False
    market_open = _market_open_now()
    env = "XENON_PORTFOLIO_SNAPSHOT_STALE_S" if market_open else "XENON_PORTFOLIO_SNAPSHOT_STALE_CLOSED_S"
    default = 300 if market_open else 1800
    try:
        threshold = int(os.environ.get(env, str(default)))
    except ValueError:
        threshold = default
    return (datetime.now(timezone.utc) - as_of).total_seconds() > threshold


# ---------------------------------------------------------------------------
# legs payload <-> coverage objects
# ---------------------------------------------------------------------------

_LEG_FIELDS = ("action", "sym", "sec", "ratio", "right", "exp", "mult", "tc", "strike", "con_id", "und_sec")


def _leg_from_dict(d: dict) -> coverage.Leg:
    vals = {k: d.get(k) for k in _LEG_FIELDS}
    return coverage.Leg(**vals)


def serialize_legs(legs: list[coverage.Leg]) -> list[dict]:
    return [{k: getattr(leg, k) for k in _LEG_FIELDS} for leg in legs]


def _decode_legs_payload(payload: Any) -> tuple[tuple[coverage.Leg, ...] | None, bool | None, int | None, Any]:
    """Return (legs, guaranteed, pending_amend_qty, account) from a stored `legs` column.

    Handles the PR-03b dict shape, the legacy bare list, and NULL. A dict
    with `"legs": null` reports legs=None so the caller can still
    re-synthesize non-BAG rows while pending_amend_qty survives. `account`
    is the raw adapter provenance — `None` (key present, value null) means
    "seen without account" which coverage flags; the sentinel `_UNSET`
    distinguishes "payload never recorded provenance" (fall back to the
    row's scope columns).
    """
    if payload is None:
        return None, None, None, _UNSET
    if isinstance(payload, dict):
        raw = payload.get("legs")
        guaranteed = payload.get("guaranteed")
        pending = payload.get("pending_amend_qty")
        account = payload["account"] if "account" in payload else _UNSET
        if raw is None:
            return None, guaranteed, pending, account
        return tuple(_leg_from_dict(x) for x in raw), guaranteed, pending, account
    if isinstance(payload, list):
        legs = tuple(_leg_from_dict(x) for x in payload)
        guaranteed = None
        if payload and all(isinstance(x, dict) for x in payload):
            vals = {x.get("guaranteed") for x in payload}
            vals.discard(None)
            guaranteed = vals.pop() if len(vals) == 1 else None
        return legs, guaranteed, None, _UNSET
    return None, None, None, _UNSET


def _synthesized_leg(m: Any) -> coverage.Leg:
    """Reconstruct a single leg from row columns (non-BAG rows only)."""
    sym = m["ticker"]
    return coverage.Leg(
        action="BUY",
        sym=sym,
        sec=m["security_type"],
        ratio=1,
        right=m["right"],
        exp=_norm_expiry(m["expiry"]),
        mult=_mult_for(sym) if sym in UNIVERSE else (int(m["multiplier"]) if m["multiplier"] is not None else 100),
        tc=sym,
        strike=float(m["strike"]) if m["strike"] is not None else None,
        con_id=int(m["con_id"]) if m["con_id"] is not None else None,
        und_sec=_und_sec(sym) if m["security_type"] == "OPT" else None,
    )


def order_from_row(row: Any, *, filled_total: int, filled_after: int) -> coverage.Order:
    """Build a coverage.Order from an order_submissions row + fill sums."""
    m = row._mapping if hasattr(row, "_mapping") else row
    legs, guaranteed, pending, account = _decode_legs_payload(m["legs"])
    if legs is None and m["security_type"] != "BAG":
        legs = (_synthesized_leg(m),)
    if account is _UNSET:
        # Payload carries no adapter provenance — the row's scope column is
        # the only account claim (place-path rows and pre-PR-03b mirrors).
        account = m["broker_account"]
    state = m["state"] or ""
    terminal = state in TERMINAL_STATES
    remaining = 0 if terminal else max(int(m["quantity"]) - int(filled_total), 0)
    return coverage.Order(
        id=m["submission_id"],
        account=account,
        envelope=m["action"],
        qty=int(m["quantity"]),
        legs=legs,
        state=state,
        filled_after_snapshot=int(filled_after),
        remaining=int(remaining),
        guaranteed=guaranteed,
        # A dead order holds no reservation — pending_amend_qty must not keep
        # reserving max(qty, pending) forever after the row goes terminal.
        pending_amend_qty=None if terminal else pending,
    )


# ---------------------------------------------------------------------------
# Coverage inputs — read inside the locked transaction
# ---------------------------------------------------------------------------


@dataclass
class Gate4Inputs:
    holdings: list[coverage.Holding]
    orders: list[coverage.Order]
    view: coverage.View
    scope_account: str
    as_of: datetime | None
    snapshot_at: datetime | None


def _holdings_from_payload(payload: dict, scope_account: str) -> list[coverage.Holding]:
    out: list[coverage.Holding] = []
    for pos in payload.get("positions") or []:
        sym = pos.get("ticker")
        exp = _norm_expiry(pos.get("expiry"))
        for leg in pos.get("legs") or []:
            leg_type = leg.get("type")
            direction = leg.get("direction")
            contracts = leg.get("contracts")
            if leg_type == "Stock":
                out.append(
                    coverage.Holding(
                        sym=sym,
                        sec="STK",
                        qty=contracts if direction == "LONG" else -contracts,
                        account=scope_account,
                    )
                )
            elif leg_type in ("Call", "Put"):
                out.append(
                    coverage.Holding(
                        sym=sym,
                        sec="OPT",
                        qty=contracts if direction == "LONG" else -contracts,
                        account=scope_account,
                        right="C" if leg_type == "Call" else "P",
                        exp=exp,
                        mult=leg.get("mult") if leg.get("mult") is not None else _mult_for(sym),
                        tc=leg.get("tc") or sym,
                        und_sec=leg.get("und_sec") or _und_sec(sym),
                    )
                )
    return out


def load_inputs(conn, scope: dict) -> Gate4Inputs:
    """Assemble holdings/orders/view for coverage.evaluate on `conn`.

    Must be called under the account-scope advisory lock so the read set is
    stable for the whole check+insert transaction.
    """
    broker = scope["broker"]
    account_env = scope["account_env"]
    broker_account = scope["broker_account"]

    snap = conn.execute(
        select(account_snapshots.c.payload, account_snapshots.c.snapshot_at)
        .where(
            account_snapshots.c.broker == broker,
            account_snapshots.c.account_env == account_env,
            account_snapshots.c.broker_account == broker_account,
        )
        .order_by(account_snapshots.c.snapshot_at.desc())
        .limit(1)
    ).first()

    complete = True
    snapshot_at: datetime | None = None
    as_of: datetime | None = None
    holdings: list[coverage.Holding] = []
    if snap is None or not snap.payload:
        complete = False
    else:
        payload = snap.payload
        snapshot_at = _as_utc(snap.snapshot_at)
        observation = payload.get("observation") if isinstance(payload, dict) else None
        if not isinstance(observation, dict):
            # Pre-PR-03b snapshot: the pass never attested positions+orders+
            # executions completeness — cannot prove the view is whole.
            complete = False
            as_of = snapshot_at
        else:
            as_of = _as_utc(observation.get("as_of")) or snapshot_at
            if observation.get("complete") is not True:
                complete = False
            obs_account = observation.get("account")
            if obs_account is not None and obs_account != broker_account:
                complete = False
        holdings = _holdings_from_payload(payload, broker_account)

    rows = conn.execute(
        select(order_submissions)
        .where(
            order_submissions.c.broker == broker,
            order_submissions.c.account_env == account_env,
            order_submissions.c.broker_account == broker_account,
        )
    ).all()

    # Fills in scope: attribute to orders via submission_id, perm_id fallback.
    fill_rows = conn.execute(
        select(
            order_fills.c.submission_id,
            order_fills.c.perm_id,
            order_fills.c.qty,
            order_fills.c.filled_at,
            order_fills.c.metadata,
        ).where(
            order_fills.c.broker == broker,
            order_fills.c.account_env == account_env,
            order_fills.c.broker_account == broker_account,
        )
    ).all()

    perm_to_sub: dict[str, str] = {}
    sub_sec: dict[str, str] = {}
    stored_filled: dict[str, int] = {}
    for r in rows:
        sub_sec[r.submission_id] = r.security_type
        stored_filled[r.submission_id] = int(r.filled_qty or 0)
        if r.perm_id:
            perm_to_sub[str(r.perm_id)] = r.submission_id

    fill_rows_total: dict[str, Decimal] = {}
    fills_before: dict[str, Decimal] = {}
    for f in fill_rows:
        sub_id = f.submission_id or perm_to_sub.get(str(f.perm_id or ""))
        if sub_id is None or sub_id not in sub_sec:
            # Orphan fill: no in-scope order to attribute it to. If it
            # post-dates the observation boundary its demand is invisible
            # to the evaluation — the view is incomplete.
            if as_of is not None and _as_utc(f.filled_at) is not None and _as_utc(f.filled_at) >= as_of:
                complete = False
            continue
        meta = f.metadata if isinstance(f.metadata, dict) else {}
        fill_sec = meta.get("sec_type")
        if fill_sec is not None and fill_sec != sub_sec[sub_id]:
            # e.g. option-leg executions sharing a BAG order's submission —
            # only the BAG envelope row carries order-unit quantity.
            continue
        if fill_sec is None and sub_sec[sub_id] == "BAG":
            # Legacy fill without sec_type on a BAG order — counting a leg
            # fill as envelope units would double-count, skipping the
            # envelope is conservative (over-reserves).
            continue
        qty = Decimal(str(f.qty))
        fill_rows_total[sub_id] = fill_rows_total.get(sub_id, Decimal(0)) + qty
        if as_of is not None and _as_utc(f.filled_at) is not None and _as_utc(f.filled_at) < as_of:
            fills_before[sub_id] = fills_before.get(sub_id, Decimal(0)) + qty

    orders = []
    for r in rows:
        # filled_total = max(fill rows, the row's denormalized filled_qty).
        # The cache can only be *ahead* of the ledger (a fill row still in
        # flight), never behind — taking the max over-reserves, which is the
        # safe direction on inconsistent data.
        total = max(int(fill_rows_total.get(r.submission_id, 0)), stored_filled[r.submission_id])
        # Fills of unknown time land in filled_after: anything not provably
        # inside the observation window is demand the snapshot can't show.
        after = total - int(fills_before.get(r.submission_id, 0))
        orders.append(order_from_row(r, filled_total=total, filled_after=max(after, 0)))

    view = coverage.View(complete=complete, stale=_snapshot_stale(as_of))
    return Gate4Inputs(
        holdings=holdings,
        orders=orders,
        view=view,
        scope_account=broker_account,
        as_of=as_of,
        snapshot_at=snapshot_at,
    )


# ---------------------------------------------------------------------------
# Candidate construction from /orders/place bodies
# ---------------------------------------------------------------------------


def _non_guaranteed_flag(body: dict) -> int:
    """Explicit NonGuaranteed mode for a BAG order: 0=guaranteed, 1=non-guaranteed.

    Current broker payload always sends NonGuaranteed=1; an explicit
    `nonGuaranteed: 0` in the request body opts into the guaranteed mode.
    Anything unrecognized stays at 1 (the deployed default).
    """
    raw = body.get("nonGuaranteed")
    if raw in (0, "0", False):
        return 0
    return 1


def _opt_leg(sym: str, *, action: str, ratio: int, right, expiry, strike, con_id=None, mult: int | None = None) -> coverage.Leg:
    return coverage.Leg(
        action=action,
        sym=sym,
        sec="OPT",
        ratio=ratio,
        right=right,
        exp=_norm_expiry(expiry),
        mult=mult if mult is not None else _mult_for(sym),
        tc=sym,
        strike=float(strike) if strike is not None else None,
        con_id=int(con_id) if con_id else None,
        und_sec=_und_sec(sym),
    )


def legs_payload_for(*, legs: list[coverage.Leg] | None, guaranteed: bool | None, account=_UNSET) -> dict:
    payload = {
        "legs": serialize_legs(legs) if legs is not None else None,
        "guaranteed": guaranteed,
        "pending_amend_qty": None,
    }
    if account is not _UNSET:
        payload["account"] = account
    return payload


def legs_payload_from_snapshot_order(order: dict) -> dict:
    """Durable legs payload for an IB open-order snapshot row.

    BAG legs come from `contract.comboLegs` (resolved by fetch_open_orders);
    unresolved BAG legs persist `legs: null`, which coverage treats as
    unresolvable — fail closed by construction. `guaranteed` mirrors the
    order's NonGuaranteed tag (0→True, 1→False, missing→None). `account`
    preserves the adapter provenance — a null here is the "seen without
    account" marker coverage flags as missing.
    """
    contract = order.get("contract") or {}
    sec_type = str(contract.get("secType") or "")
    sym = str(contract.get("symbol") or order.get("symbol") or "").upper()
    account = order.get("account")

    if sec_type == "BAG":
        combo = contract.get("comboLegs") or []
        ng = order.get("nonGuaranteed")
        guaranteed = True if ng == 0 else (False if ng == 1 else None)
        if not combo:
            return legs_payload_for(legs=None, guaranteed=guaranteed, account=account)
        legs = [
            coverage.Leg(
                action=str(cl.get("action") or "").upper(),
                sym=str(cl.get("symbol") or sym).upper(),
                sec=str(cl.get("secType") or "OPT"),
                ratio=int(cl.get("ratio") or 1),
                right=cl.get("right"),
                exp=_norm_expiry(cl.get("expiry")),
                mult=_mult_for(sym),
                tc=sym,
                strike=float(cl["strike"]) if cl.get("strike") is not None else None,
                con_id=int(cl["conId"]) if cl.get("conId") is not None else None,
                und_sec=_und_sec(sym) if str(cl.get("secType") or "OPT") == "OPT" else None,
            )
            for cl in combo
        ]
        return legs_payload_for(legs=legs, guaranteed=guaranteed, account=account)

    if sec_type not in ("STK", "OPT"):
        return legs_payload_for(legs=None, guaranteed=None, account=account)

    leg = coverage.Leg(
        action="BUY",
        sym=sym,
        sec=sec_type,
        ratio=1,
        right=contract.get("right"),
        exp=_norm_expiry(contract.get("expiry")),
        mult=_mult_for(sym) if sec_type == "OPT" else None,
        tc=sym,
        strike=float(contract["strike"]) if contract.get("strike") is not None else None,
        con_id=int(contract["conId"]) if contract.get("conId") is not None else None,
        und_sec=_und_sec(sym) if sec_type == "OPT" else None,
    )
    return legs_payload_for(legs=[leg], guaranteed=None, account=account)


def candidate_from_place_body(body: dict, scope_account: str | None) -> tuple[coverage.Order | None, dict]:
    """Build the coverage candidate + durable legs payload for /orders/place.

    Returns (candidate, legs_payload). candidate is None only for the pinned
    single-leg BUY pass-through (spec §3): it creates no demand, so the gate
    must not evaluate it — view/completeness problems must not block a BUY.

    Every other body (all SELLs, every combo regardless of envelope) becomes
    a coverage.Order and is evaluated under the advisory lock.
    """
    sym = str(body.get("symbol", "")).upper()
    action = str(body.get("action", "")).upper()
    # Raw quantity — coverage validates it is a real int (bool/float/str
    # reject); coercing here would launder invalid bodies into valid ints.
    qty = body.get("quantity")

    if body.get("type") == "combo":
        non_guaranteed = _non_guaranteed_flag(body)
        legs = [
            _opt_leg(
                sym,
                action=str(leg.get("action", "")).upper(),
                ratio=int(leg.get("ratio") or 1),
                right=str(leg.get("right") or "").upper() or None,
                expiry=leg.get("expiry"),
                strike=leg.get("strike"),
                con_id=leg.get("con_id") or leg.get("conId"),
            )
            for leg in body.get("legs") or []
        ]
        payload = legs_payload_for(legs=legs, guaranteed=(non_guaranteed == 0))
    else:
        sec = "STK" if body.get("type") == "stock" else "OPT"
        if sec == "STK":
            legs = [coverage.Leg(action="BUY", sym=sym, sec="STK", ratio=1)]
        else:
            legs = [
                _opt_leg(
                    sym,
                    action="BUY",
                    ratio=1,
                    right=str(body.get("right") or "").upper() or None,
                    expiry=body.get("expiry"),
                    strike=body.get("strike"),
                    con_id=body.get("con_id"),
                )
            ]
        payload = legs_payload_for(legs=legs, guaranteed=False)

    if body.get("type") != "combo" and action == "BUY":
        return None, payload

    return (
        coverage.Order(
            id=str(body.get("client_attempt_id") or "candidate"),
            account=scope_account,
            envelope=action,
            qty=qty,
            legs=tuple(legs),
            state="WORKING",
            filled_after_snapshot=0,
            remaining=qty,
            guaranteed=payload["guaranteed"],
        ),
        payload,
    )


# ---------------------------------------------------------------------------
# Atomic evaluate + reserve / modify reservations
# ---------------------------------------------------------------------------


@dataclass
class Gate4Outcome:
    status: Literal["winner", "duplicate", "terminal", "body_mismatch", "rejected"]
    submission_id: str | None = None
    state: str | None = None
    duplicate_of: str | None = None
    reason_code: str | None = None
    reasons: list[str] = field(default_factory=list)
    # Structured reject detail for reason-code mapping (no string parsing).
    uncovered: dict | None = None
    short_stock: dict | None = None
    # Rejected leg-by-leg but accepted under guaranteed-BAG netting — the
    # spec §2.3 leg-out case: any guaranteed all-OPT BAG, open or close
    # (route maps it to COMBO_SPLIT_REQUIRED).
    split_required: bool = False


def _accepted_with_netting(inputs: Gate4Inputs, candidate) -> bool:
    """Would this candidate pass under guaranteed-BAG netting?

    The re-eval only nets when the candidate is guaranteed and every leg is
    OPT, so a non-guaranteed or mixed-security candidate fails identically —
    and a truly naked BAG stays rejected under both modes.
    """
    return coverage.evaluate(
        inputs.holdings,
        inputs.orders,
        candidate,
        inputs.view,
        inputs.scope_account,
        netting_enabled=True,
    ).accept


def _legs_for_body_match(legs_payload):
    """Strip server-side reservation state before request-body comparison.

    `pending_amend_qty` is written by the modify path, not the client — a
    replayed identical body must still dedupe after an amend was pending.
    """
    if isinstance(legs_payload, dict):
        return {k: v for k, v in legs_payload.items() if k != "pending_amend_qty"}
    return legs_payload


def _classify_existing(row, request: RequestRow, legs_payload) -> Gate4Outcome:
    m = row._mapping if hasattr(row, "_mapping") else row
    body_legs = _legs_for_body_match(m["legs"])
    if not _request_body_matches(_RowView(m, legs=body_legs), request, _legs_for_body_match(legs_payload)):
        return Gate4Outcome(
            status="body_mismatch",
            submission_id=row.submission_id,
            state=row.state,
            reason_code=ATTEMPT_BODY_MISMATCH,
        )
    if row.state in {"REJECTED", "CANCELLED", "FAILED"}:
        return Gate4Outcome(
            status="terminal",
            submission_id=row.submission_id,
            state=row.state,
            reason_code=row.reason_code,
        )
    return Gate4Outcome(
        status="duplicate",
        submission_id=row.submission_id,
        state=row.state,
        duplicate_of=row.ib_order_id,
    )


class _RowView:
    """Attribute shim over a row mapping so _request_body_matches can read a
    sanitized `legs` value without mutating the stored row."""

    def __init__(self, mapping, **overrides):
        self._m = mapping
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return self._m[name]


def evaluate_and_reserve(
    conn,
    user_id: str,
    client_attempt_id: str,
    request: RequestRow,
    *,
    candidate: coverage.Order | None,
    legs_payload,
    scope: dict,
    submission_id: str,
    override_audit: dict | None = None,
) -> Gate4Outcome:
    """Dedupe → coverage.evaluate → insert — inside the caller's locked txn.

    `conn` must already hold `pg_advisory_xact_lock(scope_lock_key(scope))`.
    candidate=None skips evaluation (pinned single-leg BUY pass-through);
    the reservation still serializes under the lock.
    """
    existing = conn.execute(
        select(order_submissions).where(
            order_submissions.c.broker == scope["broker"],
            order_submissions.c.account_env == scope["account_env"],
            order_submissions.c.broker_account == scope["broker_account"],
            order_submissions.c.client_attempt_id == client_attempt_id,
        )
    ).first()
    if existing is not None:
        return _classify_existing(existing, request, legs_payload)

    if candidate is not None:
        inputs = load_inputs(conn, scope)
        decision = coverage.evaluate(
            inputs.holdings,
            inputs.orders,
            candidate,
            inputs.view,
            inputs.scope_account,
            netting_enabled=netting_enabled(),
        )
        if not decision.accept:
            return Gate4Outcome(
                status="rejected",
                reasons=list(decision.reasons),
                uncovered=dict(decision.uncovered),
                short_stock=dict(decision.short_stock),
                split_required=(
                    not netting_enabled()
                    and _accepted_with_netting(inputs, candidate)
                ),
            )

    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(order_submissions)
        .values(
            submission_id=submission_id,
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
            broker=scope["broker"],
            account_env=scope["account_env"],
            broker_account=scope["broker_account"],
            legs=legs_payload,
        )
        .on_conflict_do_nothing()
        .returning(order_submissions.c.submission_id)
    )
    inserted = conn.execute(stmt).first()
    if inserted is None:
        # Conflict landed on a key our scope/attempt lookup cannot see —
        # never a second insert, never an exception (same contract as
        # orders_store.reserve_attempt).
        row = conn.execute(
            select(order_submissions).where(
                order_submissions.c.broker == scope["broker"],
                order_submissions.c.account_env == scope["account_env"],
                order_submissions.c.broker_account == scope["broker_account"],
                order_submissions.c.client_attempt_id == client_attempt_id,
            )
        ).first()
        if row is None:
            return Gate4Outcome(status="body_mismatch", reason_code=ATTEMPT_BODY_MISMATCH)
        return _classify_existing(row, request, legs_payload)

    if override_audit is not None:
        conn.execute(
            insert(regime_overrides).values(
                user_id=user_id,
                account_env=scope["account_env"],
                broker=scope["broker"],
                broker_account=scope["broker_account"],
                submission_id=submission_id,
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
    return Gate4Outcome(status="winner", submission_id=submission_id, state="PENDING")


def reserve_with_coverage(
    user_id: str,
    client_attempt_id: str,
    request: RequestRow,
    *,
    candidate: coverage.Order | None,
    legs_payload,
    scope: dict,
    override_audit: dict | None = None,
) -> Gate4Outcome:
    """Place-side entry: advisory lock + evaluate_and_reserve in one txn."""
    ensure_writable()
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": scope_lock_key(scope)},
        )
        return evaluate_and_reserve(
            conn,
            user_id,
            client_attempt_id,
            request,
            candidate=candidate,
            legs_payload=legs_payload,
            scope=scope,
            submission_id=str(uuid.uuid4()),
            override_audit=override_audit,
        )


@dataclass
class ModifyReservationOutcome:
    accept: bool
    reasons: list[str] = field(default_factory=list)
    submission_id: str | None = None
    uncovered: dict | None = None
    short_stock: dict | None = None
    split_required: bool = False


def _set_pending_amend(conn, submission_id: str, value: int | None) -> None:
    row = conn.execute(
        select(order_submissions.c.legs).where(order_submissions.c.submission_id == submission_id)
    ).first()
    payload = row[0] if row and isinstance(row[0], dict) else {}
    payload = dict(payload)
    payload.setdefault("legs", None)
    payload.setdefault("guaranteed", None)
    payload["pending_amend_qty"] = value
    conn.execute(
        update(order_submissions)
        .where(order_submissions.c.submission_id == submission_id)
        .values(legs=payload, updated_at=datetime.now(timezone.utc))
    )


def begin_modify(submission_id: str, new_qty: int, scope: dict) -> ModifyReservationOutcome:
    """Gate a quantity modify: coverage says yes → persist pending_amend_qty.

    Lock + view + evaluate + pending write in one transaction. On accept the
    reservation keeps max(old,new)-filled until settle_modify resolves the
    broker outcome; a lost acknowledgement leaves pending_amend_qty in place
    so the reservation never under-counts.
    """
    ensure_writable()
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": scope_lock_key(scope)},
        )
        inputs = load_inputs(conn, scope)
        modify_candidate = coverage.Modify(order_id=submission_id, new_qty=new_qty)
        decision = coverage.evaluate(
            inputs.holdings,
            inputs.orders,
            modify_candidate,
            inputs.view,
            inputs.scope_account,
            netting_enabled=netting_enabled(),
        )
        if not decision.accept:
            return ModifyReservationOutcome(
                accept=False,
                reasons=list(decision.reasons),
                uncovered=dict(decision.uncovered),
                short_stock=dict(decision.short_stock),
                split_required=(
                    not netting_enabled()
                    and _accepted_with_netting(inputs, modify_candidate)
                ),
            )
        _set_pending_amend(conn, submission_id, int(new_qty))
        return ModifyReservationOutcome(accept=True, submission_id=submission_id)


def settle_modify(submission_id: str, outcome: Literal["confirmed", "rejected", "uncertain"], new_qty: int | None = None) -> None:
    """Resolve a pending modify reservation against the broker result.

    confirmed → row quantity becomes new_qty, pending clears.
    rejected  → broker state provably unchanged, pending clears.
    uncertain → acknowledgement lost: keep pending so coverage reserves
    max(old,new)-filled until the next sync/modify settles it.
    """
    ensure_writable()
    engine = get_sync_engine()
    with engine.begin() as conn:
        if outcome == "confirmed" and new_qty is not None:
            conn.execute(
                update(order_submissions)
                .where(order_submissions.c.submission_id == submission_id)
                .values(quantity=int(new_qty), updated_at=datetime.now(timezone.utc))
            )
        if outcome != "uncertain":
            _set_pending_amend(conn, submission_id, None)


def persisted_guarantee(row: Any) -> bool | None:
    """Stored BAG guarantee mode from a submission row's legs payload."""
    _, guaranteed, _, _ = _decode_legs_payload(row["legs"] if isinstance(row, dict) else row.legs)
    return guaranteed
