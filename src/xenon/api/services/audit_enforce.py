"""Naked-short audit — enforce mode (spec §2.4, Phase B).

Opt-in with ``XENON_AUDIT_CANCEL=1`` (default off: a rollout switch, removed
once enforcement is proven). The sync subprocess only audits (shadow) and
hands its summary to the API in ``ScriptResult.data["naked_short_audit"]``;
the API process — whose ``orders`` connection is the only order owner
(PR-10) — enforces it inside the single-flight sync task.

Rules (the loop in :func:`enforce`):

- Never act on an audit that is not ``ok`` / ``violations`` (skipped,
  stale, incomplete, error or missing), on a placeholder scope account, or
  on an audit of another account.
- ``blocked()`` is checked before every cancel (read-only, owner missing or
  halted, order writes denied): any reason stops the loop, nothing is sent.
- Candidates: violations of the scope account, placed by the owner
  clientId, with a positive permId. Anything else is skipped + logged and
  never attempted (no orderId fallback). Wizard orders are not exempt.
- One order at a time, newest first (highest orderId on the owner client).
- Every target is confirmed by a fresh pass first: `resync()` runs once and
  the same (client_id, order_id, perm_id) must still be an eligible
  violation of an ok/violations audit of the scope account. One pass reads
  positions seconds before open orders, so it can show a just-covered call
  as naked; a target the fresh pass does not confirm is dropped (logged,
  never cancelled) and the next one is chosen from the confirming audit.
- Only a confirmed cancel counts; then the sync re-runs and the audit is
  recomputed before the next choice. A permId the owner cannot find, or an
  order the owner reports already Filled/Cancelled, is skipped and ends the
  trigger; an unconfirmed or failed cancel, or a raising callback, stops the
  loop with health ``error`` (earlier cancels / skips are kept).
- At most :data:`MAX_CANCELS_PER_TRIGGER` cancels and
  :data:`MAX_UNCONFIRMED_PER_TRIGGER` dropped targets per trigger.

Only orders this environment's owner placed can be cancelled; an exposure
created by a TWS / mobile / other-client order is reported, never cancelled.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from xenon.db.service_health import record_service_health
from xenon.execution import coverage

logger = logging.getLogger("xenon.audit_enforce")

ENV_FLAG = "XENON_AUDIT_CANCEL"
SERVICE = "naked_short_audit"
REASON_CODE = "NAKED_SHORT_AUDIT"
AUDIT_KEY = "naked_short_audit"
MAX_CANCELS_PER_TRIGGER = 5
MAX_UNCONFIRMED_PER_TRIGGER = 3

# Cancel outcomes the caller's `cancel` returns. GONE = the owner found the
# order already Filled/Cancelled (nothing sent).
CONFIRMED, NOT_FOUND, GONE, FAILED, UNCERTAIN = "ok", "not_found", "gone", "failed", "uncertain"


def enabled() -> bool:
    return os.environ.get(ENV_FLAG) == "1"


def audit_from(data: Optional[dict]) -> Optional[dict]:
    audit = (data or {}).get(AUDIT_KEY)
    return audit if isinstance(audit, dict) else None


@dataclass
class Outcome:
    state: str  # ok | violations | skipped | error
    audit_status: Optional[str] = None
    reason: Optional[str] = None
    stopped: Optional[str] = None  # why the loop ended early (None = audit clean)
    cancelled: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # enforcement skips (orders)
    violations: int = 0
    # The latest audit's own coverage evidence (same meaning as the shadow
    # row this enforce row replaces): symbols it could not evaluate, and
    # exposure already held with no attributable order.
    audit_skipped: list = field(default_factory=list)
    held_uncovered: dict = field(default_factory=dict)
    held_short_stock: dict = field(default_factory=dict)

    def note_audit(self, audit: Optional[dict]) -> None:
        a = audit if isinstance(audit, dict) else {}
        self.audit_status = a.get("status")
        self.reason = a.get("reason")
        self.audit_skipped = list(a.get("skipped") or [])
        self.held_uncovered = dict(a.get("held_uncovered") or {})
        self.held_short_stock = dict(a.get("held_short_stock") or {})

    def health_detail(self) -> str:
        return json.dumps(
            {
                "mode": "enforce",
                "audit_status": self.audit_status,
                "reason": self.reason,
                "stopped": self.stopped,
                "violations": self.violations,
                # Audit coverage evidence, as in the shadow row.
                "skipped": len(self.audit_skipped),
                "audit_skipped": self.audit_skipped,
                "held_uncovered": self.held_uncovered,
                "held_short_stock": self.held_short_stock,
                # Enforcement.
                "cancelled": len(self.cancelled),
                "orders_cancelled": self.cancelled,
                "enforce_skipped": len(self.skipped),
                "enforce_skipped_orders": self.skipped,
            },
            default=str,
        )


def _ident(v: dict) -> dict:
    return {k: v.get(k) for k in ("symbol", "account", "client_id", "order_id", "perm_id")}


def _positive_int(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 and str(n) == str(value).strip() else None


def _candidates(audit: dict, scope_account: str, owner_client_id: int, seen: set) -> tuple[list[dict], list[dict]]:
    """(eligible newest-first, skipped) from one audit's violations."""
    eligible: list[dict] = []
    skipped: list[dict] = []
    for v in audit.get("violations") or []:
        perm_id = _positive_int(v.get("perm_id"))
        key = (v.get("client_id"), v.get("order_id"), v.get("perm_id"))
        if key in seen:
            continue
        seen.add(key)
        reason = None
        if v.get("account") != scope_account:
            reason = "account_mismatch"
        elif _positive_int(v.get("client_id")) != owner_client_id:
            reason = "not_owned"
        elif perm_id is None:
            reason = "perm_id_missing"
        elif _positive_int(v.get("order_id")) is None:
            reason = "order_id_missing"
        if reason is not None:
            skipped.append({**_ident(v), "reason": reason})
        else:
            eligible.append({**_ident(v), "client_id": owner_client_id, "perm_id": perm_id})
    eligible.sort(key=lambda c: (int(c["order_id"]), c["perm_id"]), reverse=True)
    return eligible, skipped


