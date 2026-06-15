import sqlalchemy as sa


def test_service_health_table_exists(pg_test_engine):
    # Inspect the real session engine, not the Phase-2 _BoundEngine wrapper
    # that get_sync_engine() is monkeypatched to under the autouse fixture.
    insp = sa.inspect(pg_test_engine)
    cols = {c["name"] for c in insp.get_columns("service_health", schema="xenon")}
    assert {
        "service",
        "broker",
        "account_env",
        "broker_account",
        "state",
        "detail",
        "last_error",
        "last_started_at",
        "last_finished_at",
        "updated_at",
    } <= cols
    pk = set(insp.get_pk_constraint("service_health", schema="xenon")["constrained_columns"])
    assert pk == {"service", "broker", "account_env", "broker_account"}
