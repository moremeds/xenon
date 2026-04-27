"""Verify normalize_payloads migration produced the expected schema."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from scripts.tests.conftest import _sync_test_db_url


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def _columns(engine, table: str) -> dict[str, dict]:
    insp = inspect(engine)
    return {c["name"]: c for c in insp.get_columns(table, schema="xenon")}


def test_uw_analyze_snapshots_has_new_jsonb_columns(engine):
    cols = _columns(engine, "uw_analyze_snapshots")
    for name in (
        "report",
        "display",
        "derived",
        "dark_pool_summary",
        "options_flow_summary",
        "flow_alerts",
        "materialized_changes",
        "report_fetched_at",
        "archived_at",
    ):
        assert name in cols, f"missing column {name}"


def test_uw_analyze_snapshots_dropped_old_jsonb_columns(engine):
    cols = _columns(engine, "uw_analyze_snapshots")
    for name in ("vrp_state", "regime", "flow_signals"):
        assert name not in cols, f"old column {name} should be dropped"


def test_uw_analyze_snapshots_has_generated_columns(engine):
    cols = _columns(engine, "uw_analyze_snapshots")
    for name in (
        "price",
        "composite_score",
        "grade",
        "bias",
        "vrp_raw",
        "regime_label",
        "gex_sign",
        "iv_rank",
        "call_wall_strike",
        "dp_score",
        "dp_signal",
        "of_total_alerts",
        "spy_iv_rank",
    ):
        assert name in cols, f"missing generated column {name}"


def test_cri_series_has_generated_columns(engine):
    cols = _columns(engine, "cri_series")
    for name in (
        "recorded_date",
        "vix",
        "vvix",
        "spy",
        "cri_score",
        "cri_components",
        "cta_exposure_pct",
        "cta_forced_reduction",
        "menthorq_cta_score",
        "crash_trigger_fired",
    ):
        assert name in cols


def test_vcg_series_table_exists(engine):
    insp = inspect(engine)
    assert "vcg_series" in insp.get_table_names(schema="xenon")
    cols = _columns(engine, "vcg_series")
    for name in (
        "scanned_at",
        "market_open",
        "credit_proxy",
        "payload",
        "vcg",
        "vcg_adj",
        "residual",
        "regime",
        "interpretation",
        "attr_vvix_pct",
        "attr_model_implied",
    ):
        assert name in cols


def test_gex_snapshots_table_exists(engine):
    insp = inspect(engine)
    assert "gex_snapshots" in insp.get_table_names(schema="xenon")
    cols = _columns(engine, "gex_snapshots")
    for name in (
        "ticker",
        "data_date",
        "scanned_at",
        "payload",
        "spot",
        "net_gex",
        "iv_30d",
        "level_max_magnet_strike",
        "level_put_wall_strike",
        "level_call_wall_strike",
    ):
        assert name in cols


def test_child_tables_exist(engine):
    insp = inspect(engine)
    names = set(insp.get_table_names(schema="xenon"))
    for t in (
        "uw_analyze_flow_alerts",
        "uw_analyze_gex_strikes",
        "uw_analyze_short_volume_trend",
        "uw_flow_event_ticks",
    ):
        assert t in names


def test_uw_flow_events_has_initial_generated_columns(engine):
    cols = _columns(engine, "uw_flow_events")
    for name in (
        "initial_premium_usd",
        "initial_oi",
        "initial_volume",
        "initial_mid",
        "initial_underlying_price",
    ):
        assert name in cols


def test_triggers_exist(engine):
    with engine.begin() as conn:
        result = (
            conn.execute(
                text(
                    """
            SELECT DISTINCT trigger_name FROM information_schema.triggers
            WHERE trigger_schema = 'xenon'
              AND trigger_name LIKE 'trg_%_fanout'
        """
                )
            )
            .scalars()
            .all()
        )
    expected = {
        "trg_uw_analyze_flow_alerts_fanout",
        "trg_uw_analyze_gex_strikes_fanout",
        "trg_uw_analyze_short_volume_trend_fanout",
        "trg_uw_flow_event_ticks_fanout",
    }
    assert expected.issubset(set(result)), f"missing: {expected - set(result)}"
