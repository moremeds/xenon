"""FUTU cash-flow loader — bridges xenon.futu_cash_flow to per-day series.

Sign convention for the returned series (matches the daily-return formula
in performance.py::_futu_returns):

  positive = external deposit / transfer-in (NAV went up partly because
             money came in, not because of investment performance)
  negative = withdrawal / transfer-out (NAV went down partly because money
             left the account)

The caller subtracts this from the NAV delta to isolate investment-driven
return:  r_t = (NAV_t - NAV_{t-1} - flow_t) / NAV_{t-1}.
"""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Mapping

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncEngine

from xenon.db.queries.futu_history import list_cashflows
from xenon.execution.account_scope import AccountScope

# cashflow_type → sign multiplier.
# Deposits/transfers-in are positive (money INTO account).
# Withdrawals/transfers-out are negative.
_TYPE_SIGN: Mapping[str, int] = {
    "DEPOSIT": +1,
    "TRANSFER_IN": +1,
    "WITHDRAW": -1,
    "TRANSFER_OUT": -1,
}

# Statement boundaries are in ET (NYSE session timezone). A flow at 23:30 UTC
# on day D should land on calendar day D in ET if before midnight ET.
_ET = zoneinfo.ZoneInfo("America/New_York")


def _occurred_date_et(occurred_at: datetime) -> date:
    """Convert a tz-aware UTC timestamp into the calendar date in ET."""
    if occurred_at.tzinfo is None:
        # Defensive — column is TIMESTAMP WITH TIME ZONE so this shouldn't fire.
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    return occurred_at.astimezone(_ET).date()


async def load_futu_flows_per_day(
    engine: AsyncEngine,
    scope: AccountScope,
    *,
    since: date,
    until: date,
) -> pd.Series:
    """Return pd.Series indexed by date (sorted, deduplicated), values are
    signed daily net external flow in USD.

    Empty index (no rows) returns an empty Series so callers can safely
    ``.reindex(curve_dates, fill_value=0.0)``.
    """
    # list_cashflows takes datetime bounds, not date bounds. Convert the
    # inclusive date window to ET datetime bounds — list_cashflows internally
    # compares against the schema column (TIMESTAMP WITH TIME ZONE) so a
    # tz-aware ET datetime is what we want.
    since_dt = datetime.combine(since, time.min).replace(tzinfo=_ET)
    until_dt = datetime.combine(until, time.max).replace(tzinfo=_ET)

    rows = await list_cashflows(engine, scope, since=since_dt, until=until_dt)
    if not rows:
        return pd.Series(dtype="float64")

    buckets: dict[date, float] = {}
    for row in rows:
        sign = _TYPE_SIGN.get(row["cashflow_type"])
        if sign is None:
            # Unknown cashflow_type — skip rather than crash.
            continue
        amount = row["amount"]
        if isinstance(amount, Decimal):
            amount = float(amount)
        else:
            amount = float(amount)
        d = _occurred_date_et(row["occurred_at"])
        buckets[d] = buckets.get(d, 0.0) + sign * amount

    if not buckets:
        return pd.Series(dtype="float64")

    series = pd.Series(buckets, dtype="float64")
    series.index = pd.Index(series.index, name="date")
    return series.sort_index()
