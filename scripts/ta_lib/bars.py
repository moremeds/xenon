"""IB historical data fetch with BarData → DataFrame conversion."""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
from ib_insync import Stock

logger = logging.getLogger(__name__)


def fetch_bars(
    ib_client,
    ticker: str,
    duration: str = "1 Y",
    bar_size: str = "1 day",
    what_to_show: str = "TRADES",
    end_date: str = "",
) -> pd.DataFrame:
    """Fetch historical bars from IB and return as DataFrame.

    Args:
        ib_client: An IBClient instance (scripts.clients.ib_client.IBClient).
        ticker: Stock symbol (e.g. "AAPL").
        duration: IB duration string (e.g. "1 Y", "1 M").
        bar_size: IB bar size (e.g. "1 day", "1 hour").
        what_to_show: Data type ("TRADES", "MIDPOINT", etc.).
        end_date: End date string (empty = now).

    Returns:
        DataFrame with columns [date, open, high, low, close, volume].

    Raises:
        ValueError: If the contract can't be qualified (invalid/ambiguous symbol).
        RuntimeError: If IB returns no data.
    """
    contract = Stock(ticker, "SMART", "USD")
    qualified = ib_client._ib.qualifyContracts(contract)
    if not qualified:
        raise ValueError(f"Could not qualify IB contract for '{ticker}'")

    bars = ib_client.get_historical_data(
        contract=qualified[0],
        duration=duration,
        bar_size=bar_size,
        what_to_show=what_to_show,
        end_date=end_date,
    )

    if not bars:
        raise RuntimeError(f"No historical data returned for '{ticker}'")

    return _bars_to_dataframe(bars)


def _bars_to_dataframe(bars: list) -> pd.DataFrame:
    """Convert list of ib_insync BarData to a pandas DataFrame."""
    rows = []
    for bar in bars:
        date_str = str(bar.date)
        # IB formatDate=1 gives "yyyyMMdd" for daily, or datetime for intraday
        if len(date_str) == 8 and date_str.isdigit():
            dt = pd.Timestamp(datetime.strptime(date_str, "%Y%m%d"))
        else:
            dt = pd.Timestamp(date_str)
        rows.append(
            {
                "date": dt,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume),
            }
        )
    df = pd.DataFrame(rows)
    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df
