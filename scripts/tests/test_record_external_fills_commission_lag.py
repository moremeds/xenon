from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import func, select

from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_fills, outbox, trades
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU0000000")
FILLED_AT = datetime(2026, 4, 29, 18, 22, tzinfo=timezone.utc)
IB_UNSET_DOUBLE = Decimal("1.7976931348623157e+308")


def _execution(
    *,
    exec_id: str = "lag-exec-001",
    commission: Decimal | str | int = Decimal("0"),
    realized_pnl: Decimal | str | int | None = Decimal("0"),
    commission_ready: bool = False,
    side: str = "BOT",
) -> dict:
    return {
        "exec_id": exec_id,
        "perm_id": "9300001",
        "ib_order_id": "200",
        "con_id": 12345,
        "time": FILLED_AT,
        "symbol": "SPX",
        "sec_type": "BAG",
        "side": side,
        "shares": 1,
        "price": Decimal("10.00"),
        "exchange": "SMART",
        "commission": commission,
        "realized_pnl": realized_pnl,
        "commission_ready": commission_ready,
        "strike": None,
        "expiry": None,
        "right": None,
    }


def _fill(exec_id: str = "lag-exec-001") -> dict:
    engine = get_sync_engine()
    with engine.connect() as conn:
        return dict(
            conn.execute(select(order_fills).where(order_fills.c.exec_id == exec_id)).one()._mapping
        )


def _trade_for_legacy_id(legacy_id: str) -> dict:
    engine = get_sync_engine()
    with engine.connect() as conn:
        return dict(
            conn.execute(select(trades).where(trades.c.metadata["legacy_id"].astext == legacy_id)).one()._mapping
        )


def test_fetch_ib_executions_marks_commission_ready_only_when_exec_ids_match():
    from xenon.execution.ib_reconcile import fetch_ib_executions

    execution = SimpleNamespace(
        execId="fetch-exec-001",
        permId=9300001,
        orderId=200,
        time=FILLED_AT,
        side="BOT",
        shares=1,
        price=Decimal("10.00"),
        exchange="SMART",
    )
    contract = SimpleNamespace(
        conId=12345,
        symbol="SPX",
        secType="BAG",
        strike=None,
        lastTradeDateOrContractMonth=None,
        right=None,
    )
    pending_report = SimpleNamespace(execId="", commission=IB_UNSET_DOUBLE, realizedPNL=0)
    ready_report = SimpleNamespace(execId="fetch-exec-001", commission=Decimal("1.23"), realizedPNL=Decimal("-12.50"))

    class FakeClient:
        def get_fills(self):
            return [
                SimpleNamespace(execution=execution, contract=contract, commissionReport=pending_report),
                SimpleNamespace(execution=execution, contract=contract, commissionReport=ready_report),
            ]

    result = fetch_ib_executions(FakeClient())

    assert result[0]["commission_ready"] is False
    assert result[0]["commission"] == 0
    assert result[1]["commission_ready"] is True
    assert result[1]["commission"] == Decimal("1.23")
    assert result[1]["realized_pnl"] == Decimal("-12.50")


def test_first_tick_with_no_commission_inserts_zero():
    from xenon.execution.ib_reconcile import record_external_fills

    result = record_external_fills(
        [
            _execution(
                exec_id="lag-zero-insert",
                commission=IB_UNSET_DOUBLE,
                realized_pnl=None,
                commission_ready=False,
            )
        ],
        scope=SCOPE,
    )

    assert result["inserted"] == 1
    assert result["updated"] == 0
    assert _fill("lag-zero-insert")["commission"] == Decimal("0.0000")


def test_second_tick_with_populated_commission_updates():
    from xenon.execution.ib_reconcile import record_external_fills

    record_external_fills([_execution(exec_id="lag-update", commission_ready=False)], scope=SCOPE)

    result = record_external_fills(
        [
            _execution(
                exec_id="lag-update",
                commission=Decimal("1.23"),
                realized_pnl=Decimal("-12.50"),
                commission_ready=True,
            )
        ],
        scope=SCOPE,
    )

    row = _fill("lag-update")
    assert result["inserted"] == 0
    assert result["updated"] == 1
    assert result["replayed"] == 0
    assert row["commission"] == Decimal("1.2300")
    assert row["metadata"]["realized_pnl"] == "-12.50"


