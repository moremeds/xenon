"""Naked-short audit — shadow mode (spec §2.4, Phase A).

Adapter over `xenon.execution.coverage`:

    IB open orders (the `ib_orders.open_orders_from_trades` shape)
      -> working-order legs (BAG `comboLegs` shaped, via gate4's adapter)
      -> coverage over the in-process holdings of the sync pass that just ran
      -> violations

Holdings come from the pass's own portfolio payload, never from Postgres, so
a read-only session audits exactly what it saw. Demand is holdings + live
orders only (no post-`as_of` overlap demand): over-reservation is a gate-4
safety margin, never grounds for a violation.

Shadow only: violations are logged and recorded in
`service_health("naked_short_audit")`. This module has no broker access and
no cancel path; enforcement (PR-11) is a separate, opt-in step in the API
process (`xenon.api.services.audit_enforce`, `XENON_AUDIT_CANCEL=1`), fed by
the summary `ib_sync` prints.

`violations` lists every contributor — each live order whose removal lowers
uncovered short calls or short stock — and is not the cancel set: two SELL
calls against 100 shares are both listed although cancelling one suffices.
Enforcement must cancel one order, then re-sync and recompute (spec §2.4).
Each entry carries its `legs` and `guaranteed` mode; `held_uncovered` /
`held_short_stock` report exposure already in holdings with no attributable
order (it does not change `status`).

Skip-and-log, never a violation. The whole audit is skipped when:
- the scope account is blank, whitespace or an unknown-account sentinel
  (`coverage.is_real_account`);
- the snapshot observation is missing, not `complete is True`, names another
  account, or its `as_of` is unparseable, more than 60 s in the future or
  older than `AUDIT_MAX_AGE_S`;
- an unresolved BAG (gate4 could not build its legs from `comboLegs`) or an
  order with no usable symbol is live — its constituents are unknown.
One symbol is skipped (all its orders) when a live order on it has no
account, a non-integer or < 1 quantity, an unsupported secType, or fields
coverage cannot classify (envelope, quantity bounds, leg action / secType /
option metadata) unless every effective leg is a BUY (owes no demand); or
when a holding on it has no account or unknown metadata. Foreign-account
orders and IB-terminal statuses are not this scope's live demand and are
ignored.

CLI (`xenon-naked-short-audit`): offline forensics over a saved portfolio
payload + open-orders file. It prints the result and touches nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from xenon.db.service_health import record_service_health
from xenon.execution import coverage, gate4

logger = logging.getLogger("xenon.naked_short_audit")

SERVICE = "naked_short_audit"

# IB order statuses that can no longer fill. Everything else is live
# (worst case: PendingCancel can still fill).
TERMINAL_IB_STATUSES = frozenset({"Cancelled", "ApiCancelled", "Filled", "Inactive"})

# A snapshot older than this is not audited (the offline CLI reads saved
# payloads; the in-process sync pass hands over its own just-taken stamp).
AUDIT_MAX_AGE_S = 15 * 60.0
# Tolerated clock skew for an `as_of` slightly in the future.
_AS_OF_SKEW_S = 60.0


@dataclass
class AuditResult:
    status: str  # ok | violations | skipped | error
    violations: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    reason: str | None = None
    # Exposure already in holdings (no order attributable): key -> contracts,
    # symbol -> shares. Reported only; it never changes `status`.
    held_uncovered: dict[str, int] = field(default_factory=dict)
    held_short_stock: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "mode": "shadow",
            "status": self.status,
            "reason": self.reason,
            "violations": self.violations,
            "skipped": self.skipped,
            "held_uncovered": self.held_uncovered,
            "held_short_stock": self.held_short_stock,
        }


def _key_str(key: coverage.Key) -> str:
    return "|".join(str(part) for part in key)


def _as_int(value) -> int | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else None


_UNRESOLVED_BAG = "unresolved_bag_symbols"


def _order_symbols(o: dict, legs) -> frozenset[str]:
    """Symbols the order touches; empty = unknown (a compound symbol such as
    'AAPL,MSFT' is not one symbol and cannot be trusted as a skip key)."""
    if legs:
        return frozenset(str(leg.sym).upper() for leg in legs if leg.sym)
    sym = str((o.get("contract") or {}).get("symbol") or o.get("symbol") or "").strip().upper()
    return frozenset({sym}) if sym and "," not in sym else frozenset()


def _unclassifiable(order: coverage.Order) -> str | None:
    """Why coverage cannot evaluate this order (None = it can)."""
    if not order.legs:
        return "unresolved_or_unsupported_legs"
    problem = coverage._order_problem(order)
    if problem is not None:
        return f"invalid_order:{problem}"
    for leg in order.legs:
        problem = coverage._leg_problem(leg)
        if problem is not None:
            return f"unknown_leg_metadata:{problem}"
    return None


def _observation_problem(observation, scope_account: str, now: datetime, max_age_s: float) -> str | None:
    """Why this snapshot observation cannot be audited (None = it can).

    The stamp must say the pass was complete (`complete is True`), name the
    scope account, and be fresh (`as_of` within `max_age_s`).
    """
    if not isinstance(observation, dict):
        return "observation_missing"
    if observation.get("complete") is not True:
        return "observation_incomplete"
    if observation.get("account") != scope_account:
        return "observation_account_mismatch"
    try:
        as_of = datetime.fromisoformat(str(observation.get("as_of")))
    except ValueError:
        return "observation_as_of_invalid"
    if as_of.tzinfo is None:
        return "observation_as_of_invalid"
    age = (now - as_of).total_seconds()
    if age < -_AS_OF_SKEW_S:
        return "observation_as_of_invalid"
    if age > max_age_s:
        return "observation_stale"
    return None


def _to_coverage_order(o: dict, idx: int) -> tuple[coverage.Order | None, frozenset[str], str | None]:
    """Map one scoped live open order to (coverage.Order, symbols, skip_reason)."""
    legs_payload = gate4.legs_payload_from_snapshot_order(o)
    raw = legs_payload["legs"]
    legs = tuple(gate4._leg_from_dict(x) for x in raw) if raw is not None else None
    syms = _order_symbols(o, legs)
    sec = (o.get("contract") or {}).get("secType")
    if legs is None and sec == "BAG":
        # gate4 leaves BAG legs unresolved when comboLegs are missing or any
        # leg has no symbol: the constituent symbols cannot all be known.
        # Checked before quantity so a malformed BAG still skips the pass.
        return None, syms, _UNRESOLVED_BAG
    qty = _as_int(o.get("totalQuantity"))
    filled = _as_int(o.get("filled") or 0)
    if qty is None or filled is None or qty < 1:
        return None, syms, "invalid_quantity"
    order = coverage.Order(
        id=f"{o.get('clientId')}:{o.get('orderId')}:{o.get('permId')}:{idx}",
        account=o.get("account"),
        envelope=str(o.get("action") or "").upper(),
        qty=qty,
        legs=legs,
        state=str(o.get("status") or ""),
        filled_after_snapshot=0,
        # Worst case: everything not yet filled can still fill.
        remaining=max(qty - filled, 0),
        guaranteed=legs_payload["guaranteed"],
    )
    if gate4._pure_buy(order):
        return order, syms, None  # owes no demand; metadata irrelevant
    if legs is None:
        return None, syms, f"unsupported_instrument:{sec}"
    return order, syms, _unclassifiable(order)


def find_violations(
    payload: dict,
    open_orders: list[dict],
    scope_account: str,
    *,
    observation: dict | None,
    netting_enabled: bool = False,
    max_age_s: float = AUDIT_MAX_AGE_S,
    now: datetime | None = None,
) -> AuditResult:
    """Pure: which live orders of `scope_account` create naked short exposure.

    An order is a violation when removing it lowers uncovered short calls on
    some key or short stock on some symbol — every contributor is reported
    with its identity (clientId, orderId, permId) for the operator.
    """
    if not coverage.is_real_account(scope_account):
        return AuditResult("skipped", reason="scope_account_invalid")
    problem = _observation_problem(observation, scope_account, now or datetime.now(timezone.utc), max_age_s)
    if problem is not None:
        return AuditResult("skipped", reason=problem)

    skipped: list[dict] = []
    skip_syms: set[str] = set()

    def _skip(sym: str, reason: str, o: dict | None = None) -> None:
        skip_syms.add(sym)
        entry = {"symbol": sym, "reason": reason}
        if o is not None:
            entry.update(order_id=o.get("orderId"), perm_id=o.get("permId"), client_id=o.get("clientId"))
        skipped.append(entry)

    holdings = gate4._holdings_from_payload(payload)
    for h in holdings:
        if h.account is None:
            _skip(str(h.sym).upper(), "holding_missing_account")
        elif h.account == scope_account and coverage._holding_problem(h) is not None:
            _skip(str(h.sym).upper(), f"holding_unknown_metadata:{coverage._holding_problem(h)}")

    live: list[tuple[coverage.Order, frozenset[str], dict]] = []
    for idx, o in enumerate(open_orders):
        if o.get("status") in TERMINAL_IB_STATUSES:
            continue
        account = o.get("account")
        if account and account != scope_account:
            continue  # another scope's order is not this account's demand
        order, syms, reason = _to_coverage_order(o, idx)
        if reason == _UNRESOLVED_BAG:
            # Its legs may consume or release cover on any symbol we cannot
            # name, so no symbol's result is safe for this pass.
            return AuditResult("skipped", skipped=skipped, reason=_UNRESOLVED_BAG)
        if not syms:
            return AuditResult("skipped", skipped=skipped, reason="order_symbol_unknown")
        if not account:
            reason = "order_missing_account"
        if reason is not None:
            for sym in sorted(syms):
                _skip(sym, reason, o)
            continue
        live.append((order, syms, o))

    kept = [(order, o) for order, syms, o in live if not syms & skip_syms]
    orders = [order for order, _o in kept]

    def _measure(subset):
        kw = {"scope_account": scope_account, "netting_enabled": netting_enabled}
        return coverage.uncovered(holdings, subset, **kw), coverage.short_stock(holdings, subset, **kw)

    unc_all, ss_all = _measure(orders)
    held_unc, held_ss = _measure([])
    violations: list[dict] = []
    for i, (order, o) in enumerate(kept):
        unc_wo, ss_wo = _measure(orders[:i] + orders[i + 1 :])
        calls = {_key_str(k): n for k, n in sorted(unc_all.items()) if n > unc_wo.get(k, 0)}
        stock = {s: n for s, n in sorted(ss_all.items()) if n > ss_wo.get(s, 0)}
        if calls or stock:
            violations.append(
                {
                    "client_id": o.get("clientId"),
                    "order_id": o.get("orderId"),
                    "perm_id": o.get("permId"),
                    "account": o.get("account"),
                    "symbol": ",".join(sorted(_order_symbols(o, order.legs))),
                    "action": order.envelope,
                    "status": order.state,
                    "uncovered_calls": calls,
                    "short_stock": stock,
                    # Shadow evidence: lets the review count legitimate
                    # structures flagged under leg independence.
                    "legs": gate4.serialize_legs(list(order.legs or ())),
                    "guaranteed": order.guaranteed,
                }
            )
    return AuditResult(
        "violations" if violations else "ok",
        violations,
        skipped,
        held_uncovered={_key_str(k): n for k, n in sorted(held_unc.items()) if k[0].upper() not in skip_syms},
        held_short_stock={s: n for s, n in sorted(held_ss.items()) if s.upper() not in skip_syms},
    )


def _log(result: AuditResult, scope_account: str) -> None:
    for s in result.skipped:
        logger.info("naked short audit (shadow) skipped %s: %s", s["symbol"], s["reason"])
    if result.status == "skipped":
        logger.warning("naked short audit (shadow) skipped for %s: %s", scope_account or "?", result.reason)
    elif result.violations:
        logger.warning(
            "NAKED SHORT AUDIT (shadow): %d violation(s) for %s — logged only, nothing cancelled",
            len(result.violations),
            scope_account,
        )
        for v in result.violations:
            logger.warning(
                "  -> %s %s clientId=%s orderId=%s permId=%s uncovered_calls=%s short_stock=%s",
                v["symbol"],
                v["action"],
                v["client_id"],
                v["order_id"],
                v["perm_id"],
                v["uncovered_calls"],
                v["short_stock"],
            )
    else:
        logger.info("naked short audit (shadow): no violations for %s", scope_account)


def _health_detail(result: AuditResult) -> str:
    return json.dumps(
        {
            "mode": "shadow",
            "reason": result.reason,
            "violations": len(result.violations),
            "skipped": len(result.skipped),
            "orders": [
                {k: v[k] for k in ("symbol", "client_id", "order_id", "perm_id", "legs", "guaranteed")}
                for v in result.violations
            ],
            "held_uncovered": result.held_uncovered,
            "held_short_stock": result.held_short_stock,
        }
    )


def run_shadow_audit(
    payload: dict,
    open_orders: list[dict],
    scope_account: str,
    *,
    observation: dict | None,
) -> AuditResult:
    """Audit the sync pass, log the outcome, record service health.

    Never raises: an audit failure is logged and recorded as `error` so the
    sync that called it still completes.
    """
    started = datetime.now(timezone.utc)
    try:
        result = find_violations(
            payload,
            open_orders,
            scope_account,
            observation=observation,
            netting_enabled=gate4.netting_enabled(),
        )
        _log(result, scope_account)
        error = None
    except Exception as exc:  # noqa: BLE001 — the sync must survive an audit bug
        logger.error("naked short audit (shadow) failed", exc_info=True)
        result = AuditResult("error", reason=f"{type(exc).__name__}: {exc}")
        error = {"msg": result.reason}
    record_service_health(
        SERVICE,
        result.status,
        broker_account=scope_account if coverage.is_real_account(scope_account) else None,
        started_at=started,
        finished_at=datetime.now(timezone.utc),
        detail=_health_detail(result),
        error=error,
    )
    return result


def main(argv=None) -> dict:
    """Offline forensics: audit a saved payload + open-orders file. Prints only."""
    parser = argparse.ArgumentParser(description="Naked short audit (shadow, offline) — never cancels")
    parser.add_argument("--portfolio", required=True, help="Portfolio payload JSON (account_snapshots.payload)")
    parser.add_argument("--orders", required=True, help='JSON with "open_orders" in the ib_orders shape')
    parser.add_argument("--account", default=None, help="Scope account (default: payload observation.account)")
    args = parser.parse_args(argv)

    with open(args.portfolio) as f:
        payload = json.load(f)
    with open(args.orders) as f:
        open_orders = json.load(f).get("open_orders", [])
    observation = payload.get("observation")
    account = args.account or (observation or {}).get("account") or ""
    summary = find_violations(payload, open_orders, account, observation=observation).summary()
    print(json.dumps(summary, indent=2, default=str))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
