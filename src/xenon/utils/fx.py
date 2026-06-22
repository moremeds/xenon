"""Native-currency → USD conversion for multi-currency IB portfolios.

IB returns position market values, avg cost, and unrealized P&L in the
contract's NATIVE currency (JPY for TSEJ, KRW for KRX) — verified against
ib_async v2.1.0 PortfolioItem semantics and IB's updatePortfolio callback.
This module normalizes rates into ONE convention: ``usd_per_unit[currency]``
is the USD value of 1 unit of that currency, so ``usd = native * rate``.

Two upstream rate sources exist and are reciprocals:
  * IB ``accountValues()`` ``ExchangeRate`` tag — already USD-per-unit for a
    USD-base account (base = native * ExchangeRate). Used here (backend).
  * IB forex tick ``USD.JPY`` = JPY-per-USD — inverted (1/tick) on the
    frontend. See web/lib/fx.ts.
"""

from __future__ import annotations

import math

# Plausibility bands for usd_per_unit, used by tests + a runtime sanity log to
# catch an inverted-direction rate before it corrupts USD values. Wide on
# purpose — only meant to catch 1/x mistakes, not small drift.
SANE_USD_PER_UNIT_BAND: dict[str, tuple[float, float]] = {
    "JPY": (0.002, 0.02),  # ~ 1/500 .. 1/50
    "KRW": (0.0003, 0.003),  # ~ 1/3300 .. 1/330
    "USD": (1.0, 1.0),
}

# Upper bound for a believable USD-per-unit rate. Even the weakest currencies
# (IDR ~6e-5, VND ~4e-5) are far below 1.0; a USD-per-unit rate >= this is a
# sentinel or inverted value, never a real FX rate. Catches IB's DBL_MAX sentinel.
_PLAUSIBLE_RATE_MAX = 100.0


def to_usd(amount: float | None, currency: str | None, usd_per_unit: dict[str, float]) -> float | None:
    """Convert a native amount to USD. Returns None when amount is None or no
    rate is available for the currency (caller decides how to surface that)."""
    if amount is None:
        return None
    cur = (currency or "USD").upper()
    if cur == "USD":
        return float(amount)
    rate = usd_per_unit.get(cur)
    if rate is None or rate <= 0:
        return None
    return round(float(amount) * rate, 2)


def usd_per_unit_from_account_values(account_values: list, base_currency: str = "USD") -> dict[str, float]:
    """Build usd_per_unit from IB accountValues() ExchangeRate rows.

    IB's ExchangeRate tag value is the rate of <currency> to the base currency,
    i.e. base_value = native_value * ExchangeRate, so for a USD-base account
    usd_per_unit[currency] = float(value)."""
    out: dict[str, float] = {base_currency.upper(): 1.0}
    for av in account_values:
        if getattr(av, "tag", None) != "ExchangeRate":
            continue
        cur = (getattr(av, "currency", "") or "").upper()
        if not cur:
            continue
        try:
            rate = float(getattr(av, "value", None))
        except (TypeError, ValueError):
            continue
        # Adversarial guard: IB emits sentinel values (DBL_MAX ~ 1.79e308) for
        # unavailable fields. A sentinel rate would inflate every USD value
        # astronomically. Accept only plausible, finite rates.
        if not math.isfinite(rate) or not (0 < rate < _PLAUSIBLE_RATE_MAX):
            continue
        out[cur] = rate
    return out
