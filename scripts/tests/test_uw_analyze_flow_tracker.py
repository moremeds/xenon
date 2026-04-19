"""Tests for the unusual flow lifecycle tracker."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xenon.api.services.uw_analyze_diff import Change  # noqa: E402
from xenon.api.services.uw_analyze_flow_tracker import (  # noqa: E402
    ANOMALY_DTE_GUARD,
    FlowEvent,
    FlowInitial,
    FlowLog,
    advance_daily_track,
    capture_event_from_alert,
    capture_from_changes,
    classify_anomaly,
    make_event_id,
    maybe_close_or_expire,
    progress_event,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _alert(**kw):
    base = {
        "option_type": "call",
        "strike": 900,
        "expiry": "2026-05-15",
        "total_premium": 4_200_000,
        "open_interest": 1000,
        "volume": 5000,
        "mid": 4.50,
    }
    base.update(kw)
    return base


def _make_event(*, expiry=None, initial_mid=4.5, initial_oi=1000, initial_underlying=870.0):
    if expiry is None:
        expiry = (date.today() + timedelta(days=60)).isoformat()
    return FlowEvent(
        id=make_event_id("NVDA", "call", 900, expiry, "2026-04-08"),
        ticker="NVDA",
        side="call",
        strike=900,
        expiry=expiry,
        detected_at=datetime.now(timezone.utc).isoformat(),
        initial=FlowInitial(
            premium_usd=4_200_000,
            oi=initial_oi,
            volume=5000,
            mid=initial_mid,
            underlying_price=initial_underlying,
        ),
    )


# ── make_event_id ──────────────────────────────────────────────────────────


def test_event_id_stable_across_polls():
    """Same contract on the same trade_date → same id, regardless of when called."""
    a = make_event_id("NVDA", "call", 900, "2026-05-15", "2026-04-08")
    b = make_event_id("nvda", "CALL", 900.0, "2026-05-15", "2026-04-08")
    assert a == b


def test_event_id_changes_per_trade_date():
    a = make_event_id("NVDA", "call", 900, "2026-05-15", "2026-04-08")
    b = make_event_id("NVDA", "call", 900, "2026-05-15", "2026-04-09")
    assert a != b


# ── capture_event_from_alert ───────────────────────────────────────────────


def test_capture_basic():
    ev = capture_event_from_alert(
        ticker="nvda",
        side="call",
        alert=_alert(),
        underlying_price=870.0,
        trade_date="2026-04-08",
    )
    assert ev is not None
    assert ev.ticker == "NVDA"
    assert ev.strike == 900
    assert ev.initial.underlying_price == 870.0
    assert ev.status == "open"


def test_capture_returns_none_when_strike_missing():
    ev = capture_event_from_alert(
        ticker="nvda",
        side="call",
        alert=_alert(strike=None),
        underlying_price=870.0,
    )
    assert ev is None


def test_capture_returns_none_when_expiry_missing():
    ev = capture_event_from_alert(
        ticker="nvda",
        side="call",
        alert=_alert(expiry=None),
        underlying_price=870.0,
    )
    assert ev is None


# ── capture_from_changes ────────────────────────────────────────────────────


def test_capture_from_changes_picks_dominant_alert():
    changes = [Change(code="UNUSUAL_CALL_SWEEP", label="x", prev=0, curr=10e6, severity="alert")]
    alerts = [
        _alert(strike=890, total_premium=1_000_000),
        _alert(strike=900, total_premium=4_200_000),  # dominant
        _alert(strike=910, total_premium=2_000_000),
    ]
    out = capture_from_changes(
        ticker="NVDA",
        changes=changes,
        flow_alerts=alerts,
        underlying_price=870.0,
    )
    assert len(out) == 1
    assert out[0].strike == 900


def test_capture_from_changes_skips_when_no_sweep():
    changes = [Change(code="GEX_FLIP_SIGN", label="x", prev="POSITIVE", curr="NEGATIVE", severity="alert")]
    out = capture_from_changes(
        ticker="NVDA",
        changes=changes,
        flow_alerts=[_alert()],
        underlying_price=870.0,
    )
    assert out == []


def test_capture_idempotent_via_id_for_same_trade_date():
    """Calling capture twice on the same day produces an id collision the
    upsert layer can dedupe on."""
    changes = [Change(code="UNUSUAL_CALL_SWEEP", label="x", prev=0, curr=10e6, severity="alert")]
    alerts = [_alert()]
    a = capture_from_changes(
        ticker="NVDA",
        changes=changes,
        flow_alerts=alerts,
        underlying_price=870.0,
        trade_date="2026-04-08",
    )
    b = capture_from_changes(
        ticker="NVDA",
        changes=changes,
        flow_alerts=alerts,
        underlying_price=870.0,
        trade_date="2026-04-08",
    )
    assert a[0].id == b[0].id


# ── advance_daily_track ────────────────────────────────────────────────────


def test_daily_track_appends_row():
    ev = _make_event()
    advance_daily_track(ev, today="2026-04-09", oi=950, mid=4.0, underlying_price=875)
    assert len(ev.daily_track) == 1
    assert ev.daily_track[0].oi == 950


def test_daily_track_idempotent_per_date():
    ev = _make_event()
    advance_daily_track(ev, today="2026-04-09", oi=950, mid=4.0, underlying_price=875)
    advance_daily_track(ev, today="2026-04-09", oi=900, mid=3.0, underlying_price=880)
    assert len(ev.daily_track) == 1  # second call no-op


def test_daily_track_pct_change_uses_initial_mid():
    ev = _make_event(initial_mid=4.0)
    advance_daily_track(ev, today="2026-04-09", oi=1000, mid=2.0, underlying_price=870)
    assert ev.daily_track[0].pct_change_premium == -0.5


# ── classify_anomaly ───────────────────────────────────────────────────────


def test_anomaly_premium_collapse_fires():
    ev = _make_event(initial_mid=4.0, initial_underlying=870.0)
    advance_daily_track(ev, today="2026-04-09", oi=1000, mid=1.0, underlying_price=870.5)
    reason = classify_anomaly(ev)
    assert reason is not None
    assert "premium collapsed" in reason


def test_anomaly_premium_collapse_skipped_when_underlying_moved():
    ev = _make_event(initial_mid=4.0, initial_underlying=870.0)
    # Mid down 75% but underlying moved >1.5%.
    advance_daily_track(ev, today="2026-04-09", oi=1000, mid=1.0, underlying_price=900)
    assert classify_anomaly(ev) is None


def test_anomaly_oi_evaporation_fires():
    ev = _make_event(initial_oi=10_000)
    detected = date.fromisoformat(ev.detected_at[:10])
    today = (detected + timedelta(days=2)).isoformat()
    # Mid steady, OI dropped 60%
    advance_daily_track(ev, today=today, oi=4000, mid=4.5, underlying_price=870.0)
    reason = classify_anomaly(ev)
    assert reason is not None
    assert "OI evaporated" in reason


def test_anomaly_oi_evaporation_skipped_outside_window():
    ev = _make_event(initial_oi=10_000)
    detected = date.fromisoformat(ev.detected_at[:10])
    today = (detected + timedelta(days=10)).isoformat()  # past 3-day window
    advance_daily_track(ev, today=today, oi=4000, mid=4.5, underlying_price=870.0)
    assert classify_anomaly(ev) is None


def test_anomaly_skipped_within_3_dte_guard():
    """Late-cycle decay shouldn't trip the rule."""
    from xenon.api.services.uw_analyze_daily_job import now_et_date

    et_today = now_et_date()
    near_expiry = (et_today + timedelta(days=ANOMALY_DTE_GUARD)).isoformat()
    ev = _make_event(expiry=near_expiry, initial_mid=4.0)
    advance_daily_track(ev, today=et_today.isoformat(), oi=1000, mid=1.0, underlying_price=870.5)
    assert classify_anomaly(ev) is None


