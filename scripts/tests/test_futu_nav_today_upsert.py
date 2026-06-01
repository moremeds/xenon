"""14:00 ET sync writes today's reported NAV; 16:30 ET sync overwrites it."""

from datetime import date
from decimal import Decimal

from xenon.db.queries.futu_history import fetch_nav_history, write_nav_row
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_intraday_then_eod_overwrite(clean_scope):
    today = date(2024, 5, 1)
    write_nav_row(
        SCOPE,
        recon_date=today,
        nav=Decimal("10000"),
        nav_type="reported",
        cash=Decimal("5000"),
        stock_value=Decimal("5000"),
        is_today=True,
    )
    write_nav_row(
        SCOPE,
        recon_date=today,
        nav=Decimal("10100"),
        nav_type="reported",
        cash=Decimal("5000"),
        stock_value=Decimal("5100"),
        is_today=True,
    )
    rows = list(fetch_nav_history(SCOPE, today, today))
    assert rows[0]["nav"] == Decimal("10100")
