from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
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
    Column("currency", Text, nullable=False, server_default=text("'USD'")),
    Column("exchange", Text),
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
    Column("quantity", Numeric(20, 8), nullable=False),
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
    Column("futu_close_id", Text),  # set only for decision='FUTU_AUTO_IMPORT'; NULL for IB rows
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
    Index(
        "uq_journal_futu_auto_import",
        "broker",
        "account_env",
        "broker_account",
        "futu_close_id",
        unique=True,
        postgresql_where=text("decision = 'FUTU_AUTO_IMPORT' AND futu_close_id IS NOT NULL"),
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
    # spec §12 / migration 260fabba18d6 — distinguish post-close from intraday snapshots.
    # Pass-2 E1(a) / migration 2026_06_03_nav_history_source_in_pk: `source` is part
    # of the PK so intraday + close rows for the same scope+date coexist as separate
    # audit rows (nav_history IS the audit table).
    Column(
        "source",
        Text,
        primary_key=True,
        nullable=False,
        server_default=text("'intraday'"),
    ),
    CheckConstraint("broker IN ('IB', 'FUTU')", name="ck_nav_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_nav_account_env",
    ),
    CheckConstraint("source IN ('close', 'intraday')", name="ck_nav_history_source"),
    # spec Decisions §13 + Pass-2 E1(a) — atomic dual-curve protection per source.
    # Excludes account_env so two rows with different envs cannot coexist for
    # the same (broker, broker_account, date, source). Includes source so
    # intraday and close coexist for the same scope+date.
    Index(
        "nav_history_one_env_per_day_per_source",
        "broker",
        "broker_account",
        "date",
        "source",
        unique=True,
    ),
)

# ---------- Futu history (read-only persistence) ----------
# Source for the backward NAV walk. Trades + cashflows pulled from Futu
# OpenD and written here once; downstream backfill reads from here so we
# can re-derive nav_history without re-pulling from Futu.
#
# v1 scope: US stocks only, USD-only cashflows. Non-US / non-USD rows are
# filtered at the sync writer and never persisted. Filter is enforced by
# the CHECK constraints below — corrupt rows cannot be silently inserted.

futu_trades = Table(
    "futu_trades",
    xenon_metadata,
    Column("broker", Text, primary_key=True),
    Column("account_env", Text, primary_key=True),
    Column("broker_account", Text, primary_key=True),
    Column("futu_deal_id", Text, primary_key=True),
    Column("futu_order_id", Text),
    Column("ticker", Text, nullable=False),  # e.g. "AAPL"
    Column("futu_code", Text, nullable=False),  # e.g. "US.AAPL"
    Column("market", Text, nullable=False),  # 'US' only for v1
    Column("action", Text, nullable=False),  # 'BUY' or 'SELL'
    Column("quantity", Numeric(20, 8), nullable=False),
    Column("price", Numeric(14, 4), nullable=False),
    Column("fees", Numeric(14, 4), nullable=False, server_default=text("0")),
    Column("filled_at", TIMESTAMP(timezone=True), nullable=False),
    Column("raw", JSONB, nullable=False),
    Column("ingested_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    CheckConstraint("broker = 'FUTU'", name="ck_futu_trades_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim')",
        name="ck_futu_trades_account_env",
    ),
    CheckConstraint("market = 'US'", name="ck_futu_trades_market_us_only"),
    CheckConstraint("action IN ('BUY', 'SELL')", name="ck_futu_trades_action"),
    Index(
        "ix_futu_trades_scope_filled_at",
        "broker",
        "account_env",
        "broker_account",
        "filled_at",
    ),
)

