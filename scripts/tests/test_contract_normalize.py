"""Tests for contract normalization helpers."""

import pytest

from xenon.execution.contract_normalize import (
    NormalizationError,
    normalize_expiry,
    normalize_ticker,
    resolve_multiplier,
)

# --- normalize_expiry ---


def test_normalize_expiry_accepts_yyyymmdd():
    assert normalize_expiry("20260117") == "20260117"


def test_normalize_expiry_strips_dashes():
    assert normalize_expiry("2026-01-17") == "20260117"


def test_normalize_expiry_strips_slashes():
    assert normalize_expiry("2026/01/17") == "20260117"


def test_normalize_expiry_rejects_too_short():
    with pytest.raises(NormalizationError):
        normalize_expiry("20260")


def test_normalize_expiry_rejects_non_digit():
    with pytest.raises(NormalizationError):
        normalize_expiry("2026JAN17")


def test_normalize_expiry_rejects_empty():
    with pytest.raises(NormalizationError):
        normalize_expiry("")


def test_normalize_expiry_rejects_none():
    with pytest.raises(NormalizationError):
        normalize_expiry(None)  # type: ignore[arg-type]


def test_normalize_expiry_rejects_impossible_month():
    with pytest.raises(NormalizationError):
        normalize_expiry("20261317")  # month 13


def test_normalize_expiry_rejects_impossible_day():
    with pytest.raises(NormalizationError):
        normalize_expiry("20260132")  # day 32


def test_normalize_expiry_accepts_feb_29_leap():
    assert normalize_expiry("20240229") == "20240229"


def test_normalize_expiry_rejects_feb_29_non_leap():
    with pytest.raises(NormalizationError):
        normalize_expiry("20230229")


def test_normalize_expiry_rejects_inner_whitespace():
    """Inner whitespace like '2026 117' must not be silently accepted."""
    with pytest.raises(NormalizationError):
        normalize_expiry("2026 117")


def test_normalize_expiry_rejects_inner_tab():
    with pytest.raises(NormalizationError):
        normalize_expiry("2026\t117")


def test_normalize_expiry_rejects_unicode_digits():
    """Fullwidth Unicode digits must not leak through — downstream IB calls require ASCII."""
    with pytest.raises(NormalizationError):
        normalize_expiry("２０２６０１１７")


def test_normalize_expiry_canonicalizes_leap_date():
    """Leap-day input is returned as canonical ASCII YYYYMMDD."""
    assert normalize_expiry("2024-02-29") == "20240229"


# --- normalize_ticker ---


def test_normalize_ticker_uppercases():
    assert normalize_ticker("spx") == "SPX"


def test_normalize_ticker_strips_whitespace():
    assert normalize_ticker("  spy  ") == "SPY"


def test_normalize_ticker_rejects_unknown():
    with pytest.raises(NormalizationError):
        normalize_ticker("AAPL")


def test_normalize_ticker_rejects_empty():
    with pytest.raises(NormalizationError):
        normalize_ticker("")


# --- resolve_multiplier ---


def test_resolve_multiplier_all_v1_are_100():
    for ticker in ("SPX", "NDX", "RUT", "SPY", "QQQ", "IWM", "GLD", "USO", "SIL"):
        assert resolve_multiplier(ticker) == 100


def test_resolve_multiplier_rejects_unknown():
    with pytest.raises(NormalizationError):
        resolve_multiplier("AAPL")
