"""orders_payload_for_scope must filter executed fills to the current ET day.

Regression for the "TODAY'S EXECUTED ORDERS" panel showing fills from weeks ago:
the route had no date predicate, so it returned the 200 most-recent fills
regardless of trading day. The panel title (and the Realized P&L card) mean the
*current ET calendar day*, mirroring web/lib/realized-pnl.ts (fillDateET/todayET).
"""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import insert

from xenon.api.routes.orders import _today_et_start_utc, orders_payload_for_scope
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_fills
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU123456")


def _insert_fill(exec_id, filled_at) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_fills).values(
                exec_id=exec_id,
                perm_id="0",
                con_id=265598,
                ticker="QQQ",
                side="BUY",
                qty=Decimal("0.5000"),
                price=Decimal("703.3400"),
                commission=Decimal("0.3500"),
                filled_at=filled_at,
                metadata={"legacy_source": "test"},
                broker="IB",
                account_env="paper",
                broker_account="DU123456",
            )
        )


def test_executed_orders_filtered_to_current_et_day():
    boundary = _today_et_start_utc()
    _insert_fill("exec-today", boundary + timedelta(hours=1))
    _insert_fill("exec-yesterday", boundary - timedelta(hours=1))

    payload = orders_payload_for_scope(SCOPE)
    exec_ids = {row["execId"] for row in payload["executed_orders"]}

    assert "exec-today" in exec_ids, "today's ET fill must appear"
    assert "exec-yesterday" not in exec_ids, "yesterday's ET fill must be filtered out"
    assert payload["executed_count"] == 1


def test_today_et_start_is_midnight_eastern_as_utc():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    # A fixed instant: 2026-06-15 18:00 UTC == 14:00 ET (EDT, UTC-4).
    now = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)
    start = _today_et_start_utc(now)

    assert start.astimezone(ZoneInfo("America/New_York")).hour == 0
    assert start.astimezone(ZoneInfo("America/New_York")).date() == datetime(2026, 6, 15).date()
    # 2026-06-15 00:00 EDT == 2026-06-15 04:00 UTC
    assert start == datetime(2026, 6, 15, 4, 0, tzinfo=timezone.utc)