futu_cash_flow = Table(
    "futu_cash_flow",
    xenon_metadata,
    Column("broker", Text, primary_key=True),
    Column("account_env", Text, primary_key=True),
    Column("broker_account", Text, primary_key=True),
    Column("futu_flow_id", Text, primary_key=True),
    Column("cashflow_type", Text, nullable=False),  # 'DEPOSIT' | 'WITHDRAW' | 'TRANSFER_IN' | 'TRANSFER_OUT'
    Column("amount", Numeric(14, 4), nullable=False),  # signed: +inflow, -outflow
    Column("currency", Text, nullable=False),  # 'USD' only for v1
    Column("occurred_at", TIMESTAMP(timezone=True), nullable=False),
    Column("raw", JSONB, nullable=False),
    Column("ingested_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    CheckConstraint("broker = 'FUTU'", name="ck_futu_cash_flow_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim')",
        name="ck_futu_cash_flow_account_env",
    ),
    CheckConstraint("currency = 'USD'", name="ck_futu_cash_flow_currency_usd_only"),
    # No CHECK on cashflow_type — Futu returns a long open enum
    # (Cash Dividend, Fund Subscription, IPO Subscription, Others, ...).
    # M5 walk decides which raw types move NAV externally.
    Index(
        "ix_futu_cash_flow_scope_occurred_at",
        "broker",
        "account_env",
        "broker_account",
        "occurred_at",
    ),
)


# Futu orders — open/working AND historical orders pulled from OpenD
# (order_list_query + history_order_list_query). UPSERT keyed on
# (scope, futu_order_id) so a resting order's status/fills re-stamp in place.
# Shaped at the API into the unified IB OpenOrder contract. Read-only: xenon
# never places Futu orders. `order_type`/`status` keep the raw Futu enum label
# (NORMAL/MARKET/STOP/..., SUBMITTED/FILLED_ALL/...); the API maps for display.
futu_orders = Table(
    "futu_orders",
    xenon_metadata,
    Column("broker", Text, primary_key=True),
    Column("account_env", Text, primary_key=True),
    Column("broker_account", Text, primary_key=True),
    Column("futu_order_id", Text, primary_key=True),
    Column("ticker", Text, nullable=False),
    Column("futu_code", Text, nullable=False),
    Column("market", Text, nullable=False),
    Column("action", Text, nullable=False),  # 'BUY' | 'SELL' (normalized)
    Column("order_type", Text, nullable=False),  # raw Futu OrderType label
    Column("quantity", Numeric(20, 8), nullable=False),
    Column("limit_price", Numeric(14, 4)),
    Column("aux_price", Numeric(14, 4)),  # stop/trigger price
    Column("status", Text, nullable=False),  # raw Futu OrderStatus label
    Column("tif", Text, nullable=False, server_default=text("'DAY'")),
    Column("filled_qty", Numeric(20, 8), nullable=False, server_default=text("0")),
    Column("avg_fill_price", Numeric(14, 4)),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False),
    Column("raw", JSONB, nullable=False),
    Column("ingested_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    CheckConstraint("broker = 'FUTU'", name="ck_futu_orders_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim')",
        name="ck_futu_orders_account_env",
    ),
    CheckConstraint("action IN ('BUY', 'SELL')", name="ck_futu_orders_action"),
    Index(
        "ix_futu_orders_scope_updated_at",
        "broker",
        "account_env",
        "broker_account",
        "updated_at",
    ),
    Index(
        "ix_futu_orders_scope_status",
        "broker",
        "account_env",
        "broker_account",
        "status",
    ),
)