# ── maybe_close_or_expire ──────────────────────────────────────────────────


def test_close_when_oi_returns_to_initial():
    ev = _make_event(initial_oi=1000)
    advance_daily_track(ev, today="2026-04-09", oi=900, mid=4.5, underlying_price=870)
    maybe_close_or_expire(ev)
    assert ev.status == "closed"
    assert ev.closed_at is not None


def test_expired_when_past_expiry():
    from xenon.api.services.uw_analyze_daily_job import now_et_date

    past = (now_et_date() - timedelta(days=1)).isoformat()
    ev = _make_event(expiry=past)
    maybe_close_or_expire(ev)
    assert ev.status == "expired"


def test_close_only_when_oi_falls_below_initial():
    ev = _make_event(initial_oi=1000)
    advance_daily_track(ev, today="2026-04-09", oi=2000, mid=4.5, underlying_price=870)
    maybe_close_or_expire(ev)
    assert ev.status == "open"


# ── progress_event end-to-end ──────────────────────────────────────────────


def test_progress_event_anomaly_can_still_expire():
    """An event already flagged 'anomaly' must still transition to 'expired'
    when its expiry date passes."""
    from datetime import date, timedelta

    from xenon.api.services.uw_analyze_flow_tracker import FlowEvent, FlowInitial, progress_event

    expired_date = (date.today() - timedelta(days=1)).isoformat()
    ev = FlowEvent(
        id="x",
        ticker="AAPL",
        side="call",
        strike=100,
        expiry=expired_date,
        detected_at="2026-04-08T15:50:00+00:00",
        initial=FlowInitial(premium_usd=7e6, oi=1000, volume=4000, mid=2.5, underlying_price=99),
        status="anomaly",
        anomaly_reason="premium collapsed -70%",
    )
    progress_event(ev, today=date.today().isoformat(), oi=950, mid=0.7, underlying_price=99, volume=200)
    assert ev.status == "expired"
    assert ev.anomaly_reason == "premium collapsed -70%"


