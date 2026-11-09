"""Risk guard loop (spec 2026-11-08 §5.1-§5.3) with fake IO edges. Prices are synthetic."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from xenon.api.services.risk_guard import service as rg_service
from xenon.api.services.risk_guard.account import DrawdownBasis
from xenon.api.services.risk_guard.incidents import CRASH, Event, Thresholds
from xenon.api.services.risk_guard.notify import SendResult
from xenon.api.services.risk_guard.service import RiskGuard, in_watch_window, priority
from xenon.api.services.risk_guard.sources import Quote
from xenon.execution.account_scope import AccountScope
from xenon.utils.market_calendar import ET

T0 = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)  # Mon 10:00 ET, RTH
LIVE = AccountScope(broker="IB", account_env="live", broker_account="U1")
PAPER = AccountScope(broker="IB", account_env="paper", broker_account="DU1")


class Clock:
    def __init__(self, start=T0):
        self.start, self.t = start, 0.0

    def mono(self):
        return self.t

    def wall(self):
        return self.start + timedelta(seconds=self.t)


class Feed:
    def __init__(self, clock):
        self.clock, self.prices = clock, {"SPX": 7000.0, "SPY": 700.0}

    async def read(self, extra=()):
        at, close = self.clock.wall(), {"SPX": 7000.0, "SPY": 700.0, "NDX": 25000.0}
        return {n: Quote(p, close[n], at, True) for n, p in self.prices.items() if p and n in ("SPX", "SPY", *extra)}


class Notifier:
    def __init__(self):
        self.sent = []

    async def send(self, title, body, priority, tags=(), today=None):
        self.sent.append((title, body, priority))
        return SendResult("sent", "off")


def _guard(clock, *, scope=LIVE, snapshot=None, read_only=False, snapshot_fn=None, health=None, extra=()):
    """Default account: a fresh snapshot on every read, ratios quiet, daily line already sent."""
    records, notifier = [], Notifier()

    async def snap(_scope):
        return snapshot or (_payload(), clock.wall() - timedelta(seconds=5))

    async def basis(_scope, _today):
        return DrawdownBasis(peak=100_000.0, peak_30d=100_000.0, adjusted=True, flows_since=None)

    async def record(_scope, **kw):
        records.append({**kw, "scope": _scope})

    g = RiskGuard(
        feed=Feed(clock),
        scope_fn=lambda: scope,
        snapshot_fn=snapshot_fn or snap,
        basis_fn=basis,
        record_fn=record,
        health_fn=lambda _w: dict(health or {}),
        notifier=notifier,
        read_only_fn=lambda: read_only,
        enabled_fn=lambda: True,
        thresholds_fn=Thresholds,
        levels_fn=lambda: (6950.0,),
        extra_fn=lambda: extra,
        mono=clock.mono,
        wall=clock.wall,
    )
    g.daily_sent = clock.wall().astimezone(ET).date()
    return g, notifier, records


async def _run(g, clock, seconds, fall=None):
    """Tick once per second; ``fall`` maps elapsed s -> fractional move from the start."""
    for _ in range(int(seconds)):
        if fall is not None:
            f = fall(clock.t)
            base = {"SPX": 7000.0, "SPY": 700.0}
            g.feed.prices = {k: base[k] * (1 + f) if v else None for k, v in g.feed.prices.items()}
        await g.tick()
        clock.t += 1
    await g.drain()


def _payload(nlv=1_000_000.0, excess=300_000.0):
    leg = lambda d, k: {"direction": d, "type": "Put", "strike": k, "contracts": 1}  # noqa: E731
    pos = {
        "ticker": "SPX",
        "expiry": "2026-11-20",
        "entry_cost": -500.0,
        "legs": [leg("LONG", 5900), leg("SHORT", 6000)],
    }
    return {"positions": [pos], "account_summary": {"net_liquidation": nlv, "excess_liquidity": excess}}


@pytest.mark.asyncio
async def test_fast_fall_sends_and_repeats_every_30s_then_ack_stops():
    clock = Clock()
    g, n, records = _guard(clock)
    await _run(g, clock, 1800)  # warm the 30 m window flat
    await _run(g, clock, 120, fall=lambda t: -0.016)  # step down 1.6 %
    fast = [s for s in n.sent if "FAST" in s[0]]
    assert fast and fast[0][2] == 4
    assert 3 <= len(fast) <= 5  # open + a repeat every 30 s over ~120 s
    assert any(r["kind"] == "open" for r in records)
    assert await g.ack("market") == "ok"
    before = len(n.sent)
    await _run(g, clock, 90, fall=lambda t: -0.016)
    assert len(n.sent) == before
    assert [r["kind"] for r in records if r["kind"] != "ack_state"][-1] == "ack"


@pytest.mark.asyncio
async def test_extra_index_own_incident_capped_at_fast_no_blind():
    clock = Clock()
    g, n, _ = _guard(clock, extra=("NDX",))
    g.feed.prices["NDX"] = 25000.0
    await _run(g, clock, 1800)
    g.feed.prices["NDX"] = 24000.0  # -4 %: CRASH-level day and 60 m, SPX flat
    await _run(g, clock, 60)
    titles = [s[0] for s in n.sent]
    assert titles and all(t.startswith("FAST NDX ") for t in titles)  # capped, SPX quiet
    assert [(i["key"], i["severity"]) for i in g.state()["incidents"]] == [("market:NDX", "FAST")]
    assert "NDX" in g.state()["sources"]
    assert await g.ack("market:NDX") == "ok"
    before = len(n.sent)
    await _run(g, clock, 60)
    assert len(n.sent) == before
    g.feed.prices["NDX"] = None  # a stale extra raises no BLIND
    await _run(g, clock, 40)
    assert g.health == {}
    g.extra_fn = lambda: ()  # removed: its incident clears
    await _run(g, clock, 302)
    assert "market:NDX" not in g.book.incidents and g.extra == {}


@pytest.mark.asyncio
async def test_single_source_counts_only_while_blind():
    clock = Clock()
    g, n, _ = _guard(clock)
    await _run(g, clock, 1800)
    g.feed.prices["SPY"] = None  # SPY stops; SPX alone falls
    await _run(g, clock, 60, fall=lambda t: -0.016)
    titles = [s[0] for s in n.sent]
    assert any("BLIND SOURCE_STALE" in t for t in titles)
    assert any("FAST" in t for t in titles)  # one source counts while BLIND


@pytest.mark.asyncio
async def test_quiet_market_sends_nothing():
    clock = Clock()
    g, n, records = _guard(clock)
    await _run(g, clock, 120)
    assert n.sent == [] and [r["kind"] for r in records] == ["ack_state"]  # the start-up state row only


@pytest.mark.asyncio
async def test_read_only_sends_but_never_records_and_ack_unavailable():
    clock = Clock()
    g, n, records = _guard(clock, read_only=True, health={"OWNER_HALTED": "orders halted"})
    await _run(g, clock, 3)
    assert any("BLIND OWNER_HALTED" in s[0] for s in n.sent)
    assert records == []
    assert await g.ack("health:OWNER_HALTED") == "unavailable"


@pytest.mark.asyncio
async def test_account_ratios_daily_line_and_snapshot_stale():
    clock = Clock()
    at = T0 - timedelta(seconds=10)
    g, n, _ = _guard(clock, snapshot=(_payload(nlv=10_000.0, excess=3_000.0), at))
    g.daily_sent = None
    await _run(g, clock, 5)
    titles = [s[0] for s in n.sent]
    assert any(t.startswith("RATIO leverage") for t in titles)  # 9 500 / 10 000 > 0.75
    assert any(t.startswith("RATIO drawdown") for t in titles)  # 10 000 vs peak 100 000
    assert any(t == "Daily risk ratios" for t in titles)
    assert g.state()["account"]["leverage"] == pytest.approx(0.95)
    await _run(g, clock, 130)  # same snapshot, now > 120 s old in RTH
    assert "SNAPSHOT_STALE" in g.health


@pytest.mark.asyncio
async def test_db_failure_is_blind_not_crash():
    clock = Clock()

    async def boom(_scope):
        raise OSError("db down")

    g, n, _ = _guard(clock, snapshot_fn=boom)
    await _run(g, clock, 2)
    assert "DB_DOWN" in g.health
    assert any("BLIND DB_DOWN" in s[0] for s in n.sent)


@pytest.mark.asyncio
async def test_paper_scope_titles_are_marked_test():
    clock = Clock()
    g, n, _ = _guard(clock, scope=PAPER, health={"GATEWAY_DOWN": "port closed"})
    await _run(g, clock, 1)
    title, body, prio = n.sent[0]
    assert title.startswith("[TEST] BLIND GATEWAY_DOWN") and prio == 5
    assert "Protection: none automatic" in body and "/risk/incidents/health:GATEWAY_DOWN/ack" in body


@pytest.mark.asyncio
async def test_disabled_guard_does_nothing():
    clock = Clock()
    g, n, _ = _guard(clock, health={"GATEWAY_DOWN": "x"})
    g.enabled_fn = lambda: False
    await _run(g, clock, 3)
    assert n.sent == [] and g.last_tick_at is None


def test_watch_window():
    et = lambda h, m, d=9: datetime(2026, 11, d, h, m, tzinfo=timezone.utc) + timedelta(hours=5)  # noqa: E731
    assert in_watch_window(et(9, 0)) and in_watch_window(et(15, 59))
    assert not in_watch_window(et(8, 59)) and not in_watch_window(et(16, 0))
    assert not in_watch_window(et(10, 0, d=8))  # Sunday
    assert not in_watch_window(et(13, 30, d=27))  # 2026-11-27 early close 13:00


def test_priorities():
    from xenon.api.services.risk_guard.incidents import Event

    assert priority(Event("market", "open", 1, (), "m")) == 3
    assert priority(Event("market", "open", 2, (), "m")) == 4
    assert priority(Event("market", "open", 3, (), "m")) == 5
    assert priority(Event("health:X", "open", 3, (), "h")) == 5
    assert priority(Event("ratio:daily", "daily", 0, (), "d")) == 2


def test_run_loop_survives_a_failed_tick():
    from xenon.api.services.risk_guard import service

    calls = []

    class G:
        async def tick(self):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            if len(calls) == 3:
                raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.run_loop(G(), tick_s=0.0))
    assert len(calls) == 3


# ── review regressions (Sonnet review 2026-11-08) ─────────────────────


@pytest.mark.asyncio
async def test_no_stale_page_in_the_open_grace_minute():
    clock = Clock(start=datetime(2026, 11, 9, 14, 30, tzinfo=timezone.utc))  # 09:30:00 ET
    g, n, _ = _guard(clock)
    g.feed.prices["SPX"] = None  # the cash index has not printed yet
    await _run(g, clock, 50)
    assert "SOURCE_STALE" not in g.health and not any("STALE" in s[0] for s in n.sent)
    await _run(g, clock, 30)  # 09:31:20 ET, still no SPX tick
    assert "SOURCE_STALE" in g.health


@pytest.mark.asyncio
async def test_a_warming_source_after_a_gap_does_not_disable_speed_triggers():
    clock = Clock()
    g, n, _ = _guard(clock)
    await _run(g, clock, 1800)
    g.feed.prices["SPX"] = None
    await _run(g, clock, 25)  # SPX gap > 20 s: its window restarts
    g.feed.prices["SPX"] = 7000.0
    await _run(g, clock, 5)
    assert g.sources["SPX"].window.ret(clock.t, 1800) is None and g.sources["SPX"].fresh(clock.t)
    n.sent.clear()
    await _run(g, clock, 60, fall=lambda t: -0.016)  # 30 m return -1.6 % on SPY only
    assert any("FAST" in s[0] and "30m" in s[0] for s in n.sent)


@pytest.mark.asyncio
async def test_hung_db_read_is_bounded_and_blind(monkeypatch):
    from xenon.api.services.risk_guard import service

    monkeypatch.setattr(service, "DB_TIMEOUT_S", 0.05)

    async def hang(_scope):
        await asyncio.sleep(30)

    clock = Clock()
    g, n, _ = _guard(clock, snapshot_fn=hang)
    await asyncio.wait_for(_run(g, clock, 2), timeout=2)
    assert "DB_DOWN" in g.health


@pytest.mark.asyncio
async def test_unreadable_snapshot_is_blind_and_market_still_evaluated():
    clock = Clock()
    bad = {"positions": [{"ticker": "SPX", "legs": [None]}], "account_summary": {"net_liquidation": 1.0}}
    g, n, _ = _guard(clock, snapshot=(bad, T0))
    await _run(g, clock, 3)
    assert "ACCOUNT_UNREADABLE" in g.health and g.last_tick_at is not None


@pytest.mark.asyncio
async def test_render_failure_still_sends(monkeypatch):
    clock = Clock()
    g, n, _ = _guard(clock, health={"GATEWAY_DOWN": "port closed"})
    monkeypatch.setattr(g, "render", lambda ev, scope: 1 / 0)
    await _run(g, clock, 1)
    assert n.sent and "render failed" in n.sent[0][0]


@pytest.mark.asyncio
async def test_slow_basis_keeps_the_snapshot(monkeypatch):
    from xenon.api.services.risk_guard import service

    monkeypatch.setattr(service, "DB_TIMEOUT_S", 0.05)
    clock = Clock()
    g, n, _ = _guard(clock)

    async def slow_basis(_scope, _today):
        await asyncio.sleep(30)

    g.basis_fn = slow_basis
    await asyncio.wait_for(_run(g, clock, 2), timeout=2)
    assert g.db_ok and g.snapshot is not None and "DB_DOWN" not in g.health and g.basis is None


# ── review fixes (Astra, PR #268) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_ack_not_applied_when_the_incident_rises_during_the_write():
    clock = Clock()
    g, n, records = _guard(clock)
    await _run(g, clock, 1800)
    await _run(g, clock, 5, fall=lambda t: -0.016)  # FAST
    gate, record = asyncio.Event(), g.record_fn

    async def slow(scope, **kw):
        if kw["kind"] == "ack":
            await gate.wait()
        await record(scope, **kw)

    g.record_fn = slow
    task = asyncio.create_task(g.ack("market"))
    await asyncio.sleep(0)
    g.feed.prices = {"SPX": 7000.0 * 0.96, "SPY": 700.0 * 0.96}  # day -4 %: CRASH
    await g.tick()
    gate.set()
    assert await task == "changed"
    inc = g.book.incidents["market"]
    assert inc.severity == CRASH and inc.acked_at is None
    await g.drain()
    assert _latest_state(records) == {"acked": []}  # the unapplied ack is never restorable


@pytest.mark.asyncio
async def test_one_shot_alert_that_reached_no_channel_is_redelivered():
    clock = Clock()
    g, n, _ = _guard(clock)
    fails = [2]

    async def send(title, body, priority, tags=(), today=None):
        n.sent.append((title, body, priority))
        if fails[0]:
            fails[0] -= 1
            return SendResult("failed", "off")
        return SendResult("sent", "off")

    g.notifier.send = send
    await _run(g, clock, 900)
    for _ in range(5):
        await _run(g, clock, 31, fall=lambda t: -0.008)  # 15 m -0.8 %: WATCH, sent once
    assert len([s for s in n.sent if s[0].startswith("WATCH")]) == 3  # failed, failed, sent
    assert g._redeliver == {}


@pytest.mark.asyncio
async def test_session_high_does_not_carry_into_the_next_day():
    clock = Clock()
    g, _, _ = _guard(clock)  # key level 6950
    await _run(g, clock, 5)  # SPX 7000 on Monday
    clock.t += 86400  # Tuesday, same time: the cash index prints again from 6900
    g.feed.prices = {"SPX": 6900.0, "SPY": 690.0}
    await _run(g, clock, 3)
    assert g.inputs.levels_broken == ()


@pytest.mark.asyncio
async def test_failed_drawdown_basis_is_blind():
    clock = Clock()
    g, n, _ = _guard(clock)

    async def boom(_scope, _today):
        raise OSError("nav_history down")

    g.basis_fn = boom
    await _run(g, clock, 2)
    assert "BASIS_UNAVAILABLE" in g.health
    assert any("BLIND BASIS_UNAVAILABLE" in s[0] for s in n.sent)


@pytest.mark.asyncio
async def test_event_write_is_bounded_and_capped(monkeypatch):
    monkeypatch.setattr(rg_service, "DB_TIMEOUT_S", 0.05)
    g, _, _ = _guard(Clock())

    async def hang(_scope, **_kw):
        await asyncio.sleep(10)

    g.record_fn = hang
    ev = Event("market", "open", 2, (), "market#1")
    assert await asyncio.wait_for(g._record(ev, LIVE, {}), 1) is False
    assert g._records_in_flight == 0
    g._records_in_flight = rg_service.MAX_RECORDS_IN_FLIGHT
    assert await g._record(ev, LIVE, {}) is False


@pytest.mark.asyncio
async def test_worsening_during_the_ack_write_still_alerts_after_the_ack():
    clock = Clock()
    g, n, records = _guard(clock)
    await _run(g, clock, 1800)
    await _run(g, clock, 5, fall=lambda t: -0.016)  # FAST
    gate, record = asyncio.Event(), g.record_fn

    async def slow(scope, **kw):
        if kw["kind"] == "ack":
            await gate.wait()
        await record(scope, **kw)

    g.record_fn = slow
    task = asyncio.create_task(g.ack("market"))
    await asyncio.sleep(0)
    g.feed.prices = {"SPX": 7000.0 * 0.981, "SPY": 700.0 * 0.981}  # still FAST, low 0.3 % lower
    await g.tick()
    clock.t += 1
    gate.set()
    assert await task == "ok"
    await _run(g, clock, 2)
    assert [r["kind"] for r in records if r["kind"] != "ack_state"][-1] == "worsening"


@pytest.mark.asyncio
async def test_failed_worsening_after_an_ack_is_redelivered():
    clock = Clock()
    g, n, _ = _guard(clock)
    await _run(g, clock, 1800)
    await _run(g, clock, 5, fall=lambda t: -0.016)
    assert await g.ack("market") == "ok"
    fails, send = [1], g.notifier.send

    async def flaky(title, body, priority, tags=(), today=None):
        if fails[0]:
            fails[0] -= 1
            n.sent.append((title, body, priority))
            return SendResult("failed", "off")
        return await send(title, body, priority, tags)

    g.notifier.send = flaky
    for _ in range(3):
        await _run(g, clock, 31, fall=lambda t: -0.019)  # worsening, sent once after the ack
    assert len([s for s in n.sent if "[worsening " in s[1]]) == 2  # failed, then redelivered


@pytest.mark.asyncio
async def test_an_older_in_flight_send_never_requeues_a_retry():
    g, _, _ = _guard(Clock())

    async def failed(*_a, **_kw):
        return SendResult("failed", "off")

    g.notifier.send = failed
    g._rev["market"] = 2  # a newer send for the key was dispatched meanwhile
    await g._send(Event("market", "open", 1, (), "market#1"), "t", "b", LIVE, 0, 0.0, 1)
    assert g._redeliver == {}


# ── restart: acks survive (plan 2026-11-09 P2.0) ─────────────────────


def _latest_state(rows):
    """What a restore would read: the newest process's highest seq (``latest_ack_state``)."""
    states = [r["detail"] for r in rows if r["kind"] == "ack_state"]
    if not states:
        return None
    same = [d for d in states if d["proc"] == states[-1]["proc"]]
    return {"acked": max(same, key=lambda d: d["seq"])["acked"]}


