"""Regression tests for the one-time Postgres migration script."""

from __future__ import annotations

import json


class _CaptureConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []
        self.scalar_value = 0

    def __enter__(self) -> "_CaptureConn":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, statement, params: dict | None = None):  # noqa: ANN001 - SQLAlchemy accepts several statement types.
        self.calls.append((str(statement), params))
        return self

    def scalar(self) -> int:
        return self.scalar_value


class _CaptureEngine:
    def __init__(self) -> None:
        self.conn = _CaptureConn()

    def connect(self) -> _CaptureConn:
        return self.conn

    def begin(self) -> _CaptureConn:
        return self.conn


def test_migrate_nav_history_uses_broker_account_composite_key(tmp_path, monkeypatch):
    from scripts.migrations import migrate_to_postgres as migration

    history = tmp_path / "nav_history.jsonl"
    history.write_text(json.dumps({"date": "2026-04-24", "nav": 12345.67, "daily_pnl": 89.01}) + "\n")
    monkeypatch.setattr(migration, "DATA_DIR", tmp_path)
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU0000000")

    engine = _CaptureEngine()

    assert migration.migrate_nav_history(engine) == 1
    sql, params = engine.conn.calls[0]

    assert "broker, account_env, broker_account, date" in sql
    assert "ON CONFLICT (broker, account_env, broker_account, date)" in sql
    assert params is not None
    assert params["broker"] == "IB"
    assert params["account_env"] == "paper"
    assert params["broker_account"] == "DU0000000"


def test_migrate_portfolio_stamps_scope_on_snapshot_and_positions(tmp_path, monkeypatch):
    from scripts.migrations import migrate_to_postgres as migration

    (tmp_path / "portfolio.json").write_text(
        json.dumps(
            {
                "bankroll": 100000,
                "peak_value": 101000,
                "net_liquidation": 100500,
                "positions": [{"ticker": "AAPL", "security_type": "STK", "quantity": 10, "avg_cost": 100}],
            }
        )
    )
    monkeypatch.setattr(migration, "DATA_DIR", tmp_path)
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "U1234567")

    engine = _CaptureEngine()

    assert migration.migrate_portfolio(engine) == 1
    insert_calls = [(sql, params) for sql, params in engine.conn.calls if sql.lstrip().startswith("INSERT")]
    assert len(insert_calls) == 2
    for sql, params in insert_calls:
        assert "broker" in sql
        assert "account_env" in sql
        assert "broker_account" in sql
        assert params is not None
        assert params["broker"] == "IB"
        assert params["account_env"] == "live"
        assert params["broker_account"] == "U1234567"


def test_migrate_trade_log_stamps_scope(tmp_path, monkeypatch):
    from scripts.migrations import migrate_to_postgres as migration

    (tmp_path / "trade_log.json").write_text(
        json.dumps({"trades": [{"ticker": "MSFT", "action": "BUY", "quantity": 1, "structure": "call"}]})
    )
    monkeypatch.setattr(migration, "DATA_DIR", tmp_path)
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU0000000")

    engine = _CaptureEngine()

    assert migration.migrate_trade_log(engine) == 1
    insert_calls = [(sql, params) for sql, params in engine.conn.calls if sql.lstrip().startswith("INSERT")]
    assert len(insert_calls) == 1
    sql, params = insert_calls[0]
    assert "broker" in sql
    assert "account_env" in sql
    assert "broker_account" in sql
    assert params is not None
    assert params["broker"] == "IB"
    assert params["account_env"] == "paper"
    assert params["broker_account"] == "DU0000000"


def test_migration_scope_rejects_partial_scope_env(monkeypatch):
    from scripts.migrations import migrate_to_postgres as migration

    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.delenv("XENON_BROKER_ACCOUNT", raising=False)

    try:
        migration._migration_scope()
    except ValueError as exc:
        assert "Set both XENON_TRADING_MODE and XENON_BROKER_ACCOUNT" in str(exc)
    else:
        raise AssertionError("partial migration scope env should fail loudly")
