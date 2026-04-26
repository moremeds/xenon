"""Tests for combo wizard store (Postgres-backed, schema managed by Alembic)."""

from pathlib import Path

from xenon.execution.combo_wizard import store


def test_init_store_is_noop():
    """init_store is a backward-compat no-op that returns a Path."""
    result = store.init_store(None)
    assert isinstance(result, Path)


def test_init_store_with_path(tmp_path):
    result = store.init_store(tmp_path / "orders.duckdb")
    assert result == tmp_path / "orders.duckdb"


def test_list_tables_returns_set():
    """list_tables should return a set (may be empty if no Postgres connection)."""
    result = store.list_tables()
    assert isinstance(result, set)
