"""Tests for native→USD FX conversion.

All amounts, prices and FX rates below are REAL market snapshots taken from
Interactive Brokers on 2026-06-22 (no synthetic data):

  * 5016  JX Advanced Metals Corp (TSEJ, JPY) — last ¥5,267/share
  * 000660 SK Hynix Inc (KRX, KRW)           — last ₩2,885,000/share
  * USD.JPY (IDEALPRO) = 161.6575 JPY/USD → usd_per_unit[JPY] = 1/161.6575 = 0.006186
  * USD.KRW (IDEALPRO) = 1538.505  KRW/USD → usd_per_unit[KRW] = 1/1538.505 = 0.00065

The ``ExchangeRate`` tag values used in the account-values tests are the same
USD-per-unit rates IB reports for a USD-base account (base = native * rate).
"""

from types import SimpleNamespace

from xenon.utils.fx import SANE_USD_PER_UNIT_BAND, to_usd, usd_per_unit_from_account_values

# Real FX snapshot, 2026-06-22 (USD value of 1 unit of the foreign currency).
JPY_USD_PER_UNIT = 0.006186  # 1 / 161.6575
KRW_USD_PER_UNIT = 0.00065  # 1 / 1538.505
RATES = {"USD": 1.0, "JPY": JPY_USD_PER_UNIT, "KRW": KRW_USD_PER_UNIT}


def test_to_usd_identity_for_usd():
    # A USD amount passes through unchanged (e.g. a US-listed position).
    assert to_usd(1234.0, "USD", {"USD": 1.0}) == 1234.0


def test_to_usd_converts_jpy_position_market_value():
    # 100 shares of 5016 at ¥5,267 = ¥526,700 market value.
    # 526_700 * 0.006186 = 3258.1662 USD → 3258.17.
    assert to_usd(526_700.0, "JPY", RATES) == 3258.17


def test_to_usd_converts_krw_position_market_value():
    # 1 share of 000660 at ₩2,885,000 market value.
    # 2_885_000 * 0.00065 = 1875.25 USD.
    assert to_usd(2_885_000.0, "KRW", RATES) == 1875.25


def test_to_usd_none_amount_returns_none():
    assert to_usd(None, "JPY", RATES) is None


def test_to_usd_missing_rate_returns_none():
    # No KRW rate available → cannot convert, surface None to caller.
    assert to_usd(2_885_000.0, "KRW", {"USD": 1.0}) is None


def test_to_usd_defaults_blank_currency_to_usd():
    assert to_usd(50.0, "", {"USD": 1.0}) == 50.0


def test_usd_per_unit_from_account_values_reads_exchange_rate_tag():
    avs = [
        SimpleNamespace(tag="ExchangeRate", value="0.006186", currency="JPY"),
        SimpleNamespace(tag="ExchangeRate", value="0.00065", currency="KRW"),
        SimpleNamespace(tag="NetLiquidation", value="100000", currency="USD"),
    ]
    out = usd_per_unit_from_account_values(avs)
    assert out["USD"] == 1.0
    assert out["JPY"] == 0.006186
    assert out["KRW"] == 0.00065


def test_usd_per_unit_skips_unparseable_values():
    avs = [SimpleNamespace(tag="ExchangeRate", value="N/A", currency="JPY")]
    out = usd_per_unit_from_account_values(avs)
    assert "JPY" not in out  # bad value skipped, USD identity still present
    assert out["USD"] == 1.0


def test_usd_per_unit_rejects_sentinel_rate():
    # IB emits DBL_MAX (~1.79e308) for unavailable fields; must never be accepted
    # as an FX rate or it inflates every converted USD value astronomically.
    avs = [SimpleNamespace(tag="ExchangeRate", value="1.7976931348623157e308", currency="JPY")]
    out = usd_per_unit_from_account_values(avs)
    assert "JPY" not in out  # sentinel rejected; USD identity remains
    assert out["USD"] == 1.0


def test_sane_band_catches_inverted_jpy_rate():
    lo, hi = SANE_USD_PER_UNIT_BAND["JPY"]
    assert lo <= JPY_USD_PER_UNIT <= hi  # correct direction (0.006186) passes
    assert not (lo <= 161.6575 <= hi)  # inverted (JPY-per-USD) fails