async def enforce(
    audit: Optional[dict],
    *,
    scope_account: str,
    owner_client_id: int,
    blocked: Callable[[], Optional[str]],
    cancel: Callable[[dict], Awaitable[str]],
    resync: Callable[[], Awaitable[Optional[dict]]],
    max_cancels: int = MAX_CANCELS_PER_TRIGGER,
    max_unconfirmed: int = MAX_UNCONFIRMED_PER_TRIGGER,
) -> Outcome:
    """Cancel naked-short violations one at a time. Never raises: a raising
    callback ends the loop as an ``error`` Outcome with its evidence kept."""
    out = Outcome("skipped")
    if not coverage.is_real_account(scope_account):
        out.stopped = "scope_account_invalid"
        return out
    try:
        await _loop(out, audit, scope_account, owner_client_id, blocked, cancel, resync, max_cancels, max_unconfirmed)
    except Exception as exc:  # noqa: BLE001 — report as an enforce error, never lose the evidence
        out.state, out.stopped = "error", f"exception:{type(exc).__name__}"
        logger.error("naked short enforce: %s; loop stopped", out.stopped, exc_info=True)
    return out


def _usable(out: Outcome, audit: Optional[dict], scope_account: str) -> bool:
    """Record the audit on `out`; False (with state/stopped set) when it
    cannot be acted on."""
    out.note_audit(audit)
    status = out.audit_status
    if status not in ("ok", "violations"):
        out.state = "error" if status == "error" else "skipped"
        out.stopped = "audit_missing" if audit is None else f"audit_{status or 'invalid'}"
        logger.warning("naked short enforce: not acting on audit (%s, %s)", out.stopped, out.reason)
        return False
    if audit.get("account") != scope_account:
        out.state, out.stopped = "skipped", "audit_account_mismatch"
        logger.warning("naked short enforce: audit account %r != scope %r", audit.get("account"), scope_account)
        return False
    out.violations = len(audit.get("violations") or [])
    out.state = "violations" if out.violations else "ok"
    out.stopped = None
    return True


