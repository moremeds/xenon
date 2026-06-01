"""Pins the FX conversion convention: divide original by currency_per_usd."""

from datetime import date
from decimal import Decimal

import pytest

from xenon.utils.futu_fx import FxRateMissing, convert_to_usd


def test_hkd_780_at_7_8_yields_100_usd(seed_fx):
    seed_fx("HKD", date(2024, 5, 1), Decimal("7.8"))
    usd = convert_to_usd(Decimal("780"), "HKD", date(2024, 5, 1))
    assert usd == Decimal("100.00")


def test_cny_700_at_7_yields_100_usd(seed_fx):
    seed_fx("CNY", date(2024, 5, 1), Decimal("7.0"))
    usd = convert_to_usd(Decimal("700"), "CNY", date(2024, 5, 1))
    assert usd == Decimal("100.00")


def test_usd_passthrough(seed_fx):
    """USD → USD never touches fx_daily."""
    usd = convert_to_usd(Decimal("123.45"), "USD", date(2024, 5, 1))
    assert usd == Decimal("123.45")


def test_missing_rate_raises_fx_rate_missing(seed_fx):
    # seed_fx fixture ensures DB is up; absence on this date triggers raise.
    with pytest.raises(FxRateMissing):
        convert_to_usd(Decimal("100"), "HKD", date(1999, 1, 1))