def test_second_tick_with_zero_commission_does_not_overwrite():
    from xenon.execution.ib_reconcile import record_external_fills

    record_external_fills(
        [
            _execution(
                exec_id="lag-zero-no-clobber",
                commission=Decimal("1.23"),
                commission_ready=True,
            )
        ],
        scope=SCOPE,
    )

    result = record_external_fills(
        [
            _execution(
                exec_id="lag-zero-no-clobber",
                commission=Decimal("0"),
                realized_pnl=Decimal("0"),
                commission_ready=True,
            )
        ],
        scope=SCOPE,
    )

    assert result["updated"] == 0
    assert _fill("lag-zero-no-clobber")["commission"] == Decimal("1.2300")


def test_zero_zero_commission_update_is_noop():
    from xenon.execution.ib_reconcile import record_external_fills
    from xenon.execution.orders_store import update_fill_commission

    record_external_fills([_execution(exec_id="lag-zero-zero", commission_ready=False)], scope=SCOPE)

    did_update = update_fill_commission(
        exec_id="lag-zero-zero",
        commission=Decimal("0"),
        realized_pnl=Decimal("0"),
    )

    assert did_update is False
    assert _fill("lag-zero-zero")["commission"] == Decimal("0.0000")


def test_unset_double_commission_skips_late_update():
    from xenon.execution.ib_reconcile import record_external_fills

    record_external_fills([_execution(exec_id="lag-sentinel", commission_ready=False)], scope=SCOPE)

    result = record_external_fills(
        [
            _execution(
                exec_id="lag-sentinel",
                commission=IB_UNSET_DOUBLE,
                realized_pnl=Decimal("-12.50"),
                commission_ready=True,
            )
        ],
        scope=SCOPE,
    )

    assert result["updated"] == 0
    assert _fill("lag-sentinel")["commission"] == Decimal("0.0000")


def test_aggregator_reruns_after_commission_update():
    from xenon.execution.ib_reconcile import record_external_fills

    first = record_external_fills([_execution(exec_id="lag-aggregate", commission_ready=False)], scope=SCOPE)
    legacy_id = first["affected_legacy_ids"][0]
    assert _trade_for_legacy_id(legacy_id)["entry_cost"] == Decimal("10.0000")

    second = record_external_fills(
        [
            _execution(
                exec_id="lag-aggregate",
                commission=Decimal("1.23"),
                realized_pnl=Decimal("-12.50"),
                commission_ready=True,
            )
        ],
        scope=SCOPE,
    )
    third = record_external_fills(
        [
            _execution(
                exec_id="lag-aggregate",
                commission=Decimal("1.23"),
                realized_pnl=Decimal("-12.50"),
                commission_ready=True,
            )
        ],
        scope=SCOPE,
    )

    trade = _trade_for_legacy_id(legacy_id)
    engine = get_sync_engine()
    with engine.connect() as conn:
        trade_count = conn.execute(select(func.count()).select_from(trades)).scalar_one()
    assert second["updated"] == 1
    assert third["updated"] == 0
    assert trade["entry_cost"] == Decimal("11.2300")
    assert trade_count == 1


def test_commission_update_emits_outbox_event():
    from xenon.db.events import CHANNEL_FILL_COMMISSION_UPDATED
    from xenon.execution.ib_reconcile import record_external_fills

    record_external_fills([_execution(exec_id="lag-outbox", commission_ready=False)], scope=SCOPE)

    record_external_fills(
        [
            _execution(
                exec_id="lag-outbox",
                commission=Decimal("1.23"),
                realized_pnl=Decimal("-12.50"),
                commission_ready=True,
            )
        ],
        scope=SCOPE,
    )

    engine = get_sync_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(outbox.c.channel, outbox.c.source, outbox.c.payload).where(
                outbox.c.channel == CHANNEL_FILL_COMMISSION_UPDATED
            )
        ).all()

    assert len(rows) == 1
    event = rows[0]._mapping
    assert event["source"] == "update_fill_commission"
    assert event["payload"]["exec_id"] == "lag-outbox"
    assert event["payload"]["submission_id"] is None
    assert event["payload"]["legacy_id"] == "ib_reconcile:perm:9300001"
    assert event["payload"]["commission"] == "1.23"
    assert event["payload"]["realized_pnl"] == "-12.50"
