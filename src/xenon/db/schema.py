from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

XENON_SCHEMA = "xenon"
EVENTS_SCHEMA = "events"

xenon_metadata = MetaData(schema=XENON_SCHEMA)
events_metadata = MetaData(schema=EVENTS_SCHEMA)

tz_now = text("now()")

# ---------- Portfolio & Trading ----------

positions = Table(
    "positions",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ticker", Text, nullable=False),
    Column("security_type", Text, nullable=False),
    Column("expiry", Date),
    Column("strike", Numeric(12, 2)),
    Column("right", Text),
    Column("quantity", Integer, nullable=False),
    Column("avg_cost", Numeric(12, 4), nullable=False),
    Column("current_price", Numeric(12, 4)),
    Column("unrealized_pnl", Numeric(12, 2)),
    Column("account", Text, nullable=False),
    Column("synced_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, nullable=False, server_default=text("'legacy_unknown'")),
    CheckConstraint("broker IN ('IB', 'FUTU')", name="ck_positions_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_positions_account_env",
    ),
)

account_snapshots = Table(
    "account_snapshots",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("account", Text, nullable=False),
    Column("bankroll", Numeric(14, 2), nullable=False),
    Column("peak_value", Numeric(14, 2)),
    Column("net_liquidation", Numeric(14, 2)),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("snapshot_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, nullable=False, server_default=text("'legacy_unknown'")),
    CheckConstraint("broker IN ('IB', 'FUTU')", name="ck_acct_snap_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_acct_snap_account_env",
    ),
)

trades = Table(
    "trades",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ticker", Text, nullable=False),
    Column("structure", Text),
    Column("action", Text, nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("entry_cost", Numeric(12, 4)),
    Column("exit_cost", Numeric(12, 4)),
    Column("realized_pnl", Numeric(12, 2)),
    Column("edge", Text),
    Column("decision", Text),
    Column("opened_at", TIMESTAMP(timezone=True)),
    Column("closed_at", TIMESTAMP(timezone=True)),
    Column(
        "submission_id",
        Text,
        ForeignKey(f"{XENON_SCHEMA}.order_submissions.submission_id"),
        nullable=True,
    ),
    Column(
        "combo_attempt_id",
        Text,
        ForeignKey(f"{XENON_SCHEMA}.wizard_combo_attempts.attempt_id"),
        nullable=True,
    ),
    Column("state", Text, nullable=False, server_default=text("'OPEN'")),
    Column("metadata", JSONB),
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, nullable=False, server_default=text("'legacy_unknown'")),
    CheckConstraint("broker = 'IB'", name="ck_trades_broker_ib_only"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_trades_account_env",
    ),
    CheckConstraint("state IN ('OPEN','PARTIALLY_FILLED','CLOSED')", name="ck_trades_state"),
    Index("ix_trades_submission", "submission_id"),
    Index("ix_trades_combo_attempt", "combo_attempt_id"),
)

journal_entries = Table(
    "journal_entries",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("trade_id", BigInteger, ForeignKey(f"{XENON_SCHEMA}.trades.id"), nullable=True),
    Column("ticker", Text, nullable=False),
    Column("decision", Text),
    Column("note", Text),
    Column("attachments", JSONB),
    Column("authored_by", Text),
    Column("authored_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("metadata", JSONB),
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, nullable=False, server_default=text("'legacy_unknown'")),
    CheckConstraint("broker IN ('IB','FUTU')", name="ck_journal_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_journal_account_env",
    ),
    Index("ix_journal_ticker_at", "ticker", "authored_at"),
    Index("ix_journal_scope_at", "broker", "account_env", "broker_account", "authored_at"),
    Index(
        "uq_journal_auto_import",
        "broker",
        "account_env",
        "broker_account",
        "trade_id",
        unique=True,
        postgresql_where=text("decision = 'IB_AUTO_IMPORT' AND trade_id IS NOT NULL"),
    ),
)

flex_divergence_runs = Table(
    "flex_divergence_runs",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ran_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("scope_broker", Text, nullable=False),
    Column("scope_account_env", Text, nullable=False),
    Column("scope_broker_account", Text, nullable=False),
    Column("total_compared", Integer, nullable=False),
    Column("divergence_count", Integer, nullable=False),
    Column("notes", JSONB, nullable=True),
    Index(
        "ix_flex_divergence_scope_ran_at",
        "scope_broker",
        "scope_account_env",
        "scope_broker_account",
        "ran_at",
    ),
)

nav_history = Table(
    "nav_history",
    xenon_metadata,
    Column("broker", Text, primary_key=True, server_default=text("'IB'")),
    Column("account_env", Text, primary_key=True, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, primary_key=True, server_default=text("'legacy_unknown'")),
    Column("date", Date, primary_key=True),
    Column("nav", Numeric(14, 2), nullable=False),
    Column("daily_pnl", Numeric(12, 2)),
    # IB Flex EquitySummaryByReportDateInBase breakdown (NULL when no Flex data).
    # Replaces data/nav_history_ib.json post-2026-05-03 PG cutoff.
    Column("total", Numeric(14, 2)),
    Column("cash", Numeric(14, 2)),
    Column("stock_value", Numeric(14, 2)),
    Column("options_value", Numeric(14, 2)),
    # spec §12 / migration 260fabba18d6 — distinguish post-close from intraday snapshots
    Column("source", Text, nullable=False, server_default=text("'intraday'")),
    CheckConstraint("broker IN ('IB', 'FUTU')", name="ck_nav_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_nav_account_env",
    ),
    CheckConstraint("source IN ('close', 'intraday')", name="ck_nav_history_source"),
    # spec Decisions §13 / migration 489476c351cc — atomic dual-curve protection.
    # Excludes account_env so two rows with different envs cannot coexist for
    # the same (broker, broker_account, date).
    Index("nav_history_one_env_per_day", "broker", "broker_account", "date", unique=True),
)

# spec § Schema changes Migration 1 — SPY/benchmark close cache.
benchmark_closes = Table(
    "benchmark_closes",
    xenon_metadata,
    Column("symbol", Text, primary_key=True),
    Column("date", Date, primary_key=True),
    Column("close", Numeric(14, 4), nullable=False),
)

# ---------- Order Lifecycle ----------

order_submissions = Table(
    "order_submissions",
    xenon_metadata,
    Column("submission_id", Text, primary_key=True),
    Column("user_id", Text),
    Column("client_attempt_id", Text),
    Column("ticker", Text, nullable=False),
    Column("security_type", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("expiry", Date),
    Column("strike", Numeric(12, 2)),
    Column("right", Text),
    Column("multiplier", Integer, server_default=text("100")),
    Column("con_id", BigInteger),
    Column("placing_client_id", Integer),
    Column("ib_order_id", Text),
    Column("perm_id", Text),
    Column("limit_price", Numeric(12, 4)),
    Column("state", Text, nullable=False),
    Column("reason_code", Text),
    Column("filled_qty", Integer, server_default=text("0")),
    Column("avg_fill_price", Numeric(12, 4)),
    Column("modify_sequence", Integer, server_default=text("0")),
    Column("tif", Text, nullable=False, server_default=text("'DAY'")),
    Column("submitted_at", TIMESTAMP(timezone=True), nullable=False),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, nullable=False, server_default=text("'legacy_unknown'")),
    CheckConstraint("broker = 'IB'", name="ck_order_sub_broker_ib_only"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_order_sub_account_env",
    ),
    UniqueConstraint(
        "broker",
        "account_env",
        "broker_account",
        "user_id",
        "client_attempt_id",
        name="uq_order_sub_user_attempt",
    ),
    # FK target for regime_overrides composite scope FK (ISSUE-5).
    # Logically redundant under the submission_id PK, but Postgres requires
    # an explicit UNIQUE/PK target for composite FKs.
    UniqueConstraint(
        "submission_id",
        "broker",
        "account_env",
        "broker_account",
        name="uq_order_sub_submission_scope",
    ),
    Index("ix_order_sub_state_ticker", "state", "ticker"),
    Index("ix_order_sub_perm_id", "broker", "account_env", "broker_account", "perm_id"),
    Index("ix_order_sub_ib_order_id", "broker", "account_env", "broker_account", "ib_order_id"),
)

order_events = Table(
    "order_events",
    xenon_metadata,
    Column("event_id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "submission_id",
        Text,
        ForeignKey(f"{XENON_SCHEMA}.order_submissions.submission_id"),
        nullable=False,
    ),
    Column("kind", Text, nullable=False),
    Column("detail", JSONB),
    Column("at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Index("ix_order_events_submission_at", "submission_id", "at"),
)

order_fills = Table(
    "order_fills",
    xenon_metadata,
    Column("exec_id", Text, primary_key=True),
    Column(
        "submission_id",
        Text,
        ForeignKey(f"{XENON_SCHEMA}.order_submissions.submission_id"),
        nullable=True,
    ),
    Column(
        "combo_attempt_id",
        Text,
        ForeignKey(f"{XENON_SCHEMA}.wizard_combo_attempts.attempt_id"),
        nullable=True,
    ),
    Column("perm_id", Text),
    Column("ib_order_id", Text),
    Column("con_id", BigInteger),
    Column("ticker", Text, nullable=False),
    Column("side", Text, nullable=False),
    Column("qty", Integer, nullable=False),
    Column("price", Numeric(12, 4), nullable=False),
    Column("commission", Numeric(12, 4), server_default=text("0")),
    Column("filled_at", TIMESTAMP(timezone=True), nullable=False),
    Column("metadata", JSONB),
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False),
    Column("broker_account", Text, nullable=False),
    CheckConstraint("broker IN ('IB','FUTU')", name="ck_fills_broker"),
    CheckConstraint(
        "submission_id IS NOT NULL "
        "OR combo_attempt_id IS NOT NULL "
        "OR (metadata IS NOT NULL AND metadata ? 'legacy_source')",
        name="ck_fills_source_present",
    ),
    Index("ix_fills_perm_id", "broker", "account_env", "broker_account", "perm_id"),
    Index("ix_fills_submission", "submission_id"),
    Index("ix_fills_combo_attempt", "combo_attempt_id"),
    Index("ix_fills_ticker_time", "ticker", "filled_at"),
)

wizard_sessions = Table(
    "wizard_sessions",
    xenon_metadata,
    Column("session_id", Text, primary_key=True),
    Column("ticker", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("structure_name", Text),
    Column("intent", Text),
    Column("payload", JSONB),
    Column("current_attempt_id", Text),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, nullable=False, server_default=text("'legacy_unknown'")),
    CheckConstraint("broker = 'IB'", name="ck_wizard_sess_broker_ib_only"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_wizard_sess_account_env",
    ),
)

wizard_events = Table(
    "wizard_events",
    xenon_metadata,
    Column("event_id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "session_id",
        Text,
        ForeignKey(f"{XENON_SCHEMA}.wizard_sessions.session_id"),
        nullable=False,
    ),
    Column("kind", Text, nullable=False),
    Column("detail", JSONB),
    Column("at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
)

wizard_combo_attempts = Table(
    "wizard_combo_attempts",
    xenon_metadata,
    Column("attempt_id", Text, primary_key=True),
    Column(
        "session_id",
        Text,
        ForeignKey(f"{XENON_SCHEMA}.wizard_sessions.session_id"),
        nullable=False,
    ),
    Column("ticker", Text, nullable=False),
    Column("structure_name", Text),
    Column("legs", JSONB),
    Column("combo_contract", JSONB),
    Column("ib_order_id", Text),
    Column("perm_id", Text),
    Column("placing_client_id", Integer),
    Column("limit_price", Numeric(12, 4)),
    Column("state", Text, nullable=False),
    Column("reason_code", Text),
    Column("filled_qty", Integer, server_default=text("0")),
    Column("avg_fill_price", Numeric(12, 4)),
    Column("modify_sequence", Integer, server_default=text("0")),
    Column("submitted_at", TIMESTAMP(timezone=True)),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, nullable=False, server_default=text("'legacy_unknown'")),
    CheckConstraint("broker = 'IB'", name="ck_wizard_attempt_broker_ib_only"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_wizard_attempt_account_env",
    ),
    Index("ix_wizard_attempts_session_updated", "session_id", "updated_at"),
)

wizard_protection = Table(
    "wizard_protection",
    xenon_metadata,
    Column("protection_id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "session_id",
        Text,
        ForeignKey(f"{XENON_SCHEMA}.wizard_sessions.session_id"),
        nullable=False,
    ),
    Column(
        "attempt_id",
        Text,
        ForeignKey(f"{XENON_SCHEMA}.wizard_combo_attempts.attempt_id"),
    ),
    Column("protection_type", Text, nullable=False),
    Column("config", JSONB, nullable=False),
    Column("state", Text, nullable=False, server_default=text("'active'")),
    Column("triggered_at", TIMESTAMP(timezone=True)),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    UniqueConstraint("session_id", name="uq_wizard_protection_session"),
)

regime_overrides = Table(
    "regime_overrides",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ts", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("user_id", Text, nullable=False),
    Column("account_env", Text, nullable=False),
    Column("broker", Text, nullable=False),
    Column("broker_account", Text, nullable=False),
    Column("submission_id", Text, nullable=False),
    Column("client_attempt_id", Text),
    Column("perm_id", BigInteger),
    Column("ib_order_id", BigInteger),
    Column("route", Text, nullable=False),
    Column("vcg_tier", Text),
    Column("cri_tier", Text),
    Column("binding_side", Text, nullable=False),
    Column("block_reason", Text, nullable=False),
    Column("user_reason", Text, nullable=False),
    Column("order_payload", JSONB, nullable=False),
    # Composite FK (tribunal ISSUE-5): the override row's scope must match
    # the parent submission's scope. Without the scope columns on the FK,
    # an override row could reference a submission in a different account.
    ForeignKeyConstraint(
        ["submission_id", "broker", "account_env", "broker_account"],
        [
            f"{XENON_SCHEMA}.order_submissions.submission_id",
            f"{XENON_SCHEMA}.order_submissions.broker",
            f"{XENON_SCHEMA}.order_submissions.account_env",
            f"{XENON_SCHEMA}.order_submissions.broker_account",
        ],
        name="fk_regime_overrides_submission_scope",
        deferrable=True,
        initially="DEFERRED",
    ),
    Index("ix_regime_overrides_ts", text("ts DESC")),
    Index("ix_regime_overrides_submission", "submission_id"),
    Index("ix_regime_overrides_user_ts", "user_id", text("ts DESC")),
    Index(
        "ix_regime_overrides_scope_ts",
        "account_env",
        "broker_account",
        text("ts DESC"),
    ),
)

# ---------- Scanner Results ----------

scan_results = Table(
    "scan_results",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("scan_type", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("scanned_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
)

cri_series = Table(
    "cri_series",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("cri_level", Numeric(8, 4), nullable=False),
    Column("alert", Boolean, server_default=text("false")),
    Column("payload", JSONB),
    Column("recorded_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column(
        "recorded_date",
        Date,
        Computed(
            "make_date(split_part(payload->>'date','-',1)::int, split_part(payload->>'date','-',2)::int, split_part(payload->>'date','-',3)::int)",
            persisted=True,
        ),
    ),
    Column("vix", Numeric(8, 4), Computed("(payload->>'vix')::numeric", persisted=True)),
    Column("vvix", Numeric(8, 4), Computed("(payload->>'vvix')::numeric", persisted=True)),
    Column("spy", Numeric(10, 4), Computed("(payload->>'spy')::numeric", persisted=True)),
    Column("vix_5d_roc", Numeric(8, 4), Computed("(payload->>'vix_5d_roc')::numeric", persisted=True)),
    Column("vvix_vix_ratio", Numeric(8, 4), Computed("(payload->>'vvix_vix_ratio')::numeric", persisted=True)),
    Column("spx_100d_ma", Numeric(10, 4), Computed("(payload->>'spx_100d_ma')::numeric", persisted=True)),
    Column("spx_distance_pct", Numeric(8, 4), Computed("(payload->>'spx_distance_pct')::numeric", persisted=True)),
    Column("cor1m", Numeric(6, 4), Computed("(payload->>'cor1m')::numeric", persisted=True)),
    Column(
        "cor1m_previous_close", Numeric(6, 4), Computed("(payload->>'cor1m_previous_close')::numeric", persisted=True)
    ),
    Column("cor1m_5d_change", Numeric(6, 4), Computed("(payload->>'cor1m_5d_change')::numeric", persisted=True)),
    Column("realized_vol", Numeric(8, 4), Computed("(payload->>'realized_vol')::numeric", persisted=True)),
    Column("cri_score", Numeric(8, 4), Computed("((payload->'cri')->>'score')::numeric", persisted=True)),
    Column("cri_components", JSONB, Computed("payload->'cri'->'components'", persisted=True)),
    Column("cta_exposure_pct", Numeric(6, 2), Computed("((payload->'cta')->>'exposure_pct')::numeric", persisted=True)),
    Column(
        "cta_forced_reduction", Boolean, Computed("((payload->'cta')->>'forced_reduction')::boolean", persisted=True)
    ),
    Column(
        "cta_selling_usd_b", Numeric(8, 2), Computed("((payload->'cta')->>'selling_usd_b')::numeric", persisted=True)
    ),
    Column(
        "menthorq_cta_score", Numeric(8, 4), Computed("((payload->'menthorq_cta')->>'score')::numeric", persisted=True)
    ),
    Column("crash_trigger_fired", Boolean, Computed("((payload->'crash_trigger')->>'fired')::boolean", persisted=True)),
    Index("ix_cri_recorded_date", "recorded_date"),
    Index("ix_cri_crash_trigger", "crash_trigger_fired", postgresql_where=text("crash_trigger_fired")),
    Index("ix_cri_cta_forced", "cta_forced_reduction", postgresql_where=text("cta_forced_reduction")),
)

vcg_series = Table(
    "vcg_series",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("scanned_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("market_open", Boolean),
    Column("credit_proxy", Text),
    Column("payload", JSONB, nullable=False),
    Column("vcg", Numeric(10, 6), Computed("((payload->'signal')->>'vcg')::numeric", persisted=True)),
    Column("vcg_adj", Numeric(10, 6), Computed("((payload->'signal')->>'vcg_adj')::numeric", persisted=True)),
    Column("residual", Numeric(12, 8), Computed("((payload->'signal')->>'residual')::numeric", persisted=True)),
    Column("beta1_vvix", Numeric(12, 8), Computed("((payload->'signal')->>'beta1_vvix')::numeric", persisted=True)),
    Column("beta2_vix", Numeric(12, 8), Computed("((payload->'signal')->>'beta2_vix')::numeric", persisted=True)),
    Column("alpha", Numeric(12, 8), Computed("((payload->'signal')->>'alpha')::numeric", persisted=True)),
    Column("vix", Numeric(8, 4), Computed("((payload->'signal')->>'vix')::numeric", persisted=True)),
    Column("vvix", Numeric(8, 4), Computed("((payload->'signal')->>'vvix')::numeric", persisted=True)),
    Column("credit_price", Numeric(10, 4), Computed("((payload->'signal')->>'credit_price')::numeric", persisted=True)),
    Column(
        "credit_5d_return_pct",
        Numeric(8, 4),
        Computed("((payload->'signal')->>'credit_5d_return_pct')::numeric", persisted=True),
    ),
    Column("ro", SmallInteger, Computed("((payload->'signal')->>'ro')::int", persisted=True)),
    Column("edr", SmallInteger, Computed("((payload->'signal')->>'edr')::int", persisted=True)),
    Column("tier", SmallInteger, Computed("((payload->'signal')->>'tier')::int", persisted=True)),
    Column("bounce", SmallInteger, Computed("((payload->'signal')->>'bounce')::int", persisted=True)),
    Column("vvix_severity", Text, Computed("(payload->'signal')->>'vvix_severity'", persisted=True)),
    Column("sign_ok", Boolean, Computed("((payload->'signal')->>'sign_ok')::boolean", persisted=True)),
    Column("sign_suppressed", Boolean, Computed("((payload->'signal')->>'sign_suppressed')::boolean", persisted=True)),
    Column("pi_panic", Numeric(8, 4), Computed("((payload->'signal')->>'pi_panic')::numeric", persisted=True)),
    Column("regime", Text, Computed("(payload->'signal')->>'regime'", persisted=True)),
    Column("interpretation", Text, Computed("(payload->'signal')->>'interpretation'", persisted=True)),
    Column(
        "attr_vvix_pct",
        Numeric(6, 2),
        Computed("((payload->'signal'->'attribution')->>'vvix_pct')::numeric", persisted=True),
    ),
    Column(
        "attr_vix_pct",
        Numeric(6, 2),
        Computed("((payload->'signal'->'attribution')->>'vix_pct')::numeric", persisted=True),
    ),
    Column(
        "attr_vvix_component",
        Numeric(12, 8),
        Computed("((payload->'signal'->'attribution')->>'vvix_component')::numeric", persisted=True),
    ),
    Column(
        "attr_vix_component",
        Numeric(12, 8),
        Computed("((payload->'signal'->'attribution')->>'vix_component')::numeric", persisted=True),
    ),
    Column(
        "attr_model_implied",
        Numeric(12, 8),
        Computed("((payload->'signal'->'attribution')->>'model_implied')::numeric", persisted=True),
    ),
    UniqueConstraint("scanned_at", name="uq_vcg_series_scanned_at"),
    Index("ix_vcg_scanned_at", "scanned_at"),
    Index("ix_vcg_regime", "regime"),
    Index("ix_vcg_tier", "tier", postgresql_where=text("tier IS NOT NULL")),
)

gex_snapshots = Table(
    "gex_snapshots",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ticker", Text, nullable=False),
    Column("data_date", Date),
    Column("scanned_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("payload", JSONB, nullable=False),
    Column("spot", Numeric(12, 4), Computed("(payload->>'spot')::numeric", persisted=True)),
    Column("net_gex", Numeric(14, 2), Computed("(payload->>'net_gex')::numeric", persisted=True)),
    Column("net_dex", Numeric(14, 2), Computed("(payload->>'net_dex')::numeric", persisted=True)),
    Column("vol_pc", Numeric(8, 4), Computed("(payload->>'vol_pc')::numeric", persisted=True)),
    Column("iv_30d", Numeric(6, 4), Computed("((payload->'iv')->>'iv30d')::numeric", persisted=True)),
    Column("iv_rank", Numeric(6, 2), Computed("((payload->'iv')->>'iv_rank')::numeric", persisted=True)),
    Column("hv_30d", Numeric(6, 4), Computed("((payload->'iv')->>'hv30')::numeric", persisted=True)),
    Column("mq_iv_30d", Numeric(6, 4), Computed("((payload->'iv')->>'mq_iv30d')::numeric", persisted=True)),
    Column(
        "level_max_magnet_strike",
        Numeric(12, 4),
        Computed("(((payload->'levels')->'max_magnet')->>'strike')::numeric", persisted=True),
    ),
    Column(
        "level_max_magnet_gamma",
        Numeric(14, 4),
        Computed("(((payload->'levels')->'max_magnet')->>'gamma')::numeric", persisted=True),
    ),
    Column(
        "level_second_magnet_strike",
        Numeric(12, 4),
        Computed("(((payload->'levels')->'second_magnet')->>'strike')::numeric", persisted=True),
    ),
    Column(
        "level_second_magnet_gamma",
        Numeric(14, 4),
        Computed("(((payload->'levels')->'second_magnet')->>'gamma')::numeric", persisted=True),
    ),
    Column(
        "level_max_accelerator_strike",
        Numeric(12, 4),
        Computed("(((payload->'levels')->'max_accelerator')->>'strike')::numeric", persisted=True),
    ),
    Column(
        "level_max_accelerator_gamma",
        Numeric(14, 4),
        Computed("(((payload->'levels')->'max_accelerator')->>'gamma')::numeric", persisted=True),
    ),
    Column(
        "level_put_wall_strike",
        Numeric(12, 4),
        Computed("(((payload->'levels')->'put_wall')->>'strike')::numeric", persisted=True),
    ),
    Column(
        "level_call_wall_strike",
        Numeric(12, 4),
        Computed("(((payload->'levels')->'call_wall')->>'strike')::numeric", persisted=True),
    ),
    Column(
        "level_gex_flip_strike",
        Numeric(12, 4),
        Computed("(((payload->'levels')->'gex_flip')->>'strike')::numeric", persisted=True),
    ),
    Index("ix_gex_ticker_time", "ticker", "scanned_at"),
    Index("ix_gex_scanned_at", "scanned_at"),
    Index("ix_gex_data_date", "data_date"),
)

# ---------- UW Analysis ----------

uw_analyze_snapshots = Table(
    "uw_analyze_snapshots",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ticker", Text, nullable=False),
    Column("portfolio_score", Numeric(6, 2)),
    Column("snapshot_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("report", JSONB),
    Column("display", JSONB),
    Column("derived", JSONB),
    Column("dark_pool_summary", JSONB),
    Column("options_flow_summary", JSONB),
    Column("flow_alerts", JSONB),
    Column("materialized_changes", JSONB),
    # Internal cache state preserved across restarts so source-aware eviction
    # priority (sources), OI diffing (oi_baseline), and prev-vs-current diff
    # comparison (previous_snapshot) survive a process bounce. Without these,
    # rehydrated entries would land in tier=0/adhoc and force unnecessary OI
    # re-fetches.
    Column("sources", JSONB),
    Column("oi_baseline", JSONB),
    Column("previous_snapshot", JSONB),
    Column("report_fetched_at", TIMESTAMP(timezone=True)),
    Column("archived_at", TIMESTAMP(timezone=True)),
    Column("price", Numeric(12, 4), Computed("(report->>'price')::numeric", persisted=True)),
    Column("composite_score", Numeric(6, 2), Computed("((report->'scores')->>'composite')::numeric", persisted=True)),
    Column("flow_score", Numeric(6, 2), Computed("((report->'scores')->>'flow')::numeric", persisted=True)),
    Column("volatility_score", Numeric(6, 2), Computed("((report->'scores')->>'volatility')::numeric", persisted=True)),
    Column(
        "market_structure_score",
        Numeric(6, 2),
        Computed("((report->'scores')->>'market_structure')::numeric", persisted=True),
    ),
    Column(
        "positioning_score", Numeric(6, 2), Computed("((report->'scores')->>'positioning')::numeric", persisted=True)
    ),
    Column("grade", Text, Computed("(report->'scores')->>'grade'", persisted=True)),
    Column("bias", Text, Computed("(report->'scores')->>'bias'", persisted=True)),
    Column("mode", Text, Computed("(report->'scores')->>'mode'", persisted=True)),
    Column("reweighted", Boolean, Computed("((report->'scores')->>'reweighted')::boolean", persisted=True)),
    Column("vrp_raw", Numeric(8, 4), Computed("((report->'vrp')->>'vrp_raw')::numeric", persisted=True)),
    Column("vrp_zscore", Numeric(8, 4), Computed("((report->'vrp')->>'vrp_zscore')::numeric", persisted=True)),
    Column("iv_percentile", Numeric(6, 2), Computed("((report->'vrp')->>'iv_percentile')::numeric", persisted=True)),
    Column("ts_ratio", Numeric(8, 4), Computed("((report->'vrp')->>'ts_ratio')::numeric", persisted=True)),
    Column("ts_inverted", Boolean, Computed("((report->'vrp')->>'ts_inverted')::boolean", persisted=True)),
    Column(
        "earnings_within_14d", Boolean, Computed("((report->'vrp')->>'earnings_within_14d')::boolean", persisted=True)
    ),
    Column("regime_label", Text, Computed("(report->'regime')->>'regime'", persisted=True)),
    Column("regime_reason", Text, Computed("(report->'regime')->>'reason'", persisted=True)),
    Column("gex_sign", Text, Computed("(report->'regime')->>'gex_sign'", persisted=True)),
    Column("gex_flip_relative", Text, Computed("(report->'regime')->>'gex_flip_relative'", persisted=True)),
    Column(
        "flip_distance_pct",
        Numeric(8, 4),
        Computed("((report->'regime')->>'flip_distance_pct')::numeric", persisted=True),
    ),
    Column("iv", Numeric(8, 4), Computed("(display->>'iv')::numeric", persisted=True)),
    Column("rv", Numeric(8, 4), Computed("(display->>'rv')::numeric", persisted=True)),
    Column("iv_rank", Numeric(6, 2), Computed("(display->>'iv_rank')::numeric", persisted=True)),
    Column("call_wall_strike", Numeric(12, 4), Computed("(display->>'call_wall_strike')::numeric", persisted=True)),
    Column("put_wall_strike", Numeric(12, 4), Computed("(display->>'put_wall_strike')::numeric", persisted=True)),
    Column("gamma_per_1pct", Numeric(18, 4), Computed("(display->>'gamma_per_1pct')::numeric", persisted=True)),
    Column("net_call_premium", Numeric(18, 2), Computed("(display->>'net_call_premium')::numeric", persisted=True)),
    Column("net_put_premium", Numeric(18, 2), Computed("(display->>'net_put_premium')::numeric", persisted=True)),
    Column("short_volume_ratio", Numeric(6, 4), Computed("(display->>'short_volume_ratio')::numeric", persisted=True)),
    Column("term_structure_label", Text, Computed("display->>'term_structure_label'", persisted=True)),
    Column("max_pain", Numeric(12, 4), Computed("(display->>'max_pain')::numeric", persisted=True)),
    Column("derived_gex_sign", Text, Computed("derived->>'gex_sign'", persisted=True)),
    Column("derived_call_wall", Numeric(12, 4), Computed("(derived->>'call_wall')::numeric", persisted=True)),
    Column("derived_put_wall", Numeric(12, 4), Computed("(derived->>'put_wall')::numeric", persisted=True)),
    Column("derived_max_pain", Numeric(12, 4), Computed("(derived->>'max_pain')::numeric", persisted=True)),
    Column("derived_spot", Numeric(12, 4), Computed("(derived->>'spot')::numeric", persisted=True)),
    Column("dp_score", Numeric(8, 4), Computed("(dark_pool_summary->>'score')::numeric", persisted=True)),
    Column("dp_signal", Text, Computed("dark_pool_summary->>'signal'", persisted=True)),
    Column("dp_direction", Text, Computed("dark_pool_summary->>'direction'", persisted=True)),
    Column("dp_strength", Integer, Computed("(dark_pool_summary->>'strength')::numeric::int", persisted=True)),
    Column("dp_buy_ratio", Numeric(6, 4), Computed("(dark_pool_summary->>'buy_ratio')::numeric", persisted=True)),
    Column(
        "dp_options_conflict", Boolean, Computed("(dark_pool_summary->>'options_conflict')::boolean", persisted=True)
    ),
    Column("dp_num_prints", Integer, Computed("(dark_pool_summary->>'num_prints')::numeric::int", persisted=True)),
    Column(
        "dp_sustained_days", Integer, Computed("(dark_pool_summary->>'sustained_days')::numeric::int", persisted=True)
    ),
    Column(
        "of_total_alerts", Integer, Computed("(options_flow_summary->>'total_alerts')::numeric::int", persisted=True)
    ),
    Column(
        "of_total_premium",
        Numeric(18, 2),
        Computed("(options_flow_summary->>'total_premium')::numeric", persisted=True),
    ),
    Column(
        "of_call_premium", Numeric(18, 2), Computed("(options_flow_summary->>'call_premium')::numeric", persisted=True)
    ),
    Column(
        "of_put_premium", Numeric(18, 2), Computed("(options_flow_summary->>'put_premium')::numeric", persisted=True)
    ),
    Column(
        "of_call_put_ratio",
        Numeric(8, 4),
        Computed("(options_flow_summary->>'call_put_ratio')::numeric", persisted=True),
    ),
    Column("of_bias", Text, Computed("options_flow_summary->>'bias'", persisted=True)),
    Column(
        "spy_iv_rank", Numeric(6, 2), Computed("((report->'benchmark'->'spy')->>'iv_rank')::numeric", persisted=True)
    ),
    Column("spy_gex_regime", Text, Computed("(report->'benchmark'->'spy')->>'gex_regime'", persisted=True)),
    Column("sector_etf_ticker", Text, Computed("(report->'benchmark'->'sector_etf')->>'ticker'", persisted=True)),
    Column(
        "sector_etf_iv_rank",
        Numeric(6, 2),
        Computed("((report->'benchmark'->'sector_etf')->>'iv_rank')::numeric", persisted=True),
    ),
    Column(
        "sector_etf_gex_regime", Text, Computed("(report->'benchmark'->'sector_etf')->>'gex_regime'", persisted=True)
    ),
    Index("ix_uw_analyze_snap_ticker_time", "ticker", "snapshot_at"),
    Index("ix_uw_analyze_snap_time_gex", "snapshot_at", "gex_sign"),
    Index("ix_uw_analyze_snap_time_regime", "snapshot_at", "regime_label"),
    Index("ix_uw_analyze_snap_time_bias", "snapshot_at", "bias"),
)

uw_flow_events = Table(
    "uw_flow_events",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("flow_event_key", Text, nullable=False, unique=True),
    Column("ticker", Text, nullable=False),
    Column("side", Text),
    Column("strike", Numeric(12, 2)),
    Column("expiry", Date),
    Column("detected_at", TIMESTAMP(timezone=True), nullable=False),
    Column("initial", JSONB, nullable=False),
    Column("daily_track", JSONB),
    Column("status", Text, nullable=False),
    Column("anomaly_reason", Text),
    Column("closed_at", TIMESTAMP(timezone=True)),
    Column("initial_premium_usd", Numeric(14, 2), Computed("(initial->>'premium_usd')::numeric", persisted=True)),
    Column("initial_oi", Integer, Computed("(initial->>'oi')::numeric::int", persisted=True)),
    Column("initial_volume", Integer, Computed("(initial->>'volume')::numeric::int", persisted=True)),
    Column("initial_mid", Numeric(10, 4), Computed("(initial->>'mid')::numeric", persisted=True)),
    Column(
        "initial_underlying_price", Numeric(12, 4), Computed("(initial->>'underlying_price')::numeric", persisted=True)
    ),
)

uw_analyze_flow_alerts = Table(
    "uw_analyze_flow_alerts",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "snapshot_id",
        BigInteger,
        ForeignKey(f"{XENON_SCHEMA}.uw_analyze_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("ordinal", Integer, nullable=False),
    Column("ticker", Text, nullable=False),
    Column("snapshot_at", TIMESTAMP(timezone=True), nullable=False),
    Column("alert_type", Text),
    Column("alert_severity", Text),
    Column("alert_payload", JSONB, nullable=False),
    UniqueConstraint("snapshot_id", "ordinal", name="uq_uw_flow_alerts_snapshot_ordinal"),
    Index("ix_uw_flow_alerts_snapshot", "snapshot_id"),
    Index("ix_uw_flow_alerts_ticker_time", "ticker", "snapshot_at"),
)

uw_analyze_gex_strikes = Table(
    "uw_analyze_gex_strikes",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "snapshot_id",
        BigInteger,
        ForeignKey(f"{XENON_SCHEMA}.uw_analyze_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("ticker", Text, nullable=False),
    Column("snapshot_at", TIMESTAMP(timezone=True), nullable=False),
    Column("strike", Numeric(12, 4), nullable=False),
    Column("call_gamma", Numeric(14, 4)),
    Column("put_gamma", Numeric(14, 4)),
    Column("net_gamma", Numeric(14, 4)),
    Column("distance_pct", Numeric(10, 6)),
    Column("is_call_wall", Boolean),
    Column("is_put_wall", Boolean),
    UniqueConstraint("snapshot_id", "strike", name="uq_uw_gex_strikes_snapshot_strike"),
    Index("ix_uw_gex_strikes_snapshot", "snapshot_id"),
    Index("ix_uw_gex_strikes_ticker_time", "ticker", "snapshot_at"),
)

uw_analyze_short_volume_trend = Table(
    "uw_analyze_short_volume_trend",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "snapshot_id",
        BigInteger,
        ForeignKey(f"{XENON_SCHEMA}.uw_analyze_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("ticker", Text, nullable=False),
    Column("snapshot_at", TIMESTAMP(timezone=True), nullable=False),
    Column("position_in_trend", Integer, nullable=False),
    Column("ratio", Numeric(8, 6)),
    UniqueConstraint("snapshot_id", "position_in_trend", name="uq_uw_short_vol_snapshot_pos"),
    Index("ix_uw_short_vol_snapshot", "snapshot_id"),
)

uw_flow_event_ticks = Table(
    "uw_flow_event_ticks",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("event_id", BigInteger, ForeignKey(f"{XENON_SCHEMA}.uw_flow_events.id", ondelete="CASCADE"), nullable=False),
    Column("flow_event_key", Text, nullable=False),
    Column("observed_at", TIMESTAMP(timezone=True), nullable=False),
    Column("track_date", Date),
    Column("oi", Integer),
    Column("volume", Integer),
    Column("mid", Numeric(10, 4)),
    Column("underlying_price", Numeric(12, 4)),
    Column("pct_change_premium", Numeric(10, 6)),
    Column("tick_payload", JSONB, nullable=False),
    UniqueConstraint("event_id", "observed_at", name="uq_uw_flow_event_ticks"),
    Index("ix_uw_flow_event_ticks_event_time", "event_id", "observed_at"),
    Index("ix_uw_flow_event_ticks_observed_at", "observed_at"),
)

uw_api_stats = Table(
    "uw_api_stats",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("bucket_hour", TIMESTAMP(timezone=True), nullable=False, unique=True),
    Column("requests", Integer, server_default=text("0")),
    Column("cache_hits", Integer, server_default=text("0")),
    Column("latency_sum", Numeric(10, 2), server_default=text("0")),
    Column("latency_count", Integer, server_default=text("0")),
    Column("status_2xx", Integer, server_default=text("0")),
    Column("status_4xx", Integer, server_default=text("0")),
    Column("status_5xx", Integer, server_default=text("0")),
)

# ---------- Caches ----------

ticker_cache = Table(
    "ticker_cache",
    xenon_metadata,
    Column("ticker", Text, nullable=False, primary_key=True),
    Column("cache_type", Text, nullable=False, primary_key=True),
    Column("data", JSONB, nullable=False),
    Column("expires_at", TIMESTAMP(timezone=True)),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
)

# ---------- Shared Event Bus ----------

outbox = Table(
    "outbox",
    events_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("channel", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("emitted_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("consumed_by", JSONB, server_default=text("'[]'::jsonb")),
    CheckConstraint("length(channel) <= 63", name="ck_outbox_channel_length"),
    Index("ix_outbox_channel_time", "channel", "emitted_at"),
)
