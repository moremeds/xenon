"""Tests for candidate seeding."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services import uw_analyze_candidates as cand  # noqa: E402


def _write(path, data):
    path.write_text(json.dumps(data))


def test_portfolio_tickers_extracted(tmp_path):
    p = tmp_path / "portfolio.json"
    _write(p, {"positions": [{"ticker": "nvda"}, {"ticker": "AAPL"}, {"symbol": "msft"}]})
    assert cand.portfolio_tickers(p) == {"NVDA", "AAPL", "MSFT"}


def test_portfolio_missing_returns_empty(tmp_path):
    assert cand.portfolio_tickers(tmp_path / "missing.json") == set()


def test_watchlist_tickers_extracted(tmp_path):
    p = tmp_path / "watchlist.json"
    _write(p, {"tickers": [{"ticker": "spy"}, {"ticker": "QQQ"}]})
    assert cand.watchlist_tickers(p) == {"SPY", "QQQ"}


def test_seed_merges_sources(tmp_path):
    port = tmp_path / "p.json"
    watch = tmp_path / "w.json"
    _write(port, {"positions": [{"ticker": "NVDA"}, {"ticker": "MSFT"}]})
    _write(watch, {"tickers": [{"ticker": "NVDA"}, {"ticker": "AAPL"}]})

    out = cand.seed_candidates(portfolio_path=port, watchlist_path=watch, extra_adhoc=["TSLA"])
    assert out["NVDA"] == ["portfolio", "watchlist"]
    assert out["MSFT"] == ["portfolio"]
    assert out["AAPL"] == ["watchlist"]
    assert out["TSLA"] == ["adhoc"]


def test_adhoc_set_persists_across_calls(tmp_path):
    cand.clear_adhoc()
    cand.add_adhoc("foo")
    cand.add_adhoc("bar")
    assert cand.adhoc_set() == {"FOO", "BAR"}
    cand.clear_adhoc()
    assert cand.adhoc_set() == set()


def test_seed_dedupes_within_a_source(tmp_path):
    port = tmp_path / "p.json"
    _write(port, {"positions": [{"ticker": "NVDA"}, {"ticker": "NVDA"}, {"ticker": "nvda"}]})
    out = cand.seed_candidates(portfolio_path=port, watchlist_path=tmp_path / "missing.json")
    assert list(out.keys()) == ["NVDA"]
    assert out["NVDA"] == ["portfolio"]
