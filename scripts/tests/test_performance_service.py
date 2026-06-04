"""Tests for performance.compute() service (spec § service)."""

import math
from datetime import date, timedelta

import pytest
import sqlalchemy as sa

from xenon.api.services import performance
from xenon.api.services.performance import compute
from xenon.db.schema import futu_cash_flow, nav_history
from xenon.execution.account_scope import AccountScope

pytestmark = pytest.mark.asyncio


async def test_futu_official_performance_math_matches_help_example():
    curve = performance.pd.DataFrame(
        [
            {"date": date(2026, 1, 1), "nav": 100.0, "daily_pnl": None, "source": "intraday"},
            {"date": date(2026, 1, 2), "nav": 150.0, "daily_pnl": None, "source": "intraday"},
            {"date": date(2026, 1, 3), "nav": 1050.0, "daily_pnl": None, "source": "intraday"},
        ]
    )
    cashflows = [
        {
            "cashflow_type": "Others",
            "amount": 1000.0,
            "occurred_at": date(2026, 1, 3),
            "raw": {"cashflow_remark": ""},
        }
    ]

    perf = performance._futu_official_performance(curve, cashflows)

    assert perf.returns[0] == 0.0
    assert perf.returns[1] == pytest.approx(0.5)
    assert perf.returns[2] == pytest.approx(-100 / 650)
    assert perf.time_weighted_return == pytest.approx((1.5 * (1 - 100 / 650)) - 1)
    assert perf.simple_return == pytest.approx(-50 / 600)
    assert perf.income == pytest.approx(-50.0)
    assert perf.net_inflow == pytest.approx(1000.0)


async def _seed(engine, n, *, broker="IB", env="paper", account="T_PERF", start_nav=50000.0, daily_pnl=100.0):
    async with engine.begin() as c:
        await c.execute(sa.delete(nav_history).where(nav_history.c.broker_account == account))
        nav = start_nav
        for i in range(n):
            d = date(2026, 1, 1) + timedelta(days=i)
            nav += daily_pnl
            await c.execute(
                sa.insert(nav_history).values(
                    broker=broker,
                    account_env=env,
                    broker_account=account,
                    date=d,
                    nav=str(nav),
                    daily_pnl=str(daily_pnl),
                    source="intraday",
                )
            )
    return account


async def _purge(engine, account):
    async with engine.begin() as c:
        await c.execute(sa.delete(nav_history).where(nav_history.c.broker_account == account))
        await c.execute(sa.delete(futu_cash_flow).where(futu_cash_flow.c.broker_account == account))


# ---------- threshold ladder ----------


async def test_under_5_rows_returns_insufficient_collecting(async_engine):
    await _seed(async_engine, 3, account="T_LT5")
    data = await compute(async_engine, AccountScope("IB", "paper", "T_LT5"))
    await _purge(async_engine, "T_LT5")
    assert data["status"] == "insufficient_history"
    assert data["reason"] == "collecting"
    assert data["days_collected"] == 3
    assert data["hero_net_liq"] is not None


async def test_5_to_30_rows_curve_only(async_engine):
    await _seed(async_engine, 10, account="T_5_30")
    data = await compute(async_engine, AccountScope("IB", "paper", "T_5_30"))
    await _purge(async_engine, "T_5_30")
    assert data["status"] == "ok"
    assert data["summary"]["sharpe_ratio"] is None  # masked: under 30
    assert data["summary"]["max_drawdown"] is not None  # always-on


async def test_30_plus_IB_unmasked_when_env_false(async_engine, monkeypatch):
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    await _seed(async_engine, 40, account="T_30_OK")
    data = await compute(async_engine, AccountScope("IB", "paper", "T_30_OK"))
    await _purge(async_engine, "T_30_OK")
    assert data["status"] == "ok"
    s = data["summary"]
    assert s["sharpe_ratio"] is not None
    assert s["calmar_ratio"] is not None
    assert s["positive_days"] is not None


async def test_30_plus_IB_MASKED_when_env_true(async_engine, monkeypatch):
    """Phase 0 default — IB risk metrics masked until verification confirms."""
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "true")
    await _seed(async_engine, 40, account="T_30_MASK")
    data = await compute(async_engine, AccountScope("IB", "paper", "T_30_MASK"))
    await _purge(async_engine, "T_30_MASK")
    s = data["summary"]
    assert s["sharpe_ratio"] is None
    assert "IB TWR requires cash-flow tracking" in " ".join(data["warnings"])


# ---------- IB return formula ----------


async def test_IB_returns_use_daily_pnl_over_prev_nav(async_engine, monkeypatch):
    """daily_pnl/prev_nav, NOT nav-delta/prev_nav. Construct a seed where
    they diverge: nav jumps from 100→200 (deposit) but daily_pnl=5 (only $5 trading)."""
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    account = "T_IB_RETF"
    await _purge(async_engine, account)
    async with async_engine.begin() as c:
        # day 1: nav=100, daily_pnl=0
        await c.execute(
            sa.insert(nav_history).values(
                broker="IB",
                account_env="paper",
                broker_account=account,
                date=date(2026, 1, 1),
                nav="100.0",
                daily_pnl="0.0",
                source="intraday",
            )
        )
        # day 2: nav=200 (deposit), daily_pnl=5 (only $5 of trading)
        await c.execute(
            sa.insert(nav_history).values(
                broker="IB",
                account_env="paper",
                broker_account=account,
                date=date(2026, 1, 2),
                nav="200.0",
                daily_pnl="5.0",
                source="intraday",
            )
        )
        for i in range(3, 8):
            await c.execute(
                sa.insert(nav_history).values(
                    broker="IB",
                    account_env="paper",
                    broker_account=account,
                    date=date(2026, 1, i),
                    nav="200.0",
                    daily_pnl="0.0",
                    source="intraday",
                )
            )
    data = await compute(async_engine, AccountScope("IB", "paper", account))
    await _purge(async_engine, account)
    series = data["series"]
    # series[1] = day 2: daily_pnl/prev_nav = 5/100 = 0.05 — NOT 100/100=1.0
    assert series[1]["daily_return"] == pytest.approx(0.05, rel=1e-9)


