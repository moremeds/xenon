from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
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
    Column("metadata", JSONB),
    Column("broker", Text, nullable=False, server_default=text("'IB'")),
    Column("account_env", Text, nullable=False, server_default=text("'legacy_unknown'")),
    Column("broker_account", Text, nullable=False, server_default=text("'legacy_unknown'")),
    CheckConstraint("broker = 'IB'", name="ck_trades_broker_ib_only"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_trades_account_env",
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
    CheckConstraint("broker IN ('IB', 'FUTU')", name="ck_nav_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_nav_account_env",
    ),
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
)

# ---------- UW Analysis ----------

uw_analyze_snapshots = Table(
    "uw_analyze_snapshots",
    xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ticker", Text, nullable=False),
    Column("vrp_state", JSONB),
    Column("regime", JSONB),
    Column("flow_signals", JSONB),
    Column("portfolio_score", Numeric(6, 2)),
    Column("snapshot_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Index("ix_uw_analyze_snap_ticker_time", "ticker", "snapshot_at"),
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
