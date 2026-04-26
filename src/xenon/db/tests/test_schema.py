from xenon.db.schema import events_metadata, xenon_metadata


def test_xenon_metadata_has_expected_tables():
    expected = {
        "positions",
        "account_snapshots",
        "trades",
        "nav_history",
        "order_submissions",
        "order_events",
        "wizard_sessions",
        "wizard_events",
        "wizard_combo_attempts",
        "wizard_protection",
        "scan_results",
        "cri_series",
        "uw_snapshots",
        "uw_flow_events",
        "uw_api_stats",
        "ticker_cache",
    }
    actual_names = {name.split(".")[-1] for name in xenon_metadata.tables.keys()}
    assert expected.issubset(actual_names), f"Missing: {expected - actual_names}"


def test_events_metadata_has_outbox():
    actual_names = {name.split(".")[-1] for name in events_metadata.tables.keys()}
    assert "outbox" in actual_names


def test_order_submissions_has_required_columns():
    table = xenon_metadata.tables["xenon.order_submissions"]
    col_names = {c.name for c in table.columns}
    required = {
        "submission_id",
        "user_id",
        "ticker",
        "security_type",
        "action",
        "quantity",
        "state",
        "submitted_at",
        "updated_at",
    }
    assert required.issubset(col_names), f"Missing: {required - col_names}"


def test_positions_has_account_column():
    table = xenon_metadata.tables["xenon.positions"]
    col_names = {c.name for c in table.columns}
    assert "account" in col_names