def test_progress_event_anomaly_can_still_close():
    from xenon.api.services.uw_analyze_flow_tracker import FlowEvent, FlowInitial, progress_event

    ev = FlowEvent(
        id="y",
        ticker="AAPL",
        side="call",
        strike=100,
        expiry="2027-01-15",
        detected_at="2026-04-08T15:50:00+00:00",
        initial=FlowInitial(premium_usd=7e6, oi=1000, volume=4000, mid=2.5, underlying_price=99),
        status="anomaly",
        anomaly_reason="OI evaporated -60%",
    )
    progress_event(ev, today="2026-04-10", oi=900, mid=2.5, underlying_price=99, volume=100)
    assert ev.status == "closed"


def test_progress_event_marks_anomaly_first():
    ev = _make_event(initial_mid=4.0)
    progress_event(ev, today="2026-04-09", oi=1000, mid=1.0, underlying_price=870.5)
    assert ev.status == "anomaly"
    assert ev.anomaly_reason and "premium collapsed" in ev.anomaly_reason


# ── FlowLog persistence ────────────────────────────────────────────────────


def test_flow_log_upsert_and_save_round_trip(tmp_path):
    log = FlowLog(path=tmp_path / "flow.json")
    ev = _make_event()
    assert log.upsert(ev) is True  # newly added
    log.save()
    raw = json.loads((tmp_path / "flow.json").read_text())
    assert ev.id in raw["events"]
    assert raw["events"][ev.id]["ticker"] == "NVDA"

    log2 = FlowLog(path=tmp_path / "flow.json")
    loaded = log2.for_ticker("NVDA")
    assert len(loaded) == 1
    assert loaded[0].id == ev.id


def test_flow_log_upsert_idempotent_on_id(tmp_path):
    log = FlowLog(path=tmp_path / "flow.json")
    ev1 = _make_event()
    ev2 = _make_event()  # same id
    log.upsert(ev1)
    log.upsert(ev2)
    log.save()
    assert len(log.all()) == 1


def test_flow_log_corrupt_file_starts_empty(tmp_path):
    p = tmp_path / "flow.json"
    p.write_text("not json {")
    log = FlowLog(path=p)
    log.load()
    assert log.all() == []


# ── Memory bounds ──────────────────────────────────────────────────────────


def test_daily_track_capped_at_max_rows():
    """advance_daily_track drops the oldest rows once MAX_DAILY_TRACK_ROWS
    is exceeded — classify_anomaly only reads the latest row, so retaining
    more is pure memory cost."""
    from xenon.api.services.uw_analyze_flow_tracker import MAX_DAILY_TRACK_ROWS

    ev = _make_event()
    # Push 2× the cap worth of days.
    for i in range(MAX_DAILY_TRACK_ROWS * 2):
        day = (date.today() + timedelta(days=i)).isoformat()
        advance_daily_track(ev, today=day, oi=1000 - i, mid=4.5, underlying_price=870.0)
    assert len(ev.daily_track) == MAX_DAILY_TRACK_ROWS
    # Latest row preserved.
    assert ev.daily_track[-1].date == (date.today() + timedelta(days=MAX_DAILY_TRACK_ROWS * 2 - 1)).isoformat()


def test_flow_log_purge_removes_old_closed_events(tmp_path):
    """purge() drops closed/expired events older than the cutoff and leaves
    recent + still-open events alone."""
    log = FlowLog(path=tmp_path / "flow.json")
    # Old closed event
    old = _make_event(expiry="2025-01-15")
    old.id = "old"
    old.status = "closed"
    old.closed_at = "2025-02-01"
    # Recent closed event
    recent = _make_event(expiry=(date.today() + timedelta(days=5)).isoformat())
    recent.id = "recent"
    recent.status = "closed"
    recent.closed_at = (date.today() - timedelta(days=2)).isoformat()
    # Still open — must survive regardless of age.
    still_open = _make_event(expiry="2025-03-01")
    still_open.id = "open"
    still_open.status = "open"

    log.upsert(old)
    log.upsert(recent)
    log.upsert(still_open)

    removed = log.purge(today=date.today())
    assert removed == 1
    ids = {e.id for e in log.all()}
    assert "old" not in ids
    assert "recent" in ids
    assert "open" in ids


