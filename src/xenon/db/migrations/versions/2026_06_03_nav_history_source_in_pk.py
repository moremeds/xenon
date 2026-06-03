"""nav_history: source in PK + secondary index — Pass-2 E1 option (a).

Lets intraday and close rows coexist for the same (broker, account_env,
broker_account, date). Audit becomes row-level: same-date `source='intraday'`
vs `source='close'` rows live side-by-side; nav_history IS the audit table.

Before: PK = (broker, account_env, broker_account, date) + secondary unique
index `nav_history_one_env_per_day` on (broker, broker_account, date).

After:  PK = (broker, account_env, broker_account, date, source) + secondary
unique index `nav_history_one_env_per_day_per_source` on
(broker, broker_account, date, source).

The cross-env collision guard is preserved by the secondary index: two rows
with different `account_env` still cannot share (broker, broker_account, date,
source). What changes is that intraday + close rows for the SAME scope+date
now coexist.

Downgrade is destructive on the `close` rows that have intraday twins (the
old PK would reject them as duplicates). The downgrade DELETEs them first,
preserving the intraday row (closer to v1 behavior). Operators can re-run
`xenon-nav-flex-refresh` to recover close NAVs.
"""

from alembic import op

revision = "2026_06_03_nav_src_pk"
down_revision = "2026_06_02_cf_open"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE xenon.nav_history DROP CONSTRAINT nav_history_pkey")
    op.drop_index("nav_history_one_env_per_day", table_name="nav_history", schema="xenon")

    op.create_primary_key(
        "nav_history_pkey",
        "nav_history",
        ["broker", "account_env", "broker_account", "date", "source"],
        schema="xenon",
    )

    op.create_index(
        "nav_history_one_env_per_day_per_source",
        "nav_history",
        ["broker", "broker_account", "date", "source"],
        unique=True,
        schema="xenon",
    )


def downgrade() -> None:
    # Pass-3 A3: clean up coexisting (intraday, close) rows for the same scope+date
    # before recreating the old 4-col PK. Preserve the intraday row.
    op.execute(
        """
        DELETE FROM xenon.nav_history
        WHERE source = 'close'
          AND (broker, account_env, broker_account, date) IN (
              SELECT broker, account_env, broker_account, date
              FROM xenon.nav_history
              GROUP BY broker, account_env, broker_account, date
              HAVING COUNT(*) > 1
          )
        """
    )

    op.drop_index(
        "nav_history_one_env_per_day_per_source",
        table_name="nav_history",
        schema="xenon",
    )
    op.execute("ALTER TABLE xenon.nav_history DROP CONSTRAINT nav_history_pkey")
    op.create_primary_key(
        "nav_history_pkey",
        "nav_history",
        ["broker", "account_env", "broker_account", "date"],
        schema="xenon",
    )
    op.create_index(
        "nav_history_one_env_per_day",
        "nav_history",
        ["broker", "broker_account", "date"],
        unique=True,
        schema="xenon",
    )
