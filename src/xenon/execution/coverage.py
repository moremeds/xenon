"""Gate 4 coverage evaluation — pure, no I/O, no DB, no IB imports.

Spec: tmp/repo-review/spec.md §2.3 (v3.2). Field semantics are pinned by the
frozen fixture `scripts/tests/fixtures/gate4_coverage_cases.json` `_doc` header.

Policy (root CLAUDE.md): no short call without cover, no short stock,
cash-secured puts allowed.

Model:

- Cover supply = confirmed holdings only. Working orders never supply cover
  and BUY legs never offset SELL legs.
- Cover demand per order = filled_after_snapshot + remaining (both in order
  units, per effective leg = ratio x demand). For a pending amend or an
  in-flight modify the outstanding is max(old_total, new_total) - filled_total.
- Effective legs: a SELL envelope flips each leg's action. Inside a
  guaranteed BAG leg deltas net per key when `netting_enabled` is set AND
  `guaranteed is True` AND every leg is OPT; a net long delta never
  supplies cover to other orders. Everywhere else legs are independent.
- Key = (sym, tc, exp, mult). A long call covers a short call of the same
  key only. Shares cover only standard deliverables: und_sec == 'STK',
  mult 100, tc == sym. Missing or non-STK und_sec means shares never
  cover that call.
- Order quantity fields are validated before projection: ints only
  (bool/float rejected), qty >= 1, remaining >= 0,
  filled_after_snapshot >= 0, filled_after_snapshot + remaining <= qty,
  pending_amend_qty / modify new_qty >= 1 when set, envelope in
  {BUY, SELL}. Any violation rejects with a reason.
- Anything we cannot classify fails closed: unknown contract metadata,
  incomplete/stale view, an order with no account, an unresolved BAG row
  with no stored legs, or a candidate from another account.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A coverage key identifies one option deliverable: (symbol, trading class,
# expiry, multiplier). Long calls cover short calls of the same key only.
Key = tuple[str, str, str, int]

_BUY = "BUY"
_SELL = "SELL"
_STK = "STK"
_OPT = "OPT"
_CALL = "C"
_PUT = "P"

_STANDARD_MULT = 100


@dataclass(frozen=True)
class Holding:
    """One confirmed position of the scope account at snapshot as_of.

    qty is signed: STK in shares, OPT in contracts.
    """

    sym: str
    sec: str
    qty: int
    right: str | None = None
    exp: str | None = None
    mult: int | None = None
    tc: str | None = None


@dataclass(frozen=True)
class Leg:
    """One order leg as stored on the reservation row.

    `action` is the order-side leg action; a SELL envelope flips it.
    `ratio` multiplies the order demand units. `strike`/`con_id` are carried
    for reservation parity but do not participate in the coverage key.
    `und_sec` is the underlying's security type; shares cover a short call
    only when und_sec == 'STK' (index/futures/unknown deliverables do not).
    """

    action: str
    sym: str
    sec: str
    ratio: int = 1
    right: str | None = None
    exp: str | None = None
    mult: int | None = None
    tc: str | None = None
    strike: float | None = None
    con_id: int | None = None
    und_sec: str | None = None


@dataclass(frozen=True)
class Order:
    """One existing order or a fresh candidate.

    `legs=None` means an unresolved BAG row (no stored legs) and fails
    closed. `legs=()` (a BAG with no legs) also fails closed.
    `filled_after_snapshot` + `remaining` are authoritative demand inputs;
    `qty` is the order's total. `pending_amend_qty` marks an uncertain amend:
    the order reserves max(qty, pending_amend_qty) - filled_total.
    """

    id: str
    account: str | None
    envelope: str
    qty: int
    legs: tuple[Leg, ...] | None
    state: str = ""
    filled_after_snapshot: int = 0
    remaining: int = 0
    guaranteed: bool | None = None
    pending_amend_qty: int | None = None


@dataclass(frozen=True)
class Modify:
    """Candidate that changes an existing order's total quantity.

    The target keeps max(old_total, new_qty) - filled_total as outstanding.
    """

    order_id: str
    new_qty: int


@dataclass(frozen=True)
class View:
    """Observation boundary state of the last sync pass."""

    complete: bool = True
    stale: bool = False


@dataclass(frozen=True)
class Decision:
    accept: bool
    reasons: list[str] = field(default_factory=list)
    uncovered: dict[Key, int] = field(default_factory=dict)
    short_stock: dict[str, int] = field(default_factory=dict)


def _flip(action: str) -> str:
    return _SELL if action == _BUY else _BUY


def _leg_problem(leg: Leg) -> str | None:
    """Return why a leg cannot be classified, or None when it is known."""
    if leg.action not in (_BUY, _SELL):
        return "action"
    if not leg.sym:
        return "sym"
    if leg.sec not in (_STK, _OPT):
        return "sec"
    if isinstance(leg.ratio, bool) or not isinstance(leg.ratio, int) or leg.ratio < 1:
        return "ratio"
    if leg.sec == _OPT:
        if leg.right not in (_CALL, _PUT):
            return "right"
        if not leg.exp:
            return "exp"
        if not isinstance(leg.mult, int) or leg.mult < 1:
            return "mult"
        if not leg.tc:
            return "tc"
    return None


def _holding_problem(h: Holding) -> str | None:
    if not h.sym:
        return "sym"
    if h.sec not in (_STK, _OPT):
        return "sec"
    if h.sec == _OPT:
        if h.right not in (_CALL, _PUT):
            return "right"
        if not h.exp:
            return "exp"
        if not isinstance(h.mult, int) or h.mult < 1:
            return "mult"
        if not h.tc:
            return "tc"
    return None


def _order_problem(order: Order) -> str | None:
    """Return why an order's quantity fields cannot be trusted, or None."""
    if order.envelope not in (_BUY, _SELL):
        return "envelope"
    for name in ("qty", "remaining", "filled_after_snapshot"):
        v = getattr(order, name)
        if isinstance(v, bool) or not isinstance(v, int):
            return name
    if order.qty < 1:
        return "qty"
    if order.remaining < 0:
        return "remaining"
    if order.filled_after_snapshot < 0:
        return "filled_after_snapshot"
    if order.filled_after_snapshot + order.remaining > order.qty:
        return "filled_bounds"
    paq = order.pending_amend_qty
    if paq is not None and (isinstance(paq, bool) or not isinstance(paq, int) or paq < 1):
        return "pending_amend_qty"
    return None


