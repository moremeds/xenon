from xenon.db.schema import XENON_SCHEMA, journal_entries


def test_journal_entries_table_shape():
    assert journal_entries.schema == XENON_SCHEMA
    assert [column.name for column in journal_entries.primary_key.columns] == ["id"]

    columns = set(journal_entries.c.keys())
    assert {
        "id",
        "trade_id",
        "ticker",
        "decision",
        "note",
        "attachments",
        "authored_by",
        "authored_at",
        "metadata",
        "broker",
        "account_env",
        "broker_account",
    }.issubset(columns)

    foreign_keys = {fk.target_fullname for fk in journal_entries.c.trade_id.foreign_keys}
    assert foreign_keys == {f"{XENON_SCHEMA}.trades.id"}
    assert journal_entries.c.authored_at.server_default is not None


def test_journal_entries_scope_constraints_and_indexes():
    constraint_names = {constraint.name for constraint in journal_entries.constraints}
    assert "ck_journal_broker" in constraint_names
    assert "ck_journal_account_env" in constraint_names

    index_names = {index.name for index in journal_entries.indexes}
    assert "ix_journal_ticker_at" in index_names
    assert "ix_journal_scope_at" in index_names