async def _snap(clock, excess):
    return _payload(excess=excess), clock.wall() - timedelta(seconds=5)


def _restarted(clock, state, excess):
    """A new guard process (a restart) whose restore read returns ``state``."""
    g, n, records = _guard(clock)
    seen = []

    async def restore(scope, since):
        seen.append(since)
        return state

    g.restore_fn = restore
    g.snapshot_fn = lambda _s: _snap(clock, excess)
    return g, n, records, seen


async def _acked_fast(clock, excess=100_000.0):
    """First process: cushion 10 % -> FAST, acked, state written."""
    g, n, rows = _guard(clock)
    g.snapshot_fn = lambda _s: _snap(clock, excess)
    await _run(g, clock, 5)
    assert any("FAST" in s[0] for s in n.sent)
    assert await g.ack("market") == "ok"
    await _run(g, clock, 2)
    return g, rows


@pytest.mark.asyncio
async def test_restart_keeps_an_acked_incident_quiet_and_alerts_on_worsening():
    clock = Clock()
    g1, rows = await _acked_fast(clock)
    state = _latest_state(rows)
    assert [a["key"] for a in state["acked"]] == ["market"]
    assert state["acked"][0]["ref"]["cushion"] == pytest.approx(0.10)

    g2, n2, _, seen = _restarted(clock, state, 100_000.0)
    await _run(g2, clock, 90)
    assert [s for s in n2.sent if "FAST" in s[0]] == []  # no re-page
    assert seen == [clock.wall().astimezone(ET).replace(hour=0, minute=0, second=0, microsecond=0)]
    assert g2.book.incidents["market"].incident_id == g1.book.incidents["market"].incident_id

    g3, n3, _, _ = _restarted(clock, state, 80_000.0)  # cushion fell 2 pp while down
    await _run(g3, clock, 3)
    assert any("FAST" in s[0] for s in n3.sent)