def _key(c: dict) -> tuple:
    return (c["client_id"], int(c["order_id"]), c["perm_id"])


async def _loop(out, audit, scope_account, owner_client_id, blocked, cancel, resync, max_cancels, max_unconfirmed):
    done: set[int] = set()  # permIds cancelled or dropped in this trigger
    unconfirmed = 0
    while True:
        if not _usable(out, audit, scope_account) or not out.violations:
            return
        # Re-evaluated per audit: an order skipped earlier may still be live.
        eligible, skipped = _candidates(audit, scope_account, owner_client_id, set())
        for s in skipped:
            if s not in out.skipped:
                out.skipped.append(s)
                logger.warning("naked short enforce: skipped %s", s)
        eligible = [c for c in eligible if c["perm_id"] not in done]
        if not eligible:
            out.stopped = "no_eligible_order"
            logger.warning("naked short enforce: %d violation(s), none cancellable here", out.violations)
            return
        if len(out.cancelled) >= max_cancels:
            out.stopped = "max_cancels"
            logger.warning(
                "naked short enforce: bound of %d cancels per trigger reached; %d violation(s) left",
                max_cancels,
                out.violations,
            )
            return
        if unconfirmed >= max_unconfirmed:
            out.stopped = "confirm_bound"
            logger.warning("naked short enforce: bound of %d unconfirmed targets reached", max_unconfirmed)
            return
        reason = blocked()
        if reason is not None:
            out.stopped = reason
            logger.warning("naked short enforce: blocked (%s); nothing sent", reason)
            return
        target = eligible[0]

        # Confirm with a fresh pass: one pass can show a just-covered call
        # as naked (its positions are read before its open orders).
        audit = await resync()
        if audit is None:
            out.state, out.stopped = "error", "resync_failed"
            logger.error("naked short enforce: confirming re-sync failed; loop stopped")
            return
        if not _usable(out, audit, scope_account):
            return
        fresh, _ = _candidates(audit, scope_account, owner_client_id, set())
        if _key(target) not in {_key(c) for c in fresh}:
            unconfirmed += 1
            done.add(target["perm_id"])
            out.skipped.append({**target, "reason": "not_confirmed"})
            logger.warning("naked short enforce: %s not confirmed by a fresh pass; dropped", target)
            continue  # choose from the confirming audit
        reason = blocked()
        if reason is not None:
            out.stopped = reason
            logger.warning("naked short enforce: blocked (%s); nothing sent", reason)
            return

        kind = await cancel(target)
        if kind in (NOT_FOUND, GONE):
            why = "perm_id_not_found" if kind == NOT_FOUND else "order_not_working"
            out.skipped.append({**target, "reason": why})
            out.stopped = why
            logger.warning("naked short enforce: %s for %s; skipped, no orderId fallback", why, target)
            return
        if kind != CONFIRMED:
            out.state, out.stopped = "error", f"cancel_{kind}"
            logger.error("naked short enforce: cancel %s for %s; loop stopped", kind, target)
            return
        done.add(target["perm_id"])
        out.cancelled.append(target)
        logger.warning("NAKED SHORT AUDIT (enforce): cancelled %s", target)
        audit = await resync()
        if audit is None:
            out.state, out.stopped = "error", "resync_failed"
            logger.error("naked short enforce: re-sync after cancel failed; loop stopped")
            return


def record_health(outcome: Outcome, scope: dict) -> None:
    error = {"msg": outcome.stopped} if outcome.state == "error" else None
    record_service_health(
        SERVICE,
        outcome.state,
        broker=scope.get("broker"),
        account_env=scope.get("account_env"),
        broker_account=scope.get("broker_account"),
        detail=outcome.health_detail(),
        error=error,
    )