async def test_first_IB_return_zeroed(async_engine, monkeypatch):
    """Correction #5: returns[0] must be 0 (no prior NAV)."""
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    account = "T_RET0"
    await _purge(async_engine, account)
    async with async_engine.begin() as c:
        await c.execute(
            sa.insert(nav_history).values(
                broker="IB",
                account_env="paper",
                broker_account=account,
                date=date(2026, 1, 1),
                nav="100.0",
                daily_pnl="999.0",
                source="intraday",
            )
        )
        for i in range(2, 8):
            await c.execute(
                sa.insert(nav_history).values(
                    broker="IB",
                    account_env="paper",
                    broker_account=account,
                    date=date(2026, 1, i),
                    nav="100.0",
                    daily_pnl="0.0",
                    source="intraday",
                )
            )
    data = await compute(async_engine, AccountScope("IB", "paper", account))
    await _purge(async_engine, account)
    assert data["series"][0]["daily_return"] == 0.0  # NOT 999/100=9.99


# ---------- FUTU masking ----------


# Two FUTU integration tests previously here
# (`test_FUTU_30_plus_metrics_unmasked_with_official_cashflow_adjusted_returns`,
# `test_FUTU_uses_official_cashflow_adjusted_twr_and_simple_return`) asserted
# the interim half-flow Futu-official formula and the legacy "Futu official TWR"
# methodology basis. Both are superseded by the PR #130 holistic upgrade which
# replaces `_futu_official_performance`'s call site with `_futu_returns` +
# `_fill_return_flavors` (Simple / TWR / IRR) under a uniform "Time-Weighted
# Return (TWR)" methodology basis. Equivalent integration coverage now lives in
# `test_performance_futu_unmasked.py::test_futu_deposit_excluded_from_simple_total_return`.
# The underlying helper `_futu_official_performance` itself is still exercised
# by `test_futu_official_performance_math_matches_help_example` above.


# ---------- low-confidence (spec §4) ----------


async def test_low_confidence_at_30_sessions(async_engine, monkeypatch):
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    await _seed(async_engine, 30, account="T_LCONF_30")
    data = await compute(async_engine, AccountScope("IB", "paper", "T_LCONF_30"))
    await _purge(async_engine, "T_LCONF_30")
    s = data["summary"]
    assert s["low_confidence"] is True
    assert s["sharpe_se"] == pytest.approx(math.sqrt(252 / 30), rel=1e-6)


async def test_no_low_confidence_at_126_sessions(async_engine, monkeypatch):
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    await _seed(async_engine, 126, account="T_LCONF_126")
    data = await compute(async_engine, AccountScope("IB", "paper", "T_LCONF_126"))
    await _purge(async_engine, "T_LCONF_126")
    s = data["summary"]
    assert s["low_confidence"] is False
    assert s["sharpe_se"] is None


async def test_env_override_lowers_threshold(async_engine, monkeypatch):
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    monkeypatch.setenv("XENON_PERF_LOW_CONFIDENCE_DAYS", "30")
    await _seed(async_engine, 40, account="T_LCONF_OVR")
    data = await compute(async_engine, AccountScope("IB", "paper", "T_LCONF_OVR"))
    await _purge(async_engine, "T_LCONF_OVR")
    assert data["summary"]["low_confidence"] is False


# ---------- scope isolation ----------


async def test_scope_isolation_ib_vs_futu(async_engine, monkeypatch):
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    await _seed(async_engine, 10, broker="IB", env="paper", account="T_ISO_IB", start_nav=50000)
    await _seed(async_engine, 10, broker="FUTU", env="live", account="T_ISO_FUTU", start_nav=200000)
    ib = await compute(async_engine, AccountScope("IB", "paper", "T_ISO_IB"))
    fu = await compute(async_engine, AccountScope("FUTU", "live", "T_ISO_FUTU"))
    await _purge(async_engine, "T_ISO_IB")
    await _purge(async_engine, "T_ISO_FUTU")
    assert ib["summary"]["ending_equity"] != fu["summary"]["ending_equity"]
    assert ib["scope"]["broker"] == "IB"
    assert fu["scope"]["broker"] == "FUTU"


# ---------- benchmark ----------


async def test_benchmark_unavailable_warning(async_engine, monkeypatch):
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    await _seed(async_engine, 40, account="T_BENCH_NA")
    # No ib_pool, no benchmark_closes rows → expect benchmark fields null + warning
    data = await compute(async_engine, AccountScope("IB", "paper", "T_BENCH_NA"))
    await _purge(async_engine, "T_BENCH_NA")
    assert data["benchmark"] is None
    assert data["summary"]["beta"] is None
    assert any("benchmark_unavailable" in w for w in data["warnings"])
