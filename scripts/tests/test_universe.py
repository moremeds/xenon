"""Tests for the V1 trading universe registry."""

import dataclasses

import pytest
from xenon.execution.universe import (
    INDEX_UNIVERSE,
    UNIVERSE,
    UniverseEntry,
    get_multiplier,
    is_index,
    is_known,
)


def test_universe_contains_exactly_nine_tickers():
    assert set(UNIVERSE.keys()) == {
        "SPX",
        "NDX",
        "RUT",
        "SPY",
        "QQQ",
        "IWM",
        "GLD",
        "USO",
        "SIL",
    }


def test_index_universe_is_spx_ndx_rut():
    assert INDEX_UNIVERSE == {"SPX", "NDX", "RUT"}


@pytest.mark.parametrize("ticker", ["SPX", "NDX", "RUT"])
def test_index_tickers_are_cash_settled(ticker):
    entry = UNIVERSE[ticker]
    assert entry.is_index is True
    assert entry.cash_settled is True
    assert entry.multiplier == 100


@pytest.mark.parametrize("ticker", ["SPY", "QQQ", "IWM", "GLD", "USO", "SIL"])
def test_etf_tickers_are_deliverable(ticker):
    entry = UNIVERSE[ticker]
    assert entry.is_index is False
    assert entry.cash_settled is False
    assert entry.multiplier == 100


def test_uso_flagged_as_k1():
    assert UNIVERSE["USO"].k1 is True


def test_non_uso_etfs_are_not_k1():
    for t in ("SPY", "QQQ", "IWM", "GLD", "SIL"):
        assert UNIVERSE[t].k1 is False


def test_is_index_helper():
    assert is_index("SPX") is True
    assert is_index("SPY") is False


def test_is_known_helper():
    assert is_known("SPX") is True
    assert is_known("AAPL") is False


def test_get_multiplier_returns_100_for_all_v1_tickers():
    for ticker in UNIVERSE:
        assert get_multiplier(ticker) == 100


def test_get_multiplier_raises_for_unknown():
    with pytest.raises(KeyError):
        get_multiplier("AAPL")


def test_is_index_raises_for_unknown():
    with pytest.raises(KeyError):
        is_index("AAPL")


def test_universe_entry_is_frozen():
    """Registry values should be immutable to prevent accidental mutation."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        UNIVERSE["SPX"].multiplier = 50  # frozen dataclass
