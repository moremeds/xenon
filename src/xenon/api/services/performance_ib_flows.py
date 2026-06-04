"""IB cash-flow loader — bridges xenon.ib_cash_flow to per-day series.

Mirror of performance_futu_flows.py for the IB broker. Sources from IB Flex
CashTransactions (currently the saved query's Deposits/Withdrawals section)
which are persisted to xenon.ib_cash_flow with USD-equivalent amounts.

Sign convention matches FUTU and the daily-return formula in
performance.py::_futu_returns:

  positive = external deposit / transfer-in (NAV went up partly because
             money came in, not because of investment performance)
  negative = withdrawal / transfer-out (NAV went down partly because money
             left the account)

The caller subtracts this from the NAV delta to isolate investment-driven
return:  r_t = (NAV_t - NAV_{t-1} - flow_t) / NAV_{t-1}.
"""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime, time

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from xenon.execution.account_scope import AccountScope

_ET = zoneinfo.ZoneInfo("America/New_York")

_QUERY = text(
    """
    SELECT (occurred_at AT TIME ZONE 'America/New_York')::date AS d,
           SUM(amount_usd)::float8 AS amount
      FROM xenon.ib_cash_flow
     WHERE broker = :broker
       AND account_env = :env
       AND broker_account = :acct
       AND occurred_at >= :since
       AND occurred_at <  :until
     GROUP BY 1
     ORDER BY 1
    """
)


async def load_ib_flows_per_day(
    engine: AsyncEngine,
    scope: AccountScope,
    *,
    since: date,
    until: date,
) -> pd.Series:
    """Return pd.Series indexed by date (sorted), values = signed USD net flow.

    Empty Series when no rows match — callers `.reindex(curve_dates, fill_value=0.0)`.
    """
    # Convert inclusive date window to ET datetime bounds. The DB column is
    # TIMESTAMP WITH TIME ZONE so a tz-aware ET datetime is correct.
    since_dt = datetime.combine(since, time.min).replace(tzinfo=_ET)
    # `until` is inclusive on the caller side; bump to next-day-midnight to
    # include flows that occurred on `until` itself.
    until_dt = datetime.combine(until, time.min).replace(tzinfo=_ET) + pd.Timedelta(days=1)

    async with engine.connect() as conn:
        result = await conn.execute(
            _QUERY,
            {
                "broker": scope.broker,
                "env": scope.account_env,
                "acct": scope.broker_account,
                "since": since_dt,
                "until": until_dt,
            },
        )
        rows = result.all()

    if not rows:
        return pd.Series(dtype="float64")

    buckets = {r.d: float(r.amount) for r in rows}
    series = pd.Series(buckets, dtype="float64")
    series.index = pd.Index(series.index, name="date")
    return series.sort_index()
