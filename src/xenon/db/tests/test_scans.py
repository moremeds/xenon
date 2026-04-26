from decimal import Decimal

import pytest


@pytest.mark.asyncio
async def test_save_and_get_latest_scan(conn):
    from xenon.db.queries.scans import get_latest_scan, save_scan

    await save_scan(conn, scan_type="watchlist", payload={"candidates": [{"ticker": "AAPL", "score": 85}]})
    await save_scan(conn, scan_type="watchlist", payload={"candidates": [{"ticker": "MSFT", "score": 92}]})
    latest = await get_latest_scan(conn, scan_type="watchlist")
    assert latest["payload"]["candidates"][0]["ticker"] == "MSFT"


@pytest.mark.asyncio
async def test_get_latest_scan_nonexistent(conn):
    from xenon.db.queries.scans import get_latest_scan

    result = await get_latest_scan(conn, scan_type="watchlist")
    assert result is None


@pytest.mark.asyncio
async def test_save_cri_datapoint(conn):
    from xenon.db.queries.scans import get_cri_series, save_cri_datapoint

    await save_cri_datapoint(conn, cri_level=Decimal("0.35"), alert=False, payload={"regime": "calm"})
    await save_cri_datapoint(conn, cri_level=Decimal("0.72"), alert=True, payload={"regime": "stress"})
    series = await get_cri_series(conn, limit=10)
    assert len(series) == 2
    assert series[1]["alert"] is True