def test_classify_anomaly_closing_volume_spike():
    from xenon.api.services.uw_analyze_flow_tracker import FlowDailyRow, FlowEvent, FlowInitial, classify_anomaly

    ev = FlowEvent(
        id="x",
        ticker="AAPL",
        side="call",
        strike=100,
        expiry="2026-09-15",
        detected_at="2026-04-08T15:50:00+00:00",
        initial=FlowInitial(premium_usd=7e6, oi=1000, volume=4000, mid=2.5, underlying_price=99),
    )
    ev.daily_track.append(
        FlowDailyRow(
            date="2026-04-09",
            oi=950,
            mid=2.4,
            underlying_price=99.5,
            pct_change_premium=-0.04,
            volume=850,
        )
    )
    reason = classify_anomaly(ev)
    assert reason and "closing volume" in reason.lower()


def test_classify_anomaly_closing_volume_silent_below_threshold():
    from xenon.api.services.uw_analyze_flow_tracker import FlowDailyRow, FlowEvent, FlowInitial, classify_anomaly

    ev = FlowEvent(
        id="x",
        ticker="AAPL",
        side="call",
        strike=100,
        expiry="2026-09-15",
        detected_at="2026-04-08T15:50:00+00:00",
        initial=FlowInitial(premium_usd=7e6, oi=1000, volume=4000, mid=2.5, underlying_price=99),
    )
    ev.daily_track.append(
        FlowDailyRow(
            date="2026-04-09",
            oi=950,
            mid=2.4,
            underlying_price=99.5,
            pct_change_premium=-0.04,
            volume=500,
        )
    )
    assert classify_anomaly(ev) is None


def test_oi_evaporation_fires_on_trading_day_window_despite_calendar_days():
    """Friday detection, next Wednesday evaluation: 5 calendar days
    (would be silent under calendar rule) but only 3 trading days (fires)."""
    from xenon.api.services.uw_analyze_flow_tracker import FlowDailyRow, FlowEvent, FlowInitial, classify_anomaly

    ev = FlowEvent(
        id="x",
        ticker="AAPL",
        side="call",
        strike=100,
        expiry="2026-09-15",
        detected_at="2026-04-17T20:00:00+00:00",  # Fri
        initial=FlowInitial(premium_usd=7e6, oi=1000, volume=4000, mid=2.5, underlying_price=99),
    )
    # Wed 2026-04-22: trading days = Mon 4-20, Tue 4-21, Wed 4-22 = 3
    ev.daily_track.append(
        FlowDailyRow(
            date="2026-04-22",
            oi=400,
            mid=2.4,
            underlying_price=99.0,
            pct_change_premium=-0.04,
            volume=100,
        )
    )
    reason = classify_anomaly(ev)
    assert reason and "OI evaporated" in reason, "3 trading days is within the window"


def test_oi_evaporation_silent_beyond_trading_day_window():
    """4 trading days > 3-day window — silent."""
    from xenon.api.services.uw_analyze_flow_tracker import FlowDailyRow, FlowEvent, FlowInitial, classify_anomaly

    ev = FlowEvent(
        id="x",
        ticker="AAPL",
        side="call",
        strike=100,
        expiry="2026-09-15",
        detected_at="2026-04-16T20:00:00+00:00",  # Thu
        initial=FlowInitial(premium_usd=7e6, oi=1000, volume=4000, mid=2.5, underlying_price=99),
    )
    # Wed 2026-04-22: Fri 4-17, Mon 4-20, Tue 4-21, Wed 4-22 = 4 trading days
    ev.daily_track.append(
        FlowDailyRow(
            date="2026-04-22",
            oi=400,
            mid=2.4,
            underlying_price=99.0,
            pct_change_premium=-0.04,
            volume=100,
        )
    )
    assert classify_anomaly(ev) is None


def test_classify_anomaly_closing_volume_dte_guard():
    from datetime import date, timedelta

    from xenon.api.services.uw_analyze_flow_tracker import FlowDailyRow, FlowEvent, FlowInitial, classify_anomaly

    near_expiry = (date.today() + timedelta(days=2)).isoformat()
    ev = FlowEvent(
        id="x",
        ticker="AAPL",
        side="call",
        strike=100,
        expiry=near_expiry,
        detected_at="2026-04-08T15:50:00+00:00",
        initial=FlowInitial(premium_usd=7e6, oi=1000, volume=4000, mid=2.5, underlying_price=99),
    )
    ev.daily_track.append(
        FlowDailyRow(
            date="2026-04-09",
            oi=950,
            mid=2.4,
            underlying_price=99.5,
            pct_change_premium=-0.04,
            volume=900,
        )
    )
    assert classify_anomaly(ev) is None
