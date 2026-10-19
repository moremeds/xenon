"""PR-18: the dead signal-layer tables are gone from the ORM metadata and the
drop migration covers exactly that set. No database needed."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from xenon import _test_db
from xenon.db.schema import xenon_metadata

_VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
_REV = "2026_10_20_drop_signal_tables"

DEAD_TABLES = {
    "scan_results",
    "cri_series",
    "vcg_series",
    "gex_snapshots",
    "uw_analyze_snapshots",
    "uw_analyze_flow_alerts",
    "uw_analyze_gex_strikes",
    "uw_analyze_short_volume_trend",
    "uw_flow_events",
    "uw_flow_event_ticks",
    "ticker_cache",
    "uw_api_stats",
}


def _migration():
    spec = importlib.util.spec_from_file_location("_pr18_mig", _VERSIONS / f"{_REV}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dead_tables_absent_from_schema_metadata():
    names = {t.name for t in xenon_metadata.tables.values()}
    assert not (DEAD_TABLES & names), DEAD_TABLES & names


def test_no_live_table_references_a_dead_table():
    for table in xenon_metadata.tables.values():
        for fk in table.foreign_keys:
            assert fk.column.table.name not in DEAD_TABLES, (table.name, fk.target_fullname)


def test_dead_tables_absent_from_test_truncate_list():
    listed = {n.split(".", 1)[1] for n in _test_db.XENON_TABLES}
    assert not (DEAD_TABLES & listed)


def test_migration_drop_list_matches_dead_tables():
    mig = _migration()
    assert set(mig.DROPPED_TABLES) == DEAD_TABLES
    assert len(mig.DROPPED_TABLES) == len(DEAD_TABLES)


def test_migration_drops_children_before_parents():
    order = list(_migration().DROPPED_TABLES)
    for child, parent in [
        ("uw_analyze_flow_alerts", "uw_analyze_snapshots"),
        ("uw_analyze_gex_strikes", "uw_analyze_snapshots"),
        ("uw_analyze_short_volume_trend", "uw_analyze_snapshots"),
        ("uw_flow_event_ticks", "uw_flow_events"),
    ]:
        assert order.index(child) < order.index(parent)


def test_downgrade_recreates_exactly_the_dropped_tables():
    mig = _migration()
    created = {m for ddl in mig._TABLE_DDL for m in re.findall(r"^\s*CREATE TABLE xenon\.(\w+)", ddl, re.M)}
    assert created == DEAD_TABLES
    funcs = {m for ddl in mig._FANOUT_FUNCTIONS for m in re.findall(r"FUNCTION xenon\.(\w+)\(", ddl)}
    assert funcs == set(mig.DROPPED_FUNCTIONS)


def test_regime_overrides_is_kept():
    assert "regime_overrides" in {t.name for t in xenon_metadata.tables.values()}
    assert "regime_overrides" not in _migration().DROPPED_TABLES
