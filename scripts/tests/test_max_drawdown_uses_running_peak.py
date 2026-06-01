"""C35: max drawdown must track the running peak so a V-shape correctly
shows a -50% drawdown.
"""

from datetime import date
from decimal import Decimal

from xenon.api.services.futu_history import compute_performance_stats
from xenon.db.queries.futu_history import write_nav_row
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_v_shape_drawdown(clean_scope):
    # 100 -> 50 -> 200. Running-peak DD on the 50 point = (50-100)/100 = -50%.
    # Global-peak DD would be 0% (peak=200, no post-peak trough).
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
        Decimal("50"),
        "reconstructed",
        Decimal("50"),
        Decimal("0"),
    )
    write_nav_row(
        SCOPE,
        date(2024, 5, 3),
        Decimal("200"),
        "reported",
        Decimal("200"),
        Decimal("0"),
        is_today=True,
    )
    stats = compute_performance_stats(SCOPE, date(2024, 5, 1), date(2024, 5, 3))
    assert abs(stats["max_drawdown"] - (-0.5)) < 0.001
