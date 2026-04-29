"""Tests for the UNKNOWN order replay backfill (W1.3)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select

from scripts.migrations._2026_04_28_replay_unknown_orders import replay_unknown
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_submissions
from xenon.execution.account_scope import AccountScope

_SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU222222")


def _insert_unknown(submission_id: str, *, ib_order_id: str | None = None, perm_id: str | None = None) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=submission_id,
                ticker="SPY",
                security_type="STK",
                action="BUY",
                quantity=10,
                limit_price=400,
                state="UNKNOWN",
                ib_order_id=ib_order_id,
                perm_id=perm_id,
                submitted_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
                broker=_SCOPE.broker,
                account_env=_SCOPE.account_env,
                broker_account=_SCOPE.broker_account,
            )
        )


def _current_state(submission_id: str) -> str | None:
    engine = get_sync_engine()
    with engine.connect() as conn:
        return conn.execute(
            select(order_submissions.c.state).where(order_submissions.c.submission_id == submission_id)
        ).scalar_one_or_none()


def _empty_ib() -> SimpleNamespace:
    return SimpleNamespace(
        get_open_orders=lambda: [],
        get_executions=lambda: [],
        get_positions=lambda: [],
    )


def test_replay_no_unknown_rows_is_noop():
    summary = replay_unknown(scope=_SCOPE, ib_client_factory=_empty_ib)
    assert summary == {"resolved": 0, "still_unknown": 0, "scanned": 0, "errors": []}


def test_replay_invokes_rehydrate_with_unknown_state_filter(monkeypatch):
    """Wiring contract: replay_unknown must call rehydrate_on_boot with states=('UNKNOWN',)."""
    _insert_unknown("sub-replay-A")

    captured: dict = {}

    def fake_rehydrate(ib_factory, store, **kwargs):
        captured["kwargs"] = kwargs
        # Return one non-noop decision proving the resolved counter works.
        return [
            SimpleNamespace(
                noop=False,
                to_state="FILLED",
                event_kind="REHYDRATE",
                detail={},
                reason_code=None,
                filled_qty=10,
                avg_fill_price=400.0,
            )
        ]

    monkeypatch.setattr(
        "scripts.migrations._2026_04_28_replay_unknown_orders.rehydrate_on_boot",
        fake_rehydrate,
    )

    summary = replay_unknown(scope=_SCOPE, ib_client_factory=_empty_ib)
    assert captured["kwargs"]["states"] == ("UNKNOWN",)
    assert captured["kwargs"]["broker"] == _SCOPE.broker
    assert captured["kwargs"]["account_env"] == _SCOPE.account_env
    assert captured["kwargs"]["broker_account"] == _SCOPE.broker_account
    assert summary["scanned"] == 1
    assert summary["resolved"] == 1
    assert summary["still_unknown"] == 0
    assert summary["errors"] == []


def test_replay_surfaces_errors_rather_than_silencing_them():
    _insert_unknown("sub-replay-B")

    def boom():
        raise RuntimeError("ib unreachable")

    fake_ib = SimpleNamespace(
        get_open_orders=boom,
        get_executions=lambda: [],
        get_positions=lambda: [],
    )
    summary = replay_unknown(scope=_SCOPE, ib_client_factory=lambda: fake_ib)
    assert summary["scanned"] >= 1
    assert summary["errors"], f"errors must be surfaced, not silenced; got {summary}"


def test_replay_default_factory_uses_auto_allocated_ib_client(monkeypatch):
    import scripts.migrations._2026_04_28_replay_unknown_orders as mod

    calls = {"connect": [], "disconnect": 0}

    class FakeClient:
        def connect(self, **kwargs):
            calls["connect"].append(kwargs)

        def disconnect(self):
            calls["disconnect"] += 1

        def get_open_orders(self):
            return []

        def get_executions(self):
            return []

        def get_positions(self):
            return []

    monkeypatch.setattr(mod, "IBClient", lambda: FakeClient())
    monkeypatch.setattr(mod, "_count_unknown", lambda scope: 1)

    def fake_rehydrate(ib_factory, store, **kwargs):
        ib = ib_factory()
        assert ib.get_open_orders() == []
        return [SimpleNamespace(noop=True, to_state="UNKNOWN")]

    monkeypatch.setattr(mod, "rehydrate_on_boot", fake_rehydrate)

    summary = mod.replay_unknown(scope=_SCOPE)

    assert calls["connect"]
    assert calls["connect"][0]["client_id"] == "auto"
    assert calls["disconnect"] == 1
    assert summary["scanned"] == 1


def test_replay_module_no_longer_imports_api_ib_pool():
    import inspect
    import scripts.migrations._2026_04_28_replay_unknown_orders as mod

    source = inspect.getsource(mod)
    assert "from xenon.api import ib_pool" not in source
    assert "ib_pool.get" not in source
