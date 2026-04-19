"""Tests for Thresholds-driven detector tuning + Type F dedupe + funnel."""
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

from scripts.analysis.models import TickerData
from scripts.uw_scan import ScanConfig, scan_universe
from scripts.uw_scan_lib.confluence import is_type_f
from scripts.uw_scan_lib.models import (
    DEFAULT_THRESHOLDS,
    WIDE_THRESHOLDS,
    SignalHit,
    Thresholds,
)
from scripts.uw_scan_lib.signals.dark_pool_accumulation import detect as dp_detect
from scripts.uw_scan_lib.signals.deep_conviction_flow import detect as dcf_detect
from scripts.uw_scan_lib.signals.earnings_iv_crush import detect as eic_detect


def _td(*, flow_alerts=None, iv_pct=None, earnings_within_14d=False,
        earnings_date=None, darkpool=None):
    return TickerData(
        ticker="X", price=100.0, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=None, rv=None, iv_percentile=iv_pct,
        term_structure=None, rr_skew_25d=None, vrp_history=None,
        flow_alerts=flow_alerts, net_premium=None, pcr=None, darkpool=darkpool,
        oi_changes=None, short_interest=None,
        earnings_date=earnings_date, earnings_within_14d=earnings_within_14d,
    )


# ── Thresholds: DCF ─────────────────────────────────────────────────

_DCF_BASE = {
    "volume": 5000, "open_interest": 1000, "ask_side_percent": "0.85",
    "total_premium": 400_000, "multileg_percent": "0.05",
    "moneyness": "0.03", "expiry_dte": 21,
}


def test_dcf_default_thresholds_reject_below_500k():
    # 400K premium below default $500K floor → no hit
    assert dcf_detect("X", _td(flow_alerts=[dict(_DCF_BASE)])) is None


def test_dcf_wide_thresholds_accept_300k_band():
    # Same 400K premium passes WIDE thresholds ($300K floor)
    hit = dcf_detect(
        "X",
        _td(flow_alerts=[dict(_DCF_BASE)]),
        thresholds=WIDE_THRESHOLDS,
    )
    assert hit is not None
    assert hit.score > 0


# ── Thresholds: EIC ─────────────────────────────────────────────────

def test_eic_default_thresholds_reject_60_iv_pctl():
    td = _td(iv_pct=65.0, earnings_within_14d=True,
             earnings_date=date.today() + timedelta(days=5))
    assert eic_detect("X", td) is None  # 65 < default 75


def test_eic_wide_thresholds_accept_60_iv_pctl():
    td = _td(iv_pct=65.0, earnings_within_14d=True,
             earnings_date=date.today() + timedelta(days=5))
    hit = eic_detect("X", td, thresholds=WIDE_THRESHOLDS)
    assert hit is not None  # 65 >= wide 60


# ── Thresholds: DP not loosened (intentional) ───────────────────────

def test_dp_thresholds_unchanged_in_wide_mode():
    # Verify WIDE_THRESHOLDS does NOT loosen DP — see ranking.py invariant
    assert WIDE_THRESHOLDS.dp_min_print_premium == DEFAULT_THRESHOLDS.dp_min_print_premium
    assert WIDE_THRESHOLDS.dp_min_prints == DEFAULT_THRESHOLDS.dp_min_prints


# ── Type F dedupe ───────────────────────────────────────────────────

def _hit(signal_type, tier=1):
    return SignalHit(
        ticker="X", signal_type=signal_type, tier=tier,
        score=0.8, evidence={}, freshness="live",
    )


def test_type_f_requires_two_distinct_classes():
    # Two non-DP signals → Type F
    assert is_type_f([_hit("deep_conviction_flow"), _hit("gex_pinning")])


def test_type_f_dedupes_dcf_plus_eic():
    # DCF + EIC alone share a single pre-earnings catalyst → NOT Type F
    assert not is_type_f([
        _hit("deep_conviction_flow"),
        _hit("earnings_iv_crush"),
    ])


def test_type_f_dcf_plus_eic_plus_third_is_type_f():
    # DCF + EIC + GEX pinning → Type F (3 distinct after dedupe = 2)
    assert is_type_f([
        _hit("deep_conviction_flow"),
        _hit("earnings_iv_crush"),
        _hit("gex_pinning"),
    ])


# ── ProcessResult / funnel ──────────────────────────────────────────

def _full_td(ticker, **kw):
    return TickerData(
        ticker=ticker, price=100.0, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=None, rv=None, iv_percentile=None, term_structure=None,
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=kw.get("flow_alerts"), net_premium=None, pcr=None,
        darkpool=None, oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=False,
    )


_QUALIFYING_ALERT = {
    "volume": 5000, "open_interest": 1000, "ask_side_percent": "0.85",
    "total_premium": 2_000_000, "multileg_percent": "0.05",
    "moneyness": "0.03", "expiry_dte": 21,
}


def test_funnel_counts_add_up(monkeypatch):
    # 4 tickers: 1 fetch_failed, 1 no_hits, 2 ok
    def fake_fetch(ticker, client):
        if ticker == "FAIL":
            raise RuntimeError("boom")
        if ticker == "NOHIT":
            return _full_td("NOHIT")
        return _full_td(ticker, flow_alerts=[dict(_QUALIFYING_ALERT)])

    monkeypatch.setattr("scripts.uw_scan.fetch_ticker_data", fake_fetch)

    cfg = ScanConfig(
        mode="targeted",
        tickers=["FAIL", "NOHIT", "TSLA", "NVDA"],
        full=True,
    )
    result = scan_universe(cfg, client=MagicMock())
    f = result["funnel"]
    assert f["universe"] == 4
    assert f["fetched"] == 3       # FAIL excluded
    assert f["regime_passed"] == 3
    assert f["with_hits"] == 2     # TSLA + NVDA
    # type_f requires 2+ signal classes; these only have DCF
    assert f["type_f"] == 0
    assert "funnel" in result
    assert result["universe_mode"] == "targeted"


# ── Market mode wiring ──────────────────────────────────────────────

def test_market_mode_calls_discover(monkeypatch):
    """load_universe(mode='market') delegates to discover.discover()."""
    from scripts.uw_scan_lib import universe as universe_module

    # Stub discover() so we don't hit the real UW API
    fake_discover = MagicMock(return_value={
        "candidates": [
            {"ticker": "TSLA"},
            {"ticker": "nvda"},  # lowercase to test normalization
            {"ticker": "AAPL"},
        ]
    })
    monkeypatch.setattr("scripts.discover.discover", fake_discover)

    out = universe_module.load_universe(
        mode="market",
        market_min_premium=300_000,
        market_top=80,
    )
    assert out == ["TSLA", "NVDA", "AAPL"]
    fake_discover.assert_called_once()
    call_kwargs = fake_discover.call_args.kwargs
    assert call_kwargs["min_premium"] == 300_000
    assert call_kwargs["top"] == 80


def test_market_mode_handles_discover_failure(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("UW down")
    monkeypatch.setattr("scripts.discover.discover", boom)
    from scripts.uw_scan_lib import universe as universe_module
    assert universe_module.load_universe(mode="market") == []


# ── Wide flag flows through to ScanConfig ───────────────────────────

def test_scan_config_wide_uses_wide_thresholds():
    cfg = ScanConfig(mode="targeted", tickers=["X"], wide=True,
                     thresholds=WIDE_THRESHOLDS)
    assert cfg.thresholds is WIDE_THRESHOLDS
    assert cfg.thresholds.dcf_min_premium == 300_000