def _key(sym: str, tc: str, exp: str, mult: int) -> Key:
    return (sym, tc, exp, mult)


def _outstanding(order: Order, new_qty: int | None = None) -> int:
    """Worst-case unfilled units for one order.

    filled_total = qty - remaining (pre-snapshot fills + post-snapshot fills).
    A pending amend or an in-flight modify keeps max(old, new) total.
    """
    filled_total = order.qty - order.remaining
    totals = [order.qty]
    if order.pending_amend_qty is not None:
        totals.append(order.pending_amend_qty)
    if new_qty is not None:
        totals.append(new_qty)
    return max(max(totals) - filled_total, 0)


def _demand_units(order: Order, new_qty: int | None = None) -> int:
    return order.filled_after_snapshot + _outstanding(order, new_qty)


def _add_order_demand(
    order: Order,
    units: int,
    call_demand: dict[Key, int],
    stock_demand: dict[str, int],
    call_share_ok: dict[Key, bool],
    *,
    netting_enabled: bool,
) -> None:
    """Accumulate one order's consuming-leg demand (consume-only).

    Netting applies only when `netting_enabled` is set AND the order is
    guaranteed AND every leg is OPT: effective leg deltas net per key,
    the net short-call delta is the demand, and a net long delta is
    discarded. Otherwise every leg is independent and only SELL call /
    SELL stock legs consume.

    `call_share_ok[key]` records whether every OPT leg seen at that key
    carries und_sec == 'STK'; a missing or non-STK und_sec on any order
    leg disables share cover for the key (holding-derived demand keeps
    the mult 100 / tc == sym deliverable check, and long-call cover is
    unaffected either way).
    """
    if units <= 0:
        return
    legs = order.legs or ()
    flip = order.envelope == _SELL
    netting = (
        netting_enabled
        and order.guaranteed is True
        and all(leg.sec == _OPT for leg in legs)
    )
    if netting:
        call_net: dict[Key, int] = {}
        for leg in legs:
            action = _flip(leg.action) if flip else leg.action
            units_leg = leg.ratio * units
            delta = units_leg if action == _BUY else -units_leg
            k = _key(leg.sym, leg.tc, leg.exp, leg.mult)
            call_share_ok[k] = call_share_ok.get(k, True) and leg.und_sec == _STK
            if leg.right == _CALL:
                call_net[k] = call_net.get(k, 0) + delta
            # puts never cover and never create naked-call demand
        for k, d in call_net.items():
            if d < 0:
                call_demand[k] = call_demand.get(k, 0) + (-d)
        return
    for leg in legs:
        if leg.sec == _OPT:
            k = _key(leg.sym, leg.tc, leg.exp, leg.mult)
            call_share_ok[k] = call_share_ok.get(k, True) and leg.und_sec == _STK
        action = _flip(leg.action) if flip else leg.action
        if action != _SELL:
            continue
        units_leg = leg.ratio * units
        if leg.sec == _OPT:
            if leg.right == _CALL:
                call_demand[k] = call_demand.get(k, 0) + units_leg
            # short put is cash-secured: allowed, no cover consumed
        else:
            stock_demand[leg.sym] = stock_demand.get(leg.sym, 0) + units_leg