# Per-order fee snapshot from OpenD order_fee_query (order_id, fee_amount, fee_details).
futu_order_fees = Table(
    "futu_order_fees",
    xenon_metadata,
    Column("broker", Text, primary_key=True),
    Column("account_env", Text, primary_key=True),
    Column("broker_account", Text, primary_key=True),
    Column("futu_order_id", Text, primary_key=True),
    Column("total_fee", Numeric(14, 4), nullable=False, server_default=text("0")),
    Column("currency", Text, nullable=False, server_default=text("'USD'")),
    Column("raw", JSONB, nullable=False),
    Column("ingested_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    CheckConstraint("broker = 'FUTU'", name="ck_futu_order_fees_broker"),
)


# Futu closed trades — FIFO-reconstructed closed lots from futu_trades.
# Feeds both the 30-day HISTORICAL TRADES table and FUTU_AUTO_IMPORT journal
# rows. The `trades` table is IB-locked (ck_trades_broker_ib_only) and IB-shaped
# (submission_id FK), so Futu closed trades get their own table. `futu_close_id`
# is the stable synthetic key `close_deal_id:open_deal_id` (see
# xenon.api.services.futu_closed_trades.match_closed_lots).
futu_closed_trades = Table(
    "futu_closed_trades",
    xenon_metadata,
    Column("broker", Text, primary_key=True),
    Column("account_env", Text, primary_key=True),
    Column("broker_account", Text, primary_key=True),
    Column("futu_close_id", Text, primary_key=True),
    Column("ticker", Text, nullable=False),
    Column("futu_code", Text, nullable=False),
    Column("structure", Text),
    Column("action", Text, nullable=False),  # closing side: 'SELL' (was long) | 'BUY' (was short)
    Column("quantity", Numeric(20, 8), nullable=False),
    Column("entry_cost", Numeric(14, 4)),
    Column("exit_cost", Numeric(14, 4)),
    Column("realized_pnl", Numeric(14, 2), nullable=False),
    Column("cost_basis", Numeric(14, 4), nullable=False),
    Column("proceeds", Numeric(14, 4), nullable=False),
    Column("opened_at", TIMESTAMP(timezone=True)),
    Column("closed_at", TIMESTAMP(timezone=True), nullable=False),
    Column("metadata", JSONB),
    Column("ingested_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    CheckConstraint("broker = 'FUTU'", name="ck_futu_closed_trades_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim')",
        name="ck_futu_closed_trades_account_env",
    ),
    Index(
        "ix_futu_closed_scope_closed_at",
        "broker",
        "account_env",
        "broker_account",
        "closed_at",
    ),
)


# IB CashTransactions ingested from IB Flex Web Service (Deposits/Withdrawals
# section of the "account" saved query). Mirrors xenon.futu_cash_flow for the
# IB broker. Used by performance.py to flow-adjust the IB headline return so
# deposits aren't counted as investment performance.
#
# Source of truth is IB Flex `CashTransaction` rows. The Type "Deposits/Withdrawals"
# is the only external category we ingest; everything else (dividends, fees,
# interest) is internal investment activity and stays out.
#
# Amount sign convention matches Futu: positive = money INTO account (deposit),
# negative = money OUT (withdrawal). Native amounts are stored alongside USD-
# equivalent so we can audit FX conversion.
ib_cash_flow = Table(
    "ib_cash_flow",
    xenon_metadata,
    Column("broker", Text, primary_key=True),
    Column("account_env", Text, primary_key=True),
    Column("broker_account", Text, primary_key=True),
    Column("transaction_id", Text, primary_key=True),  # IB's TransactionID
    Column("txn_type", Text, nullable=False),  # IB's Type field, e.g. "Deposits/Withdrawals"
    Column("description", Text, nullable=True),  # IB's Description, e.g. "CASH RECEIPTS / ELECTRONIC FUND TRANSFERS"
    Column("amount_native", Numeric(14, 4), nullable=False),  # signed amount in native currency
    Column("currency", Text, nullable=False),  # 'USD', 'HKD', etc.
    Column("amount_usd", Numeric(14, 4), nullable=False),  # signed USD-equivalent
    Column("fx_rate", Numeric(14, 6), nullable=False),  # native→USD rate applied (1.0 for USD)
    Column("occurred_at", TIMESTAMP(timezone=True), nullable=False),
    Column("raw", JSONB, nullable=False),
    Column("ingested_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    CheckConstraint("broker = 'IB'", name="ck_ib_cash_flow_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim')",
        name="ck_ib_cash_flow_account_env",
    ),
    Index(
        "ix_ib_cash_flow_scope_occurred_at",
        "broker",
        "account_env",
        "broker_account",
        "occurred_at",
    ),
)


# Futu daily statement — official daily NAV / Portfolio / Cash / Funds from
# the password-protected PDF emailed by Futu HK. Pulled via Outlook IMAP +
# pikepdf decrypt + pdfplumber text extraction; persisted here.
#
# Why we need this: futu_nav_history is synthetic (flat-line MTM between
# trades). The Futu app's Performance view uses comprehensive income which
# includes daily mark-to-market on open positions — the statement gives us
# that NAV series directly per trading day in HKD + per-currency breakdown.
#
# raw_pdf stores the original encrypted PDF bytes so the source-of-truth is
# preserved; re-decryption requires FUTU_STATEMENT_PASSWORD (env-only).
futu_daily_statement = Table(
    "futu_daily_statement",
    xenon_metadata,
    Column("broker", Text, primary_key=True),
    Column("account_env", Text, primary_key=True),
    Column("broker_account", Text, primary_key=True),
    Column("statement_date", Date, primary_key=True),
    Column("preparation_date", Date, nullable=False),
    Column("account_number", Text, nullable=False),
    Column("account_suffix", Text, nullable=False),
    Column("client_name", Text, nullable=False),
    Column("base_currency", Text, nullable=False),
    # Portfolio summary in base currency (HKD for the 5668 account)
    Column("starting_portfolio_base", Numeric(20, 4), nullable=False),
    Column("ending_portfolio_base", Numeric(20, 4), nullable=False),
    Column("starting_funds_base", Numeric(20, 4), nullable=False),
    Column("ending_funds_base", Numeric(20, 4), nullable=False),
    Column("starting_cash_base", Numeric(20, 4), nullable=False),
    Column("ending_cash_base", Numeric(20, 4), nullable=False),
    Column("starting_nav_base", Numeric(20, 4), nullable=False),
    Column("ending_nav_base", Numeric(20, 4), nullable=False),
    # Per-currency NAV breakdown — JSONB so we can add currencies later
    # without a migration. Example: {"HKD": 25264.93, "USD": 222269.38, ...}
    Column("starting_nav_by_currency", JSONB, nullable=False),
    Column("ending_nav_by_currency", JSONB, nullable=False),
    # Reference rates the statement used. JSONB so format is flexible.
    # Example: {"USD/HKD": 7.836615, "CNH/HKD": 1.158600, ...}
    Column("exchange_rates", JSONB, nullable=False),
    # Plain-text per page (JSONB array) — saved alongside the encrypted raw_pdf
    # so downstream queries / future parsers don't need to re-decrypt.
    Column("page_text", JSONB, nullable=True),
    # Page 9: financing overview (margin interest + securities lending interest).
    # Example: {"margin_interest": [{"date": "...", "currency": "USD", "amount": 7.17,
    # "rate": 0.048, "principal": 53766.52, "cumulative": 113.85}],
    # "securities_lending": [{"date": "...", "symbol": "LITX", ...}]}
    Column("financing", JSONB, nullable=True),
    # Page 8: daily transaction totals by currency.
    # Example: {"USD": {"transaction_amount": 34271.89, "commission": 24.40,
    # "platform_fees": 9.39, "change_in_amount": 1052.16, ...}}
    Column("transaction_totals", JSONB, nullable=True),
    # Source PDF for reproducibility — encrypted bytes, re-decryptable with env password
    Column("raw_pdf", LargeBinary, nullable=True),
    Column("source_uid", Text, nullable=True),  # IMAP UID of the email
    Column("source_subject", Text, nullable=True),
    Column("ingested_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    CheckConstraint("broker = 'FUTU'", name="ck_futu_daily_statement_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim')",
        name="ck_futu_daily_statement_account_env",
    ),
    Index(
        "ix_futu_daily_statement_scope_date",
        "broker",
        "account_env",
        "broker_account",
        "statement_date",
    ),
)


# Catch-all for raw statement PDFs we couldn't parse — keyed on IMAP UID so
# re-running the sync is idempotent. When the parser learns an older layout,
# rows here can be drained into `futu_daily_statement` offline.
futu_statement_inbox = Table(
    "futu_statement_inbox",
    xenon_metadata,
    Column("broker", Text, primary_key=True),
    Column("account_env", Text, primary_key=True),
    Column("broker_account", Text, primary_key=True),
    Column("source_uid", Text, primary_key=True),
    Column("subject", Text, nullable=True),
    Column("sender", Text, nullable=True),
    Column("received_at", TIMESTAMP(timezone=True), nullable=True),
    Column("attachment_name", Text, nullable=True),
    Column("raw_pdf", LargeBinary, nullable=False),
    Column("parse_error", Text, nullable=True),
    Column("ingested_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    CheckConstraint("broker = 'FUTU'", name="ck_futu_statement_inbox_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim')",
        name="ck_futu_statement_inbox_account_env",
    ),
    Index(
        "ix_futu_statement_inbox_scope_received",
        "broker",
        "account_env",
        "broker_account",
        "received_at",
    ),
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
    # Durable reservation payload: effective legs (actions, ratios, conIds,
    # multiplier, guarantee mode, original/replacement qty) for Gate-4
    # coverage. NULL for pre-PR-03a rows and snapshot-imported orders.
    Column("legs", JSONB),
    # The broker's own order id as a string (Futu order ids); NULL for IB
    # rows, which use ib_order_id / perm_id. Unique per scope when set.
    Column("broker_order_id", Text),
    CheckConstraint("broker IN ('IB','FUTU')", name="ck_order_sub_broker"),
    CheckConstraint(
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        name="ck_order_sub_account_env",
    ),
    # ck_order_sub_state (state IN orders_store.ORDER_STATES) lives in
    # migration 2026_10_18_order_state_check; it is not declared here
    # because schema.py cannot import orders_store (import cycle).
    # EXPAND state (lead decision, spec expand/contract): the legacy
    # identity-scoped key stays so the previous release's writer keeps
    # working; the account-scoped key runs alongside and dedupes the same
    # (scope, client_attempt_id) across caller identities. The contract
    # migration (drop of the old key) is a later PR after rollout.
    UniqueConstraint(
        "broker",
        "account_env",
        "broker_account",
        "user_id",
        "client_attempt_id",
        name="uq_order_sub_user_attempt",
    ),
    UniqueConstraint(
        "broker",
        "account_env",
        "broker_account",
        "client_attempt_id",
        name="uq_order_sub_scope_attempt",
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
    Index(
        "uq_order_sub_broker_order_id",
        "broker",
        "account_env",
        "broker_account",
        "broker_order_id",
        unique=True,
        postgresql_where=text("broker_order_id IS NOT NULL"),
    ),
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
    Column("qty", Numeric(20, 8), nullable=False),
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

# Operator console — background-writer heartbeats (Tier B). Single row per
# (service, AccountScope). Scope columns are REQUIRED by the repo rule: the
# nightly core_dev→core_test refresh copies live (prod) heartbeats into the dev
# DB where paper-session heartbeats share the same `service` key — without scope
# they'd collide. Composite PK keeps live vs paper rows distinct.
service_health = Table(
    "service_health",
    xenon_metadata,
    Column("service", Text, nullable=False),
    Column("broker", Text, nullable=False),
    Column("account_env", Text, nullable=False),
    Column("broker_account", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("detail", Text),
    Column("last_error", Text),
    Column("last_started_at", TIMESTAMP(timezone=True)),
    Column("last_finished_at", TIMESTAMP(timezone=True)),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("service", "broker", "account_env", "broker_account", name="pk_service_health"),
)

# ---------- Runtime settings ----------

# DB overrides for runtime knobs (registry: xenon.runtime_settings). No row =
# the env/default value. Process config, not an execution row: no AccountScope
# columns (safety keys are narrow-only, so a row copied by the nightly
# core_dev -> core_test refresh can only restrict, never widen).
runtime_settings = Table(
    "runtime_settings",
    xenon_metadata,
    Column("key", Text, primary_key=True),
    Column("value", JSONB, nullable=False),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("updated_by", Text),
    Column("note", Text),
)

# Append-only change log; one row per PUT / DELETE, same transaction.
runtime_settings_audit = Table(
    "runtime_settings_audit",
    xenon_metadata,
    Column("id", BigInteger, Identity(always=False), primary_key=True),
    Column("key", Text, nullable=False),
    Column("old_value", JSONB),
    Column("new_value", JSONB),
    Column("changed_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("changed_by", Text),
    Column("note", Text),
    Index("ix_runtime_settings_audit_key_changed", "key", "changed_at"),
)

# ---------- Watchlist ----------

user_watchlist = Table(
    "user_watchlist",
    xenon_metadata,
    Column("id", Text, primary_key=True),  # uuid4 hex
    Column("user_id", Text, nullable=False),  # operator id; today always "local" (single-operator terminal)
    Column("symbol", Text, nullable=False),
    Column("sector", Text, nullable=True),
    Column("added_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    UniqueConstraint("user_id", "symbol", name="uq_user_watchlist_user_symbol"),
    Index("ix_user_watchlist_user_added", "user_id", "added_at"),
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