@pytest.mark.asyncio
async def test_worsening_sent_before_the_restart_is_not_sent_again():
    clock = Clock()
    g1, rows = await _acked_fast(clock)
    g1.snapshot_fn = lambda _s: _snap(clock, 80_000.0)
    before = len(g1.notifier.sent)
    await _run(g1, clock, 35)
    assert len(g1.notifier.sent) == before + 1  # the worsening
    assert _latest_state(rows)["acked"][0]["ref"]["cushion"] == pytest.approx(0.08)

    g2, n2, _, _ = _restarted(clock, _latest_state(rows), 80_000.0)
    await _run(g2, clock, 60)
    assert [s for s in n2.sent if "FAST" in s[0]] == []


@pytest.mark.asyncio
async def test_restart_without_state_or_failed_read_pages_again_with_a_new_id():
    clock = Clock()
    g1, rows = await _acked_fast(clock)
    g2, n2, rows2, _ = _restarted(clock, None, 100_000.0)
    g3, n3, _, _ = _restarted(clock, _latest_state(rows), 100_000.0)

    async def down(scope, since):
        raise OSError("db down")

    g3.restore_fn = down
    await _run(g2, clock, 3)
    await _run(g3, clock, 3)
    assert any("FAST" in s[0] for s in n2.sent) and any("FAST" in s[0] for s in n3.sent)  # fail loud
    ids = {g.book.incidents["market"].incident_id for g in (g1, g2, g3)}
    assert len(ids) == 3  # unique per process, even at the same wall time
    assert _latest_state(rows2) == {"acked": []}  # the first tick writes: an old row never outlives a process


