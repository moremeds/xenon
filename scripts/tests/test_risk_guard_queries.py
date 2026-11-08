"""Risk guard Postgres reads/writes: latest snapshot, risk_events, drawdown basis."""

from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio
import sqlalchemy as sa

from xenon.api.services.risk_guard.account import load_drawdown_basis
from xenon.db.queries import risk_guard as q
from xenon.db.schema import account_snapshots, ib_cash_flow, nav_history, risk_events
from xenon.execution.account_scope import AccountScope

pytestmark = pytest.mark.asyncio

ACCT = "T_RISK_GUARD"
SCOPE = AccountScope(broker="IB", account_env="paper", broker_account=ACCT)


async def _purge(engine):
    async with engine.begin() as c:
        for t in (account_snapshots, risk_events, nav_history, ib_cash_flow):
            await c.execute(sa.delete(t).where(t.c.broker_account == ACCT))


@pytest_asyncio.fixture
async def eng(async_engine):
    await _purge(async_engine)
    yield async_engine
    await _purge(async_engine)


async def test_latest_snapshot_returns_payload_and_time(eng):
    t0 = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
    async with eng.begin() as c:
        for i, nlv in enumerate((100.0, 90.0)):
            await c.execute(
                sa.insert(account_snapshots).values(
                    account=ACCT,
                    bankroll=nlv,
                    payload={"account_summary": {"net_liquidation": nlv}},
                    snapshot_at=t0 + timedelta(seconds=30 * i),
                    **SCOPE.as_dict(),
                )
            )
    payload, at = await q.latest_snapshot(eng, SCOPE)
    assert payload["account_summary"]["net_liquidation"] == 90.0
    assert at == t0 + timedelta(seconds=30)
    other = AccountScope(broker="IB", account_env="live", broker_account=ACCT)
    assert await q.latest_snapshot(eng, other) is None


async def test_record_and_read_events(eng):
    await q.record_event(
        eng,
        SCOPE,
        incident_key="market",
        incident_id="market#1",
        kind="open",
        severity=3,
        detail={"reasons": ["day -3.10%"], "push": "sent"},
    )
    await q.record_event(eng, SCOPE, incident_key="market", incident_id="market#1", kind="ack", severity=3, detail={})
    rows = await q.recent_events(eng, SCOPE)
    assert [r["kind"] for r in rows] == ["ack", "open"]
    assert rows[1]["detail"]["reasons"] == ["day -3.10%"]
    assert isinstance(rows[0]["occurred_at"], str)


async def test_drawdown_basis_uses_close_rows_and_flows(eng):
    async with eng.begin() as c:
        for d, nav, src in (
            (date(2026, 10, 1), 100, "close"),
            (date(2026, 10, 2), 120, "close"),
            (date(2026, 10, 3), 999, "intraday"),  # an intraday row never sets the peak
        ):
            await c.execute(sa.insert(nav_history).values(date=d, nav=nav, source=src, **SCOPE.as_dict()))
        await c.execute(
            sa.insert(ib_cash_flow).values(
                transaction_id="T_RG_1",
                txn_type="Deposits/Withdrawals",
                amount_native=50,
                currency="USD",
                amount_usd=50,
                fx_rate=1,
                occurred_at=datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc),
                raw={},
                **SCOPE.as_dict(),
            )
        )
    basis = await load_drawdown_basis(eng, SCOPE, date(2026, 10, 5))
    assert basis.peak == 170.0
    assert basis.adjusted is True and basis.flows_since == date(2026, 10, 3)


async def test_drawdown_basis_without_flows_is_unadjusted(eng):
    async with eng.begin() as c:
        await c.execute(
            sa.insert(nav_history).values(date=date(2026, 10, 1), nav=100, source="close", **SCOPE.as_dict())
        )
    basis = await load_drawdown_basis(eng, SCOPE, date(2026, 10, 5))
    assert basis.peak == 100.0 and basis.adjusted is False and basis.flows_since is None
