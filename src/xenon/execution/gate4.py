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
  fills after `as_of - FILL_LAG_S` + outstanding remaining; terminal rows keep
  remaining=0 but still consume via post-snapshot fills.
- fills     — `order_fills` in scope, attributed to orders by submission_id
  (or perm_id fallback). Fills after `as_of - FILL_LAG_S` that resolve to no in-scope order
  mark the view incomplete — an unattributed fill is demand we cannot
  model, so new risk must fail closed.

Durable reservation payload (`order_submissions.legs` JSONB):

    {"legs": [ {action, sym, sec, ratio, right, exp, mult, tc, strike,
                con_id, und_sec} ... ] | null,
     "guaranteed": bool | null,          # BAG guarantee mode (NonGuaranteed=0)
     "account": str | null,              # adapter provenance (mirrored rows)
     "pending_amend_qty": int | null,    # uncertain modify keeps max(old,new)
     "pending_amend_id": str | null}     # amend generation in flight

A pre-PR-03b row stores a bare leg list (or NULL); non-BAG rows without
stored legs are re-synthesized from row columns, unresolved BAG rows fail
closed — `coverage` treats legs=None/() as unclassifiable.

Option metadata (tradingClass, multiplier, underlying sec type) comes only
from IB contract qualification (`contract_meta`). Nothing here derives it
from the symbol or a universe default: a leg without it fails closed.

Liveness (round 3): only rows that can still owe demand enter the
evaluation — non-terminal rows and rows with fills at/after `as_of - FILL_LAG_S`.
Quantity-0 rows and pure-BUY rows hold no cover demand and are ignored. A row
that cannot be classified (unresolved BAG legs, unknown metadata, invalid
quantity fields) blocks only candidates on its own symbol: cover is
per-symbol, so it cannot change another symbol's result.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import Numeric, case, func, insert, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xenon.db.engine import get_sync_engine
from xenon.db.schema import (
    account_snapshots,
    order_events,
    order_fills,
    order_submissions,
    regime_overrides,
    service_health,
)
from xenon.execution import coverage, orders_store
from xenon.execution.orders_store import (
    ATTEMPT_BODY_MISMATCH,
    RequestRow,
    _canon_json,
    _request_body_matches,
    ensure_writable,
)
from xenon.execution.universe import UNIVERSE, get_multiplier

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
    return orders_store.gate4_lock_key(scope.get("broker"), scope.get("account_env"), scope.get("broker_account"))


def _norm_expiry(value: Any) -> str | None:
    """Expiry → YYYYMMDD. Dates, 'YYYY-MM-DD', 'YYYYMMDD' all normalize."""
    if value is None:
        return None
    if isinstance(value, str):
        digits = value.replace("-", "")
        return digits if len(digits) == 8 and digits.isdigit() else None
    return value.strftime("%Y%m%d") if hasattr(value, "strftime") else None


def server_multiplier(sym: str) -> int:
    """Request-row `multiplier` column (idempotency body match only).

    Coverage never reads this column: option legs carry the qualified IB
    multiplier in the legs payload, and a leg without one fails closed.
    """
    return get_multiplier(sym) if sym in UNIVERSE else 100


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
    norm = orders_store.normalize_legs_payload(payload)
    raw = norm["legs"]
    legs = tuple(_leg_from_dict(x) for x in raw) if raw is not None else None
    account = norm["account"] if "account" in norm else _UNSET
    return legs, norm["guaranteed"], norm["pending_amend_qty"], account


