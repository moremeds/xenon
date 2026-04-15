"""E2E tests — opt-in (pytest -m e2e). Requires MASSIVE_API_KEY."""

from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

if not os.environ.get("MASSIVE_API_KEY"):
    pytest.skip("MASSIVE_API_KEY not set — skipping E2E", allow_module_level=True)


def test_e2e_1_smoke_aapl_30d():
    from scripts.clients.massive_client import MassiveClient

    mc = MassiveClient()
    to_d = date.today()
    from_d = to_d - timedelta(days=30)
    df = mc.get_aggregates("AAPL", "1d", from_d.isoformat(), to_d.isoformat())
    assert len(df) >= 18
    assert df["date"].is_monotonic_increasing
    assert (df["open"] > 0).all()
    assert (df["close"] > 0).all()
    assert not df["close"].isna().any()


def test_e2e_2_full_pipeline_mini_universe():
    """Persistence-level assertion using read_ohlc."""
    from scripts.clients.massive_client import MassiveClient
    from scripts.ta_lib.service import TAService
    from scripts.ta_lib.store import read_ohlc

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "ta.duckdb"
        svc = TAService(db_path=str(db), massive_client=MassiveClient())
        result = svc.bulk_refresh(["AAPL", "TSLA", "SPY", "MSFT", "NVDA"])
        assert result.succeeded >= 4, f"too many failures: {result.failed_tickers}"
        for t in ["AAPL", "TSLA", "SPY", "MSFT", "NVDA"]:
            for tf in ("1d", "1h"):
                df = read_ohlc(svc._conn, t, tf)
                assert df is not None and len(df) > 0, f"{t} {tf} has no persisted rows"
                assert {"open", "high", "low", "close", "volume"}.issubset(df.columns)


@pytest.mark.skipif(
    not os.environ.get("XENON_ENABLE_IB_E2E"),
    reason="IB E2E requires running IB Gateway; set XENON_ENABLE_IB_E2E=1",
)
def test_e2e_3_vendor_divergence_ib_vs_massive():
    """Close-price divergence < 0.5% on last 5 daily bars for AAPL, SPY, TSLA."""
    from scripts.clients.ib_client import IBClient
    from scripts.clients.massive_client import MassiveClient
    from scripts.ta_lib.bars import fetch_bars as fetch_massive

    ib = IBClient()
    ib.connect(client_id="auto")
    massive = MassiveClient()
    try:
        for ticker in ("AAPL", "SPY", "TSLA"):
            m = fetch_massive(massive, ticker, timeframe="1d", lookback_days=7).tail(5)
            from ib_insync import Stock

            contract = ib._ib.qualifyContracts(Stock(ticker, "SMART", "USD"))[0]
            bars = ib.get_historical_data(
                contract=contract,
                duration="7 D",
                bar_size="1 day",
                what_to_show="TRADES",
                end_date="",
            )
            ib_closes = [float(b.close) for b in bars[-5:]]
            m_closes = m["close"].tolist()
            for i, (ibc, mc) in enumerate(zip(ib_closes, m_closes)):
                div = abs(ibc - mc) / ibc
                assert div < 0.005, f"{ticker} bar {i} divergence {div:.4%} (IB={ibc}, Massive={mc})"
    finally:
        ib.disconnect()
