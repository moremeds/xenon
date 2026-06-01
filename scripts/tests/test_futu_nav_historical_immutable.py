"""After backfill, daily syncs must NOT touch past dates. Only --rematch does.

Pins the `is_today` guard in write_nav_row's UPSERT clause.
"""

from datetime import date
from decimal import Decimal

from xenon.db.queries.futu_history import fetch_nav_history, write_nav_row
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_write_nav_row_with_is_today_false_does_not_upsert(clean_scope):
    write_nav_row(
        SCOPE,
        recon_date=date(2024, 5, 1),
        nav=Decimal("100"),
        nav_type="reconstructed",
        cash=Decimal("100"),
        stock_value=Decimal("0"),
    )
    # Second attempt without is_today must NOT overwrite.
    write_nav_row(
        SCOPE,
        recon_date=date(2024, 5, 1),
        nav=Decimal("999"),
        nav_type="reported",
        cash=Decimal("999"),
        stock_value=Decimal("0"),
    )
    rows = list(fetch_nav_history(SCOPE, date(2024, 5, 1), date(2024, 5, 1)))
    assert rows[0]["nav"] == Decimal("100")
    assert rows[0]["nav_type"] == "reconstructed"


def test_is_today_true_does_upsert(clean_scope):
    today = date(2024, 5, 1)
    write_nav_row(
        SCOPE,
        recon_date=today,
        nav=Decimal("100"),
        nav_type="reported",
        cash=Decimal("100"),
        stock_value=Decimal("0"),
        is_today=True,
    )
    write_nav_row(
        SCOPE,
        recon_date=today,
        nav=Decimal("200"),
        nav_type="reported",
        cash=Decimal("200"),
        stock_value=Decimal("0"),
        is_today=True,
    )
    rows = list(fetch_nav_history(SCOPE, today, today))
    assert rows[0]["nav"] == Decimal("200")