@pytest.mark.asyncio
async def test_bad_state_entries_are_dropped_and_the_guard_keeps_ticking():
    clock = Clock()
    _, rows = await _acked_fast(clock)
    good = _latest_state(rows)["acked"][0]
    bad = [
        {**good, "ref": {**good["ref"], "cushion": "x"}},
        {**good, "ref": {**good["ref"], "shorts_crossed": None}},
        {**good, "ref": None},  # a market incident without its reference could never worsen
        {**good, "ref": []},
        {**good, "ref": {}},  # incomplete: a missing baseline would hide a downtime worsening
        {**good, "ref": {k: v for k, v in good["ref"].items() if k != "cushion"}},
        {**good, "ref": {**good["ref"], "session_low": 10**400}},  # would overflow in the worsening check
        {**good, "ref": {**good["ref"], "levels_broken": [10**400]}},
        {**good, "sent_severity": True},
        {**good, "sent_severity": 3, "max_sent": 2},
        {**good, "key": "bogus"},
        {"key": "market"},
    ]
    for entry in bad:
        g, n, _, _ = _restarted(clock, {"acked": [entry]}, 100_000.0)
        await _run(g, clock, 3)
        assert any("FAST" in s[0] for s in n.sent), entry  # dropped: pages again, no crash
    for state in ("x", {"acked": "x"}, {"acked": [None]}):
        g, n, _, _ = _restarted(clock, state, 100_000.0)
        await _run(g, clock, 3)
        assert any("FAST" in s[0] for s in n.sent)
    health = {
        "key": "health:DB_DOWN",
        "incident_id": "health:DB_DOWN#x.1",
        "sent_severity": 3,
        "max_sent": 3,
        "ref": None,
    }
    assert rg_service.restorable({"acked": [health]})[0][0] == "health:DB_DOWN"


