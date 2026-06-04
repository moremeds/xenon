"""FUTU mask lifts when cash flows are integrated."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from xenon.api.services.performance import compute
from xenon.db.queries.futu_history import insert_cashflows
from xenon.execution.account_scope import AccountScope
from xenon.utils.portfolio_loader import upsert_nav_sync

pytestmark = pytest.mark.asyncio

_SCOPE = AccountScope(broker="FUTU", account_env="live", broker_account="FUTU_UNMASK1")


def _seed_nav(scope, navs: list[tuple[date, float]]):
    for d, n in navs:
        upsert_nav_sync(scope=scope, day=d, nav=Decimal(str(n)), source="close")


def _build_50_day_nav(start_d: date, start_nav: float) -> list[tuple[date, float]]:
    """50 alternating up/down days for a stable test fixture."""
    out, n = [], start_nav
    for i in range(50):
        n = n * (1.01 if i % 2 == 0 else 0.995)
        out.append((start_d + timedelta(days=i), n))
    return out


async def test_futu_old_masking_warning_gone(async_engine):
    _seed_nav(_SCOPE, _build_50_day_nav(date(2026, 3, 1), 100_000.0))
    result = await compute(async_engine, _SCOPE, as_of=date(2026, 6, 3))
    warnings_text = " ".join(result["warnings"])
    # The pre-Pass-2 masking warning must NOT appear.
    assert "True Time-Weighted Return requires cash-flow tracking" not in warnings_text


async def test_futu_new_soft_note_present(async_engine):
    _seed_nav(_SCOPE, _build_50_day_nav(date(2026, 3, 1), 100_000.0))
    result = await compute(async_engine, _SCOPE, as_of=date(2026, 6, 3))
    warnings_text = " ".join(result["warnings"])
    assert "flow-adjusted via xenon.futu_cash_flow" in warnings_text


async def test_futu_risk_metrics_populated(async_engine):
    _seed_nav(_SCOPE, _build_50_day_nav(date(2026, 3, 1), 100_000.0))
    result = await compute(async_engine, _SCOPE, as_of=date(2026, 6, 3))
    summary = result["summary"]
    # Was None pre-change; should now be a float.
    assert summary["sharpe_ratio"] is not None
    assert summary["sortino_ratio"] is not None
    assert summary["annualized_return"] is not None


async def test_futu_deposit_excluded_from_simple_total_return(async_engine):
    """Deposit 5000 mid-period. End - start = 8000, real gain = 3000 = +3%."""
    scope = AccountScope(broker="FUTU", account_env="live", broker_account="FUTU_DEP1")
    _seed_nav(
        scope,
        [
            (date(2026, 1, 2), 100_000.0),
            (date(2026, 3, 15), 102_500.0),
            (date(2026, 6, 1), 108_000.0),
        ],
    )
    await insert_cashflows(
        async_engine,
        scope,
        [
            {
                "futu_flow_id": "FUTU_DEP1-d1",
                "cashflow_type": "DEPOSIT",
                "amount": Decimal("5000.00"),
                "currency": "USD",
                "occurred_at": datetime(2026, 3, 15, 14, 30, tzinfo=timezone.utc),
                "raw": {},
            },
        ],
    )
    result = await compute(async_engine, scope, as_of=date(2026, 6, 3))
    summary = result["summary"]
    assert summary["net_external_flows"] == pytest.approx(5000.0)
    assert summary["simple_total_return"] == pytest.approx(0.03, abs=1e-4)
    assert summary["twr_total_return"] is not None
    assert summary["irr_total_return"] is not None
