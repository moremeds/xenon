"""Tests for the UW analyze daily job."""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytz  # type: ignore  # noqa: E402
from xenon.api.services.uw_analyze_cache import UwAnalyzeCache  # noqa: E402
from xenon.api.services.uw_analyze_daily_job import (  # noqa: E402
    DAILY_TRIGGER,
    is_trading_day,
    run_once,
    seconds_until_next_trigger,
)
from xenon.api.services.uw_analyze_flow_tracker import (  # noqa: E402
    FlowEvent,
    FlowInitial,
    FlowLog,
    make_event_id,
)
from xenon.api.services.uw_analyze_oi_tracker import OiChange  # noqa: E402

ET = pytz.timezone("America/New_York")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _et(year, month, day, hour=10, minute=0):
    return ET.localize(datetime(year, month, day, hour, minute))


# ── seconds_until_next_trigger ─────────────────────────────────────────────


def test_trigger_later_today_when_before_1550():
    # Wednesday 2026-04-08 at 10:00 ET → next trigger is today at 15:50 ET
    now = _et(2026, 4, 8, 10, 0)
    secs = seconds_until_next_trigger(now)
    expected = (5 * 3600) + (50 * 60)  # 5h50m
    assert abs(secs - expected) < 5


def test_trigger_next_day_when_after_1550():
    # Wednesday 2026-04-08 at 16:00 ET → next trigger is Thursday 15:50 ET
    now = _et(2026, 4, 8, 16, 0)
    secs = seconds_until_next_trigger(now)
    expected_h = (24 - 16) + 15 + 50 / 60  # ~23h50m
    assert secs / 3600 > 20 and secs / 3600 < 25


def test_trigger_skips_weekend_to_monday():
    # Friday 2026-04-10 at 16:00 ET → trigger is Monday 15:50 ET
    now = _et(2026, 4, 10, 16, 0)
    secs = seconds_until_next_trigger(now)
    hours = secs / 3600
    # Friday 16:00 → Mon 15:50 ≈ 71h50m
    assert 65 < hours < 75


def test_trigger_at_exactly_1550_moves_to_next_day():
    now = _et(2026, 4, 8, 15, 50)
    secs = seconds_until_next_trigger(now)
    assert secs > 0  # never returns 0; always picks the future


def test_is_trading_day_weekday():
    assert is_trading_day(_et(2026, 4, 8))  # Wednesday


def test_is_trading_day_weekend():
    assert not is_trading_day(_et(2026, 4, 11))  # Saturday
    assert not is_trading_day(_et(2026, 4, 12))  # Sunday


# ── run_once orchestration ─────────────────────────────────────────────────


def _seed_cache(tmp_path, ticker="NVDA"):
    cache = UwAnalyzeCache(
        cache_path=tmp_path / "cache.json",
        market_open_fn=lambda: True,
    )

    async def runner(t):
        return (
            {"ticker": t, "price": 100, "regime": {"gex_sign": "POSITIVE"}, "scores": {"flow": 5}},
            {
                "iv_rank": 40,
                "max_pain": 100,
                "net_call_premium": 0,
                "net_put_premium": 0,
                "gex_flip": 99,
                "call_wall_strike": 110,
                "put_wall_strike": 90,
            },
            [],
        )

    _run(cache.get_or_run(ticker, runner=runner, sources=["portfolio"]))
    return cache


def test_run_once_attaches_oi_baseline(tmp_path):
    cache = _seed_cache(tmp_path)
    flow_log = FlowLog(path=tmp_path / "flow.json")

    async def fake_oi(ticker, spot):
        return [
            OiChange(
                strike=100,
                side="call",
                prev_oi=1000,
                curr_oi=2500,
                delta=1500,
                delta_pct=1.5,
                label="+1.5K calls @ $100 (+150%)",
            )
        ]

    stats = _run(
        run_once(
            cache=cache,
            flow_log=flow_log,
            uw_client=None,
            oi_fetcher=fake_oi,
        )
    )
    assert stats["tickers_oi"] == 1
    entry = cache.get_entry("NVDA")
    from xenon.api.services.uw_analyze_daily_job import now_et_date

    assert entry["oi_baseline"]["data_date"] == now_et_date().isoformat()
    assert entry["oi_baseline"]["changes"][0]["strike"] == 100


