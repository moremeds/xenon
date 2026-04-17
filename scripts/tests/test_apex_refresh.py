"""Unit tests for scripts.apex_refresh."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_cli_rejects_unknown_mode():
    from scripts.apex_refresh import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--mode", "wibble"])


def test_cli_accepts_incremental_and_full():
    from scripts.apex_refresh import build_parser

    parser = build_parser()
    args = parser.parse_args(["--mode", "incremental"])
    assert args.mode == "incremental"
    args = parser.parse_args(["--mode", "full", "--timeframes", "1d"])
    assert args.mode == "full"
    assert args.timeframes == ["1d"]


def test_load_universe_returns_tickers_and_timeframes():
    from scripts.apex_refresh import load_universe

    fake_r2 = MagicMock()
    fake_r2.get_json.return_value = {
        "tickers": [
            {"symbol": "AAPL", "timeframes": ["1d", "1h"]},
            {"symbol": "MSFT", "timeframes": ["1d"]},
        ]
    }
    rows = load_universe(fake_r2)
    assert len(rows) == 2
    assert rows[0]["symbol"] == "AAPL"
    fake_r2.get_json.assert_called_once_with("meta/universe.json")


def test_load_universe_raises_on_empty_tickers():
    from scripts.apex_refresh import load_universe

    fake_r2 = MagicMock()
    fake_r2.get_json.return_value = {"tickers": []}
    with pytest.raises(RuntimeError, match="no tickers"):
        load_universe(fake_r2)


def test_expand_targets_only_includes_requested_timeframes():
    from scripts.apex_refresh import expand_targets

    universe = [
        {"symbol": "AAPL", "timeframes": ["1d", "1h", "4h"]},
        {"symbol": "BOND", "timeframes": ["1d"]},  # no 1h
    ]
    targets = list(expand_targets(universe, timeframes=("1d", "1h")))
    assert ("AAPL", "1d") in targets
    assert ("AAPL", "1h") in targets
    assert ("BOND", "1d") in targets
    assert ("BOND", "1h") not in targets


def test_expand_targets_skips_rows_without_symbol():
    from scripts.apex_refresh import expand_targets

    universe = [
        {"symbol": "AAPL", "timeframes": ["1d"]},
        {"timeframes": ["1d"]},  # missing symbol
    ]
    targets = list(expand_targets(universe, timeframes=("1d",)))
    assert targets == [("AAPL", "1d")]
