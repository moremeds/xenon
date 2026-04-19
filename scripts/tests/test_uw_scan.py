from datetime import datetime
from unittest.mock import MagicMock

from scripts.analysis.models import TickerData
from scripts.scanners.uw.scan import scan_universe, ScanConfig


def _full_td(ticker, *, flow_alerts=None, darkpool=None, iv_pct=None,
             earnings_within_14d=False, gex_by_strike=None, price=100.0):
    return TickerData(
        ticker=ticker, price=price, fetched_at=datetime.now(),
        gex={"net": 1e9} if gex_by_strike else None,
        gex_by_strike=gex_by_strike,
        iv=None, rv=None, iv_percentile=iv_pct, term_structure=None,
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=flow_alerts, net_premium=None, pcr=None, darkpool=darkpool,
        oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=earnings_within_14d,
    )


_ALERT = {
    "volume": 5000, "open_interest": 1000, "ask_side_percent": "0.85",
    "total_premium": 2_000_000, "multileg_percent": "0.05",
    "moneyness": "0.03", "expiry_dte": 21,
}


def test_scan_targeted_mode_runs_all_signals(monkeypatch):
    def fake_fetch(ticker, client):
        if ticker == "TSLA":
            return _full_td("TSLA", flow_alerts=[dict(_ALERT)])
        return _full_td(ticker)

    monkeypatch.setattr("scripts.scanners.uw.scan.fetch_ticker_data", fake_fetch)

    cfg = ScanConfig(mode="targeted", tickers=["TSLA", "NVDA"], full=True)
    result = scan_universe(cfg, client=MagicMock())

    assert result["mode"] == "targeted"
    assert result["universe_size"] == 2
    assert len(result["candidates"]) >= 1
    tsla = next((c for c in result["candidates"] if c["ticker"] == "TSLA"), None)
    assert tsla is not None
    assert any(h["signal"] == "deep_conviction_flow" for h in tsla["hits"])


def test_scan_output_schema_has_required_fields(monkeypatch):
    monkeypatch.setattr(
        "scripts.scanners.uw.scan.fetch_ticker_data",
        lambda t, c: _full_td(t),
    )
    cfg = ScanConfig(mode="targeted", tickers=["AAPL"], full=False)
    result = scan_universe(cfg, client=MagicMock())
    assert "scan_time" in result
    assert "regime" in result
    assert "candidates" in result


def test_scan_min_confluence_filter(monkeypatch):
    def fake_fetch(ticker, client):
        return _full_td(ticker, flow_alerts=[dict(_ALERT)])
    monkeypatch.setattr("scripts.scanners.uw.scan.fetch_ticker_data", fake_fetch)

    cfg = ScanConfig(mode="targeted", tickers=["A", "B"], full=True, min_confluence=2)
    result = scan_universe(cfg, client=MagicMock())
    assert all(c.get("is_type_f") for c in result["candidates"])
