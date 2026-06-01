"""Lock down the typed DB CRUD shape used by sync + API."""

from datetime import date, datetime
from decimal import Decimal

from xenon.db.queries.futu_history import (
    cursor_for_cash_flows,
    cursor_for_corp_actions,
    cursor_for_fills,
    fetch_fills,
    fetch_nav_history,
    insert_corporate_actions,
    insert_fills,
    update_realized_pnl,
    write_nav_row,
    write_recon_anomaly,
    write_recon_daily,
)
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="FUTU", account_env="paper", broker_account="pytest")


def _fill(deal_id, action, qty, price, t):
    return dict(
        futu_deal_id=deal_id,
        futu_order_id="o",
        ticker="AAPL",
        futu_code="US.AAPL",
        market="US",
        action=action,
        quantity=qty,
        price=Decimal(str(price)),
        fees=Decimal("0"),
        filled_at=datetime.fromisoformat(t),
        raw={},
    )


def test_insert_fills_idempotent(clean_scope):
    rows = [_fill("d1", "BUY", 100, 150, "2024-05-01T10:00:00+00:00")]
    n1 = insert_fills(SCOPE, rows)
    n2 = insert_fills(SCOPE, rows)
    assert n1 == 1
    assert n2 == 0


def test_cursor_for_fills_returns_max_date(clean_scope):
    insert_fills(
        SCOPE,
        [
            _fill("d1", "BUY", 1, 1, "2024-05-01T10:00:00+00:00"),
            _fill("d2", "BUY", 1, 1, "2024-05-02T10:00:00+00:00"),
        ],
    )
    assert cursor_for_fills(SCOPE) == date(2024, 5, 2)


def test_cursor_returns_none_for_empty_scope(clean_scope):
    assert cursor_for_fills(SCOPE) is None
    assert cursor_for_cash_flows(SCOPE) is None
    assert cursor_for_corp_actions(SCOPE) is None


def test_update_realized_pnl(clean_scope):
    insert_fills(
        SCOPE,
        [
            _fill("d1", "BUY", 100, 150, "2024-05-01T10:00:00+00:00"),
            _fill("d2", "SELL", 100, 160, "2024-05-02T10:00:00+00:00"),
        ],
    )
    update_realized_pnl(SCOPE, "d2", Decimal("1000.00"))
    fills = list(fetch_fills(SCOPE, date(2024, 5, 1), date(2024, 5, 3)))
    by_id = {f["futu_deal_id"]: f for f in fills}
    assert by_id["d1"]["realized_pnl"] is None
    assert by_id["d2"]["realized_pnl"] == Decimal("1000.00")


def test_write_nav_row_then_fetch(clean_scope):
    write_nav_row(
        SCOPE,
        recon_date=date(2024, 5, 1),
        nav=Decimal("10000"),
        nav_type="reconstructed",
        cash=Decimal("10000"),
        stock_value=Decimal("0"),
    )
    rows = list(fetch_nav_history(SCOPE, date(2024, 5, 1), date(2024, 5, 1)))
    assert rows[0]["nav"] == Decimal("10000")
    assert rows[0]["nav_type"] == "reconstructed"


def test_recon_daily_unique_per_date_anomalies_not(clean_scope):
    write_recon_daily(
        SCOPE,
        recon_date=date(2024, 5, 1),
        reported=Decimal("100"),
        reconstructed=Decimal("100"),
        threshold=Decimal("25"),
    )
    # Second daily for same date upserts (does NOT raise).
    write_recon_daily(
        SCOPE,
        recon_date=date(2024, 5, 1),
        reported=Decimal("110"),
        reconstructed=Decimal("100"),
        threshold=Decimal("25"),
    )
    # But two anomalies on the same date are both stored.
    write_recon_anomaly(
        SCOPE,
        recon_date=date(2024, 5, 1),
        notes={"anomaly_type": "missing_fx", "currency": "HKD"},
    )
    write_recon_anomaly(
        SCOPE,
        recon_date=date(2024, 5, 1),
        notes={"anomaly_type": "over_close", "ticker": "AAPL"},
    )


def test_insert_corporate_actions_idempotent(clean_scope):
    rows = [
        dict(
            ticker="NVDA",
            action_type="split",
            effective_date=date(2024, 6, 10),
            ratio_numerator=Decimal("10"),
            ratio_denominator=Decimal("1"),
            new_ticker=None,
            notes={},
        )
    ]
    n1 = insert_corporate_actions(rows)
    n2 = insert_corporate_actions(rows)
    assert n1 == 1
    assert n2 == 0