@pytest.mark.asyncio
async def test_failed_state_write_is_retried():
    clock = Clock()
    g, n, rows = _guard(clock)
    g.snapshot_fn = lambda _s: _snap(clock, 100_000.0)
    record, fail = g.record_fn, [2]

    async def flaky(scope, **kw):
        if kw["kind"] == "ack_state" and fail[0]:
            fail[0] -= 1
            raise OSError("db down")
        await record(scope, **kw)

    g.record_fn = flaky
    await _run(g, clock, 3)  # first write fails
    await g.ack("market")
    await g.drain()
    assert _latest_state(rows) is None  # second fails too
    for _ in range(4):
        await _run(g, clock, 3)  # drained per step: the retry runs after STATE_RETRY_S
    assert fail[0] == 0 and [a["key"] for a in _latest_state(rows)["acked"]] == ["market"]


@pytest.mark.asyncio
async def test_ack_is_written_at_once_and_shutdown_flushes_past_a_full_write_cap():
    """An ack applied right before shutdown survives even when the writer is refused by the 4-write cap."""
    from xenon.api.services.risk_guard import app as rg_app

    clock = Clock()
    g, n, rows = _guard(clock)
    g.snapshot_fn = lambda _s: _snap(clock, 100_000.0)
    await _run(g, clock, 3)
    capped = g._record

    async def cap_full(ev, scope, detail):
        return False if ev.kind == "ack_state" else await capped(ev, scope, detail)  # as when 4 writes are in flight

    g._record = cap_full
    assert await g.ack("market") == "ok"
    await asyncio.sleep(0)
    assert g._state_due > clock.t  # the writer ran at once (no tick needed) and was refused

    class _State:
        risk_guard_task, risk_guard = None, g

    class _App:
        state = _State()

    g.notifier.aclose = _noop
    await rg_app.stop(_App())
    assert [a["key"] for a in _latest_state(rows)["acked"]] == ["market"]  # flush_state bypasses the cap


