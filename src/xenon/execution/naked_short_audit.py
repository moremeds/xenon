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
no cancel path; enforcement (PR-11) is a separate, opt-in step.

Skip-and-log, never a violation: an incomplete pass view or missing scope
account skips the whole audit; an order without account, an unsupported
instrument, unresolved BAG legs, unknown option metadata or a fractional
quantity skips that order's symbol (as does an unclassifiable holding).
Foreign-account orders are not this scope's demand and are ignored.

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


@dataclass
class AuditResult:
    status: str  # ok | violations | skipped | error
    violations: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    reason: str | None = None

    def summary(self) -> dict:
        return {
            "mode": "shadow",
            "status": self.status,
            "reason": self.reason,
            "violations": self.violations,
            "skipped": self.skipped,
        }


def _key_str(key: coverage.Key) -> str:
    return "|".join(str(part) for part in key)


def _as_int(value) -> int | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else None


def _order_symbols(o: dict, legs) -> frozenset[str]:
    if legs:
        return frozenset(str(leg.sym).upper() for leg in legs if leg.sym)
    sym = str((o.get("contract") or {}).get("symbol") or o.get("symbol") or "").strip().upper()
    return frozenset({sym}) if sym else frozenset()


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


def _to_coverage_order(o: dict, idx: int) -> tuple[coverage.Order | None, frozenset[str], str | None]:
    """Map one scoped live open order to (coverage.Order, symbols, skip_reason)."""
    legs_payload = gate4.legs_payload_from_snapshot_order(o)
    raw = legs_payload["legs"]
    legs = tuple(gate4._leg_from_dict(x) for x in raw) if raw is not None else None
    syms = _order_symbols(o, legs)
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
        sec = (o.get("contract") or {}).get("secType")
        return None, syms, "unresolved_bag_legs" if sec == "BAG" else f"unsupported_instrument:{sec}"
    return order, syms, _unclassifiable(order)


def find_violations(
    payload: dict,
    open_orders: list[dict],
    scope_account: str,
    *,
    view_complete: bool,
    netting_enabled: bool = False,
) -> AuditResult:
    """Pure: which live orders of `scope_account` create naked short exposure.

    An order is a violation when removing it lowers uncovered short calls on
    some key or short stock on some symbol — every contributor is reported
    with its identity (clientId, orderId, permId) for the operator.
    """
    if not view_complete:
        return AuditResult("skipped", reason="view_incomplete")
    if not scope_account:
        return AuditResult("skipped", reason="scope_account_missing")

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
                }
            )
    return AuditResult("violations" if violations else "ok", violations, skipped)


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
            "orders": [{k: v[k] for k in ("symbol", "client_id", "order_id", "perm_id")} for v in result.violations],
        }
    )


def run_shadow_audit(
    payload: dict,
    open_orders: list[dict],
    scope_account: str,
    *,
    view_complete: bool,
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
            view_complete=view_complete,
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
        broker_account=scope_account or None,
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
    account = args.account or (payload.get("observation") or {}).get("account") or ""
    summary = find_violations(payload, open_orders, account, view_complete=True).summary()
    print(json.dumps(summary, indent=2, default=str))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
