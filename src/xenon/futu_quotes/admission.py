"""Desired-set → assign partition state machine (no I/O).

Implements the admission half of ``docs/architecture/futu-quotes.md``:

- For the latest ``want`` the partition ``ok`` / ``pending`` / ``rejected``
  is disjoint and complete.
- ``pending.reason``: ``subscribing`` | ``session`` | ``opend_down`` |
  ``backoff``; ``rejected.reason``: ``quota`` | ``unknown_symbol`` |
  ``not_supported`` | ``provider_error``.
- Ceiling = min(FUTU_QUOTE_QUOTA, own_used + remain). It is a local limit;
  a provider rejection is authoritative.
- Unsubscribe only when a symbol left the desired set, ≥ ``MIN_LIFETIME_S``
  after it was subscribed, plus a further ``UNSUB_GRACE_S`` grace.
- Re-admission of pending/rejected symbols is re-evaluated with bounded
  exponential backoff.

All timing goes through the injected ``now`` clock so tests drive it
deterministically.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

MIN_LIFETIME_S = 60.0
UNSUB_GRACE_S = 60.0
BACKOFF_BASE_S = 5.0
BACKOFF_CAP_S = 120.0
UNSUB_RETRY_S = 15.0
UNSUB_RETRY_CAP_S = 120.0

PENDING_REASONS = ("subscribing", "session", "opend_down", "backoff")
REJECTED_REASONS = ("quota", "unknown_symbol", "not_supported", "provider_error")

_OPEN_US_STATES = frozenset({"MORNING", "AFTERNOON"})


def futu_code(symbol: str) -> str:
    """Plain relay ticker → Futu code. ``BRK.B`` → ``US.BRK.B``."""
    return f"US.{symbol}"


def ticker_of(code: str) -> Optional[str]:
    """Futu code → plain ticker; ``None`` for non-US codes."""
    if isinstance(code, str) and code.startswith("US.") and len(code) > 3:
        return code[3:]
    return None


@dataclass
class SymbolState:
    symbol: str
    code: str
    verify: str = "unverified"  # unverified | ok | unknown_symbol | not_supported
    verify_inflight: bool = False
    subscribed_at: Optional[float] = None
    subscribing: bool = False
    # In-flight op reservations: one outstanding provider call per symbol per
    # kind at a time; the op's pre-check re-validates demand under the adapter
    # lock before anything is sent.
    unsub_inflight: bool = False
    # Believed-owned after an OpenD reconnect: needs an idempotent reconfirm
    # that must NOT reserve a second quota unit (the SDK restores its own subs).
    reconfirm: bool = False
    # The in-flight subscribe was issued as a reconfirm, not a new admission.
    subscribing_reconfirm: bool = False
    rejected_reason: Optional[str] = None
    retry_at: float = 0.0
    backoff_step: int = 0
    unwanted_since: Optional[float] = None
    unsub_retry_at: Optional[float] = None
    unsub_attempts: int = 0

    @property
    def subscribed(self) -> bool:
        return self.subscribed_at is not None


@dataclass
class Actions:
    verify: list[str] = field(default_factory=list)
    subscribe: list[str] = field(default_factory=list)
    unsubscribe: list[str] = field(default_factory=list)

    def any(self) -> bool:
        return bool(self.verify or self.subscribe or self.unsubscribe)


@dataclass
class EvalResult:
    partition: dict  # {ok: [...], pending: [{symbol,reason}], rejected: [{symbol,reason}]}
    actions: Actions
    changed: bool


class Admission:
    """Pure admission state; the service performs the emitted actions."""

    def __init__(
        self,
        *,
        quota_cap: int = 800,
        now: Any = time.monotonic,
        min_lifetime_s: float = MIN_LIFETIME_S,
        unsub_grace_s: float = UNSUB_GRACE_S,
        backoff_base_s: float = BACKOFF_BASE_S,
        backoff_cap_s: float = BACKOFF_CAP_S,
        unsub_retry_s: float = UNSUB_RETRY_S,
        unsub_retry_cap_s: float = UNSUB_RETRY_CAP_S,
    ) -> None:
        self.now = now
        self.quota_cap = quota_cap
        self.min_lifetime_s = min_lifetime_s
        self.unsub_grace_s = unsub_grace_s
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.unsub_retry_s = unsub_retry_s
        self.unsub_retry_cap_s = unsub_retry_cap_s

        self.gen = 0
        # OpenD session epoch: bumped on every disconnect and (re)connect.
        # Provider ops and callbacks carrying an older epoch are discarded.
        self.epoch = 0
        self.desired: list[str] = []
        self.session_open = False
        self.opend_connected = False
        self.symbols: dict[str, SymbolState] = {}

        self.provider_own_used: Optional[int] = None
        self.provider_remain: Optional[int] = None
        self.provider_all_used: Optional[int] = None
        self.refresh_quota_requested = False

        self._last_key: Optional[tuple] = None

    # ------------------------------------------------------------------
    # wanted set
    # ------------------------------------------------------------------

    def state(self, symbol: str) -> SymbolState:
        st = self.symbols.get(symbol)
        if st is None:
            st = SymbolState(symbol=symbol, code=futu_code(symbol))
            self.symbols[symbol] = st
        return st

    def set_desired(self, gen: int, symbols: list[str]) -> None:
        """Accept a ``want`` frame's set; newly unwanted symbols start the
        unsubscribe grace clock."""
        self.gen = gen
        seen: set[str] = set()
        deduped: list[str] = []
        for s in symbols:
            if s not in seen:
                seen.add(s)
                deduped.append(s)
        now = self.now()
        prev_desired = set(self.desired)
        self.desired = deduped
        for s in deduped:
            st = self.state(s)
            if s not in prev_desired:
                st.unwanted_since = None
                st.unsub_retry_at = None
                st.unsub_attempts = 0
        for s in prev_desired - seen:
            st = self.symbols[s]
            if st.unwanted_since is None:
                st.unwanted_since = now

    def release_desired(self) -> None:
        """Owner connection lost: desired set released via normal grace."""
        now = self.now()
        self.desired = []
        for st in self.symbols.values():
            if st.subscribed and st.unwanted_since is None:
                st.unwanted_since = now

    # ------------------------------------------------------------------
    # environment
    # ------------------------------------------------------------------

    def set_session_open(self, open_: bool) -> None:
        self.session_open = open_

    @staticmethod
    def session_is_open(market_us: Any) -> bool:
        """get_global_state().market_us → RTH flag; unknown ⇒ closed."""
        return str(market_us or "").upper() in _OPEN_US_STATES

    def mark_disconnected(self) -> None:
        self.epoch += 1
        self.opend_connected = False
        self.session_open = False
        self.provider_own_used = None
        self.provider_remain = None
        self.provider_all_used = None
        for st in self.symbols.values():
            st.subscribing = False
            st.subscribing_reconfirm = False
            st.verify_inflight = False
            st.unsub_inflight = False

    def mark_connected(self) -> None:
        """Reconnect: never assume subscriptions survived.

        Wanted symbols believed subscribed become ``reconfirm``: an
        idempotent resubscribe that does not reserve a second quota unit —
        the provider may already count them (the SDK restores its own subs).
        Unwanted-but-believed-subscribed symbols get an immediate
        unsubscribe attempt; if OpenD already dropped them the
        ``not_subscribed`` reply clears the record the same as a success.
        """
        self.epoch += 1
        self.opend_connected = True
        for st in self.symbols.values():
            if not st.subscribed:
                continue
            if st.symbol in self.desired:
                st.subscribed_at = None
                st.reconfirm = True
            else:
                st.unsub_retry_at = 0.0

    def ceiling(self) -> int:
        """Local admission ceiling; 0 while OpenD is down or remain unknown
        and provider truth was never seen."""
        if not self.opend_connected:
            return 0
        if self.provider_remain is None and self.provider_own_used is None:
            return self.quota_cap
        return min(
            self.quota_cap,
            (self.provider_own_used or 0) + (self.provider_remain or 0),
        )

    def own_used(self) -> int:
        """Local believed subscription count."""
        return sum(1 for st in self.symbols.values() if st.subscribed)

    def controlled_used(self) -> int:
        """own_used + in-flight NEW admissions (the ≤ ceiling invariant).

        Reconfirms are excluded: a believed-owned sub is already inside
        ``own_used``/provider counts; charging it twice would deadlock
        recovery at the ceiling.
        """
        inflight = sum(
            1 for st in self.symbols.values() if st.subscribing and not st.subscribing_reconfirm
        )
        return max(self.provider_own_used or 0, self.own_used()) + inflight

    def quota_report(self) -> dict:
        """health frame quota block."""
        if not self.opend_connected:
            return {"ceiling": 0, "own_used": 0, "all_used": None, "remain": None}
        return {
            "ceiling": self.ceiling(),
            "own_used": self.own_used(),
            "all_used": self.provider_all_used,
            "remain": self.provider_remain,
        }

    def apply_quota_snapshot(self, data: dict) -> None:
        """Ingest query_subscription(is_all_conn=True) numbers.

        Only the counts are used: ``sub_list`` is merged across all
        connections, so it cannot attribute codes to this connection. Own
        subscription truth stays local; an unsubscribe of a code OpenD
        already dropped just errors and is dropped after bounded retries.
        """
        self.provider_own_used = _as_int(data.get("own_used"))
        self.provider_remain = _as_int(data.get("remain"))
        self.provider_all_used = _as_int(data.get("total_used"))

    # ------------------------------------------------------------------
    # evaluation → partition + actions
    # ------------------------------------------------------------------

    def evaluate(self, *, emit_always: bool = False) -> EvalResult:
        now = self.now()
        ok: list[str] = []
        pending: list[dict] = []
        rejected: list[dict] = []
        actions = Actions()

        for sym in self.desired:
            st = self.state(sym)
            if st.verify == "unverified":
                if not self.opend_connected:
                    pending.append({"symbol": sym, "reason": "opend_down"})
                elif st.verify_inflight:
                    pending.append({"symbol": sym, "reason": "subscribing"})
                elif now < st.retry_at:
                    pending.append({"symbol": sym, "reason": "backoff"})
                else:
                    st.verify_inflight = True
                    actions.verify.append(sym)
                    pending.append({"symbol": sym, "reason": "subscribing"})
                continue
            if st.verify == "unknown_symbol":
                rejected.append({"symbol": sym, "reason": "unknown_symbol"})
                continue
            if st.verify == "not_supported":
                rejected.append({"symbol": sym, "reason": "not_supported"})
                continue
            # verified ok
            if not self.opend_connected:
                pending.append({"symbol": sym, "reason": "opend_down"})
            elif not self.session_open:
                pending.append({"symbol": sym, "reason": "session"})
            elif st.subscribed:
                ok.append(sym)
            elif st.subscribing:
                pending.append({"symbol": sym, "reason": "subscribing"})
            elif now < st.retry_at:
                reason = st.rejected_reason or "backoff"
                if reason in REJECTED_REASONS:
                    rejected.append({"symbol": sym, "reason": reason})
                else:
                    pending.append({"symbol": sym, "reason": "backoff"})
            elif self.controlled_used() >= self.ceiling() and not st.reconfirm:
                st.rejected_reason = "quota"
                st.retry_at = now + self._backoff(st)
                st.backoff_step += 1
                rejected.append({"symbol": sym, "reason": "quota"})
            else:
                st.subscribing = True
                st.subscribing_reconfirm = st.reconfirm
                st.reconfirm = False
                st.rejected_reason = None
                actions.subscribe.append(sym)
                pending.append({"symbol": sym, "reason": "subscribing"})

        # Unsubscribe pass: subscribed but no longer desired. The op is
        # RESERVED per symbol before it is emitted (unsub_inflight) so a
        # due op can never fan out across eval passes.
        for st in self.symbols.values():
            if not st.subscribed or st.symbol in self.desired or st.unsub_inflight:
                continue
            if st.unwanted_since is None:
                st.unwanted_since = now
            due = max(
                (st.subscribed_at or 0.0) + self.min_lifetime_s,
                st.unwanted_since + self.unsub_grace_s,
            )
            if st.unsub_retry_at is not None:
                due = max(due, st.unsub_retry_at)
            if now >= due and self.opend_connected:
                st.unsub_inflight = True
                actions.unsubscribe.append(st.symbol)

        partition = {"ok": ok, "pending": pending, "rejected": rejected}
        key = (self.gen, _freeze(partition))
        changed = emit_always or key != self._last_key
        self._last_key = key
        return EvalResult(partition=partition, actions=actions, changed=changed)

    def assign_frame(self, partition: dict) -> dict:
        return {
            "type": "assign",
            "gen": self.gen,
            "ok": list(partition["ok"]),
            "pending": list(partition["pending"]),
            "rejected": list(partition["rejected"]),
        }

    def _backoff(self, st: SymbolState) -> float:
        return min(self.backoff_base_s * (2**st.backoff_step), self.backoff_cap_s)

    # ------------------------------------------------------------------
    # action results
    # ------------------------------------------------------------------

    def verify_result(self, symbol: str, outcome: str) -> None:
        """outcome: ok | unknown | not_supported | transient."""
        st = self.state(symbol)
        st.verify_inflight = False
        if outcome == "ok":
            st.verify = "ok"
            st.rejected_reason = None
            st.retry_at = 0.0
            st.backoff_step = 0
        elif outcome in ("unknown_symbol", "not_supported"):
            st.verify = outcome
        else:  # transient
            st.verify = "unverified"
            st.retry_at = self.now() + self._backoff(st)
            st.backoff_step += 1

    def subscribe_result(self, symbol: str, err: Optional[str], reason: Optional[str] = None) -> None:
        """Record a provider subscribe outcome. A late completion still
        lands: the subscription is real at OpenD whether or not the symbol
        is still wanted (assignment re-evaluation decides ``ok``). The
        service discards stale-epoch results before reaching here."""
        st = self.state(symbol)
        st.subscribing = False
        st.subscribing_reconfirm = False
        if err is None:
            st.subscribed_at = self.now()
            st.rejected_reason = None
            st.retry_at = 0.0
            st.backoff_step = 0
            if symbol in self.desired:
                st.unwanted_since = None
            return
        reason = reason or "provider_error"
        if reason == "unknown_symbol":
            st.verify = "unknown_symbol"
        st.rejected_reason = reason
        if reason == "quota":
            self.refresh_quota_requested = True
            st.retry_at = self.now() + self._backoff(st)
            st.backoff_step += 1
        elif reason == "not_supported":
            st.verify = "not_supported"
            st.retry_at = float("inf")
        else:  # provider_error / transient
            st.retry_at = self.now() + self._backoff(st)
            st.backoff_step += 1

    def unsubscribe_result(self, symbol: str, err: Optional[str], kind: Optional[str] = None) -> None:
        """kind: too_early | not_subscribed | provider_error.

        Success — or a "was never subscribed" reply — clears the local
        record. ``too_early`` retries at the 60 s mark; other errors retry
        with bounded escalation. A failed unsubscribe never frees the local
        record: the provider may still hold and count the subscription.
        """
        st = self.state(symbol)
        st.unsub_inflight = False
        if err is None or kind == "not_subscribed":
            st.subscribed_at = None
            st.unsub_retry_at = None
            st.unsub_attempts = 0
            if symbol in self.desired:
                st.unwanted_since = None
            else:
                st.backoff_step = 0
            return
        st.unsub_attempts += 1
        now = self.now()
        if kind == "too_early" and st.subscribed_at is not None:
            # Provider's 60 s clock may disagree with ours; retry at the
            # mark if it is still ahead, else shortly after.
            st.unsub_retry_at = max(
                st.subscribed_at + self.min_lifetime_s, now + 1.0
            )
        else:
            # A failed unsubscribe NEVER releases the local record — the
            # provider may still hold (and count) the subscription. Bounded
            # backoff limits request rate while ownership stays counted.
            delay = min(
                self.unsub_retry_s * (1 << min(st.unsub_attempts - 1, 3)),
                self.unsub_retry_cap_s,
            )
            st.unsub_retry_at = now + delay

    def is_ok(self, symbol: str) -> bool:
        """Producer fence: is ``symbol`` in the current ok set?"""
        return symbol in self.desired and self.state(symbol).subscribed and self.session_open and self.opend_connected


def _as_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _freeze(partition: dict) -> tuple:
    return (
        tuple(partition["ok"]),
        tuple((p["symbol"], p["reason"]) for p in partition["pending"]),
        tuple((r["symbol"], r["reason"]) for r in partition["rejected"]),
    )