async def _noop():
    return None


@pytest.mark.asyncio
async def test_late_older_state_write_never_wins():
    clock = Clock()
    g, n, rows = _guard(clock)
    g.snapshot_fn = lambda _s: _snap(clock, 100_000.0)
    await _run(g, clock, 3)
    gate, record = asyncio.Event(), g.record_fn

    async def slow(scope, **kw):
        if kw["kind"] == "ack_state" and kw["detail"]["acked"]:
            await gate.wait()  # the "acked" write commits late
        await record(scope, **kw)

    g.record_fn = slow
    assert await g.ack("market") == "ok"
    await asyncio.sleep(0)  # the writer starts and blocks on its "acked" row
    g.book.incidents["market"].acked_at = None  # then a rise un-acks it
    assert await g.flush_state(1.0)  # shutdown writes the newer, empty set first
    gate.set()
    await g.drain()
    order = [(r["detail"]["seq"], r["detail"]["acked"] != []) for r in rows if r["kind"] == "ack_state"]
    assert order.index((2, True)) > order.index((3, False))  # the older "acked" set committed after the flush
    assert _latest_state(rows) == {"acked": []}  # but its seq is lower: restore takes the newer set


@pytest.mark.asyncio
async def test_restore_waits_for_a_scope_and_a_scope_change_clears_acks():
    clock = Clock()
    _, rows = await _acked_fast(clock)
    state = _latest_state(rows)
    g, n, rec2, seen = _restarted(clock, state, 100_000.0)
    scope = [None]
    g.scope_fn = lambda: scope[0]
    await _run(g, clock, 3)
    assert seen == [] and [r for r in rec2 if r["kind"] == "ack_state"] == []  # nothing read or written yet
    scope[0] = LIVE
    await _run(g, clock, 3)
    assert len(seen) == 1 and g.book.incidents["market"].acked_at is not None
    assert [s for s in n.sent if "FAST" in s[0]] == []

    other = AccountScope(broker="IB", account_env="live", broker_account="U2")
    g.restore_fn = lambda sc, since: _none()
    scope[0] = other
    await _run(g, clock, 31)
    assert any("FAST" in s[0] for s in n.sent)  # U1's ack does not silence U2
    written = [r for r in rec2 if r["kind"] == "ack_state"]
    assert written[0]["scope"] == LIVE and written[-1]["scope"] == other and written[-1]["detail"]["acked"] == []


