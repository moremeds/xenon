from unittest.mock import MagicMock
from scripts.analysis.benchmark import load_benchmark_context, SECTOR_ETF_MAP


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