def _project(
    holdings: list[Holding] | tuple[Holding, ...],
    orders: list[Order] | tuple[Order, ...],
    candidate: Order | Modify | None,
    scope_account: str | None,
    *,
    strict: bool,
    netting_enabled: bool = False,
) -> tuple[dict[Key, int], dict[str, int], list[str]]:
    """Compute uncovered short calls per key and short stock per symbol.

    Returns (uncovered, short_stock, problems). In strict mode every
    unresolvable input is a problem; in lenient mode it is skipped (audit
    callers log and skip per spec §2.4).
    """
    problems: list[str] = []
    call_pos: dict[Key, int] = {}
    shares: dict[str, int] = {}
    call_share_ok: dict[Key, bool] = {}

    modify_valid = isinstance(candidate, Modify) and (
        isinstance(candidate.new_qty, int)
        and not isinstance(candidate.new_qty, bool)
        and candidate.new_qty >= 1
    )

    for h in holdings:
        bad = _holding_problem(h)
        if bad is not None:
            if strict:
                problems.append(f"holding_unknown_metadata:{h.sym}:{bad}")
            continue
        if h.sec == _OPT:
            if h.right == _CALL:
                k = _key(h.sym, h.tc, h.exp, h.mult)
                call_pos[k] = call_pos.get(k, 0) + h.qty
        else:
            shares[h.sym] = shares.get(h.sym, 0) + h.qty

    call_demand: dict[Key, int] = {}
    stock_demand: dict[str, int] = {}

    for o in orders:
        if o.account is None:
            if strict:
                problems.append(f"order_missing_account:{o.id}")
            continue
        if scope_account is not None and o.account != scope_account:
            continue
        bad = _order_problem(o)
        if bad is not None:
            if strict:
                problems.append(f"order_invalid:{o.id}:{bad}")
            continue
        new_qty = (
            candidate.new_qty
            if modify_valid and candidate.order_id == o.id
            else None
        )
        if not o.legs:
            if strict:
                problems.append(f"order_unresolved_legs:{o.id}")
            continue
        bad_leg = next((leg for leg in o.legs if _leg_problem(leg) is not None), None)
        if bad_leg is not None:
            if strict:
                problems.append(f"order_unknown_metadata:{o.id}:{_leg_problem(bad_leg)}")
            continue
        _add_order_demand(
            o, _demand_units(o, new_qty), call_demand, stock_demand,
            call_share_ok, netting_enabled=netting_enabled,
        )

    if isinstance(candidate, Order):
        if scope_account is not None and candidate.account != scope_account:
            problems.append(f"candidate_account_mismatch:{candidate.account}")
        elif _order_problem(candidate) is not None:
            problems.append(f"candidate_invalid:{candidate.id}:{_order_problem(candidate)}")
        elif not candidate.legs:
            problems.append(f"candidate_unresolved_legs:{candidate.id}")
        else:
            bad_leg = next((leg for leg in candidate.legs if _leg_problem(leg) is not None), None)
            if bad_leg is not None:
                problems.append(f"candidate_unknown_metadata:{candidate.id}:{_leg_problem(bad_leg)}")
            else:
                _add_order_demand(
                    candidate, candidate.qty, call_demand, stock_demand,
                    call_share_ok, netting_enabled=netting_enabled,
                )
    elif isinstance(candidate, Modify):
        if not modify_valid:
            problems.append(f"modify_invalid_new_qty:{candidate.new_qty}")
        scoped = [
            o
            for o in orders
            if o.account is not None and (scope_account is None or o.account == scope_account)
        ]
        if not any(o.id == candidate.order_id for o in scoped):
            problems.append(f"modify_target_not_found:{candidate.order_id}")

    # Shares are cover only after stock-sell demand; short stock is a
    # projected negative share balance.
    projected = {sym: shares.get(sym, 0) - d for sym, d in stock_demand.items()}
    for sym in shares:
        projected.setdefault(sym, shares[sym])
    short_stock = {sym: -q for sym, q in projected.items() if q < 0}

    # Long calls first (they can only cover their own key), then shares on
    # standard stock-underlying deliverables only (und_sec STK, mult 100,
    # tc == sym). Shares are allocated per symbol across its keys in
    # sorted-key order so every unit is used once and the result is
    # deterministic.
    uncovered: dict[Key, int] = {}
    share_pool = {sym: max(q, 0) for sym, q in projected.items()}
    for k in sorted(call_demand.keys() | call_pos.keys()):
        residual = max(0, call_demand.get(k, 0) - call_pos.get(k, 0))
        sym, tc, _exp, mult = k
        if (
            residual > 0
            and mult == _STANDARD_MULT
            and tc == sym
            and call_share_ok.get(k, True)
        ):
            pool = share_pool.get(sym, 0)
            covered = min(residual, pool // mult)
            if covered:
                residual -= covered
                share_pool[sym] = pool - covered * mult
        if residual > 0:
            uncovered[k] = residual

    return uncovered, short_stock, problems


def evaluate(
    holdings: list[Holding] | tuple[Holding, ...],
    orders: list[Order] | tuple[Order, ...],
    candidate: Order | Modify,
    view: View,
    scope_account: str,
    netting_enabled: bool = False,
) -> Decision:
    """Preflight gate: accept iff the candidate adds no short stock and does
    not increase uncovered short calls on any key.

    Any fail-closed condition (incomplete or stale view, missing account,
    unresolved BAG legs, unknown metadata, foreign-account candidate,
    unknown modify target, invalid quantity/envelope fields) rejects.

    `netting_enabled` rolls out guaranteed-BAG per-key netting; while off,
    guaranteed bags are evaluated leg-by-leg like any other order.
    """
    reasons: list[str] = []
    if view is None or not view.complete:
        reasons.append("view_incomplete")
    elif view.stale:
        reasons.append("view_stale")
    if not scope_account:
        reasons.append("scope_account_missing")

    base_unc, base_ss, base_problems = _project(
        holdings, orders, None, scope_account, strict=True, netting_enabled=netting_enabled
    )
    with_unc, with_ss, with_problems = _project(
        holdings, orders, candidate, scope_account, strict=True, netting_enabled=netting_enabled
    )
    problems = sorted(set(base_problems) | set(with_problems))
    reasons.extend(problems)

    if not problems and not reasons:
        for sym, qty in with_ss.items():
            if qty > base_ss.get(sym, 0):
                reasons.append(f"short_stock:{sym}")
        for k, qty in with_unc.items():
            if qty > base_unc.get(k, 0):
                reasons.append(f"uncovered_call_increase:{k}")

    return Decision(
        accept=not reasons,
        reasons=reasons,
        uncovered=with_unc,
        short_stock=with_ss,
    )


def uncovered(
    holdings: list[Holding] | tuple[Holding, ...],
    orders: list[Order] | tuple[Order, ...],
    candidate: Order | Modify | None = None,
    *,
    netting_enabled: bool = False,
) -> dict[Key, int]:
    """Uncovered short-call contracts per key for the naked-short audit.

    Lenient: orders it cannot classify are skipped (the audit logs and
    skips per spec §2.4). Pass live orders only; no post-as_of overlap
    demand is added here.
    """
    unc, _ss, _problems = _project(
        holdings, orders, candidate, None, strict=False, netting_enabled=netting_enabled
    )
    return unc


def short_stock(
    holdings: list[Holding] | tuple[Holding, ...],
    orders: list[Order] | tuple[Order, ...],
    candidate: Order | Modify | None = None,
    *,
    netting_enabled: bool = False,
) -> dict[str, int]:
    """Projected short stock per symbol for the naked-short audit."""
    _unc, ss, _problems = _project(
        holdings, orders, candidate, None, strict=False, netting_enabled=netting_enabled
    )
    return ss