async def _none():
    return None


@pytest.mark.asyncio
async def test_undelivered_worsening_reference_is_not_persisted():
    clock = Clock()
    g, rows = await _acked_fast(clock)
    sent = g.notifier.send

    async def down(title, body, priority, tags=(), today=None):
        g.notifier.sent.append((title, body, priority))
        return SendResult("failed", "off")

    g.notifier.send = down
    g.snapshot_fn = lambda _s: _snap(clock, 80_000.0)
    await _run(g, clock, 35)
    assert _latest_state(rows)["acked"][0]["ref"]["cushion"] == pytest.approx(0.10)  # not the undelivered 8 %
    g2, n2, _, _ = _restarted(clock, _latest_state(rows), 80_000.0)
    await _run(g2, clock, 3)
    assert any("FAST" in s[0] for s in n2.sent)  # the worsening alerts again after the restart
    g.notifier.send = sent


@pytest.mark.asyncio
async def test_rise_during_downtime_alerts_on_the_first_tick():
    clock = Clock()
    _, rows = await _acked_fast(clock)
    entry = {**_latest_state(rows)["acked"][0], "max_sent": 3}  # CRASH was sent before, then it fell to FAST
    g, n, _, _ = _restarted(clock, {"acked": [entry]}, 50_000.0)  # cushion 5 %: CRASH now
    await _run(g, clock, 1)
    assert any("CRASH" in s[0] for s in n.sent)


