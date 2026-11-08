"""Tiers, incidents and ratio alarms (spec §5.2-§5.3). Pure state, no IO.

Two channels of state:

- market: one incident ``market`` whose severity is the tier (WATCH < FAST < CRASH);
- health: one incident ``health:<CAUSE>`` per BLIND cause.

Cadence. WATCH sends once. FAST, CRASH and every health incident re-send every
``repeat_s`` until acked. After an ack the incident sends only on a severity
rise (which re-arms the repeats) or on worsening: a session low >= 0.25% under
the low at the last send, a cushion >= 2 pp lower, or one more short strike
crossed. A rise to a tier never sent before is immediate; a return to a tier
already sent (after a drop) waits ``repeat_s`` since the last send, so a value
flapping on a threshold sends at most every ``repeat_s``. A missing reference
value is seeded by the first value seen after the ack. An incident closes
after its condition stays clear for ``clear_s``; a market evaluation with no
available input at all holds the incident (missing data is a BLIND cause,
never "clear"). A closed incident is queued in ``closed`` (recorded, never
sent). ``restore`` re-creates an incident acked before a restart.

Leverage and drawdown are standing conditions (``RatioAlarm``): they send on a
level crossing in either direction and on each further step of worsening.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time

from xenon.utils import market_calendar

WATCH, FAST, CRASH = 1, 2, 3
TIER_NAMES = {0: "OK", WATCH: "WATCH", FAST: "FAST", CRASH: "CRASH"}
DAILY_LINE_AT = time(9, 0)


@dataclass(frozen=True)
class Thresholds:
    watch_15m: float = -0.0075
    watch_day: float = -0.01
    watch_cushion: float = 0.20
    fast_30m: float = -0.015
    fast_day: float = -0.02
    fast_strike_distance: float = 0.005
    fast_cushion: float = 0.12
    crash_day: float = -0.03
    crash_60m: float = -0.025
    crash_cushion: float = 0.08
    leverage_levels: tuple[float, float, float] = (0.50, 0.75, 1.00)
    leverage_step: float = 0.25
    drawdown_levels: tuple[float, float, float] = (-0.05, -0.10, -0.15)
    drawdown_step: float = 0.02
    repeat_s: float = 30.0
    clear_s: float = 300.0
    worsen_low: float = 0.0025
    worsen_cushion: float = 0.02


@dataclass(frozen=True)
class MarketInputs:
    """One evaluation of the market channel. ``None`` = UNAVAILABLE, never a trigger."""

    ret_15m: float | None = None
    ret_30m: float | None = None
    ret_60m: float | None = None
    day: float | None = None
    levels_broken: tuple[float, ...] = ()
    min_short_distance: float | None = None  # fraction of spot; negative = crossed
    long_itm: bool = False
    cushion: float | None = None
    session_low: float | None = None
    shorts_crossed: int = 0


def _le(v: float | None, limit: float) -> bool:
    return v is not None and v <= limit


def _lt(v: float | None, limit: float) -> bool:
    return v is not None and v < limit


def market_tier(m: MarketInputs, th: Thresholds) -> tuple[int, list[str]]:
    """Highest tier any trigger reaches, plus a reason for every trigger that fired."""
    checks = [
        (CRASH, _le(m.day, th.crash_day), f"day {_pct(m.day)}"),
        (CRASH, _le(m.ret_60m, th.crash_60m), f"60m {_pct(m.ret_60m)}"),
        (CRASH, m.long_itm, "long leg ITM"),
        (CRASH, _lt(m.cushion, th.crash_cushion), f"cushion {_pct(m.cushion)}"),
        (FAST, _le(m.ret_30m, th.fast_30m), f"30m {_pct(m.ret_30m)}"),
        (FAST, _le(m.day, th.fast_day), f"day {_pct(m.day)}"),
        (FAST, _le(m.min_short_distance, th.fast_strike_distance), f"short strike {_pct(m.min_short_distance)} away"),
        (FAST, _lt(m.cushion, th.fast_cushion), f"cushion {_pct(m.cushion)}"),
        (WATCH, _le(m.ret_15m, th.watch_15m), f"15m {_pct(m.ret_15m)}"),
        (WATCH, _le(m.day, th.watch_day), f"day {_pct(m.day)}"),
        (WATCH, bool(m.levels_broken), "level broken " + ", ".join(f"{x:g}" for x in m.levels_broken)),
        (WATCH, _lt(m.cushion, th.watch_cushion), f"cushion {_pct(m.cushion)}"),
    ]
    tier, reasons = 0, []
    for level, hit, reason in checks:
        if hit:
            tier = max(tier, level)
            if reason not in reasons:
                reasons.append(reason)
    return tier, reasons


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:+.2f}%"


@dataclass(frozen=True)
class Event:
    key: str
    kind: str  # open | repeat | rise | worsening | cross | step
    severity: int
    reasons: tuple[str, ...]
    incident_id: str


@dataclass
class Incident:
    key: str
    incident_id: str
    severity: int
    sent_severity: int  # tier of the last send; drops with the tier
    last_sent_at: float
    max_sent: int = 0
    reasons: tuple[str, ...] = ()
    acked_at: float | None = None
    clear_since: float | None = None
    ref: MarketInputs | None = None  # inputs at the last send (worsening reference)
    last_inputs: MarketInputs | None = None


@dataclass
class IncidentBook:
    th: Thresholds
    incidents: dict[str, Incident] = field(default_factory=dict)
    epoch: str = ""  # per-process prefix: incident ids stay unique across restarts
    closed: list[Event] = field(default_factory=list)
    _seq: int = 0

    def step(
        self,
        now: float,
        market: tuple[int, list[str]],
        inputs: MarketInputs,
        health: dict[str, str],
        extras: dict[str, tuple[int, list[str], MarketInputs]] | None = None,
    ) -> list[Event]:
        """Advance every incident to ``now``; return the notifications to send.

        ``extras``: per extra index, its own ``market:<SYM>`` incident.
        """
        events: list[Event] = []
        extras = extras or {}
        for key, (tier, reasons), m in [("market", market, inputs)] + [
            (f"market:{sym}", (t, r), m) for sym, (t, r, m) in extras.items()
        ]:
            if tier or _any_input(m):
                self._advance(key, now, tier, tuple(reasons), m, repeats=tier >= FAST, events=events)
            elif key in self.incidents:
                self.incidents[key].clear_since = None
        for key in [k for k in self.incidents if k.startswith("market:") and k[7:] not in extras]:
            self._clear(key, now)
        for cause, detail in health.items():
            self._advance(f"health:{cause}", now, CRASH, (detail,) if detail else (), None, repeats=True, events=events)
        for key in [k for k in self.incidents if k.startswith("health:") and k[7:] not in health]:
            self._clear(key, now)
        return events

    def ack(self, key: str, now: float, ref: MarketInputs | None = None) -> bool:
        """``ref``: the inputs the operator saw when acking (default: the latest)."""
        inc = self.incidents.get(key)
        if inc is None:
            return False
        inc.acked_at = now
        inc.ref = ref if ref is not None else inc.last_inputs
        return True

    def restore(self, key: str, incident_id: str, severity: int, ref: MarketInputs | None, now: float) -> None:
        """An incident acked before a restart: acked at ``now``, no new open. The next
        step alerts on a rise above ``severity`` or a worsening against ``ref``."""
        self.incidents[key] = Incident(key, incident_id, severity, severity, now, severity, acked_at=now, ref=ref)

    def _advance(self, key, now, severity, reasons, inputs, *, repeats, events) -> None:
        if severity == 0:
            self._clear(key, now)
            return
        inc = self.incidents.get(key)
        if inc is None:
            self._seq += 1
            inc = Incident(
                key,
                f"{key}#{self.epoch}{self._seq}",
                severity,
                severity,
                now,
                severity,
                reasons,
                ref=inputs,
                last_inputs=inputs,
            )
            self.incidents[key] = inc
            events.append(Event(key, "open", severity, reasons, inc.incident_id))
            return
        inc.clear_since, inc.severity, inc.reasons, inc.last_inputs = None, severity, reasons, inputs
        if inputs is not None and inc.ref is not None:
            inc.ref = _seed(inc.ref, inputs)
        if severity < inc.sent_severity:
            inc.sent_severity = severity
        kind = None
        if severity > inc.sent_severity:
            if severity > inc.max_sent or now - inc.last_sent_at >= self.th.repeat_s:
                kind, inc.acked_at = "rise", None
        elif inc.acked_at is None:
            if repeats and now - inc.last_sent_at >= self.th.repeat_s:
                kind = "repeat"
        elif inputs is not None and self._worse(inputs, inc.ref):
            kind = "worsening"
        if kind:
            inc.sent_severity, inc.last_sent_at, inc.ref = severity, now, inputs
            inc.max_sent = max(inc.max_sent, severity)
            events.append(Event(key, kind, severity, reasons, inc.incident_id))

    def _clear(self, key: str, now: float) -> None:
        inc = self.incidents.get(key)
        if inc is None:
            return
        if inc.clear_since is None:
            inc.clear_since = now
        elif now - inc.clear_since >= self.th.clear_s:
            del self.incidents[key]
            self.closed.append(Event(key, "close", inc.severity, inc.reasons, inc.incident_id))

    def _worse(self, m: MarketInputs, ref: MarketInputs | None) -> bool:
        if ref is None:
            return False
        if m.session_low is not None and ref.session_low is not None:
            if m.session_low <= ref.session_low * (1 - self.th.worsen_low) + 1e-12:
                return True
        if m.cushion is not None and ref.cushion is not None:
            if m.cushion <= ref.cushion - self.th.worsen_cushion + 1e-12:
                return True
        return m.shorts_crossed > ref.shorts_crossed


def _any_input(m: MarketInputs) -> bool:
    return any(
        v is not None for v in (m.ret_15m, m.ret_30m, m.ret_60m, m.day, m.min_short_distance, m.cushion)
    ) or bool(m.levels_broken or m.long_itm)


def _seed(ref: MarketInputs, now: MarketInputs) -> MarketInputs:
    """Fill reference values that were missing at the ack with the first value seen."""
    fills = {f: getattr(now, f) for f in ("session_low", "cushion") if getattr(ref, f) is None}
    return replace(ref, **fills) if any(v is not None for v in fills.values()) else ref


@dataclass
class RatioAlarm:
    """Standing ratio (leverage or drawdown): send on level change and per step worse.

    An improving level change within ``quiet_s`` of the last send is not sent
    (a ratio hovering on a threshold); a worsening one always is.
    """

    name: str
    levels: tuple[float, ...]
    step: float
    higher_is_worse: bool
    quiet_s: float = 600.0
    level: int | None = None
    last_sent_value: float | None = None
    last_sent_at: float | None = None

    def level_of(self, value: float) -> int:
        if self.higher_is_worse:
            return sum(value > x for x in self.levels)
        return sum(value <= x for x in self.levels)

    def update(self, value: float | None, now: float = 0.0) -> Event | None:
        if value is None:
            return None
        level = self.level_of(value)
        prev, self.level = self.level, level
        kind = None
        if (prev is None and level > 0) or (prev is not None and level > prev):
            kind = "cross"
        elif prev is not None and level < prev:
            quiet = self.last_sent_at is not None and now - self.last_sent_at < self.quiet_s
            kind = None if quiet else "cross"
        elif level > 0 and self.last_sent_value is not None:
            delta = value - self.last_sent_value if self.higher_is_worse else self.last_sent_value - value
            if delta >= self.step - 1e-12:
                kind = "step"
        if kind is None:
            return None
        self.last_sent_value, self.last_sent_at = value, now
        return Event(f"ratio:{self.name}", kind, level, (f"{self.name} {value:.4g}",), f"ratio:{self.name}")


def daily_line_due(now_et: datetime, last_sent: date | None) -> bool:
    """The 09:00 ET ratio line: once per trading day, at or after 09:00."""
    d = now_et.astimezone(market_calendar.ET).date()
    if last_sent == d or now_et.astimezone(market_calendar.ET).time() < DAILY_LINE_AT:
        return False
    return market_calendar.is_trading_day(d)
