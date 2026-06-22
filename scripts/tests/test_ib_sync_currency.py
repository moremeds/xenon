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

from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import insert, select

from xenon.db.engine import get_sync_engine
from xenon.db.schema import positions
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


def test_get_fx_rates_reads_exchange_rate():
    from xenon.execution.ib_sync import get_fx_rates

    # Real USD-per-unit rates derived from IB IDEALPRO (2026-06-22):
    # USD.JPY 161.6575 → 0.006186 ; USD.KRW 1538.505 → 0.00065.
    avs = [
        SimpleNamespace(tag="ExchangeRate", value="0.006186", currency="JPY"),
        SimpleNamespace(tag="ExchangeRate", value="0.00065", currency="KRW"),
    ]
    client = SimpleNamespace(ib=SimpleNamespace(accountValues=lambda: avs))
    rates = get_fx_rates(client)
    assert rates["JPY"] == 0.006186
    assert rates["KRW"] == 0.00065
    assert rates["USD"] == 1.0


def test_convert_to_portfolio_format_adds_usd_fields_and_totals(monkeypatch):
    # Avoid PG lookups. convert_to_portfolio_format imports
    # load_entry_date_lookups_sync + EntryDateLookups LOCALLY from
    # xenon.utils.portfolio_loader, so patch THAT module.
    import xenon.utils.portfolio_loader as _pl
    from xenon.execution import ib_sync

    monkeypatch.setattr(_pl, "load_entry_date_lookups_sync", lambda scope: _pl.EntryDateLookups({}, {}, {}, {}))

    # Real snapshots (2026-06-22):
    #   5016: entry 100sh @ prior-close ¥4,747 = ¥474,700; mkt 100sh @ ¥5,267 = ¥526,700
    #   AAPL: 10sh @ $295.27 = $2,952.70 (already USD)
    collapsed = [
        {
            "ticker": "5016",
            "currency": "JPY",
            "structure": "Stock (100 shares)",
            "expiry": "N/A",
            "entry_cost": 474_700.0,
            "market_value": 526_700.0,
            "risk_profile": "equity",
            "legs": [],
            "max_risk": None,
        },
        {
            "ticker": "AAPL",
            "currency": "USD",
            "structure": "Stock (10 shares)",
            "expiry": "N/A",
            "entry_cost": 2_952.70,
            "market_value": 2_952.70,
            "risk_profile": "equity",
            "legs": [],
            "max_risk": None,
        },
    ]
    # bankroll/NAV is the account's consolidated USD NetLiquidation, which is live
    # account data — not asserted here (covered by the Phase 5 live E2E); omitted
    # so no figure is invented. deployed_pct falls back to 0 with bankroll absent.
    account = {}
    fx_rates = {"USD": 1.0, "JPY": 0.006186}
    out = ib_sync.convert_to_portfolio_format(account, collapsed, fx_rates=fx_rates)

    assert out["base_currency"] == "USD"
    assert out["fx_rates"]["JPY"] == 0.006186
    # 474_700 JPY * 0.006186 = 2936.49 ; 526_700 * 0.006186 = 3258.17
    assert out["positions"][0]["entry_cost_usd"] == 2936.49
    assert out["positions"][0]["market_value_usd"] == 3258.17
    # AAPL already USD — passes through unchanged.
    assert out["positions"][1]["entry_cost_usd"] == 2952.70
    # total deployed USD = 2936.49 + 2952.70 = 5889.19
    assert out["total_deployed_dollars"] == 5889.19
    assert out["fx_unconverted_count"] == 0


def test_positions_table_persists_currency_and_exchange(pg_test_engine):
    """The positions table round-trips native currency + exchange (migration
    2026_06_22_positions_currency). Uses the shared-txn engine; rolls back."""
    engine = get_sync_engine()
    # 5016 JX Advanced Metals, TSEJ/JPY, 100 shares @ ¥5,267 (2026-06-22).
    with engine.begin() as conn:
        new_id = conn.execute(
            insert(positions)
            .values(
                ticker="5016",
                security_type="STK",
                currency="JPY",
                exchange="TSEJ",
                quantity=100,
                avg_cost=Decimal("5267.0"),
                account="DU-JPKR-TEST",
                broker="IB",
                account_env="paper",
                broker_account="DU-JPKR-TEST",
            )
            .returning(positions.c.id)
        ).scalar_one()

    with engine.connect() as conn:
        row = conn.execute(select(positions.c.currency, positions.c.exchange).where(positions.c.id == new_id)).one()
    assert row.currency == "JPY"
    assert row.exchange == "TSEJ"


def test_positions_table_currency_defaults_usd(pg_test_engine):
    """Existing/US rows that omit currency backfill to 'USD' via server_default."""
    engine = get_sync_engine()
    # AAPL, 10 shares @ $295.27 (2026-06-22); currency omitted on insert.
    with engine.begin() as conn:
        new_id = conn.execute(
            insert(positions)
            .values(
                ticker="AAPL",
                security_type="STK",
                quantity=10,
                avg_cost=Decimal("295.27"),
                account="DU-JPKR-TEST",
                broker="IB",
                account_env="paper",
                broker_account="DU-JPKR-TEST",
            )
            .returning(positions.c.id)
        ).scalar_one()

    with engine.connect() as conn:
        currency = conn.execute(select(positions.c.currency).where(positions.c.id == new_id)).scalar_one()
    assert currency == "USD"
