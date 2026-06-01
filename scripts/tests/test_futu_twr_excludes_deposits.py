"""TWR by sub-period — a deposit that grows NAV without market move
must yield ~0 TWR.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from xenon.api.services.futu_history import compute_performance_stats
from xenon.db.queries.futu_history import insert_cash_flows, write_nav_row
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_deposit_with_zero_market_move_yields_zero_twr(clean_scope):
    write_nav_row(
        SCOPE,
        date(2024, 5, 1),
        Decimal("100"),
        "reconstructed",
        Decimal("100"),
        Decimal("0"),
    )
    write_nav_row(
        SCOPE,
        date(2024, 5, 2),
        Decimal("1100"),
        "reported",
        Decimal("1100"),
        Decimal("0"),
        is_today=True,
    )
    insert_cash_flows(
        SCOPE,
        [
            dict(
                futu_flow_id="f1",
                flow_type="deposit",
                futu_type_raw="FUND_IN",
                amount=Decimal("1000"),
                amount_original=Decimal("1000"),
                original_currency="USD",
                fx_rate_used=None,
                ticker=None,
                description="wire",
                flow_at=datetime(2024, 5, 2, 13, tzinfo=timezone.utc),
                raw={},
            )
        ],
    )
    stats = compute_performance_stats(SCOPE, date(2024, 5, 1), date(2024, 5, 2))
    # (1100 - 100 - 1000) / 100 = 0 → TWR = 0%.
    assert abs(stats["total_return_twr"]) < 0.001