@pytest.mark.asyncio
async def test_ack_in_flight_across_an_account_change_is_refused():
    clock = Clock()
    g, n, rows = _guard(clock)
    scope = [LIVE]
    g.scope_fn = lambda: scope[0]
    g.snapshot_fn = lambda _s: _snap(clock, 100_000.0)
    await _run(g, clock, 3)
    gate, record = asyncio.Event(), g.record_fn

    async def slow(sc, **kw):
        if kw["kind"] == "ack":
            await gate.wait()
        await record(sc, **kw)

    g.record_fn = slow
    task = asyncio.create_task(g.ack("market"))
    await asyncio.sleep(0)
    scope[0] = AccountScope(broker="IB", account_env="live", broker_account="U2")
    await g.tick()  # rebinds to U2 while U1's ack write waits
    gate.set()
    assert await task == "changed"
    assert g.book.incidents["market"].acked_at is None
    await g.drain()
    assert all(r["detail"]["acked"] == [] for r in rows if r["kind"] == "ack_state" and r["scope"] != LIVE)


@pytest.mark.asyncio
async def test_late_worsening_completion_does_not_replace_a_newer_ack_reference():
    clock = Clock()
    g, rows = await _acked_fast(clock)
    gate, send = asyncio.Event(), g.notifier.send

    async def slow(title, body, priority, tags=(), today=None):
        await gate.wait()  # the worsening alert is delivered late
        return await send(title, body, priority, tags=tags, today=today)

    g.notifier.send = slow
    g.snapshot_fn = lambda _s: _snap(clock, 80_000.0)  # cushion 8 %: worsening
    for _ in range(35):
        await g.tick()  # the worsening is dispatched; its delivery hangs
        clock.t += 1
    assert any(k[0] == "market" for k in g._worse_refs)
    g.snapshot_fn = lambda _s: _snap(clock, 115_000.0)  # cushion rebounds to 11.5 %
    for _ in range(31):
        await g.tick()
        clock.t += 1
    assert await g.ack("market") == "ok"  # the operator acks at 11.5 %
    gate.set()
    await g.drain()
    assert g._durable_ref[g.book.incidents["market"].incident_id].cushion == pytest.approx(0.115)


@pytest.mark.asyncio
async def test_shutdown_during_the_restore_read_writes_nothing():
    from xenon.api.services.risk_guard import app as rg_app

    clock = Clock()
    _, rows = await _acked_fast(clock)
    g, n, rec2, _ = _restarted(clock, _latest_state(rows), 100_000.0)
    hang = asyncio.Event()

    async def slow_restore(scope, since):
        await hang.wait()

    g.restore_fn = slow_restore
    task = asyncio.create_task(rg_service.run_loop(g, tick_s=0.01))
    await asyncio.sleep(0.02)  # the first tick waits on the restore read

    class _State:
        risk_guard_task, risk_guard = task, g

    class _App:
        state = _State()

    g.notifier.aclose = _noop
    await rg_app.stop(_App())
    assert [r for r in rec2 if r["kind"] == "ack_state"] == []  # the saved set stays the latest


@pytest.mark.asyncio
async def test_pending_write_never_publishes_an_unrestored_account():
    """A's ack_state write is in flight, the scope moves to B, B's restore hangs, then shutdown."""
    from xenon.api.services.risk_guard import app as rg_app

    clock = Clock()
    g, n, rows = _guard(clock)
    scope = [LIVE]
    g.scope_fn = lambda: scope[0]
    g.snapshot_fn = lambda _s: _snap(clock, 100_000.0)
    await _run(g, clock, 3)
    gate, record = asyncio.Event(), g.record_fn

    async def slow(sc, **kw):
        if kw["kind"] == "ack_state":
            await gate.wait()
        await record(sc, **kw)

    g.record_fn = slow
    assert await g.ack("market") == "ok"
    await asyncio.sleep(0)  # A's write is now in flight
    hang = asyncio.Event()

    async def slow_restore(sc, since):
        await hang.wait()

    g.restore_fn = slow_restore
    scope[0] = AccountScope(broker="IB", account_env="live", broker_account="U2")
    task = asyncio.create_task(g.tick())  # rebinds to U2, then waits on U2's restore
    await asyncio.sleep(0)
    gate.set()  # A's write commits; the writer loops while U2 is unrestored
    await asyncio.sleep(0.01)
    task.cancel()

    class _State:
        risk_guard_task, risk_guard = task, g

    class _App:
        state = _State()

    g.notifier.aclose = _noop
    await rg_app.stop(_App())
    assert [r for r in rows if r["kind"] == "ack_state" and r["scope"] != LIVE] == []
