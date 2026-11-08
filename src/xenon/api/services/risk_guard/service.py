"""Risk guard loop (spec §5.1-§5.3): read, evaluate, notify. Never orders.

One ``tick`` per second:

1. read the IB quotes and update each source (``sources``);
2. every ``ACCOUNT_REFRESH_S`` read the latest account snapshot, and every
   ``BASIS_REFRESH_S`` the drawdown basis; feed leverage / drawdown alarms;
3. collect BLIND causes: market feed health (RTH) plus system causes in the
   watch window (trading day, 09:00 ET to the close);
4. step the incident book; send each event (push + email), then record it in
   ``risk_events`` best-effort (never under ``XENON_READ_ONLY``);
5. every ``HEARTBEAT_S`` post the dead-man heartbeat, when configured.

IO edges are injected so the loop is testable without IB or Postgres.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import date, datetime, timezone
from datetime import time as dtime
from typing import Any, Awaitable, Callable

from xenon.api.services.risk_guard import config as rg_config
from xenon.api.services.risk_guard.account import AccountView, DrawdownBasis, account_view, drawdown
from xenon.api.services.risk_guard.incidents import (
    CRASH,
    FAST,
    TIER_NAMES,
    Event,
    IncidentBook,
    MarketInputs,
    RatioAlarm,
    Thresholds,
    daily_line_due,
    market_tier,
)
from xenon.api.services.risk_guard.sources import Quote, SourceState, agree, feed_health
from xenon.api.services.risk_guard.windows import day_return
from xenon.utils import market_calendar

logger = logging.getLogger(__name__)

TICK_S = 1.0
ACCOUNT_REFRESH_S = 30.0
BASIS_REFRESH_S = 600.0
HEARTBEAT_S = 30.0
SNAPSHOT_STALE_S = 120.0
DB_TIMEOUT_S = 5.0
OPEN_GRACE_S = 60.0
WATCH_FROM = dtime(9, 0)
WINDOWS = {"5m": 300, "15m": 900, "30m": 1800, "60m": 3600}

SnapshotFn = Callable[[Any], Awaitable["tuple[dict, datetime] | None"]]
BasisFn = Callable[[Any, date], Awaitable[DrawdownBasis]]
RecordFn = Callable[..., Awaitable[None]]


def in_watch_window(wall: datetime) -> bool:
    """Trading day, 09:00 ET up to the regular close (system BLIND causes alarm here)."""
    et = wall.astimezone(market_calendar.ET)
    try:
        if not market_calendar.is_trading_day(et.date()):
            return False
        close = market_calendar.session_close(et.date())
    except market_calendar.CalendarYearMissing:
        return et.weekday() < 5 and WATCH_FROM <= et.time() < market_calendar.MARKET_CLOSE
    return WATCH_FROM <= et.time() < close


class RiskGuard:
    def __init__(
        self,
        *,
        feed: Any,
        scope_fn: Callable[[], Any],
        snapshot_fn: SnapshotFn,
        basis_fn: BasisFn,
        record_fn: RecordFn,
        health_fn: Callable[[datetime], dict[str, str]],
        notifier: Any,
        heartbeat_fn: Callable[[dict], Awaitable[bool]] | None = None,
        read_only_fn: Callable[[], bool] = lambda: False,
        enabled_fn: Callable[[], bool] = rg_config.enabled,
        thresholds_fn: Callable[[], Thresholds] = rg_config.thresholds,
        levels_fn: Callable[[], tuple[float, ...]] = rg_config.key_levels,
        extra_fn: Callable[[], tuple[str, ...]] = rg_config.watch_symbols,
        mono: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.feed, self.scope_fn, self.notifier = feed, scope_fn, notifier
        self.snapshot_fn, self.basis_fn, self.record_fn, self.health_fn = snapshot_fn, basis_fn, record_fn, health_fn
        self.heartbeat_fn, self.read_only_fn, self.enabled_fn = heartbeat_fn, read_only_fn, enabled_fn
        self.thresholds_fn, self.levels_fn, self.mono, self.wall = thresholds_fn, levels_fn, mono, wall
        self.extra_fn = extra_fn
        th = Thresholds()
        self.sources = {"SPX": SourceState("SPX"), "SPY": SourceState("SPY")}
        self.extra: dict[str, SourceState] = {}  # single-source indexes, own incidents, no BLIND
        self.book = IncidentBook(th)
        self.leverage = RatioAlarm("leverage", th.leverage_levels, th.leverage_step, higher_is_worse=True)
        self.drawdown = RatioAlarm("drawdown", th.drawdown_levels, th.drawdown_step, higher_is_worse=False)
        self.snapshot: tuple[dict, datetime] | None = None
        self.basis: DrawdownBasis | None = None
        self.account: AccountView | None = None
        self.view_error: str | None = None
        self.health: dict[str, str] = {}
        self.inputs = MarketInputs()
        self.tier = 0
        self.db_ok = True
        self.daily_sent: date | None = None
        self.last_result: dict[str, Any] = {}
        self.last_tick_at: str | None = None
        self._acct_at = self._basis_at = self._beat_at = -1e18
        self._pending: set[asyncio.Task] = set()

    # ── evaluation ───────────────────────────────────────────────────

    async def tick(self) -> list[Event]:
        if not self.enabled_fn():
            return []
        th = self.thresholds_fn()
        self.book.th = th
        for alarm, levels, step in (
            (self.leverage, th.leverage_levels, th.leverage_step),
            (self.drawdown, th.drawdown_levels, th.drawdown_step),
        ):
            alarm.levels, alarm.step = levels, step
        now, wall = self.mono(), self.wall()
        rth = market_calendar.is_open(wall, strict=False)
        try:
            eq_session = market_calendar.us_equity_session(wall)
        except market_calendar.CalendarYearMissing:
            eq_session = "closed"
        extra = tuple(self.extra_fn())
        self.extra = {s: self.extra.get(s) or SourceState(s) for s in extra}
        quotes: dict[str, Quote] = await self.feed.read(extra)
        spx_session = "rth" if rth else "closed"  # cash indexes print in RTH only
        for name, src in {**self.sources, **self.extra}.items():
            src.observe(now, wall, quotes.get(name), eq_session if name == "SPY" else spx_session)

        scope = self.scope_fn()
        events: list[Event] = []
        if scope is not None and now - self._acct_at >= ACCOUNT_REFRESH_S:
            self._acct_at = now
            await self._refresh_account(scope, wall, now, events)
        self.account = self._view(wall, now)
        guard_errors = {"ACCOUNT_UNREADABLE": self.view_error} if self.view_error else {}

        # The cash index first prints at the open: give it OPEN_GRACE_S before
        # "no tick" counts (else SOURCE_STALE pages every morning at 09:30).
        et = wall.astimezone(market_calendar.ET)
        after_open = (et - et.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() >= OPEN_GRACE_S
        health = feed_health(list(self.sources.values()), now, rth=rth and after_open)
        health.update(guard_errors)
        if in_watch_window(wall):
            try:
                health.update(self.health_fn(wall))
            except Exception as exc:  # noqa: BLE001
                health["GUARD_ERROR"] = f"system health check failed ({type(exc).__name__})"
            if not self.db_ok:
                health["DB_DOWN"] = "account snapshot read failed"
            age = self.account.snapshot_age_s if self.account else None
            if rth and scope is not None and (age is None or age > SNAPSHOT_STALE_S):
                health["SNAPSHOT_STALE"] = "no IB account snapshot" if age is None else f"IB snapshot {age:.0f} s old"
        self.health = health

        self.inputs = self._market_inputs(now, blind=bool(health))
        self.tier, reasons = market_tier(self.inputs, th)
        extras = {}
        for sym, src in self.extra.items():
            m = self._single_inputs(src, now)
            t, r = market_tier(m, th)
            extras[sym] = (min(t, FAST), r, m)  # one source, no cross-check: FAST at most
        events = self.book.step(now, (self.tier, reasons), self.inputs, health, extras) + events

        if self.account is not None and daily_line_due(et, self.daily_sent):
            self.daily_sent = et.date()
            events.append(Event("ratio:daily", "daily", 0, (), f"ratio:daily:{et.date()}"))

        for ev in events:
            self._dispatch(ev, scope)
        if self.heartbeat_fn is not None and now - self._beat_at >= HEARTBEAT_S:
            self._beat_at = now
            self._spawn(self.heartbeat_fn(self.heartbeat_payload(wall, now)))
        self.last_tick_at = wall.isoformat()
        return events

    async def _refresh_account(self, scope: Any, wall: datetime, now: float, events: list[Event]) -> None:
        try:
            # ponytail: inline with a deadline, so a black-holed DB stalls one
            # tick by <= DB_TIMEOUT_S every 30 s (ages are monotonic, no false
            # stale); a background task if that ever matters.
            async with asyncio.timeout(DB_TIMEOUT_S):
                snap = await self.snapshot_fn(scope)
        except Exception as exc:  # noqa: BLE001 — a DB failure is a BLIND cause, never a crash
            if self.db_ok:
                logger.error("risk guard account read failed: %s", type(exc).__name__)
            self.db_ok = False
            return
        self.db_ok = True
        if now - self._basis_at >= BASIS_REFRESH_S:
            # Own budget: a slow drawdown basis keeps the good snapshot and
            # only leaves drawdown on the last basis (retried every refresh).
            try:
                async with asyncio.timeout(DB_TIMEOUT_S):
                    self.basis = await self.basis_fn(scope, wall.astimezone(market_calendar.ET).date())
                self._basis_at = now
            except Exception as exc:  # noqa: BLE001
                logger.warning("risk guard drawdown basis read failed: %s", type(exc).__name__)
        is_new = snap is not None and (self.snapshot is None or snap[1] != self.snapshot[1])
        self.snapshot = snap
        if not is_new:
            return
        view = self._view(wall, now)
        if view is None:
            return
        for alarm, value in ((self.leverage, view.leverage), (self.drawdown, self._drawdown(view))):
            ev = alarm.update(value, now)
            if ev is not None:
                events.append(ev)

    def _spots(self, now: float) -> dict[str, float]:
        return {n: s.last for n, s in self.sources.items() if s.fresh(now) and s.last}

    def _view(self, wall: datetime, now: float) -> AccountView | None:
        """Never raises: a bad snapshot must not silence the market channel."""
        self.view_error = None
        if self.snapshot is None:
            return None
        payload, at = self.snapshot
        try:
            return account_view(payload, at, wall, self._spots(now))
        except Exception as exc:  # noqa: BLE001
            self.view_error = f"account snapshot could not be evaluated ({type(exc).__name__})"
            return None

    def _drawdown(self, view: AccountView | None) -> float | None:
        return drawdown(view.nlv if view else None, self.basis.peak if self.basis else None)

    @staticmethod
    def _single_inputs(s: SourceState, now: float) -> MarketInputs:
        if not s.fresh(now):
            return MarketInputs()
        return MarketInputs(
            ret_15m=s.window.ret(now, WINDOWS["15m"]),
            ret_30m=s.window.ret(now, WINDOWS["30m"]),
            ret_60m=s.window.ret(now, WINDOWS["60m"]),
            day=day_return(s.last, s.prior_close) if s.window.session == "rth" else None,
            session_low=s.window.low,
        )

    def _market_inputs(self, now: float, *, blind: bool) -> MarketInputs:
        fresh = [s for s in self.sources.values() if s.fresh(now)]
        n = len(self.sources)

        def metric(fn) -> float | None:
            # A fresh source still warming its window (after a gap) has no
            # value and does not veto; a stale or absent source does, unless
            # BLIND. Else one 25 s SPX gap would disable the 60 m trigger for
            # an hour with no BLIND cause.
            vals = [v for v in (fn(s) for s in fresh) if v is not None]
            if not vals:
                return None
            return max(vals) if len(fresh) == n else agree(vals + [None] * (n - len(fresh)), blind=blind)

        spx = self.sources["SPX"]
        levels: tuple[float, ...] = ()
        if spx.fresh(now) and spx.last is not None and spx.window.high is not None:
            levels = tuple(x for x in self.levels_fn() if spx.window.high >= x > spx.last)
        acct = self.account
        return MarketInputs(
            ret_15m=metric(lambda s: s.window.ret(now, WINDOWS["15m"])),
            ret_30m=metric(lambda s: s.window.ret(now, WINDOWS["30m"])),
            ret_60m=metric(lambda s: s.window.ret(now, WINDOWS["60m"])),
            day=metric(lambda s: day_return(s.last, s.prior_close) if s.window.session == "rth" else None),
            levels_broken=levels,
            min_short_distance=acct.min_short_distance if acct else None,
            long_itm=bool(acct and acct.long_itm),
            cushion=acct.cushion if acct else None,
            session_low=spx.window.low if spx.fresh(now) else None,
            shorts_crossed=acct.shorts_crossed if acct else 0,
        )

    # ── notification ─────────────────────────────────────────────────

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _dispatch(self, ev: Event, scope: Any) -> None:
        try:
            title, body = self.render(ev, scope)
        except Exception as exc:  # noqa: BLE001 — a render bug must not drop the alert
            logger.exception("risk guard render failed")
            title = f"RISK {ev.key} {ev.kind} (render failed: {type(exc).__name__})"
            body = "; ".join(ev.reasons) or ev.key
        self._spawn(self._send(ev, title, body, scope))

    async def _send(self, ev: Event, title: str, body: str, scope: Any) -> None:
        result = await self.notifier.send(title, body, priority(ev), tags=tags(ev))
        self.last_result = {"at": self.wall().isoformat(), "key": ev.key, "kind": ev.kind, **asdict(result)}
        if result.push != "sent" and result.email != "sent":
            logger.error(
                "risk guard: %s %s reached no channel (push=%s email=%s)", ev.key, ev.kind, result.push, result.email
            )
        await self._record(ev, scope, {"title": title, "reasons": list(ev.reasons), **asdict(result)})

    async def _record(self, ev: Event, scope: Any, detail: dict[str, Any]) -> bool:
        if scope is None or self.read_only_fn():
            return False
        try:
            await self.record_fn(
                scope,
                incident_key=ev.key,
                incident_id=ev.incident_id,
                kind=ev.kind,
                severity=ev.severity,
                detail=detail,
            )
        except Exception as exc:  # noqa: BLE001 — record is best-effort; the alert already went out
            logger.error("risk guard: risk_events write failed: %s", type(exc).__name__)
            self.db_ok = False
            return False
        return True

    async def ack(self, key: str) -> str:
        """Persist the ack, then apply it. ``ok`` | ``unknown`` | ``unavailable`` (no DB: alerts go on)."""
        inc = self.book.incidents.get(key)
        if inc is None:
            return "unknown"
        ev = Event(key, "ack", inc.severity, inc.reasons, inc.incident_id)
        if not await self._record(ev, self.scope_fn(), {"reasons": list(inc.reasons)}):
            return "unavailable"
        self.book.ack(key, self.mono())
        return "ok"

    async def drain(self) -> None:
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)

    def render(self, ev: Event, scope: Any) -> tuple[str, str]:
        test = "" if scope is not None and getattr(scope, "account_env", None) == "live" else "[TEST] "
        now = self.mono()
        a = self.account
        if ev.key == "ratio:daily":
            title = f"{test}Daily risk ratios"
        elif ev.key.startswith("ratio:"):
            title = f"{test}RATIO {ev.reasons[0] if ev.reasons else ev.key} ({['ok', 'warn', 'severe', 'critical'][min(ev.severity, 3)]})"
        elif ev.key.startswith("health:"):
            title = f"{test}BLIND {ev.key[7:]}: stops and alerts may be blind"
        else:
            sym = ev.key[7:] if ev.key.startswith("market:") else "SPX"
            title = f"{test}{TIER_NAMES[ev.severity]} {sym} " + (", ".join(ev.reasons[:2]) or "")
        lines = [f"{title.removeprefix(test)}  [{ev.kind} {ev.incident_id}]"]
        if ev.reasons:
            lines.append("Why: " + "; ".join(ev.reasons))
        for s in {**self.sources, **self.extra}.values():
            age = s.age(now)
            lines.append(
                f"{s.name} {s.last if s.last is not None else '---'} (tick age {'never' if age is None else f'{age:.0f} s'})"
            )
        m = self.inputs
        lines.append(
            "Speed: "
            + " · ".join(
                f"{k} {_pct(v)}"
                for k, v in (("15m", m.ret_15m), ("30m", m.ret_30m), ("60m", m.ret_60m), ("day", m.day))
            )
        )
        if m.levels_broken:
            lines.append("Levels broken: " + ", ".join(f"{x:g}" for x in m.levels_broken))
        if a is not None:
            dd = self._drawdown(a)
            basis = self.basis
            dd_note = "" if basis is None or basis.adjusted else " (unadjusted for deposits)"
            lines.append(
                f"Account (snapshot {a.snapshot_age_s:.0f} s old): NLV {_usd(a.nlv)} · cushion {_pct(a.cushion, sign=False)}"
                f" · leverage {_x(a.leverage)} · width/NLV {_x(a.width_ratio)} · drawdown {_pct(dd)}{dd_note}"
            )
            shorts = sorted((d for d in a.distances if d.side == "short"), key=lambda d: d.distance)[:4]
            for d in shorts:
                lines.append(
                    f"  short {d.ticker} {d.expiry} {d.strike:g}{d.right}: {_pct(d.distance)} {'ITM' if d.distance < 0 else 'away'}"
                )
            if a.undefined_positions or a.positions_without_spot:
                lines.append(
                    f"  not measured: {a.undefined_positions} unbounded/unreadable, {a.positions_without_spot} without spot"
                )
        if self.health:
            lines.append("Data health: " + "; ".join(f"{k} {v}" for k, v in self.health.items()))
        lines.append("Protection: none automatic (P1 alerts only). Close in Xenon or TWS.")
        if ev.kind not in ("daily", "cross", "step") and ev.key in self.book.incidents:
            lines.append(f"Ack: Xenon risk banner, or POST /risk/incidents/{ev.key}/ack")
        return title, "\n".join(lines)

    # ── state ────────────────────────────────────────────────────────

    def heartbeat_payload(self, wall: datetime, now: float) -> dict[str, Any]:
        return {
            "at": wall.isoformat(),
            "tier": TIER_NAMES[self.tier],
            "blind": sorted(self.health),
            "sources": {n: s.age(now) for n, s in self.sources.items()},
        }

    def state(self) -> dict[str, Any]:
        now = self.mono()
        a = self.account
        return {
            "enabled": self.enabled_fn(),
            "last_tick_at": self.last_tick_at,
            "tier": TIER_NAMES[self.tier],
            "health": self.health,
            "incidents": [
                {
                    "key": i.key,
                    "incident_id": i.incident_id,
                    "severity": "BLIND" if i.key.startswith("health:") else TIER_NAMES.get(i.severity, str(i.severity)),
                    "reasons": list(i.reasons),
                    "acked": i.acked_at is not None,
                    "clearing": i.clear_since is not None,
                }
                for i in self.book.incidents.values()
            ],
            "sources": {
                n: {
                    "last": s.last,
                    "prior_close": s.prior_close,
                    "age_s": s.age(now),
                    "fresh": s.fresh(now),
                    "session": s.window.session,
                    "rejected": s.rejected,
                    "returns": {k: s.window.ret(now, w) for k, w in WINDOWS.items()},
                    "day": day_return(s.last, s.prior_close),
                }
                for n, s in {**self.sources, **self.extra}.items()
            },
            "account": None
            if a is None
            else {
                **{k: v for k, v in asdict(a).items() if k != "distances"},
                "drawdown": self._drawdown(a),
                "drawdown_adjusted": self.basis.adjusted if self.basis else None,
                "flows_since": self.basis.flows_since.isoformat() if self.basis and self.basis.flows_since else None,
                "peak": self.basis.peak if self.basis else None,
                "peak_30d": self.basis.peak_30d if self.basis else None,
                "nearest_shorts": [
                    asdict(d)
                    for d in sorted((d for d in a.distances if d.side == "short"), key=lambda d: d.distance)[:6]
                ],
            },
            "ratio_levels": {"leverage": self.leverage.level, "drawdown": self.drawdown.level},
            "db_ok": self.db_ok,
            "last_notification": self.last_result,
        }


def priority(ev: Event) -> int:
    if ev.kind == "daily":
        return 2
    if ev.key.startswith("health:"):
        return 5
    return {0: 3, 1: 3, 2: 4}.get(ev.severity, 5) if ev.severity < CRASH else 5


def tags(ev: Event) -> tuple[str, ...]:
    if ev.key.startswith("health:"):
        return ("no_entry",)
    if ev.key.startswith("ratio:"):
        return ("bar_chart",)
    return {1: ("eyes",), 2: ("warning",), 3: ("rotating_light",)}.get(ev.severity, ())


def _pct(v: float | None, sign: bool = True) -> str:
    return "n/a" if v is None else (f"{v * 100:+.2f}%" if sign else f"{v * 100:.2f}%")


def _x(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.2f}x"


def _usd(v: float | None) -> str:
    return "n/a" if v is None else f"${v:,.0f}"


async def run_loop(guard: RiskGuard, *, tick_s: float = TICK_S) -> None:
    """Forever loop; one failed tick never stops the task."""
    while True:
        started = time.monotonic()
        try:
            await guard.tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("risk guard tick failed")
        await asyncio.sleep(max(0.05, tick_s - (time.monotonic() - started)))
