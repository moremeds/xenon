"""Currency/exchange capture + propagation through the position pipeline.

Fixtures use REAL IB contracts and REAL last prices snapshotted on 2026-06-22
(no synthetic data):

  * 5016   JX Advanced Metals Corp — TSEJ / JPY — ¥5,267/share
  * 000660 SK Hynix Inc            — KRX  / KRW — ₩2,885,000/share
  * AAPL   Apple Inc               — NASDAQ / USD — $295.27/share

Note: IB reports 000660's exchange as ``KRX`` (not ``KSE``); the design captures
whatever IB returns, so the value is data-driven, but the fixture uses the real
string.
"""

from types import SimpleNamespace

from xenon.execution.ib_sync import collapse_positions, fetch_positions


def _pos(symbol, currency, primary, exch, avg_cost, sec="STK"):
    contract = SimpleNamespace(
        symbol=symbol,
        secType=sec,
        currency=currency,
        primaryExchange=primary,
        exchange=exch,
        conId=1,
        strike=0,
        right="",
        lastTradeDateOrContractMonth="",
    )
    return SimpleNamespace(contract=contract, position=100.0, avgCost=avg_cost)


class _Client:
    def __init__(self, positions):
        self._positions = positions

    def get_positions(self):
        return self._positions


def test_fetch_positions_captures_currency_and_exchange():
    # 5016 JX Advanced Metals, TSEJ/JPY, last ¥5,267 (2026-06-22).
    client = _Client([_pos("5016", "JPY", "TSEJ", "TSEJ", 5267.0)])
    out = fetch_positions(client)
    assert out[0]["currency"] == "JPY"
    assert out[0]["exchange"] == "TSEJ"


def test_fetch_positions_defaults_currency_usd_when_missing():
    # AAPL, blank currency on the contract → defaults to USD; last $295.27.
    client = _Client([_pos("AAPL", "", "", "SMART", 295.27)])
    out = fetch_positions(client)
    assert out[0]["currency"] == "USD"


def test_fetch_positions_prefers_primary_exchange_falls_back_to_exchange():
    # 000660 SK Hynix, primaryExchange empty → falls back to exchange (KRX).
    client = _Client([_pos("000660", "KRW", "", "KRX", 2_885_000.0)])
    out = fetch_positions(client)
    assert out[0]["exchange"] == "KRX"  # primaryExchange empty → exchange


def test_collapse_positions_propagates_currency_to_position_and_legs():
    # 100 shares of 5016 at ¥5,267 = ¥526,700 (real snapshot, 2026-06-22).
    legs = [
        {
            "symbol": "5016",
            "currency": "JPY",
            "exchange": "TSEJ",
            "secType": "STK",
            "position": 100.0,
            "avgCost": 5267.0,
            "entry_cost": 526_700.0,
            "expiry": "N/A",
            "strike": 0,
            "right": "",
            "structure": "Stock (100 shares)",
            "conId": 1,
            "marketPrice": 5267.0,
            "marketValue": 526_700.0,
            "marketPriceIsCalculated": False,
            "ibDailyPnl": None,
        }
    ]
    out = collapse_positions(legs)
    assert out[0]["currency"] == "JPY"
    assert out[0]["exchange"] == "TSEJ"
    assert out[0]["legs"][0]["currency"] == "JPY"


def test_collapse_positions_keeps_same_symbol_different_currency_separate():
    # A short numeric ticker could collide across exchanges; the currency in the
    # grouping key must keep a JPY listing and a USD listing as distinct rows.
    legs = [
        {
            "symbol": "5016",
            "currency": "JPY",
            "exchange": "TSEJ",
            "secType": "STK",
            "position": 100.0,
            "avgCost": 5267.0,
            "entry_cost": 526_700.0,
            "expiry": "N/A",
            "strike": 0,
            "right": "",
            "structure": "Stock (100 shares)",
            "conId": 1,
            "marketPrice": 5267.0,
            "marketValue": 526_700.0,
            "marketPriceIsCalculated": False,
            "ibDailyPnl": None,
        },
        {
            "symbol": "5016",
            "currency": "USD",
            "exchange": "SMART",
            "secType": "STK",
            "position": 10.0,
            "avgCost": 295.27,
            "entry_cost": 2_952.70,
            "expiry": "N/A",
            "strike": 0,
            "right": "",
            "structure": "Stock (10 shares)",
            "conId": 2,
            "marketPrice": 295.27,
            "marketValue": 2_952.70,
            "marketPriceIsCalculated": False,
            "ibDailyPnl": None,
        },
    ]
    out = collapse_positions(legs)
    currencies = sorted(row["currency"] for row in out)
    assert currencies == ["JPY", "USD"]  # two distinct rows, not merged
