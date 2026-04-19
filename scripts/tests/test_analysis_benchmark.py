from unittest.mock import MagicMock
from xenon.analysis.benchmark import load_benchmark_context, SECTOR_ETF_MAP


def test_sector_etf_map_has_common_sectors():
    assert "Technology" in SECTOR_ETF_MAP
    assert SECTOR_ETF_MAP["Technology"] == "XLK"


def test_load_benchmark_context_spy_only_when_sector_unknown():
    client = MagicMock()
    client.get_volatility_stats.return_value = {"iv_rank": "0.45"}
    client.get_greek_exposure.return_value = {"net": 1e9, "flip": 450.0, "price": 460.0}
    client.get_stock_info.return_value = {}

    ctx = load_benchmark_context(client, ticker_sector=None)
    assert ctx.spy.ticker == "SPY"
    assert ctx.sector_etf is None


def test_load_benchmark_context_with_sector_etf():
    client = MagicMock()
    def vs(ticker):
        return {"iv_rank": "0.40" if ticker == "SPY" else "0.55"}
    def ge(ticker):
        return {"net": 1e9, "flip": 100.0, "price": 105.0}
    client.get_volatility_stats.side_effect = vs
    client.get_greek_exposure.side_effect = ge

    ctx = load_benchmark_context(client, ticker_sector="Technology")
    assert ctx.spy.ticker == "SPY"
    assert ctx.sector_etf is not None
    assert ctx.sector_etf.ticker == "XLK"


def test_load_benchmark_context_degrades_on_spy_fetch_failure():
    client = MagicMock()
    client.get_volatility_stats.side_effect = Exception("boom")
    client.get_greek_exposure.side_effect = Exception("boom")

    ctx = load_benchmark_context(client, ticker_sector=None)
    assert ctx.spy.freshness == "unavailable"


def _real_shape_client():
    """Mock client returning the REAL nested UW payload shapes (probed 2026-04-08)."""
    c = MagicMock()
    c.get_stock_state.return_value = {"data": {"close": "656.635"}}
    c.get_volatility_stats.return_value = {
        "data": {"iv": "0.21", "rv": "0.20", "iv_rank": "16.0252"}
    }
    c.get_greek_exposure.return_value = {
        "data": [
            {"date": "2026-04-07", "call_gamma": "5000000", "put_gamma": "-3000000"}
        ]
    }
    c.get_greek_exposure_by_strike.return_value = {
        "data": [
            {"date": "2026-04-07", "strike": "650", "call_gex": "1.0", "put_gex": "-3.0"},
            {"date": "2026-04-07", "strike": "655", "call_gex": "2.0", "put_gex": "-2.0"},
            {"date": "2026-04-07", "strike": "660", "call_gex": "5.0", "put_gex": "-1.0"},
        ]
    }
    return c


def test_load_benchmark_context_parses_real_nested_shapes():
    """C4 regression — _load_snapshot must parse the real UW nested payloads.
    Pre-fix, vol_stats and greek_exposure were read as flat dicts, so every
    snapshot in production had iv_rank=None, gex_regime=None, price=None,
    gex_flip=None — but freshness still said 'live' because no exception
    fired. Tests passed because they used flat-mock fixtures."""
    c = _real_shape_client()
    ctx = load_benchmark_context(c, ticker_sector=None)
    spy = ctx.spy
    assert spy.iv_rank == 16.0252  # already 0..100, not multiplied
    assert spy.price == 656.635
    assert spy.gex_regime == "positive"  # 5M call_gamma + (-3M) put_gamma > 0
    assert spy.gex_flip is not None
    assert spy.freshness == "live"


def test_load_benchmark_context_iv_rank_legacy_fraction_still_works():
    """Heuristic: values <= 1.0 are treated as legacy 0..1 fractions."""
    c = _real_shape_client()
    c.get_volatility_stats.return_value = {"data": {"iv_rank": "0.42"}}
    ctx = load_benchmark_context(c, ticker_sector=None)
    assert ctx.spy.iv_rank == 42.0  # 0.42 * 100
