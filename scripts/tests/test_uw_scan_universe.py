import json

from xenon.scanners.uw.universe import load_universe


def test_targeted_mode_returns_explicit_list():
    tickers = load_universe(mode="targeted", tickers=["aapl", "MSFT"])
    assert tickers == ["AAPL", "MSFT"]


def test_targeted_mode_deduplicates():
    tickers = load_universe(mode="targeted", tickers=["AAPL", "aapl", "MSFT"])
    assert tickers == ["AAPL", "MSFT"]


def test_watchlist_mode_reads_watchlist_json(tmp_path):
    watch = tmp_path / "watchlist.json"
    watch.write_text(json.dumps({
        "tickers": [
            {"ticker": "AAPL"}, {"ticker": "msft"}, {"ticker": "NVDA"},
        ]
    }))
    tickers = load_universe(mode="watchlist", watchlist_path=str(watch))
    assert "AAPL" in tickers and "MSFT" in tickers and "NVDA" in tickers
