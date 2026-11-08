"""Risk guard tiers + incidents (spec 2026-11-08 §5.2-§5.3). Inputs are synthetic."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from xenon.api.services.risk_guard.incidents import (
    CRASH,
    FAST,
    WATCH,
    IncidentBook,
    MarketInputs,
    RatioAlarm,
    Thresholds,
    daily_line_due,
    market_tier,
)

TH = Thresholds()
ET = ZoneInfo("America/New_York")


def _m(**kw) -> MarketInputs:
    return MarketInputs(**kw)


# ── market_tier ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "inputs,tier",
    [
        (_m(), 0),
        (_m(ret_15m=-0.0074), 0),
        (_m(ret_15m=-0.0075), WATCH),
        (_m(day=-0.01), WATCH),
        (_m(levels_broken=(7500.0,)), WATCH),
        (_m(cushion=0.199), WATCH),
        (_m(ret_30m=-0.015), FAST),
        (_m(day=-0.02), FAST),
        (_m(min_short_distance=0.005), FAST),
        (_m(min_short_distance=-0.01), FAST),
        (_m(cushion=0.119), FAST),
        (_m(day=-0.03), CRASH),
        (_m(ret_60m=-0.025), CRASH),
        (_m(long_itm=True), CRASH),
        (_m(cushion=0.079), CRASH),
        (_m(cushion=0.08), FAST),
    ],
)
def test_market_tier(inputs, tier):
    assert market_tier(inputs, TH)[0] == tier


def test_unavailable_windows_never_trigger():
    tier, reasons = market_tier(_m(ret_15m=None, ret_30m=None, ret_60m=None, day=None, cushion=None), TH)
    assert tier == 0 and reasons == []


def test_reasons_list_every_trigger():
    tier, reasons = market_tier(_m(day=-0.031, cushion=0.10), TH)
    assert tier == CRASH
    assert any("day" in r for r in reasons) and any("cushion" in r for r in reasons)


# ── market incident cadence ───────────────────────────────────────────


def _step(book, t, inputs=None, health=None):
    inputs = inputs or _m()
    return book.step(t, market_tier(inputs, TH), inputs, health or {})


def test_watch_sends_once():
    book = IncidentBook(TH)
    ev = _step(book, 0, _m(day=-0.011))
    assert [e.kind for e in ev] == ["open"] and ev[0].severity == WATCH
    assert _step(book, 31, _m(day=-0.011)) == []
    assert _step(book, 600, _m(day=-0.011)) == []


def test_fast_repeats_every_30s_until_ack():
    book = IncidentBook(TH)
    fast = _m(day=-0.021, session_low=97.9)
    assert [e.kind for e in _step(book, 0, fast)] == ["open"]
    assert _step(book, 29, fast) == []
    assert [e.kind for e in _step(book, 30, fast)] == ["repeat"]
    assert book.ack("market", 31) is True
    assert _step(book, 61, fast) == []
    assert _step(book, 600, fast) == []


def test_rise_after_ack_sends_and_rearms_repeats():
    book = IncidentBook(TH)
    _step(book, 0, _m(day=-0.021))
    book.ack("market", 1)
    ev = _step(book, 5, _m(day=-0.031))
    assert [e.kind for e in ev] == ["rise"] and ev[0].severity == CRASH
    assert [e.kind for e in _step(book, 35, _m(day=-0.031))] == ["repeat"]


def test_worsening_after_ack_new_low():
    book = IncidentBook(TH)
    _step(book, 0, _m(day=-0.021, session_low=100.0, cushion=0.15))
    book.ack("market", 1)
    assert _step(book, 40, _m(day=-0.021, session_low=99.76, cushion=0.15)) == []  # -0.24%
    ev = _step(book, 41, _m(day=-0.021, session_low=99.75, cushion=0.15))  # -0.25%
    assert [e.kind for e in ev] == ["worsening"]
    # The reference moves to the worsening point: same low again is quiet.
    assert _step(book, 42, _m(day=-0.021, session_low=99.75, cushion=0.15)) == []


def test_return_to_crash_after_ack_and_drop_resends():
    book = IncidentBook(TH)
    _step(book, 0, _m(day=-0.031))
    book.ack("market", 1)
    _step(book, 5, _m(day=-0.021))  # drops to FAST, incident stays open
    ev = _step(book, 40, _m(day=-0.031))
    assert [e.kind for e in ev] == ["rise"]


def test_re_rise_flapping_is_rate_limited():
    book = IncidentBook(TH)
    _step(book, 0, _m(day=-0.031))
    book.ack("market", 1)
    _step(book, 2, _m(day=-0.029))
    assert _step(book, 3, _m(day=-0.031)) == []  # re-rise inside 30 s of the last send
    assert [e.kind for e in _step(book, 30, _m(day=-0.031))] == ["rise"]


def test_first_rise_is_immediate():
    book = IncidentBook(TH)
    _step(book, 0, _m(day=-0.011))
    assert [e.kind for e in _step(book, 1, _m(day=-0.021))] == ["rise"]


def test_worsening_reference_seeded_when_missing_at_ack():
    book = IncidentBook(TH)
    _step(book, 0, _m(day=-0.031, cushion=None))
    book.ack("market", 1)
    assert _step(book, 2, _m(day=-0.031, cushion=0.15)) == []  # first value seeds the reference
    assert [e.kind for e in _step(book, 3, _m(day=-0.031, cushion=0.05))] == ["worsening"]


def test_all_inputs_unavailable_holds_the_incident():
    book = IncidentBook(TH)
    _step(book, 0, _m(cushion=0.07))
    book.ack("market", 1)
    _step(book, 10)  # no data at all: hold, do not start the clear timer
    _step(book, 400)
    assert "market" in book.incidents


def test_worsening_after_ack_cushion_and_strike():
    book = IncidentBook(TH)
    _step(book, 0, _m(day=-0.021, cushion=0.15, shorts_crossed=0))
    book.ack("market", 1)
    assert _step(book, 2, _m(day=-0.021, cushion=0.131)) == []
    assert [e.kind for e in _step(book, 3, _m(day=-0.021, cushion=0.13))] == ["worsening"]
    assert [e.kind for e in _step(book, 4, _m(day=-0.021, cushion=0.13, shorts_crossed=1))] == ["worsening"]


def test_incident_closes_after_5_min_clear_then_reopens():
    book = IncidentBook(TH)
    _step(book, 0, _m(day=-0.021))
    book.ack("market", 1)
    assert _step(book, 10, _m(day=-0.001, cushion=0.5)) == []
    assert _step(book, 309, _m(day=-0.001, cushion=0.5)) == []
    assert "market" in book.incidents
    _step(book, 310, _m(day=-0.001, cushion=0.5))
    assert "market" not in book.incidents
    assert [e.kind for e in _step(book, 311, _m(day=-0.021))] == ["open"]


def test_short_clear_keeps_incident_and_ack():
    book = IncidentBook(TH)
    _step(book, 0, _m(day=-0.021))
    book.ack("market", 1)
    _step(book, 10, _m(day=-0.001, cushion=0.5))
    assert _step(book, 100, _m(day=-0.021)) == []


# ── health incidents ──────────────────────────────────────────────────


def test_ack_fast_then_db_down_sends_new_incident():
    book = IncidentBook(TH)
    _step(book, 0, _m(day=-0.021))
    book.ack("market", 1)
    ev = _step(book, 2, _m(day=-0.021), {"DB_DOWN": "connection refused"})
    assert [(e.key, e.kind) for e in ev] == [("health:DB_DOWN", "open")]


def test_ack_blind_then_crash_sends():
    book = IncidentBook(TH)
    _step(book, 0, health={"FEED_STALE": "SPX 25 s"})
    assert book.ack("health:FEED_STALE", 1)
    assert _step(book, 40, health={"FEED_STALE": "SPX 65 s"}) == []
    ev = _step(book, 41, _m(day=-0.031), {"FEED_STALE": "SPX 66 s"})
    assert [(e.key, e.severity) for e in ev] == [("market", CRASH)]


def test_blind_repeats_until_ack():
    book = IncidentBook(TH)
    assert [e.kind for e in _step(book, 0, health={"OWNER_HALTED": ""})] == ["open"]
    assert [e.kind for e in _step(book, 30, health={"OWNER_HALTED": ""})] == ["repeat"]
    book.ack("health:OWNER_HALTED", 31)
    assert _step(book, 60, health={"OWNER_HALTED": ""}) == []


def test_ack_unknown_key_is_false():
    assert IncidentBook(TH).ack("market", 0) is False


# ── ratio alarms ──────────────────────────────────────────────────────


def test_leverage_cross_and_step():
    a = RatioAlarm("leverage", TH.leverage_levels, TH.leverage_step, higher_is_worse=True)
    assert a.update(0.4, now=0) is None
    ev = a.update(0.51, now=1)
    assert ev.kind == "cross" and ev.severity == 1
    assert a.update(0.74, now=2) is None
    assert a.update(0.76, now=3).severity == 2
    assert a.update(1.2, now=4).severity == 3
    assert a.update(1.44, now=5) is None
    assert a.update(1.45, now=6).kind == "step"  # +0.25 vs last send 1.2
    assert a.update(0.9, now=1000).kind == "cross"  # improves a level: also sent
    assert a.update(None, now=1001) is None


def test_drawdown_cross_and_step():
    a = RatioAlarm("drawdown", TH.drawdown_levels, TH.drawdown_step, higher_is_worse=False)
    assert a.update(-0.04) is None
    assert a.update(-0.05).severity == 1
    assert a.update(-0.069) is None
    assert a.update(-0.07).kind == "step"
    assert a.update(-0.16).severity == 3


def test_ratio_improving_flap_is_rate_limited():
    a = RatioAlarm("leverage", TH.leverage_levels, TH.leverage_step, higher_is_worse=True)
    assert a.update(1.01, now=0).kind == "cross"
    assert a.update(0.99, now=10) is None  # improvement within 10 min of a send
    assert a.update(1.01, now=20).kind == "cross"  # worsening cross always sends
    assert a.update(0.99, now=700).kind == "cross"


def test_ratio_standing_at_boot_sends_once():
    a = RatioAlarm("leverage", TH.leverage_levels, TH.leverage_step, higher_is_worse=True)
    assert a.update(1.45).kind == "cross"
    assert a.update(1.45) is None


# ── daily line ────────────────────────────────────────────────────────


def test_daily_line_due():
    mon_0859 = datetime(2026, 11, 9, 8, 59, tzinfo=ET)
    mon_0900 = datetime(2026, 11, 9, 9, 0, tzinfo=ET)
    assert daily_line_due(mon_0859, None) is False
    assert daily_line_due(mon_0900, None) is True
    assert daily_line_due(mon_0900, mon_0900.date()) is False
    assert daily_line_due(datetime(2026, 11, 8, 10, 0, tzinfo=ET), None) is False  # Sunday
