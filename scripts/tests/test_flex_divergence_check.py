"""Tests for the nightly PG↔Flex divergence job (V.4)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from xenon.execution.account_scope import AccountScope
from xenon.jobs.flex_divergence_check import (
    _main,
    compute_divergence,
    latest_run,
    record_run,
)

_SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU333333")


def test_compute_divergence_counts_disagreements():
    pg = {
        "closed_trades": [
            {"perm_id": "x1", "realized_pnl": 10.0, "total_commission": 1},
            {"perm_id": "x2", "realized_pnl": 5.0, "total_commission": 1},
        ]
    }
    flex = {
        "closed_trades": [
            {"perm_id": "x1", "realized_pnl": 10.0, "total_commission": 1},
            {"perm_id": "x2", "realized_pnl": 5.5, "total_commission": 1},
            {"perm_id": "x3", "realized_pnl": 1.0, "total_commission": 1},
        ]
    }
    summary = compute_divergence(pg, flex)
    assert summary["total_compared"] == 2
    assert summary["divergence_count"] == 1


def test_record_run_round_trips_latest():
    summary = {"total_compared": 4, "divergence_count": 1, "notes": {"sample": "ok"}}
    inserted_id = record_run(scope=_SCOPE, summary=summary)
    latest = latest_run(scope=_SCOPE)
    assert latest is not None
    assert latest["id"] == inserted_id
    assert latest["divergence_count"] == 1
    assert latest["total_compared"] == 4
    assert latest["ran_at"] is not None


def test_main_skips_when_flex_unavailable(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", _SCOPE.account_env)
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", _SCOPE.broker_account)
    fake_pg = {"closed_trades": [], "open_trades": []}

    async def fake_run_module(*_a, **_kw):
        return SimpleNamespace(ok=False, exit_code=2, error="FLEX_NOT_CONFIGURED", data=None)

    with (
        patch("xenon.jobs.flex_divergence_check.fetch_blotter_pg", return_value=fake_pg),
        patch("xenon.jobs.flex_divergence_check.run_module", side_effect=fake_run_module),
    ):
        rc = _main(["--apply"])
    assert rc == 0  # graceful no-op


def test_main_records_a_run_when_both_sides_present(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", _SCOPE.account_env)
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", _SCOPE.broker_account)
    fake_pg = {"closed_trades": [{"perm_id": "p1", "realized_pnl": 10}], "open_trades": []}
    fake_flex_data = {"closed_trades": [{"perm_id": "p1", "realized_pnl": 10}], "open_trades": []}

    async def fake_run_module(*_a, **_kw):
        return SimpleNamespace(ok=True, data=fake_flex_data)

    with (
        patch("xenon.jobs.flex_divergence_check.fetch_blotter_pg", return_value=fake_pg),
        patch("xenon.jobs.flex_divergence_check.run_module", side_effect=fake_run_module),
    ):
        rc = _main(["--apply"])
    assert rc == 0
    latest = latest_run(scope=_SCOPE)
    assert latest is not None