def _synthesized_leg(m: Any) -> coverage.Leg:
    """Reconstruct a single leg from row columns (non-BAG rows only).

    Row columns never carried qualified option metadata, so an OPT leg
    rebuilt here has no tradingClass / multiplier / underlying type and
    fails closed (for its own symbol) if it can still owe demand.
    """
    sec = m["security_type"]
    return coverage.Leg(
        action="BUY",
        sym=m["ticker"],
        sec=sec,
        ratio=1,
        right=m["right"],
        exp=_norm_expiry(m["expiry"]),
        mult=None,
        tc=None,
        strike=float(m["strike"]) if m["strike"] is not None else None,
        con_id=int(m["con_id"]) if m["con_id"] is not None else None,
        und_sec=None,
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
    # The mirror records the broker's quantity when it differs from the
    # durable one (TWS edit of a Xenon order, stale amend): reserve the max.
    observed = orders_store.normalize_legs_payload(m["legs"]).get("observed_qty")
    qty = max(int(m["quantity"]), int(observed or 0))
    remaining = 0 if terminal else max(qty - int(filled_total), 0)
    return coverage.Order(
        id=m["submission_id"],
        account=account,
        envelope=m["action"],
        qty=qty,
        legs=legs,
        state=state,
        filled_after_snapshot=int(filled_after),
        remaining=int(remaining),
        guaranteed=guaranteed,
        # A dead order holds no reservation — pending_amend_qty must not keep
        # reserving max(qty, pending) forever after the row goes terminal.
        pending_amend_qty=None if terminal else pending,
    )


def _pure_buy(order: coverage.Order) -> bool:
    """True when no effective leg can consume cover (every leg BUY after the
    envelope flip). Such an order owes no demand under any netting mode, so
    its metadata is irrelevant. Unknown actions/envelopes are never pure."""
    if order.envelope not in ("BUY", "SELL") or not order.legs:
        return False
    flip = order.envelope == "SELL"
    for leg in order.legs:
        if leg.action not in ("BUY", "SELL"):
            return False
        effective = ("SELL" if leg.action == "BUY" else "BUY") if flip else leg.action
        if effective == "SELL":
            return False
    return True


def _unresolvable(order: coverage.Order) -> bool:
    """Order coverage would flag as a problem (other than its account)."""
    if coverage._order_problem(order) is not None or not order.legs:
        return True
    return any(coverage._leg_problem(leg) is not None for leg in order.legs)


def _row_symbols(m: Any, legs: tuple[coverage.Leg, ...] | None) -> frozenset[str] | None:
    """Symbols an order can touch; None = unknown (relevant to every symbol)."""
    if legs and all(leg.sym for leg in legs):
        return frozenset(str(leg.sym).upper() for leg in legs)
    ticker = str(m["ticker"] or "").strip().upper()
    if not ticker or "," in ticker:
        return None
    return frozenset({ticker})


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
    # submission_id -> symbols the order can touch (None = unknown).
    order_syms: dict[str, frozenset[str] | None] = field(default_factory=dict)
    # Live pure-BUY rows: excluded from `orders` (they owe no demand) but
    # still valid modify targets (round 4, item 5).
    pure_buy_ids: frozenset[str] = frozenset()


def _holdings_from_payload(payload: dict) -> list[coverage.Holding]:
    """Holdings straight from the sync pass's stamped legs.

    Every leg carries the raw IB `account` and, for options, the qualified
    `tc` / `mult` / `und_sec` (ib_sync stamps them from contract details).
    Nothing is defaulted: a leg without account fails the view closed, an
    option leg without metadata fails its symbol closed.
    """
    out: list[coverage.Holding] = []
    for pos in payload.get("positions") or []:
        sym = pos.get("ticker")
        exp = _norm_expiry(pos.get("expiry"))
        for leg in pos.get("legs") or []:
            leg_type = leg.get("type")
            direction = leg.get("direction")
            contracts = leg.get("contracts")
            qty = contracts if direction == "LONG" else -contracts if isinstance(contracts, int) else contracts
            account = leg.get("account")
            if leg_type == "Stock":
                out.append(coverage.Holding(sym=sym, sec="STK", qty=qty, account=account))
            elif leg_type in ("Call", "Put"):
                out.append(
                    coverage.Holding(
                        sym=sym,
                        sec="OPT",
                        qty=qty,
                        account=account,
                        right="C" if leg_type == "Call" else "P",
                        exp=exp,
                        mult=leg.get("mult"),
                        tc=leg.get("tc"),
                        und_sec=leg.get("und_sec"),
                    )
                )
    return out


# Seconds an IB position update may lag the execution that caused it.
FILL_LAG_S = 60.0


# xenon.service_health row (one per scope) holding the Gate-4 invalidation
# watermark in `last_finished_at`. Reused instead of a new table: the PK is
# (service, broker, account_env, broker_account) and the column is timestamptz.
VIEW_WATERMARK_SERVICE = "gate4_view_invalidated"


def _scope_where(table, scope: dict):
    return (
        table.c.broker == scope["broker"],
        table.c.account_env == scope["account_env"],
        table.c.broker_account == scope["broker_account"],
    )


# This Postgres cluster's identity: random per initdb, the same for every DB
# in the cluster, executable by PUBLIC. An xid8 seq orders things only
# within the cluster that issued it, so every stored seq carries it.
_CLUSTER_SQL = "(SELECT system_identifier FROM pg_control_system())::text"


def next_pass_seq() -> tuple[int, str]:
    """(xid8, cluster id) — this sync pass's sequence number.

    Taken at pass start in its own short transaction. xids are handed out in
    increasing order within one cluster, so `pass_seq` orders passes and
    watermarks without any wall clock (`as_of` stays for display and
    freshness only). Raises if either value is unavailable — the pass then
    has no seq and is not published.
    """
    with get_sync_engine().begin() as conn:
        seq, cluster = conn.execute(text(f"SELECT pg_current_xact_id()::text, {_CLUSTER_SQL}")).one()
    if not cluster:
        raise RuntimeError("pg_control_system() returned no system_identifier")
    return int(seq), cluster


def _seq(value: Any) -> int | None:
    """xid8 text/int → int (numeric compare, never string order)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


# Snapshot ordering key: observation.pass_seq as a number, only when the
# snapshot's pass_cluster is this cluster. Legacy rows (no cluster) and rows
# restored from another cluster are FOREIGN forever: NULL here, so they rank
# last and never count as the newest published pass.
_PASS_SEQ = account_snapshots.c.payload[("observation", "pass_seq")].astext.cast(Numeric)
_PASS_CLUSTER = account_snapshots.c.payload[("observation", "pass_cluster")].astext
_LOCAL_PASS_SEQ = case((_PASS_CLUSTER == literal_column(_CLUSTER_SQL), _PASS_SEQ), else_=None)


def _local_seq(conn, seq: int | None, cluster: Any) -> int | None:
    """`seq` if this cluster issued it, else None (foreign, legacy or missing)."""
    if seq is None or not isinstance(cluster, str) or not cluster:
        return None
    return seq if cluster == conn.execute(text(f"SELECT {_CLUSTER_SQL}")).scalar() else None


_INVALIDATE_SQL = text(
    f"""
    INSERT INTO xenon.service_health
        (service, broker, account_env, broker_account, state, detail, last_finished_at, updated_at)
    VALUES (:service, :broker, :account_env, :broker_account, 'invalidated',
            json_build_object('reason', CAST(:reason AS text), 'seq', pg_current_xact_id()::text,
                              'cluster', {_CLUSTER_SQL})::text,
            :at, :at)
    ON CONFLICT ON CONSTRAINT pk_service_health DO UPDATE SET
        detail = CASE
            WHEN service_health.detail IS NULL OR left(service_health.detail, 1) <> '{{' THEN EXCLUDED.detail
            -- Keep the stored seq only when the same cluster issued it and it
            -- is not lower; a foreign/legacy stored seq never outranks a
            -- local one (view_watermark ignores it) and is replaced.
            WHEN (service_health.detail::jsonb ->> 'cluster') IS DISTINCT FROM (EXCLUDED.detail::jsonb ->> 'cluster')
                THEN EXCLUDED.detail
            WHEN (service_health.detail::jsonb ->> 'seq')::numeric >= (EXCLUDED.detail::jsonb ->> 'seq')::numeric
                THEN service_health.detail
            ELSE EXCLUDED.detail
        END,
        state = 'invalidated',
        last_finished_at = GREATEST(service_health.last_finished_at, EXCLUDED.last_finished_at),
        updated_at = GREATEST(service_health.updated_at, EXCLUDED.updated_at)
    """
)


def invalidate_view(scope: dict, reason: str) -> None:
    """Move this scope's Gate-4 invalidation watermark forward.

    A writer that drops an accountless open order or fill (#198 filter) leaves
    its demand out of Postgres. From then on a snapshot proves cover only if
    its `pass_seq` is greater than the watermark's `seq` (`load_inputs`).
    The seq is this transaction's xid, assigned after the scope lock and after
    the writer's IB read; the row keeps the greater seq and is never cleared.
    `last_finished_at` is display only.
    """
    ensure_writable()
    at = datetime.now(timezone.utc)
    with get_sync_engine().begin() as conn:
        _lock_scope(conn, scope)
        conn.execute(
            _INVALIDATE_SQL,
            {
                "service": VIEW_WATERMARK_SERVICE,
                **{k: scope[k] for k in ("broker", "account_env", "broker_account")},
                "reason": reason,
                "at": at,
            },
        )


def view_watermark(conn, scope: dict) -> int | None:
    """Local watermark seq, or None. Non-JSON, legacy (no cluster) and
    other-cluster watermarks are ignored: every pass in this cluster ran
    after a restore, so it is after that invalidation."""
    detail = conn.execute(
        select(service_health.c.detail).where(
            service_health.c.service == VIEW_WATERMARK_SERVICE, *_scope_where(service_health, scope)
        )
    ).scalar()
    try:
        mark = json.loads(detail) if detail else None
        if not isinstance(mark, dict):
            return None
    except ValueError:
        return None
    return _local_seq(conn, _seq(mark.get("seq")), mark.get("cluster"))


def is_stale_publication(conn, scope: dict, observation: dict) -> bool:
    """Snapshot writer check, under the scope lock on `conn`.

    True when this pass has no local `pass_seq` or its `pass_seq` is not
    greater than the newest published local one for the scope: the writer then skips
    publishing, so the newer snapshot's holdings stay what `load_inputs`
    reads (it orders by `pass_seq`).
    """
    _lock_scope(conn, scope)
    seq = _local_seq(conn, _seq(observation.get("pass_seq")), observation.get("pass_cluster"))
    if seq is None:
        return True
    prior = conn.execute(select(func.max(_LOCAL_PASS_SEQ)).where(*_scope_where(account_snapshots, scope))).scalar()
    return prior is not None and seq <= int(prior)


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
        # Newest local pass by pass_seq (xid order); legacy and other-cluster
        # rows sort last and fall back to snapshot_at.
        .order_by(_LOCAL_PASS_SEQ.desc().nulls_last(), account_snapshots.c.snapshot_at.desc())
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
            # A writer dropped an accountless row at watermark seq `mark`:
            # only a pass that started after it can prove cover. No pass_seq
            # (or a legacy / other-cluster one) → the pass cannot be ordered.
            pass_seq = _local_seq(conn, _seq(observation.get("pass_seq")), observation.get("pass_cluster"))
            mark = view_watermark(conn, scope)
            if pass_seq is None or (mark is not None and pass_seq <= mark):
                complete = False
        holdings = _holdings_from_payload(payload)

    rows = conn.execute(
        select(order_submissions).where(
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
    # Round 5, item 6: a fill shortly before as_of may not be in the
    # positions read yet (IB position updates lag executions). Fills inside
    # FILL_LAG_S of as_of count as after-snapshot demand — over-reserves.
    fill_boundary = as_of - timedelta(seconds=FILL_LAG_S) if as_of is not None else None
    for f in fill_rows:
        sub_id = f.submission_id or perm_to_sub.get(str(f.perm_id or ""))
        if sub_id is None or sub_id not in sub_sec:
            # Orphan fill: no in-scope order to attribute it to. If it
            # post-dates the observation boundary its demand is invisible
            # to the evaluation — the view is incomplete.
            if fill_boundary is not None and _as_utc(f.filled_at) is not None and _as_utc(f.filled_at) >= fill_boundary:
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
        if fill_boundary is not None and _as_utc(f.filled_at) is not None and _as_utc(f.filled_at) < fill_boundary:
            fills_before[sub_id] = fills_before.get(sub_id, Decimal(0)) + qty

    orders = []
    order_syms: dict[str, frozenset[str] | None] = {}
    pure_buy_ids: set[str] = set()
    for r in rows:
        # filled_total = max(fill rows, the row's denormalized filled_qty).
        # The cache can only be *ahead* of the ledger (a fill row still in
        # flight), never behind — taking the max over-reserves, which is the
        # safe direction on inconsistent data.
        total = max(int(fill_rows_total.get(r.submission_id, 0)), stored_filled[r.submission_id])
        # Fills of unknown time land in filled_after: anything not provably
        # inside the observation window is demand the snapshot can't show.
        after = max(total - int(fills_before.get(r.submission_id, 0)), 0)
        # Liveness: a quantity-0 row reserves nothing; a terminal row with no
        # fill at/after as_of owes nothing the snapshot doesn't already show.
        if int(r.quantity or 0) <= 0:
            continue
        if (r.state or "") in TERMINAL_STATES and after == 0:
            continue
        order = order_from_row(r, filled_total=total, filled_after=after)
        if _pure_buy(order):
            pure_buy_ids.add(order.id)
            continue
        orders.append(order)
        order_syms[order.id] = _row_symbols(r._mapping, order.legs)

    view = coverage.View(complete=complete, stale=_snapshot_stale(as_of))
    return Gate4Inputs(
        holdings=holdings,
        orders=orders,
        view=view,
        scope_account=broker_account,
        as_of=as_of,
        snapshot_at=snapshot_at,
        order_syms=order_syms,
        pure_buy_ids=frozenset(pure_buy_ids),
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


def _opt_leg(sym: str, *, action: str, ratio: int, right, expiry, strike, meta: dict | None) -> coverage.Leg:
    """Candidate option leg. tc / mult / und_sec / con_id come only from the
    IB-qualified `meta` (contract_meta); without it they stay None and the
    candidate fails closed (GATE4_UNRESOLVED)."""
    meta = meta or {}
    return coverage.Leg(
        action=action,
        sym=sym,
        sec="OPT",
        ratio=ratio,
        right=right,
        exp=_norm_expiry(expiry),
        mult=meta.get("mult"),
        tc=meta.get("tc"),
        strike=float(strike) if strike is not None else None,
        con_id=meta.get("con_id"),
        und_sec=meta.get("und_sec"),
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


def _snapshot_meta(c: dict) -> dict:
    """Qualified metadata the open-order adapter attached (contract_meta)."""
    mult = c.get("multiplier")
    try:
        mult = int(mult) if mult is not None else None
    except (TypeError, ValueError):
        mult = None
    return {"tc": c.get("tradingClass") or None, "mult": mult, "und_sec": c.get("underSecType") or None}


def legs_payload_from_snapshot_order(order: dict) -> dict:
    """Durable legs payload for an IB open-order snapshot row.

    BAG legs come from `contract.comboLegs` (resolved by fetch_open_orders);
    unresolved BAG legs persist `legs: null`, which coverage treats as
    unresolvable — fail closed by construction. Option legs carry the
    tradingClass / multiplier / underSecType the adapter qualified via
    `contract_meta`; absent metadata stays None (fail closed), it is never
    derived from the symbol. `guaranteed` mirrors the order's NonGuaranteed
    tag (0→True, 1→False, missing→None). `account` preserves the adapter
    provenance — a null here is the "seen without account" marker coverage
    flags as missing.
    """
    contract = order.get("contract") or {}
    sec_type = str(contract.get("secType") or "")
    sym = str(contract.get("symbol") or order.get("symbol") or "").upper()
    account = order.get("account")

    if sec_type == "BAG":
        combo = contract.get("comboLegs") or []
        ng = order.get("nonGuaranteed")
        guaranteed = True if ng == 0 else (False if ng == 1 else None)
        if not combo or any(not cl.get("symbol") for cl in combo):
            return legs_payload_for(legs=None, guaranteed=guaranteed, account=account)
        legs = []
        for cl in combo:
            leg_sec = str(cl.get("secType") or "")
            meta = _snapshot_meta(cl) if leg_sec == "OPT" else {}
            legs.append(
                coverage.Leg(
                    action=str(cl.get("action") or "").upper(),
                    sym=str(cl.get("symbol")).upper(),
                    sec=leg_sec,
                    ratio=int(cl.get("ratio") or 1),
                    right=cl.get("right"),
                    exp=_norm_expiry(cl.get("expiry")),
                    mult=meta.get("mult"),
                    tc=meta.get("tc"),
                    strike=float(cl["strike"]) if cl.get("strike") is not None else None,
                    con_id=int(cl["conId"]) if cl.get("conId") is not None else None,
                    und_sec=meta.get("und_sec"),
                )
            )
        return legs_payload_for(legs=legs, guaranteed=guaranteed, account=account)

    if sec_type not in ("STK", "OPT"):
        return legs_payload_for(legs=None, guaranteed=None, account=account)

    meta = _snapshot_meta(contract) if sec_type == "OPT" else {}
    leg = coverage.Leg(
        action="BUY",
        sym=sym,
        sec=sec_type,
        ratio=1,
        right=contract.get("right"),
        exp=_norm_expiry(contract.get("expiry")),
        mult=meta.get("mult"),
        tc=meta.get("tc"),
        strike=float(contract["strike"]) if contract.get("strike") is not None else None,
        con_id=int(contract["conId"]) if contract.get("conId") is not None else None,
        und_sec=meta.get("und_sec"),
    )
    return legs_payload_for(legs=[leg], guaranteed=None, account=account)


def needs_contract_meta(body: dict) -> bool:
    """Bodies the gate evaluates AND that carry option legs: they need
    IB-qualified metadata before the reservation (single-leg BUY and stock
    bodies do not)."""
    if body.get("type") == "combo":
        return True
    return body.get("type") == "option" and str(body.get("action", "")).upper() != "BUY"


def candidate_from_place_body(
    body: dict, scope_account: str | None, leg_meta: list[dict | None] | None = None
) -> tuple[coverage.Order | None, dict]:
    """Build the coverage candidate + durable legs payload for /orders/place.

    Returns (candidate, legs_payload). candidate is None only for the pinned
    single-leg BUY pass-through (spec §3): it creates no demand, so the gate
    must not evaluate it — view/completeness problems must not block a BUY.

    `leg_meta` is the IB-qualified metadata per option leg (combo: one entry
    per body leg; single option: one entry), produced by the route via
    `contract_meta`. A missing entry leaves the leg unqualified → the
    candidate fails closed.

    Every other body (all SELLs, every combo regardless of envelope) becomes
    a coverage.Order and is evaluated under the advisory lock.
    """
    sym = str(body.get("symbol", "")).upper()
    action = str(body.get("action", "")).upper()
    # Raw quantity — coverage validates it is a real int (bool/float/str
    # reject); coercing here would launder invalid bodies into valid ints.
    qty = body.get("quantity")
    metas = list(leg_meta or [])

    def _meta(i: int) -> dict | None:
        return metas[i] if i < len(metas) else None

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
                meta=_meta(i),
            )
            for i, leg in enumerate(body.get("legs") or [])
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
                    meta=_meta(0),
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
    status: Literal["winner", "duplicate", "terminal", "body_mismatch", "rejected", "in_progress", "target_unresolved"]
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
    # claim_replacement: targets resolved to canonical identities (winner),
    # or the request target that could not be (target_unresolved).
    targets: list[dict] = field(default_factory=list)
    detail: str | None = None


def _candidate_symbols(inputs: Gate4Inputs, candidate) -> frozenset[str] | None:
    """Symbols the candidate touches; None = unknown (everything relevant)."""
    if isinstance(candidate, coverage.Modify):
        return inputs.order_syms.get(candidate.order_id)
    legs = getattr(candidate, "legs", None) or ()
    if legs and all(leg.sym for leg in legs):
        return frozenset(str(leg.sym).upper() for leg in legs)
    return None


def _evaluation_inputs(inputs: Gate4Inputs, candidate) -> tuple[list, list]:
    """Holdings/orders for one evaluation, with unclassifiable rows of OTHER
    symbols dropped (liveness: an unresolved row blocks its own symbol only).
    Cover and demand are per symbol, so a row on another symbol can never
    change the candidate's result. A missing account stays global — an
    accountless row means the view itself is incomplete (adv-14)."""
    syms = _candidate_symbols(inputs, candidate)
    if syms is None:
        return list(inputs.holdings), list(inputs.orders)
    orders = []
    for o in inputs.orders:
        if o.account is not None and _unresolvable(o):
            o_syms = inputs.order_syms.get(o.id)
            if o_syms is not None and not (o_syms & syms):
                continue
        orders.append(o)
    holdings = [
        h
        for h in inputs.holdings
        if h.account is None or coverage._holding_problem(h) is None or str(h.sym or "").upper() in syms
    ]
    return holdings, orders


def _evaluate(inputs: Gate4Inputs, candidate, *, netting: bool) -> coverage.Decision:
    holdings, orders = _evaluation_inputs(inputs, candidate)
    return coverage.evaluate(holdings, orders, candidate, inputs.view, inputs.scope_account, netting_enabled=netting)


def _accepted_with_netting(inputs: Gate4Inputs, candidate) -> bool:
    """Would this candidate pass as a guaranteed BAG under netting?

    The UI never sends a guarantee mode, so a place candidate is re-evaluated
    as guaranteed whenever every leg is OPT, regardless of the requested
    mode (round 3, item 9). A modify keeps its stored mode — the order
    already exists at the broker. A mixed-security or truly naked BAG fails
    identically under both modes and keeps its uncovered code.
    """
    if isinstance(candidate, coverage.Order):
        legs = candidate.legs or ()
        # Only a multi-leg option combo can be split into legs; a single-leg
        # order never gets the split hint.
        if len(legs) < 2 or any(leg.sec != "OPT" for leg in legs):
            return False
        candidate = replace(candidate, guaranteed=True)
    return _evaluate(inputs, candidate, netting=True).accept


def _legs_for_body_match(legs_payload):
    """Project a legs payload onto the request-owned keys (round 5, item 5).

    Whitelist, not blacklist: only what the client's body determines in
    `candidate_from_place_body` is compared — the leg identity and the
    guarantee mode. Everything else in the payload is server or mirror
    state (pending amend, observed_qty, account provenance, IB-qualified
    tc/mult/und_sec/con_id) and must not turn an identical replay into a
    body mismatch.
    """
    if not isinstance(legs_payload, dict):
        return legs_payload
    legs = legs_payload.get("legs")
    if isinstance(legs, list):
        legs = [{k: leg.get(k) for k in _REQUEST_LEG_KEYS} if isinstance(leg, dict) else leg for leg in legs]
    return {"legs": legs, "guaranteed": legs_payload.get("guaranteed")}


# Leg fields the place body itself determines (the rest come from IB).
_REQUEST_LEG_KEYS = ("action", "sym", "sec", "ratio", "right", "exp", "strike")


ATTEMPT_ALREADY_PLACED = "ATTEMPT_ALREADY_PLACED"


def _was_amended(conn, m) -> bool:
    """The row's price/qty moved after placement: a modify passed the
    sequence gate, or the mirror stored a broker price (PR-08)."""
    if int(m["modify_sequence"] or 0) > 0:
        return True
    return (
        conn.execute(
            select(order_events.c.submission_id)
            .where(
                order_events.c.submission_id == m["submission_id"],
                order_events.c.detail.has_key("limit_price"),
            )
            .limit(1)
        ).first()
        is not None
    )


def _classify_existing(conn, row, request: RequestRow, legs_payload) -> Gate4Outcome:
    m = row._mapping if hasattr(row, "_mapping") else row
    body_legs = _legs_for_body_match(m["legs"])
    req_legs = _legs_for_body_match(legs_payload)
    if not _request_body_matches(_RowView(m, legs=body_legs), request, req_legs):
        # A replay of the ORIGINAL body after a modify differs only in the
        # amended price/qty: the order exists (and may be working), so the
        # client must not be told to rebuild and resubmit.
        amended_replay = _was_amended(conn, m) and _request_body_matches(
            _RowView(m, legs=body_legs, limit_price=request.limit_price, quantity=request.quantity),
            request,
            req_legs,
        )
        return Gate4Outcome(
            status="body_mismatch",
            submission_id=row.submission_id,
            state=row.state,
            reason_code=ATTEMPT_ALREADY_PLACED if amended_replay else ATTEMPT_BODY_MISMATCH,
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
    release_ids: frozenset[str] = frozenset(),
) -> Gate4Outcome:
    """Dedupe → coverage.evaluate → insert — inside the caller's locked txn.

    `conn` must already hold `pg_advisory_xact_lock(scope_lock_key(scope))`.
    candidate=None skips evaluation (pinned single-leg BUY pass-through);
    the reservation still serializes under the lock. `release_ids`: rows
    the caller is about to cancel (replacement targets) — evaluated like
    terminal rows (remaining 0, post-snapshot fills still count).
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
        return _classify_existing(conn, existing, request, legs_payload)

    if candidate is not None:
        inputs = load_inputs(conn, scope)
        if release_ids:
            inputs.orders = [
                replace(o, remaining=0, pending_amend_qty=None) if o.id in release_ids else o for o in inputs.orders
            ]
        decision = _evaluate(inputs, candidate, netting=netting_enabled())
        if not decision.accept:
            return Gate4Outcome(
                status="rejected",
                reasons=list(decision.reasons),
                uncovered=dict(decision.uncovered),
                short_stock=dict(decision.short_stock),
                split_required=(not netting_enabled() and _accepted_with_netting(inputs, candidate)),
            )

    _after_check_hook()
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
            tif=request.tif,
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
        return _classify_existing(conn, row, request, legs_payload)

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


# ---------------------------------------------------------------------------
# Combo replacement (cancel original + place successor) — PR-07 fix3
# ---------------------------------------------------------------------------

# legs-payload key on a replacement's row: the orders it replaces.
REPLACES_KEY = "replaces"
# Claim states that prove nothing of this claim is (or may be) at IB.
_DEAD_CLAIM_STATES = ("FAILED", "REJECTED")


def _nonzero(value: Any) -> str | None:
    v = str(value or "").strip()
    return None if v in ("", "0") else v


def _identity_keys(targets: list[dict]) -> set[str]:
    """Canonical keys of resolved targets: the exact submission row and the
    permId. Never the orderId — it is unique only per IB clientId."""
    keys = set()
    for t in targets:
        if _nonzero(t.get("submission_id")):
            keys.add(f"sub:{t['submission_id']}")
        if _nonzero(t.get("perm_id")):
            keys.add(f"perm:{t['perm_id']}")
    return keys


def _resolve_target(conn, in_scope: tuple, target: dict) -> tuple[dict | None, str | None]:
    """One request target → canonical identity, or (None, why).

    permId (globally unique) wins: its row, if any, must carry the given
    orderId. Without a permId the pair (placing clientId, orderId) must name
    exactly one row in scope; the clientId may be omitted when the scope
    holds a single row for that orderId. Never orderId alone."""
    perm, oid = _nonzero(target.get("perm_id")), _nonzero(target.get("ib_order_id"))
    # clientId 0 is a real IB client (permId 0 / orderId 0 are not ids):
    # only a missing value means unknown.
    raw_client = target.get("client_id")
    client = None if raw_client is None or str(raw_client).strip() == "" else str(int(raw_client))
    cols = (
        order_submissions.c.submission_id,
        order_submissions.c.perm_id,
        order_submissions.c.ib_order_id,
        order_submissions.c.placing_client_id,
    )
    if perm is not None:
        rows = conn.execute(select(*cols).where(*in_scope, order_submissions.c.perm_id == perm)).all()
        if len(rows) > 1 or (rows and oid is not None and str(rows[0].ib_order_id or "") != oid):
            return None, f"permId {perm} does not name exactly one order"
        if not rows:  # not mirrored (e.g. a TWS order): the permId is the identity
            return {"perm_id": perm, "ib_order_id": oid, "client_id": client, "submission_id": None}, None
    elif oid is not None:
        where = [*in_scope, order_submissions.c.ib_order_id == oid]
        if client is not None:
            where.append(order_submissions.c.placing_client_id == int(client))
        rows = conn.execute(select(*cols).where(*where)).all()
        if len(rows) != 1:
            return None, f"orderId {oid} names {len(rows)} orders; send its permId"
    else:
        return None, "target has no permId or orderId"
    r = rows[0]
    return {
        "perm_id": _nonzero(r.perm_id),
        "ib_order_id": _nonzero(r.ib_order_id),
        "client_id": str(r.placing_client_id) if r.placing_client_id is not None else client,
        "submission_id": r.submission_id,
    }, None


def claim_replacement(
    user_id: str,
    client_attempt_id: str,
    request: RequestRow,
    *,
    candidate: coverage.Order | None,
    legs_payload: dict,
    scope: dict,
    targets: list[dict],
) -> Gate4Outcome:
    """Claim a replacement BEFORE any cancel, in one txn under the scope lock.

    `targets` = [{"perm_id", "ib_order_id", "client_id"}] from the request.
    Each is resolved ONCE, under the lock, to a canonical identity
    (`_resolve_target`); an ambiguous or unresolved target refuses
    (target_unresolved) before anything is cancelled. Refuses (in_progress)
    when another replacement of the same submission row or permId exists
    that is or may be at IB — every state but FAILED / REJECTED: a
    successful earlier replacement already cancelled the target, and a
    cancel of a gone order counts as success, so a second one would place a
    duplicate. Otherwise dedupe → Gate-4 evaluate (exactly the resolved
    rows released) → PENDING insert, identities under legs[REPLACES_KEY].
    """
    ensure_writable()
    engine = get_sync_engine()
    with engine.begin() as conn:
        _lock_scope(conn, scope)
        in_scope = (
            order_submissions.c.broker == scope["broker"],
            order_submissions.c.account_env == scope["account_env"],
            order_submissions.c.broker_account == scope["broker_account"],
        )
        resolved = []
        for t in targets:
            ident, why = _resolve_target(conn, in_scope, t)
            if ident is None:
                return Gate4Outcome(status="target_unresolved", targets=[t], detail=why)
            resolved.append(ident)
        wanted = _identity_keys(resolved)
        claims = conn.execute(
            select(order_submissions.c.submission_id, order_submissions.c.state, order_submissions.c.legs).where(
                *in_scope,
                order_submissions.c.legs.has_key(REPLACES_KEY),
                order_submissions.c.state.notin_(_DEAD_CLAIM_STATES),
            )
        ).all()
        for c in claims:
            if _identity_keys(list((c.legs or {}).get(REPLACES_KEY) or [])) & wanted:
                return Gate4Outcome(status="in_progress", submission_id=c.submission_id, state=c.state)
        outcome = evaluate_and_reserve(
            conn,
            user_id,
            client_attempt_id,
            request,
            candidate=candidate,
            legs_payload={**legs_payload, REPLACES_KEY: resolved},
            scope=scope,
            submission_id=str(uuid.uuid4()),
            release_ids=frozenset(t["submission_id"] for t in resolved if t["submission_id"]),
        )
        outcome.targets = resolved
        return outcome


def recheck_claim(submission_id: str, candidate: coverage.Order, scope: dict) -> Gate4Outcome | None:
    """Gate 4 again after the targets are cancelled, just before the place:
    the view may have changed while the cancel ran (fills, a new snapshot).
    The claim's own reservation is left out. None = accepted."""
    engine = get_sync_engine()
    with engine.begin() as conn:
        _lock_scope(conn, scope)
        inputs = load_inputs(conn, scope)
        inputs.orders = [o for o in inputs.orders if o.id != submission_id]
        decision = _evaluate(inputs, candidate, netting=netting_enabled())
        if decision.accept:
            return None
        return Gate4Outcome(
            status="rejected",
            submission_id=submission_id,
            reasons=list(decision.reasons),
            uncovered=dict(decision.uncovered),
            short_stock=dict(decision.short_stock),
            split_required=(not netting_enabled() and _accepted_with_netting(inputs, candidate)),
        )


@dataclass
class ModifyReservationOutcome:
    accept: bool
    reasons: list[str] = field(default_factory=list)
    submission_id: str | None = None
    uncovered: dict | None = None
    short_stock: dict | None = None
    split_required: bool = False
    # Generation token of this amend; settle_modify is a no-op unless the
    # row still carries it (stale replies never release newer demand).
    amend_id: str | None = None
    # Latest instant any sender may still transmit this amend (round 5,
    # item 2). The route refuses to spawn after it and passes it to the CLI
    # as --send-by; the CLI checks it before every placeOrder.
    send_by: datetime | None = None


MODIFY_PENDING_REASON = "modify_pending"

# Send window of one amend generation, from begin_modify (round 5, item 2).
MODIFY_SEND_WINDOW_S = 15.0
# Seconds IB may still take to apply an amend message it already received.
# Accepted residual: IB applying a received amend later than this is not
# measured.
AMEND_IB_GRACE_S = 60.0


def amend_deadlines(opened_at: datetime) -> tuple[datetime, datetime]:
    """(send_by, expiry deadline) of an amend opened at `opened_at`.

    No sender transmits after send_by (route + CLI fence), so after
    send_by + AMEND_IB_GRACE_S a fresh read shows the broker order as it
    really is. The CLI watchdog stays as a backstop only."""
    send_by = opened_at + timedelta(seconds=MODIFY_SEND_WINDOW_S)
    return send_by, send_by + timedelta(seconds=AMEND_IB_GRACE_S)


def _lock_scope(conn, scope: dict) -> None:
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": scope_lock_key(scope)})


def _row_payload(conn, submission_id: str) -> dict | None:
    row = conn.execute(
        select(order_submissions.c.legs).where(order_submissions.c.submission_id == submission_id)
    ).first()
    if row is None:
        return None
    return orders_store.normalize_legs_payload(row[0])


def _write_payload(conn, submission_id: str, payload: dict, now: datetime | None = None, **values) -> None:
    """Caller holds the account-scope lock; the watermark is monotone."""
    conn.execute(
        update(order_submissions)
        .where(order_submissions.c.submission_id == submission_id)
        .values(legs=payload, updated_at=orders_store.monotone_updated_at(now or datetime.now(timezone.utc)), **values)
    )


def begin_modify(submission_id: str, new_qty: int | None, scope: dict) -> ModifyReservationOutcome:
    """Open one amend generation on a submission row (round 3, item 5).

    Lock + pending check + (quantity amends only) view + evaluate + pending
    write, all in one transaction under the account-scope advisory lock:

    - A row that already carries an unresolved amend (`pending_amend_id` or
      `pending_amend_qty`) refuses the new modify with `modify_pending` —
      a later amend can never overwrite an earlier uncertain maximum.
    - A quantity amend evaluates coverage with max(old,new) and, on accept,
      stores pending_amend_qty=new_qty. A price-only amend stores only the
      generation token so a concurrent quantity amend cannot interleave.
    """
    ensure_writable()
    engine = get_sync_engine()
    with engine.begin() as conn:
        _lock_scope(conn, scope)
        payload = _row_payload(conn, submission_id)
        if payload is None:
            return ModifyReservationOutcome(accept=False, reasons=[f"modify_target_not_found:{submission_id}"])
        if payload.get("pending_amend_id") is not None or payload.get("pending_amend_qty") is not None:
            return ModifyReservationOutcome(accept=False, reasons=[MODIFY_PENDING_REASON])
        inputs = load_inputs(conn, scope) if new_qty is not None else None
        if inputs is not None and submission_id not in inputs.pure_buy_ids:
            # A resting pure-BUY order owes no cover at any quantity.
            modify_candidate = coverage.Modify(order_id=submission_id, new_qty=new_qty)
            decision = _evaluate(inputs, modify_candidate, netting=netting_enabled())
            if not decision.accept:
                return ModifyReservationOutcome(
                    accept=False,
                    reasons=list(decision.reasons),
                    uncovered=dict(decision.uncovered),
                    short_stock=dict(decision.short_stock),
                    split_required=(not netting_enabled() and _accepted_with_netting(inputs, modify_candidate)),
                )
        amend_id = str(uuid.uuid4())
        opened_at = datetime.now(timezone.utc)  # under the lock
        send_by, deadline = amend_deadlines(opened_at)
        payload["pending_amend_id"] = amend_id
        payload["pending_amend_qty"] = int(new_qty) if new_qty is not None else None
        payload["pending_amend_opened_at"] = opened_at.isoformat()
        payload["pending_amend_send_by"] = send_by.isoformat()
        payload["pending_amend_deadline"] = deadline.isoformat()
        _write_payload(conn, submission_id, payload, now=opened_at)
        return ModifyReservationOutcome(accept=True, submission_id=submission_id, amend_id=amend_id, send_by=send_by)


def _finite_decimal(value) -> Decimal | None:
    """Decimal for a finite number, else None (the broker already accepted
    the amend; an unparsable echo must never fail the settle)."""
    try:
        out = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return out if out.is_finite() else None


def settle_modify(
    submission_id: str,
    amend_id: str | None,
    outcome: Literal["confirmed", "rejected", "uncertain"],
    scope: dict,
    new_qty: int | None = None,
    new_limit_price: Decimal | float | str | None = None,
) -> bool:
    """Resolve one amend generation against the broker reply.

    Runs under the same account-scope advisory lock as begin_modify and the
    place path. A no-op (returns False) unless the row still carries
    `amend_id` — a delayed reply from an older amend never touches a newer
    one, and a reply after mirror reconciliation never rewrites quantity.

    confirmed → quantity becomes new_qty (quantity amends) and limit_price
                becomes new_limit_price (when given), amend clears.
    rejected  → the broker provably did not apply the amend, amend clears.
    uncertain → a quantity amend keeps pending_amend_qty + token so coverage
                keeps reserving max(old,new); a price-only amend clears its
                token (it resent IB's own quantity, so no demand moved).
    """
    ensure_writable()
    engine = get_sync_engine()
    with engine.begin() as conn:
        _lock_scope(conn, scope)
        payload = _row_payload(conn, submission_id)
        if payload is None or amend_id is None or payload.get("pending_amend_id") != amend_id:
            return False
        qty_amend = payload.get("pending_amend_qty") is not None
        if outcome == "uncertain" and qty_amend:
            # Demand stays max(old, new) until a fresh read after the
            # send_by + grace deadline confirms or expires this generation.
            return False
        values = {}
        if outcome == "confirmed" and qty_amend and new_qty is not None:
            values["quantity"] = int(new_qty)
        if outcome == "confirmed" and new_limit_price is not None:
            price = _finite_decimal(new_limit_price)
            if price is not None:
                values["limit_price"] = price
        _write_payload(conn, submission_id, orders_store._clear_pending(payload), **values)
        return True


def _after_check_hook() -> None:
    """Test seam between the coverage check and the reservation insert.

    No-op in production. The two-process race test replaces it (inside the
    child processes) with a barrier so the check→insert window is held open
    deterministically; the advisory lock is what keeps a second process out
    of that window.
    """


def persisted_guarantee(row: Any) -> bool | None:
    """Stored BAG guarantee mode from a submission row's legs payload."""
    _, guaranteed, _, _ = _decode_legs_payload(row["legs"] if isinstance(row, dict) else row.legs)
    return guaranteed
