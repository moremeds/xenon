"""Bearish pipeline tests — mirrored scoring against the bullish gate."""

from unittest.mock import MagicMock


def test_passes_bearish_gate_mirrors_bullish_logic():
    from scripts.trend_scan_lib.stages.ta_prefilter import passes_bearish_gate

    # Clearly bearish: close below MA20, weak RSI, liquid
    assert passes_bearish_gate(
        close=95.0,
        ma_20=100.0,
        rsi=35.0,
        dollar_volume=50_000_000,
        min_dollar_volume=10_000_000,
    )

    # Rejects on RSI too high (not actually weak)
    assert not passes_bearish_gate(
        close=95.0,
        ma_20=100.0,
        rsi=65.0,
        dollar_volume=50_000_000,
        min_dollar_volume=10_000_000,
    )


def test_detect_breakdown_mirrors_breakout():
    from scripts.trend_scan_lib.stages.ta_prefilter import detect_breakdown

    # Close below 20d low with tight consolidation = breakdown
    assert detect_breakdown(
        close=94.0,
        low_52w=90.0,
        low_20d=95.0,
        range_20d_pct=0.03,
        atr_pct=0.02,
    )
    # Not near 52w low, not below 20d low = no breakdown
    assert not detect_breakdown(
        close=100.0,
        low_52w=80.0,
        low_20d=95.0,
        range_20d_pct=0.08,
        atr_pct=0.02,
    )


def test_scan_emits_both_directions_when_universe_has_both(monkeypatch):
    """Feed the scanner one bullish and one bearish mock ticker via a fake
    DataFetcher; both must appear in output with correct direction labels."""
    from scripts.trend_scan import run_scan_pipeline
    from scripts.trend_scan_lib.config import TrendScanConfig

    def mock_ohlcv(ticker, bullish):
        base = 150 if bullish else 95
        return {
            "ticker": ticker,
            "close": base,
            "ma_20": base - 5 if bullish else base + 5,
            "ma_50": base - 10 if bullish else base + 10,
            "ma_200": base - 20 if bullish else base + 20,
            "rsi": 62 if bullish else 35,
            "adx": 32,
            "macd": 1.5 if bullish else -1.5,
            "macd_signal": 1.0 if bullish else -1.0,
            "macd_histogram": 0.5 if bullish else -0.5,
            "rs_vs_spy": 1.15 if bullish else 0.85,
            "ma_20_series": [base - i for i in range(5)] if bullish else [base + i for i in range(5)],
            "recent_avg_volume": 1_500_000,
            "avg_20d_volume": 1_000_000,
            "recent_up_ratio": 0.7 if bullish else 0.3,
            "up_day_volume_ratio": 1.3 if bullish else 0.7,
            "bbw": 0.05,
            "high_52w": 152 if bullish else 120,
            "high_20d": 151 if bullish else 102,
            "low_20d": 140 if bullish else 95,
            "low_52w": 130 if bullish else 80,
            "range_20d_pct": 0.04,
            "atr_pct": 0.015,
            "dollar_volume": 20_000_000,
            "market_cap": 2_000_000_000,
            "price": base,
        }

    class FakeDataFetcher:
        def fetch_ohlcv(self, ticker):
            return mock_ohlcv(ticker, bullish=(ticker == "BULL"))

        def fetch_structure(self, ticker):
            bullish = ticker == "BULL"
            return {
                "spot": 150 if bullish else 95,
                "max_pain": 148 if bullish else 97,
                "gex_at_spot": 0,
                "gamma_flip": 145 if bullish else 97,
                "net_gex": 1e9,
                "call_wall": 160 if bullish else 97,
                "put_wall": 145 if bullish else 85,
                "net_call_oi_change": 5000 if bullish else -500,
                "net_put_oi_change": -500 if bullish else 5000,
            }

        def fetch_volatility(self, ticker):
            return {"iv_rank": 45, "term_structure": "normal", "earnings_days": 30}

        def fetch_flow(self, ticker):
            return {
                "ask_dominance": 0.7 if ticker == "BULL" else 0.3,
                "flow_count": 20,
                "expiry_cluster_ratio": 0.6,
                "avg_strike_pct_otm": 0.05,
                "net_delta": 1e6 if ticker == "BULL" else -1e6,
                "net_vega": 5e5,
                "dp_direction": "bullish" if ticker == "BULL" else "bearish",
            }

        def fetch_market_context(self):
            return {"spy_close": 500.0, "vix_close": 15.0, "regime": "bullish"}

    import scripts.trend_scan as ts

    monkeypatch.setattr(ts, "build_universe", lambda cfg, **k: ["BULL", "BEAR"])
    monkeypatch.setattr(ts, "_resolve_universe", lambda **k: ["BULL", "BEAR"])

    cfg = TrendScanConfig(top_n=10)
    result = run_scan_pipeline(
        cfg,
        data_fetcher=FakeDataFetcher(),
        uw_client=None,
        ib_client=None,
        db_path=":memory:",
    )

    candidates = result["candidates"]
    directions = {c["direction"] for c in candidates}
    assert directions == {"bullish", "bearish"}, (
        f"expected both directions, got {directions} from {len(candidates)} candidates"
    )
    bull_cand = next(c for c in candidates if c["direction"] == "bullish")
    bear_cand = next(c for c in candidates if c["direction"] == "bearish")
    assert bull_cand["ticker"] == "BULL"
    assert bear_cand["ticker"] == "BEAR"