def test_run_once_advances_open_flow_events(tmp_path):
    cache = _seed_cache(tmp_path)
    flow_log = FlowLog(path=tmp_path / "flow.json")
    expiry = (date.today() + timedelta(days=60)).isoformat()
    ev = FlowEvent(
        id=make_event_id("NVDA", "call", 900, expiry, "2026-04-08"),
        ticker="NVDA",
        side="call",
        strike=900,
        expiry=expiry,
        detected_at=datetime.utcnow().isoformat(),
        initial=FlowInitial(premium_usd=4e6, oi=10_000, volume=5000, mid=4.0, underlying_price=870),
    )
    flow_log.upsert(ev)
    flow_log.save()

    async def fake_oi(ticker, spot):
        return []

    async def fake_contract(*, ticker, side, strike, expiry):
        # Premium collapse (mid -75%, underlying flat) — triggers anomaly but
        # OI unchanged so close rule does not fire.
        return {"oi": 10_001, "mid": 1.0, "underlying_price": 870.5}

    stats = _run(
        run_once(
            cache=cache,
            flow_log=flow_log,
            uw_client=None,
            oi_fetcher=fake_oi,
            contract_fetcher=fake_contract,
        )
    )
    assert stats["events_advanced"] == 1
    assert stats["events_anomaly"] == 1
    reloaded = FlowLog(path=tmp_path / "flow.json").for_ticker("NVDA")[0]
    assert reloaded.status == "anomaly"


def test_run_once_skips_already_closed_events(tmp_path):
    cache = _seed_cache(tmp_path)
    flow_log = FlowLog(path=tmp_path / "flow.json")
    expiry = (date.today() + timedelta(days=60)).isoformat()
    ev = FlowEvent(
        id="dummy",
        ticker="NVDA",
        side="call",
        strike=900,
        expiry=expiry,
        detected_at=datetime.utcnow().isoformat(),
        initial=FlowInitial(premium_usd=4e6, oi=1000, volume=5000, mid=4.0, underlying_price=870),
        status="closed",
    )
    flow_log.upsert(ev)
    flow_log.save()

    async def fake_oi(ticker, spot):
        return []

    contract_calls = []

    async def fake_contract(*, ticker, side, strike, expiry):
        contract_calls.append((ticker, side, strike, expiry))
        return None

    _run(
        run_once(
            cache=cache,
            flow_log=flow_log,
            uw_client=None,
            oi_fetcher=fake_oi,
            contract_fetcher=fake_contract,
        )
    )
    assert contract_calls == []  # closed events are skipped


# ════════════════════════════════════════════════════════════════════
# run_loop — double-start suppression + test_trigger_now fire-once
# ════════════════════════════════════════════════════════════════════


def test_run_loop_double_start_suppressed(monkeypatch):
    """Starting run_loop twice should leave _job_running True and skip the
    second invocation cleanly; finally: must reset _job_running to False
    after both tasks are cancelled."""
    from xenon.api.services import uw_analyze_daily_job as job

    job._job_running = False

    async def fake_run_once(**kwargs):
        return {}

    monkeypatch.setattr(job, "run_once", fake_run_once)
    monkeypatch.setattr(job, "seconds_until_next_trigger", lambda: 0.01)

    async def scenario():
        t1 = asyncio.create_task(
            job.run_loop(
                cache=None,
                flow_log=None,
                uw_client=None,
                test_trigger_now=True,
            )
        )
        await asyncio.sleep(0.05)
        t2 = asyncio.create_task(
            job.run_loop(
                cache=None,
                flow_log=None,
                uw_client=None,
                test_trigger_now=True,
            )
        )
        await asyncio.sleep(0.05)
        assert job._job_running is True

        t1.cancel()
        t2.cancel()
        for t in (t1, t2):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    _run(scenario())
    assert job._job_running is False  # cleaned up in finally


def test_run_loop_test_trigger_now_fires_run_once(monkeypatch):
    """With test_trigger_now=True, run_loop should call run_once at least once
    before entering the long sleep."""
    from xenon.api.services import uw_analyze_daily_job as job

    job._job_running = False
    fired = {"count": 0}

    async def fake_run_once(**kwargs):
        fired["count"] += 1

    monkeypatch.setattr(job, "run_once", fake_run_once)
    monkeypatch.setattr(job, "seconds_until_next_trigger", lambda: 999_999)

    async def scenario():
        t = asyncio.create_task(
            job.run_loop(
                cache=None,
                flow_log=None,
                uw_client=None,
                test_trigger_now=True,
            )
        )
        await asyncio.sleep(0.1)
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    _run(scenario())
    assert fired["count"] >= 1
    assert job._job_running is False
