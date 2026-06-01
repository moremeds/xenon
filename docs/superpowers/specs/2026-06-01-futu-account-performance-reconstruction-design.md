# Futu account performance reconstruction — design

**Date:** 2026-06-01 (revised 2026-06-01 after `/review-cycle` + scope correction)
**Status:** Design approved, pending implementation plan
**Scope target:** Full equivalence with IB blotter/journal surfaces for the Futu side (Q1 = D), achieved using **Futu-only data sources** (no IB, no external mark feeds, no kline-based historical pricing).

## Goal

Reconstruct the full performance history of the Futu trading account from
2024-05-01 forward. Surface an equity curve, per-trade history, and
performance statistics in the Futu account tab, plus a cross-broker equity
curve that shows IB and Futu side by side.

This is **observation, not authorship** — Futu remains read-only per the
existing policy in `src/xenon/CLAUDE.md`. The pipeline ingests fills,
cash flows, and snapshots; it never places or modifies orders.

## Why

Today's `FutuClient` exposes only a current positions snapshot and account
info. There is no historical record, no equity curve, no realized P&L
attribution. To evaluate the Futu account's actual performance — and to
compare it against the IB book in xenon's existing surfaces — we need a
durable trade and NAV history in Postgres.

## Non-goals

- Trading on Futu. Futu stays read-only.
- HK/CN-market trades. US-listed names only at ingest.
- Intra-day equity curve. EOD granularity is sufficient.
- Multi-currency NAV display. USD-only, FX-converted at ingest.
- Real-time fill ingestion. Daily/on-demand sync is enough.
- Options support. The Futu account is stock-only in scope.
- **Mark-to-market historical NAV on open positions.** The historical
  curve is **cost-basis equity** (cash + Σ qty × avg_cost). MTM gain on
  positions still open today is captured only from deployment date
  forward via daily reported NAV snapshots. Closed-trade realized P&L is
  captured fully across the entire history from fill matching.
- **IB dependency.** This feature MUST NOT touch IB Gateway, the IB
  client, or any IB historical-data endpoint. All inputs come from Futu
  OpenD plus FRED for FX.
- **External price feeds.** No FMP, no Polygon, no Yahoo, no kline
  pricing. We never need a historical price for a US stock under this
  design.

## Decisions locked in via brainstorm

| #   | Question                                   | Decision                                                                                                                                                                                                                                                                                                          |
| --- | ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Q1  | Scope of reconstruction                    | **D** — Full equivalence with IB blotter/journal surfaces                                                                                                                                                                                                                                                         |
| Q2  | NAV reconstruction method                  | **C** — Snapshot forward + anchor-and-walk-backward backfill. **Revised post-review:** the anchor is `cost_basis_NAV_today` and the walk back uses cost-basis valuation for open positions (no external marks).                                                                                                   |
| Q3  | Currency handling                          | **A** — USD-only NAV with daily FX series; ingest filtered to US-market trades                                                                                                                                                                                                                                    |
| Q4  | Schema collision (Futu trades)             | **B** — Separate `xenon.futu_trades` table                                                                                                                                                                                                                                                                        |
| Q5  | Refresh cadence + location                 | **D** — Daily launchd + on-demand sync, both calling one idempotent function. Runtime-location-agnostic; OpenD co-located with FastAPI per environment.                                                                                                                                                           |
| Q6  | Reconciliation policy                      | **B** — Reported NAV wins; `source` column on `nav_history` distinguishes; reconciliation log captures delta. Threshold `max($25, 0.1%)`.                                                                                                                                                                         |
| Q7  | Frontend surface                           | **D** — Futu tab gets equity + trade history + perf stats; new cross-broker equity curve renders IB and Futu lines with optional combined toggle (off by default).                                                                                                                                                |
| Q8  | Mark source for backfill (post-review)     | **Futu-only, cost-basis** — Futu has no historical NAV endpoint and IB is out of scope. We anchor at today's `cost_basis_NAV = cash + Σ(qty × avg_cost)` (= reported_NAV − unrealized_pl) and walk backwards through trades + cash flows + corporate actions sourced from Futu's `get_rehab`. No external prices. |
| Q9  | Historical NAV row lifecycle (post-review) | **Lock-in once.** The backfill walks historical dates once and writes `source='reconstructed'` rows that are then **immutable**. Forward operation appends daily `source='reported'` rows. `--rematch` is the only operator-explicit lever that can recompute historical rows.                                    |

## Deployment model

OpenD is co-located with FastAPI per environment. No cross-machine Futu
reachability is required and no Tailscale dependency for this feature.

| Phase                            | FastAPI runs on | OpenD runs on | launchd on | Postgres DB |
| -------------------------------- | --------------- | ------------- | ---------- | ----------- |
| Phase 1 (now)                    | this Mac        | this Mac      | this Mac   | `core_dev`  |
| Phase 2 (post-FastAPI migration) | Mac mini        | Mac mini      | Mac mini   | `core`      |

The sync code is identical across phases. Only the launchd plist deployment
target and the `FUTU_OPEND_HOST` / `FUTU_OPEND_PORT` env vars change.

Dev OpenD may point at the live Futu account or a paper/sim account; the
`(broker, account_env, broker_account)` scope value distinguishes them and
the schema is invariant.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  FutuClient  (existing — positions + accinfo snapshot)           │
│  FutuHistoryFetcher  (new — wraps history_deal_list_query,        │
│      history_order_list_query, get_acc_cash_flow, get_rehab       │
│      with paging, rate-limit cooldown, US-market filter)          │
└──────────────────────────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────────┐
│  futu_history_sync  (new — idempotent sync function)              │
│    - pull deltas of trades, cash flows, corp actions              │
│    - dedup via futu_deal_id / futu_flow_id / (ticker, ex_date)    │
│    - write futu_trades, futu_cash_flow, corporate_actions rows    │
│    - run FIFO matcher to populate realized_pnl                    │
│    - snapshot today's accinfo → account_snapshots                 │
└──────────────────────────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────────┐
│  futu_nav_walk  (new — backward walk, NOT forward mark-based)     │
│    Initial backfill (runs once):                                  │
│      anchor = (cost_basis_NAV_today, cash_today, positions_today) │
│      for D in [today-1, today-2, ..., 2024-05-01]:                │
│          un-apply D+1's fills, cash flows, corp actions           │
│          nav[D] = cash[D] + Σ(qty[D] × avg_cost[D])               │
│          INSERT nav_history (..., source='reconstructed')         │
│    Daily forward:                                                 │
│      INSERT today's reported nav from accinfo (source='reported') │
│      Historical rows are IMMUTABLE — never recomputed unless      │
│      `--rematch` is explicitly invoked                            │
└──────────────────────────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────────┐
│  Postgres tables                                                 │
│    new: futu_trades, futu_cash_flow, futu_reconciliation_log,    │
│         fx_daily, corporate_actions                              │
│    altered: nav_history (+ source column)                        │
│    existing reuse: account_snapshots, positions (already         │
│         scope-aware for FUTU)                                    │
│    NOT in scope: us_eod_bars (DROPPED — no marks needed)         │
└──────────────────────────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────────┐
│  FastAPI routes                                                  │
│    new: /futu/history/sync, /futu/equity-curve, /futu/trades,    │
│         /futu/performance, /futu/reconciliation, /equity-curve   │
│    existing: /futu/portfolio unchanged                           │
└──────────────────────────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────────┐
│  Frontend                                                        │
│    Futu tab: FutuEquityCurve, FutuTradeHistoryTable,             │
│              FutuPerformanceStats                                │
│    Cross-broker: EquityCurveCrossBroker (IB + Futu lines,        │
│                  optional combined toggle)                       │
└──────────────────────────────────────────────────────────────────┘
```

## Schema

All new tables are in the `xenon` schema. All carry the standard scope
columns `(broker, account_env, broker_account)` per the production database
strategy in `docs/architecture/production-database-strategy.md`.

### `xenon.futu_trades` (new)

Raw fill records. One row per Futu deal. Idempotent on `futu_deal_id`.

```sql
CREATE TABLE xenon.futu_trades (
  id              BIGSERIAL PRIMARY KEY,
  futu_deal_id    TEXT      NOT NULL,
  futu_order_id   TEXT      NOT NULL,
  ticker          TEXT      NOT NULL,         -- normalized via futu_to_ib()
  futu_code       TEXT      NOT NULL,         -- raw "US.AAPL" preserved
  market          TEXT      NOT NULL,         -- 'US' only (filter at ingest)
  action          TEXT      NOT NULL,         -- 'BUY' | 'SELL'
  quantity        INTEGER   NOT NULL,
  price           NUMERIC(12,4) NOT NULL,     -- per-share USD
  fees            NUMERIC(10,4) NOT NULL DEFAULT 0,  -- commission + platform fee, USD.
                                                     -- 0 means "no fees on this fill";
                                                     -- NULL would be ambiguous so we forbid it.
  realized_pnl    NUMERIC(12,2),              -- NULL for opening fills;
                                              -- net of fees on closing fills
  filled_at       TIMESTAMPTZ NOT NULL,
  raw             JSONB     NOT NULL,
  broker          TEXT      NOT NULL DEFAULT 'FUTU',
  account_env     TEXT      NOT NULL,
  broker_account  TEXT      NOT NULL,
  CHECK (broker = 'FUTU'),
  CHECK (account_env IN ('paper','live','sim','legacy_unknown')),
  CHECK (market = 'US'),
  UNIQUE (broker, account_env, broker_account, futu_deal_id)
);
CREATE INDEX ix_futu_trades_scope_filled
  ON xenon.futu_trades (broker, account_env, broker_account, filled_at);
CREATE INDEX ix_futu_trades_ticker_filled
  ON xenon.futu_trades (ticker, filled_at);
```

`xenon.trades.ck_trades_broker_ib_only` stays as-is. Futu rows live in
`futu_trades`; cross-broker readers use the `xenon.all_trades` view.

### `xenon.futu_cash_flow` (new)

Cash flow ledger. Deposits/withdrawals do not affect P&L; dividends,
interest, and standalone fees do.

```sql
CREATE TABLE xenon.futu_cash_flow (
  id                BIGSERIAL PRIMARY KEY,
  futu_flow_id      TEXT      NOT NULL,
  flow_type         TEXT      NOT NULL,     -- normalized taxonomy below
  futu_type_raw     TEXT      NOT NULL,     -- Futu's native enum, preserved
  amount            NUMERIC(12,2),          -- signed, USD-converted.
                                            -- NULLABLE on purpose: missing-FX
                                            -- rows are written with amount=NULL
                                            -- + amount_original populated, so
                                            -- the failure mode in "FX
                                            -- conversion" is storable rather
                                            -- than crashing the migration.
  amount_original   NUMERIC(12,2) NOT NULL, -- signed, in original currency
  original_currency TEXT      NOT NULL,
  fx_rate_used      NUMERIC(12,6),          -- NULL when currency='USD' or FX missing
  ticker            TEXT,                   -- non-null for dividends
  description       TEXT,                   -- Futu's remark field
  flow_at           TIMESTAMPTZ NOT NULL,
  raw               JSONB     NOT NULL,
  broker            TEXT      NOT NULL DEFAULT 'FUTU',
  account_env       TEXT      NOT NULL,
  broker_account    TEXT      NOT NULL,
  CHECK (broker = 'FUTU'),
  CHECK (account_env IN ('paper','live','sim','legacy_unknown')),
  CHECK (flow_type IN ('deposit','withdraw','dividend','interest','fee','other')),
  UNIQUE (broker, account_env, broker_account, futu_flow_id)
);
CREATE INDEX ix_futu_cash_flow_scope_at
  ON xenon.futu_cash_flow (broker, account_env, broker_account, flow_at);
```

**Default taxonomy mapping** (raw Futu enum names verified from SDK at
implementation time; mapping may need touch-up):

| Futu native enum (representative) | `flow_type`           | Affects P&L?                    |
| --------------------------------- | --------------------- | ------------------------------- |
| `FUND_IN`, `FUND_OUT`             | `deposit`, `withdraw` | No                              |
| `DIVIDEND`                        | `dividend`            | Yes                             |
| `INTEREST`                        | `interest`            | Yes                             |
| `COMMISSION`, `PLATFORM_FEE`      | `fee`                 | Yes (standalone only)           |
| anything else                     | `other`               | Conservative — visible in audit |

`futu_trades.fees` is the source of truth for per-fill commissions.
`futu_cash_flow` rows of `flow_type='fee'` cover only fees not tied to a
fill (e.g., account maintenance, ADR pass-through). At ingest, fee events
that reference a known `futu_deal_id` are dropped to prevent double-counting.

### `xenon.futu_reconciliation_log` (new)

Daily append-only log of reported-vs-reconstructed NAV deltas.

Two kinds of rows: per-date daily reconciliation, and append-only anomalies
(missing marks, FIFO over-close warnings, late-arriving fills, etc.). Daily
rows are unique per `(scope, recon_date)`; anomalies are not.

```sql
CREATE TABLE xenon.futu_reconciliation_log (
  id                BIGSERIAL PRIMARY KEY,
  kind              TEXT      NOT NULL,         -- 'daily' | 'anomaly'
  recon_date        DATE      NOT NULL,
  reported_nav      NUMERIC(14,2),              -- NULL for anomaly rows that
                                                 -- aren't tied to a reported NAV
  reconstructed_nav NUMERIC(14,2),              -- NULL when no reconstruction was run
  delta             NUMERIC(14,2),              -- NULL when either side is NULL
  threshold         NUMERIC(14,2),
  exceeded          BOOLEAN,
  notes             JSONB,                       -- {anomaly_type, ticker, …}
  computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  broker            TEXT      NOT NULL DEFAULT 'FUTU',
  account_env       TEXT      NOT NULL,
  broker_account    TEXT      NOT NULL,
  CHECK (kind IN ('daily', 'anomaly'))
);
-- Daily rows are unique per scope+date; anomalies are not.
CREATE UNIQUE INDEX uq_futu_recon_daily
  ON xenon.futu_reconciliation_log (broker, account_env, broker_account, recon_date)
  WHERE kind = 'daily';
CREATE INDEX ix_futu_recon_kind_date
  ON xenon.futu_reconciliation_log (kind, recon_date);
```

### `xenon.nav_history` — altered

```sql
ALTER TABLE xenon.nav_history
  ADD COLUMN source TEXT NOT NULL DEFAULT 'reported'
    CHECK (source IN ('reported','reconstructed'));
```

Existing IB rows backfill to `'reported'` (they come from IB Flex). Futu
backfill rows are `'reconstructed'`; Futu daily-forward rows are `'reported'`.

**Migration ordering:** the `ALTER TABLE` must land **before** any query
code that filters by `source`. The Alembic revision that adds the column
must precede the revision that adds the `/futu/equity-curve` route's
backing query.

The deployment boundary (first `'reported'` Futu row) is inferred at
query time as
`MIN(date) FROM xenon.nav_history WHERE broker='FUTU' AND source='reported'`
rather than stored as config.

### `xenon.fx_daily` (new)

Daily FX rates, populated from FRED on first run for the full date
range. Source IDs: `DEXHKUS` (HKD per USD), `DEXCHUS` (CNY per USD).

**Direction.** The stored value is the **foreign currency per USD** —
matching FRED's native convention. Naming `currency_per_usd` reflects
this. Conversion formula:

```
amount_usd = amount_original / fx_daily.currency_per_usd
```

i.e., a HKD amount of 780 with `currency_per_usd = 7.8` yields
`780 / 7.8 = 100 USD`. Implementers must use the division form, not
multiplication.

```sql
CREATE TABLE xenon.fx_daily (
  currency           TEXT NOT NULL,
  date               DATE NOT NULL,
  currency_per_usd   NUMERIC(12,6) NOT NULL,    -- foreign-currency-per-USD;
                                                 -- divide original amount by this
  source             TEXT NOT NULL DEFAULT 'FRED',
  PRIMARY KEY (currency, date)
);
```

### `xenon.corporate_actions` (new)

Splits, reverse splits, and ticker changes for US stocks ever held in
Futu. Populated from **Futu's own `get_rehab` endpoint** (the
"restoration / adjustment" data) during the backfill and refreshed at
each daily sync. `get_rehab` returns split factors and dividend events
per ticker; we use the split factors here.

```sql
CREATE TABLE xenon.corporate_actions (
  id                 BIGSERIAL PRIMARY KEY,
  ticker             TEXT NOT NULL,    -- original ticker
  action_type        TEXT NOT NULL,    -- 'split' | 'reverse_split' | 'symbol_change'
  effective_date     DATE NOT NULL,
  ratio_numerator    NUMERIC(8,4),     -- post / pre — e.g. 10 for a 10:1 split
  ratio_denominator  NUMERIC(8,4),     -- e.g. 1 for a 10:1 split
  new_ticker         TEXT,             -- non-null for symbol_change
  source             TEXT NOT NULL DEFAULT 'FUTU_GET_REHAB',
  notes              JSONB,
  UNIQUE (ticker, action_type, effective_date)
);
CREATE INDEX ix_corp_actions_ticker_date
  ON xenon.corporate_actions (ticker, effective_date);
```

The **backward-walk NAV reconstruction** (see "NAV reconstruction"
below) applies each action **in reverse** to the running position
book as it walks from today into the past: a 10:1 split that occurred
on date D means `positions(D-1) = positions(D) / 10` and
`avg_cost(D-1) = avg_cost(D) × 10`. Symbol changes rewrite the ticker
key in the running book during reversal.

The FIFO matcher applies each action **forward** (replaying fills in
event-time order) to ensure realized P&L on a post-split SELL uses the
post-split cost basis on the matched lots.

**Out-of-scope corporate actions (documented limitation, not silent):**
spinoffs, mergers with cash + stock consideration, special dividends
that don't appear in `get_acc_cash_flow` or `get_rehab`. When the
post-deployment reconciliation delta exceeds threshold and a
corp-action event lands near that date, the reconciliation note flags
`possible_unmodeled_action`.

### `xenon.all_fills` (new view) — fill-grain, broker-agnostic

The original draft used a `xenon.all_trades` view that mixed
**fill-grain** Futu rows with **lifecycle-grain** IB rows from
`xenon.trades` (one row per logical opened-then-closed trade). That
grain mismatch silently lost IB commission detail and made
broker-agnostic readers compare apples to oranges.

Switch to a fill-grain view that joins **`xenon.order_fills`** (the
existing IB execution-grain table) on the IB side with
`xenon.futu_trades` on the Futu side. Both sides expose one row per
fill; readers that need lifecycle pairs (entry+exit) still hit
`xenon.trades` directly on the IB side. This view is for blotter-style
listings only.

```sql
CREATE VIEW xenon.all_fills AS
SELECT 'IB'::text  AS broker,
       f.account_env, f.broker_account,
       f.ticker,
       f.side         AS action,    -- order_fills.side ('BUY'/'SELL')
       f.qty          AS quantity,
       f.price,
       f.commission   AS fees,
       f.filled_at,
       NULL::numeric  AS realized_pnl,  -- IB realized P&L lives on
                                        -- xenon.trades, not order_fills;
                                        -- readers needing it join
                                        -- trades on submission_id
       NULL::jsonb    AS raw
FROM xenon.order_fills f WHERE f.broker = 'IB'
UNION ALL
SELECT 'FUTU'::text AS broker,
       t.account_env, t.broker_account,
       t.ticker,
       t.action,
       t.quantity,
       t.price,
       t.fees,
       t.filled_at,
       t.realized_pnl,
       t.raw
FROM xenon.futu_trades t;
```

Column-name mapping is from the actual `xenon.order_fills` schema
(`schema.py:272`): `side → action`, `qty → quantity`, `commission →
fees`. `xenon.order_fills` already permits `broker IN ('IB','FUTU')`
via its check constraint, but Futu rows live in `futu_trades` per
Q4; the view's `WHERE f.broker = 'IB'` keeps the boundary clean.

## Sync algorithm

`futu_history_sync(scope, until_date=today)` — idempotent. Six steps:

1. **Compute per-stream cursors.** Three independent cursor dates for
   the append-only streams (trades, cash flows, corp actions), plus a
   `backfill_complete` flag for the NAV walk:

   ```python
   cursor_deals       = MAX(filled_at::date)  FROM futu_trades       WHERE scope
   cursor_flows       = MAX(flow_at::date)    FROM futu_cash_flow    WHERE scope
   cursor_corp_action = MAX(effective_date)   FROM corporate_actions WHERE ticker IN <Futu universe>
   backfill_complete  = EXISTS(SELECT 1 FROM nav_history WHERE scope AND date = '2024-05-01')
   ```

   `start_from = min(cursor_deals, cursor_flows, cursor_corp_action) -
1 day` for streams 2/3/6, defaulting to `'2024-05-01'`. The NAV
   step (step 7) branches on `backfill_complete`: false → full
   backward walk, true → today's row only.

2. **Pull fills.** `history_deal_list_query` paginated by Futu's 90-day
   max window. Filter `market='US'` at the boundary; drop non-US rows
   entirely. `INSERT ... ON CONFLICT DO NOTHING` into `futu_trades`.
3. **Pull cash flow.** `get_acc_cash_flow` same paging. Convert
   `original_currency` → USD via the FX series (Section "FX conversion").
   `INSERT ... ON CONFLICT DO NOTHING`.

   **Fee dedup strategy** (corrected — the original "drop fees tied to a
   known deal_id at ingest" was not idempotent: if the fee arrives before
   its deal, it gets inserted then would be double-counted via
   `futu_trades.fees` once the deal appears). Instead:
   - Always insert standalone fee events into `futu_cash_flow` with
     their `futu_flow_id`. Tag the row's `notes` JSONB with the
     `deal_id` if the cash flow event references one.
   - The NAV reconstruction layer is the de-duplication boundary: when
     summing cash flow events for a date, **skip** any
     `futu_cash_flow` row whose `notes.deal_id` resolves to an existing
     `futu_trades.futu_deal_id` (whose `fees` column already accounted
     for it).
   - This makes dedup order-independent: whichever event arrives first
     gets stored, and the consumer ignores the duplicate at query time.
     Verified by `test_futu_fee_dedup_order_independence.py`.

4. **Compute realized P&L.** Per ticker per scope, if **any**
   `futu_trades.filled_at` in this scope+ticker is more recent than the
   newest non-null `realized_pnl` for that scope+ticker, OR any
   `corporate_actions` row for that ticker postdates the newest non-null
   `realized_pnl`, NULL out all closing-fill `realized_pnl` for that
   scope+ticker and recompute via the FIFO matcher (next section).

   This stricter rule corrects the "late-arriving fills" gap: a backfilled
   older BUY changes lot-matching for all subsequent SELLs in the same
   ticker, so we cannot trust previously-computed rows. The `--rematch`
   flag forces a full rebuild regardless of cursor state and remains the
   bug-fix lever.

5. **Snapshot current account.** Call existing
   `FutuClient.fetch_account()` + `fetch_positions()`. Insert one row
   into `account_snapshots` (existing scope-aware table). **Column
   mapping** — the existing IB writer populates the table's typed
   columns (`bankroll`, `peak_value`, `net_liquidation`) plus the
   `payload` JSONB blob. The Futu writer in this feature must map from
   `FutuClient.fetch_portfolio()`'s envelope as follows:

   ```
   account_snapshots.net_liquidation ← account_summary.net_liquidation
   account_snapshots.bankroll        ← account_summary.net_liquidation
                                       (Futu has no separate bankroll
                                       concept; mirror NAV)
   account_snapshots.peak_value      ← MAX(prior peak, current NAV)
                                       — computed by the writer, not Futu
   account_snapshots.payload         ← the full fetch_portfolio() envelope
                                       (preserve raw for debugging)
   account_snapshots.broker/env/acc  ← from AccountScope
   ```

   The Next.js consumer (`useFutuPortfolio.ts`) reads from `payload`
   first and falls back to typed columns; mismatch is caught by
   `test_account_snapshots_futu_payload_shape.py`.

6. **Pull corporate actions.** `get_rehab` per ticker in the Futu
   position universe (current + historical from `futu_trades.ticker`).
   `INSERT ... ON CONFLICT DO NOTHING` into `corporate_actions`.

7. **Record / reconstruct NAV.** Two distinct paths:
   - **First-run backfill** (no existing `nav_history` row for this
     scope dated < today): invoke the backward walk (see "NAV
     reconstruction" section). Walks from `cost_basis_nav_today`
     anchor backwards through trades + cash flows + corp actions,
     writing one `source='reconstructed'` row per date in
     `[2024-05-01, today]`. Past rows are immutable thereafter.
   - **Daily forward** (subsequent runs): write/UPSERT exactly one
     row for today: `source='reported'`, `nav = accinfo.net_liquidation`.
     Never touch past dates. Write a `kind='daily'` reconciliation
     row with the delta between reported and `cost_basis_nav_today`.

   `--rematch` is the only operator-explicit lever that re-runs the
   backward walk over historical rows. Without it, the daily sync
   only ever appends today's row.

**Invocation paths into this same function:**

- CLI: `uv run xenon-futu-history-sync --account <id> --since auto`
- FastAPI: `POST /futu/history/sync` body `{since: "auto" | "YYYY-MM-DD"}`
- launchd: runs the CLI form at 16:30 ET daily, machine-local. The
  plist must set `StartCalendarInterval.Hour = 16, Minute = 30` AND
  declare TZ via either (a) host TZ set to `America/New_York` or (b)
  the `LaunchOnlyOnce`/`UserName` mechanism plus a wrapper shell
  script that exec's `TZ=America/New_York /usr/bin/env -- uv run
xenon-futu-history-sync ...`. Default to (a) on the dev Mac and prod
  Mac mini, both of which run in NY time today; verify on Mac mini
  before Phase 2.

**Singleflight:** Postgres advisory lock via the existing
`pg_try_advisory_lock` helper at `src/xenon/api/services/advisory_lock.py`.
Add a new constant `LOCK_KEY_FUTU_HISTORY_SYNC = 7343001` matching the
existing pattern (`LOCK_KEY_UW_DAILY = 7341001`, `LOCK_KEY_VCG_CRI =
7342001`). The lock takes a single `bigint` key, not a tuple; a single
global lock per process is sufficient because there is one Futu account
in scope. Concurrent invocations log "another sync in progress" and exit
cleanly.

**Resumability:** each `INSERT ... ON CONFLICT DO NOTHING` runs in its own
transaction. No single wrapping transaction across steps. A mid-batch
failure (OpenD disconnect, network blip) leaves partial state that the
next sync run picks up cleanly via the cursor recompute in step 1. Do not
wrap the six steps in a single `BEGIN ... COMMIT` block — it would force a
full re-pull on any failure and lose this property.

**Time-zone partitioning.** All `flow_at::date` / `filled_at::date` /
trade-date computations partition by **`America/New_York`**, never the
session TZ:

```sql
WHERE (filled_at AT TIME ZONE 'America/New_York')::date = $1
```

`Postgres TIMESTAMPTZ::date` uses the session's `TimeZone` GUC; a 21:30 ET
fill is 01:30 UTC next day. Without the explicit cast the trade-date
boundary slides with the host's session TZ. Affects `day_cash_delta()`,
the FIFO event sort, NAV walk date keys, and the date filters on the
trades API.

**Request validation.** `POST /futu/history/sync` accepts `since` as
`"auto"` or an ISO date in `[2024-05-01, today]`. Reject any other value
with `422 Unprocessable Entity` and an explicit error code
`since_out_of_range` / `since_unparseable`. Same constraint applied to
`/futu/equity-curve`, `/futu/trades`, `/futu/performance` `from`/`to`
params.

**Settlement-date vs trade-date — expected reconciliation noise.**
Futu's `accinfo_query` may book some cash items on the settlement date
(T+1 for US stocks since 2024-05-28), while our reconstruction uses
trade date for fills and the cash flow event's own `flow_at` timestamp
for cash flows. This produces small daily reconciliation deltas around
trade boundaries, especially Fridays (T+1 settle = Monday). Document
this in the reconciliation UI tooltip so future-you doesn't chase it
as a bug.

## FIFO lot matcher

Per `(scope, ticker)`, replay fills **and corporate actions** in event-time
order, maintaining an in-memory FIFO queue of open lots. Each closing fill
pops from the queue in order, weighted-averaging the cost basis of matched
lots, and netting fees on both the closing fill and the matched opening
lots. Splits and reverse splits are applied to every open lot on their
`effective_date`.

**Stable ordering.** Sort key is `(filled_at, futu_deal_id)` for fills and
`(effective_date, 0)` for corporate actions, with fills sorted before
actions on the same date for symmetric BUY-then-split behavior. Sorting on
`filled_at` alone is unstable across re-pulls (DataFrame row order, API
return order, DB scan order can all differ) and would produce inconsistent
realized P&L between runs.

```python
open_lots: deque[Lot]   # each Lot has (qty, avg_cost, qty_at_creation, fees_amortized)

events = sorted(
    [('fill', f.filled_at, f.futu_deal_id, f) for f in fills_for_ticker]
  + [('corp', ca.effective_date, '0', ca) for ca in corp_actions_for_ticker],
    key=lambda e: (e[1], e[0] == 'corp', e[2])   # date asc, fills before corp, then stable id
)

for kind, _, _, event in events:
    if kind == 'corp':
        ratio = event.ratio_numerator / event.ratio_denominator
        for lot in open_lots:
            lot.qty             *= ratio
            lot.qty_at_creation *= ratio          # keep fee allocation proportion correct
            lot.avg_cost        /= ratio          # preserve total cost basis
            # lot.fees_amortized is unchanged — same total fees spread over
            # the same post-split shares.
        continue

    fill = event
    if fill.action == 'BUY':
        open_lots.append(Lot(qty=fill.quantity,
                             avg_cost=fill.price,
                             qty_at_creation=fill.quantity,
                             fees_amortized=fill.fees))
        fill.realized_pnl = None
    else:  # SELL
        remaining = fill.quantity
        gross_pnl = 0.0
        matched_buy_fees = 0.0
        while remaining > 0 and open_lots:
            lot = open_lots[0]
            matched_qty = min(remaining, lot.qty)
            gross_pnl += (fill.price - lot.avg_cost) * matched_qty
            matched_buy_fees += lot.fees_amortized * (matched_qty / lot.qty_at_creation)
            lot.qty -= matched_qty
            remaining -= matched_qty
            if lot.qty == 0:
                open_lots.popleft()
        if remaining > 0:
            log_warning("closed more than opened", scope, ticker, fill)
            # Write an anomaly row to futu_reconciliation_log (kind='anomaly',
            # notes={anomaly_type: 'over_close', ticker, fill_id, ...}).
        fill.realized_pnl = gross_pnl - fill.fees - matched_buy_fees
```

**Edge cases:**

- "Closed more than opened" — a SELL whose quantity exceeds open long lots
  (corrupt data, missing earlier fills, or an unmodeled corp action).
  Log a warning, write an anomaly row to `futu_reconciliation_log`, do not
  raise. Use the partial-match result for `realized_pnl`.
- Short selling — not expected in a US stock cash account. The matcher
  treats unrecognized states as warnings rather than fabricating data.
- Re-run safety — when **any** fill or corporate action for a
  `(scope, ticker)` is newer than the most recent non-null `realized_pnl`
  for that pair, step 4 nulls and rematches the whole ticker. The
  `--rematch` flag forces a full rebuild regardless of cursor state.
- Symbol changes — handled separately at the `corporate_actions` layer:
  rows for the _old_ ticker are migrated into the rematch input for the
  _new_ ticker before replay. Documented in `test_futu_symbol_change.py`.

## FX conversion

- **Source:** FRED H.10 daily noon rates. Series IDs `DEXHKUS` (HKD per
  USD), `DEXCHUS` (CNY per USD). Public CSV endpoint; no auth required.
- **Cache:** `xenon.fx_daily` table. Backfilled on first sync run for the
  full date range. Forward-filled daily by the sync job.
- **Backfill failure handling.** If FRED is unreachable during the
  initial backfill, the backfill **aborts loudly** with a clear error
  rather than degrading every cash-flow row to NULL `amount`. Retry up
  to 3× with backoff; surface failure as a sync result error code
  `fx_backfill_failed`. A single-day FRED gap during normal daily
  operation still degrades to the missing-FX anomaly row path.
- **Direction:** stored column is `currency_per_usd` (foreign-per-USD,
  matching FRED). Conversion: `amount_usd = amount_original /
currency_per_usd`. Multiplying instead would silently 60× a HKD amount.
- **Failure mode:** missing FX rate for a date → the cash flow row writes
  with `amount_original` populated and `amount` **NULL** (schema
  permits this on purpose). NAV reconstruction skips that day's
  missing-FX rows with a logged gap; an anomaly row is written to
  `futu_reconciliation_log` with `notes={missing_fx: 'HKD', date: D}`
  rather than guessing a rate.

## NAV reconstruction — backward walk from today's anchor

There is **no historical NAV endpoint** on Futu's API and no external
mark source in scope. Instead of computing historical NAV by valuing
positions at past market prices, we **anchor at today's known state and
walk backwards** through trades + cash flows + corporate actions. The
backward walk requires zero external price data.

The series the user sees is therefore **cost-basis equity**, not
mark-to-market equity, for the historical period. From the deployment
date onward, we append real reported NAV from daily Futu accinfo
snapshots.

### The anchor

Today's known state from Futu's live endpoints:

```
reported_nav_today      = accinfo_query().net_liquidation
cash_today              = accinfo_query().cash
positions_today         = position_list_query()      # ticker → (qty, avg_cost)
unrealized_pl_today     = accinfo_query().unrealized_pl
cost_basis_nav_today    = cash_today + Σ(qty × avg_cost)
                        = reported_nav_today - unrealized_pl_today
```

We anchor the historical reconstruction at **`cost_basis_nav_today`**,
not `reported_nav_today`. Rationale: the walk back uses `qty × avg_cost`
for open positions (no historical price data), so the curve is
end-to-end consistent in cost-basis terms. Anchoring at reported NAV
would inject `unrealized_pl_today` into every historical date, which is
both wrong (positions had different unrealized P&L historically) and
arbitrary (no way to back it out per-date).

The deploy-day point on the historical curve is therefore
`cost_basis_nav_today`; the daily forward series starts the next day at
`reported_nav` (which includes that day's unrealized component). The
delta between them (= today's `unrealized_pl`) is captured in the
reconciliation log so the visible transition is documented, not
mysterious.

### The walk

Runs once on initial backfill. Historical rows are immutable thereafter.

```text
cash         = cash_today
positions    = positions_today                  # {ticker: (qty, avg_cost)}

# Anchor: write today's reconstructed cost-basis NAV. The daily
# forward sync will additionally write today's reported NAV on the
# next day if not already present.
nav_anchor   = cash + Σ(qty × avg_cost)
INSERT nav_history (..., date=today, nav=nav_anchor, source='reconstructed',
                    total=nav_anchor, cash=cash,
                    stock_value=Σ(qty × avg_cost), options_value=0)

# Walk backwards. Pre-fetch all events (deals, cash flows, corp
# actions) for this scope, indexed by trade-date (NYSE tz).
for D in [today - 1 day, today - 2 days, ..., 2024-05-01]:
    next_day = D + 1 day                       # the day whose effects we un-apply

    # 1. Un-apply trades from next_day. Each fill on next_day moved
    #    cash and positions; reverse those moves to get end-of-day-D
    #    state.
    for fill in fills_on(next_day):
        if fill.action == 'BUY':
            cash    += fill.price × fill.quantity + fill.fees
            qty_post = positions[fill.ticker].qty - fill.quantity
            # Un-incorporate the BUY from avg_cost (FIFO-consistent —
            # see "Un-applying fills" below).
            positions[fill.ticker] = unapply_buy(positions[fill.ticker], fill)
        else:  # SELL
            cash    -= fill.price × fill.quantity - fill.fees
            positions[fill.ticker] = unapply_sell(positions[fill.ticker], fill)

    # 2. Un-apply cash-flow events from next_day, skipping those whose
    #    notes.deal_id is represented by a known fill (avoids double-
    #    counting fee-on-deal — same logic as the forward path).
    for cf in cash_flows_on(next_day):
        if cf.notes.deal_id in known_deal_ids:
            continue
        cash -= cf.amount

    # 3. Un-apply corporate actions from next_day. A 10:1 split on
    #    next_day means: pre-split qty = post-split qty / 10,
    #    pre-split avg_cost = post-split avg_cost × 10.
    for ca in corp_actions_on(next_day):
        ratio = ca.ratio_numerator / ca.ratio_denominator   # post / pre
        positions[ca.ticker].qty      /= ratio
        positions[ca.ticker].avg_cost *= ratio
        # Symbol changes get unwound by renaming the key back to the
        # original ticker.
        if ca.action_type == 'symbol_change':
            positions[ca.ticker_old] = positions.pop(ca.ticker)

    # 4. Compute cost-basis NAV for date D.
    mv_costbasis = Σ(positions[ticker].qty × positions[ticker].avg_cost)
    nav_D        = cash + mv_costbasis

    INSERT nav_history (..., date=D, nav=nav_D, source='reconstructed',
                        total=nav_D, cash=cash,
                        stock_value=mv_costbasis, options_value=0)
```

### Un-applying fills (cost-basis preservation)

To "un-apply" a BUY of qty Q at price P with fees F:

- Pre-fill `qty = qty_post + Q`
- Pre-fill total cost basis = post-fill total cost basis − (P × Q + F)
- Pre-fill `avg_cost = pre_total_cost / pre_qty`

To "un-apply" a SELL of qty Q at price P (with realized P&L recorded
from the forward FIFO pass on the closing fill):

- Pre-fill `qty = qty_post + Q`
- Pre-fill `avg_cost` = post-fill `avg_cost` (FIFO SELL doesn't change
  per-share avg cost for the remaining lots — it only removes the
  earliest matched lots' contribution, which were already at that
  same avg_cost in aggregate. The walk-back avg_cost is therefore a
  reasonable approximation; documented test
  `test_unapply_sell_preserves_avg_cost.py` pins this.)

For the rare case where a SELL on next_day fully closed a ticker
(`positions[ticker].qty` goes to 0 after applying forward), the
walk-back needs to recreate the lot at avg_cost = the original buys'
weighted avg_cost from the FIFO pass. Implementation: the FIFO matcher
emits a side-channel `lot_history` event log keyed by ticker; the
walk-back consumer uses it to fully restore open-lot state on
ticker-reopen.

### Daily forward (post-anchor)

Each subsequent daily sync only writes today's row:

```text
reported = FutuClient.fetch_account().net_liquidation
INSERT nav_history (..., date=today, nav=reported, source='reported',
                    total=reported, cash=accinfo.cash,
                    stock_value=Σ(qty × current_price),
                    options_value=0)
ON CONFLICT (broker, account_env, broker_account, date)
DO UPDATE SET nav = EXCLUDED.nav, source = EXCLUDED.source,
              total = EXCLUDED.total, cash = EXCLUDED.cash,
              stock_value = EXCLUDED.stock_value
WHERE nav_history.source IN ('reported', 'reconstructed')
  AND nav_history.date = today           -- never touch past dates

threshold = max(25, reported × 0.001)
expected  = cost_basis_nav_today_recomputed
            + (today's_realized_pnl_delta_from_yesterday)
            + (today's_dividends_minus_fees)
INSERT futu_reconciliation_log (kind='daily', recon_date=today,
                                reported_nav=reported,
                                reconstructed_nav=expected,
                                delta=reported - expected,
                                threshold=threshold,
                                exceeded=(abs(reported - expected) > threshold))
```

The UPSERT on today's row handles the intraday→EOD case (Pass-3 P3-1).
The `WHERE nav_history.date = today` guard ensures the daily run never
mutates past dates.

### Lock-in semantics

Historical `source='reconstructed'` rows are **immutable** after the
backfill. Re-running `xenon-futu-history-sync` without `--rematch`
never touches past rows. Only operator-explicit `--rematch` recomputes
historical rows (e.g., after fixing a bug in the FIFO matcher or
discovering a missed corp action). Forward `source='reported'` rows
grow daily and reflect Futu's truth.

This lets future-you build cleanly on top of locked-in history rather
than chasing recomputation drift.

### Failure modes

- **Ticker symbol change mid-history.** The corp action row with
  `action_type='symbol_change'` rewrites the position key during
  reversal. Test: `test_futu_backwalk_symbol_change.py`.
- **A SELL on next_day fully closed a ticker that was open before.**
  Walk-back recreates the lot using the FIFO `lot_history` side
  channel. If the side channel is missing (corrupt FIFO state), the
  walk writes an anomaly row and zeroes the position's avg_cost from
  D backward (so it's invisible in the curve). Documented in test
  `test_futu_backwalk_ticker_reopen_missing_lot_history.py`.
- **Corporate action mid-history that we don't have a corp_actions
  row for.** Realized P&L for closing fills around that date will be
  wrong; anomaly row flags `possible_unmodeled_action`. Operator
  manually adds the corp action and re-runs `--rematch`.
- **Multiple-day market closures (holidays, weekends).** The walk
  iterates every calendar day in `[2024-05-01, today-1]`. Days with
  zero events trivially copy the previous day's state — no special
  handling.

## API surface

All routes scope-gated via `xenon.api.guards.get_account_scope`.

| Route                      | Method | Body / Query                                                           | Response shape                                                                                                                                                                     |
| -------------------------- | ------ | ---------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /futu/history/sync`  | POST   | `{since: "auto" \| "YYYY-MM-DD"}`                                      | `{started_at, completed_at, fills_added, cash_flows_added, nav_rows_added, reconciliation_delta_today, warnings: []}`                                                              |
| `GET /futu/equity-curve`   | GET    | `?from&to`                                                             | `[{date, nav, source, daily_pnl, recon: {delta, exceeded, notes} \| null}, ...]` — `recon` is non-null only when a daily reconciliation row exists for that date                   |
| `GET /futu/trades`         | GET    | `?from&to&ticker&action&sort=filled_at&direction=desc&limit=50&cursor` | `{rows: [...], next_cursor: string \| null}`; cursor is composite `{filled_at, id}` to disambiguate same-timestamp rows; `sort` ∈ {filled_at, realized_pnl, ticker, fees}          |
| `GET /futu/performance`    | GET    | `?from&to`                                                             | `{total_return_twr, cagr, max_drawdown, win_rate, avg_holding_days, best_trade, worst_trade, total_commissions, monthly_pnl: {...}, external_cash_flows: {deposits, withdrawals}}` |
| `GET /futu/reconciliation` | GET    | —                                                                      | Last 30 `futu_reconciliation_log` rows (kind='daily' or 'anomaly') + `current_delta` from the most recent daily row                                                                |
| `GET /equity-curve`        | GET    | `?brokers=IB,FUTU&combined=false`                                      | `{per_broker: {IB: [...], FUTU: [...]}, combined: [...] \| null}`                                                                                                                  |

Existing `GET /futu/portfolio` stays untouched (see CI-guard caveat in
the Testing section below — that route currently reads
`data/futu_portfolio.json`; this feature does not change that path).
New routes live in `src/xenon/api/routes/futu_history.py`; **business
logic lives in `src/xenon/api/services/futu_history.py`** per the
`src/xenon/api/CLAUDE.md` convention (routers thin, services own the
sync orchestration, lot matcher, NAV reconstruction).

## Frontend

Three new components live under the existing Futu tab in `WorkspaceShell` /
`AccountTabBar`. One shared component lives at a cross-broker workspace
position.

### `<FutuEquityCurve />`

Single-line area chart, May 2024 → today. Pre-deployment segment renders
with a lighter stroke and a "reconstructed" badge in the tooltip;
post-deployment renders solid. Tooltip surfaces NAV, daily P&L, and the
reconciliation delta if a log row exists for that date.

### `<FutuTradeHistoryTable />`

Virtualized table of `futu_trades` rows. Columns: `filled_at`, `ticker`,
`action`, `quantity`, `price`, `fees`, `realized_pnl`. Sortable per column.
Filter chips: ticker autocomplete, action (BUY/SELL/all), date range.
Closed-trade rows tint realized P&L for scannability. Cursor pagination on
`filled_at`.

### `<FutuPerformanceStats />`

Strip of stat cards next to the existing `<MetricCards />` Futu block:
**time-weighted total return (TWR)**, CAGR, max drawdown, win rate, avg
holding days, best trade, worst trade, total commissions paid. Each card
is clickable to drill through (best-trade → trade history filtered to
that ticker/date).

**Performance math contract** — explicit because the naive
`(nav_end - nav_start) / nav_start` treats deposits as profit:

- **TWR** sub-periods each external cash flow. For each sub-period
  bracketed by a deposit/withdrawal: `r_i = (NAV_end_i - NAV_start_i -
cash_flow_i) / NAV_start_i`. TWR is the geometric chain:
  `TWR = Π(1 + r_i) - 1`. Deposits/withdrawals never inflate return.
- **CAGR** is annualised TWR over the requested window.
- **Max drawdown** uses the reconstructed/reported NAV series directly
  (drawdown survives cash flows — adding money mid-drawdown doesn't
  reset the trough).
- **Win rate, holding days, best/worst trade, commissions** come from
  closed `futu_trades` rows (`realized_pnl IS NOT NULL`). Day-trades
  count; same-day BUY+SELL pairs are one trade with holding_days=0.
- **External cash flow series** (`futu_cash_flow` rows with
  `flow_type IN ('deposit','withdraw')`) is returned alongside so the
  UI can render deposit/withdrawal markers on the equity curve.

### `<EquityCurveCrossBroker />`

New shared component at a cross-broker workspace position (above the
per-broker tabs). Two lines by default — IB color = primary, Futu color =
secondary per `brand/CLAUDE.md`. Legend toggle "Combined NAV" sums both
series at each date. The combined line only renders for dates where both
brokers have a row (no extrapolation).

### Data layer

A new `useFutuHistory.ts` hook colocated with the existing
`useFutuPortfolio.ts`. Calls the four `/futu/*` routes, exposes typed
selectors. The cross-broker chart uses a separate `useEquityCurve.ts` that
hits `/equity-curve?brokers=IB,FUTU`.

### Staleness and reconciliation UX

Reuses the existing `futuStaleness.ts` pattern.

- If latest `nav_history` row for Futu is > 1 NYSE trading day stale
  (using the existing NYSE calendar source in xenon; see `time_norm.py`
  or add a thin wrapper if no calendar helper exists yet) → yellow
  "Last synced: 3 days ago" banner with a "Sync now" button firing
  `POST /futu/history/sync`.
- If `current_delta` exceeds threshold → orange "Reconciliation mismatch:
  $XX delta on YYYY-MM-DD" indicator linking to `/futu/reconciliation`.

## Testing

### Python unit tests (under `scripts/tests/`)

- `test_futu_history_fetcher.py` — paging, US-market filter, rate-limit
  cooldown reuse from existing `FutuClient`, malformed-row tolerance.
- `test_futu_fifo_matcher.py` — opening fills, full close, partial close
  across multiple lots, the "closed more than opened" edge case (warning,
  no exception), commission-net accuracy to the cent.
- `test_futu_nav_backwalk.py` — golden-day cases for the backward
  walk: pure cash deposit (un-applies to zero on D-1), single
  round-trip (closes in middle of history; positions[D] reflects
  open state on both sides of the close), dividend payment (cash up
  on D, walking back removes it on D-1), FX-converted cash flow,
  missing FX (anomaly + no contribution to nav on that day).
- `test_futu_sync_idempotent.py` — running sync twice with no new data is
  a no-op; overlapping date ranges produce no duplicate rows.
- `test_futu_reconciliation_log.py` — delta computation, threshold scaling.
- `test_futu_api_routes.py` — scope-gating, paginated trades response,
  equity curve shape.
- `test_futu_corporate_actions.py` — golden cases: 10:1 split mid-hold,
  reverse split mid-hold, symbol change mid-hold, split on day of close
  (timing edge). Asserts running position book and NAV match expected
  after each action.
- `test_futu_fx_missing.py` — cash flow row written with `amount=NULL`
  when FX series is missing for that date; NAV reconstruction skips it
  with a logged warning and the day's NAV is reported with a
  `missing_fx_for_date` note on the reconciliation row.
- `test_futu_sync_resumable.py` — sync that crashes after step 2 leaves
  partial state; second sync run completes cleanly with no duplicate
  rows (relies on UNIQUE + ON CONFLICT DO NOTHING).
- `test_all_fills_view.py` — `xenon.all_fills` correctness when
  filtered by `(broker, account_env, broker_account)`; ensures the view
  preserves scope, that fill-grain semantics hold on both sides, and
  that the IB-side join to `order_fills` doesn't drop rows.
- `test_futu_fifo_corp_action.py` — golden cases: 10:1 split mid-hold
  (qty up 10×, cost basis down 10×, total realized P&L unchanged),
  reverse 1:5 split mid-hold, split on same day as a closing fill (corp
  action sorts after fill via the stable-order key), fee amortization
  preserved across splits.
- `test_futu_fifo_stable_order.py` — two fills with identical
  `filled_at` and different `futu_deal_id` produce deterministic
  realized P&L across re-runs; randomizing input order does not change
  result.
- `test_futu_fee_dedup_order_independence.py` — fee event arriving
  before its deal, and after its deal: NAV reconstruction sum-of-cash
  is identical in both orderings (validates the consumer-side dedup).
- `test_futu_sync_late_arrival_rematch.py` — backfill an older BUY
  after later SELLs have computed realized_pnl; assert step 4 detects
  the late row and recomputes all subsequent realized_pnl for the
  ticker without needing `--rematch`.
- `test_futu_nav_continue_advances_state.py` — pre-seed a partial
  `nav_history` row in the middle of the window; sync run must NOT
  skip cash/position state advancement on that date, and the
  subsequent day's NAV must match the all-from-scratch reference.
- `test_futu_nav_past_missing_is_reconstructed.py` — sync run where
  `until_date == today` and there are 3 missing days in the past:
  past-missing rows must be `source='reconstructed'`, only today is
  `source='reported'`.
- `test_futu_anomaly_log.py` — anomaly rows are writable to
  `futu_reconciliation_log` with NULL `reported_nav`/`reconstructed_nav`
  (kind='anomaly'); partial unique index allows multiple anomalies on
  the same date.
- `test_futu_backwalk_anchor_consistency.py` — backward walk from
  `cost_basis_nav_today` ends at 2024-05-01 with the deterministic
  starting state (cash = first deposit, positions = empty). Verifies
  the walk inverts the forward FIFO exactly.
- `test_futu_backwalk_symbol_change.py` — historical symbol change
  unwinds the ticker key correctly during reversal.
- `test_futu_backwalk_ticker_reopen_missing_lot_history.py` — when a
  closing fill on D+1 zeros a ticker that was reopened later, the
  walk uses the FIFO `lot_history` side channel; if missing, writes
  anomaly + zeros avg_cost prior to reopen. P&L on closing fills
  remains correct (forward FIFO is the source of truth).
- `test_futu_backwalk_corp_action_reversal.py` — 10:1 split on D+1
  means walk-back qty(D) = qty(D+1) / 10, avg_cost(D) = avg_cost(D+1)
  × 10. Multiple sequential splits in a single ticker also tested.
- `test_futu_nav_historical_immutable.py` — after backfill, running
  daily sync repeatedly does NOT touch any pre-today nav_history row.
  Running with `--rematch` DOES recompute them.
- `test_futu_twr_excludes_deposits.py` — TWR calculation: deposit of
  $1000 followed by zero market move yields 0% return, not +∞%.
- `test_futu_symbol_change.py` — corporate-action symbol change
  mid-hold: rematch input migrates lots from old ticker to new.
- `test_fx_daily_direction.py` — store `currency_per_usd`, verify
  conversion uses division. A 7.8 HKD/USD rate applied to a 780 HKD
  flow yields exactly $100 USD, not $6084.
- `test_futu_nav_today_upsert.py` — sync at 14:00 writes today's
  reported NAV from current accinfo; sync again at 16:30 must UPSERT
  the row with the later snapshot (not skip). Past-day rows must NOT
  be touched by today's sync.
- `test_futu_trade_date_tz_partition.py` — a 21:30 ET (01:30 UTC next
  day) fill is bucketed into the ET trade date for cash-delta and FIFO
  ordering even when the Postgres session runs in UTC.
- `test_futu_sync_request_validation.py` — `POST /futu/history/sync`
  rejects `since='yesterday'`, `since='2050-01-01'`, `since='2020-01-01'`
  with a 422 + structured error code.
- `test_fx_backfill_fred_failure.py` — FRED endpoint returns 503 during
  backfill; sync aborts with `fx_backfill_failed`, does NOT write
  hundreds of NULL `amount` cash-flow rows.

### Frontend tests (under `web/tests/`)

- `useFutuHistory.test.ts` — query merging, staleness, refresh button.
- `futu-equity-curve.test.tsx` — reconstructed vs reported visual
  distinction, tooltip formatting.
- `futu-trade-history-table.test.tsx` — filtering, sorting, virtualization
  correctness on 1000+ rows.
- `equity-curve-cross-broker.test.tsx` — two-line render, combined
  toggle, dates-with-both-brokers logic.

### E2E (`web/e2e/futu-history.spec.ts`)

- Open Futu tab → equity curve, trade table, perf stats all render.
- Click "Sync now" → fills count increases, equity curve updates.
- Switch to cross-broker workspace position → two lines, toggle combined.

### CI guards

- New `test_futu_trades_constraint.py` — assert `xenon.trades` constraint
  stays `broker = 'IB'`, i.e., Futu rows route to `futu_trades`, not the
  IB trades table.
- **NEW guard `scripts/checks/no_json_fallback_on_futu_history_path.py`** —
  patterned on `no_json_fallback_on_order_path.py` but scoped to the
  new Futu **history** routes (`src/xenon/api/routes/futu_history.py`,
  `src/xenon/api/services/futu_history.py`, `web/app/api/futu/history/`,
  `web/app/api/futu/equity-curve/`, etc.). Fails on `readDataFile`,
  `readFile`, `json.load` against any `data/futu*.json` in those
  paths. The existing order-path guard does **not** cover Futu routes.
- **Existing `/futu/portfolio` JSON fallback is out of scope.** The
  current `src/xenon/api/server.py:3658` reads
  `data/futu_portfolio.json`; that path is for the live-positions
  view, separately tracked in `project_postgres_migration_read_side_gap.md`
  (Futu still on JSON by design). This feature does not migrate it
  and does not introduce new JSON reads — but it also does not assert
  that the existing path is gone.

## Error handling — silent-degrade hierarchy

| Failure                                                 | Behavior                                                                                                                                                                                           |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OpenD unreachable                                       | Return last-known data with `is_stale=true`, log warning. Sync returns `{ok: false, reason: "opend_unreachable"}`. UI shows yellow banner.                                                         |
| Rate limit                                              | `FutuClient` cooldown logic. Sync retries up to 3× with backoff, then returns partial result.                                                                                                      |
| `get_rehab` returns empty for an actively-traded ticker | Anomaly row `{anomaly_type: 'missing_rehab', ticker: 'XXXX'}`. FIFO matcher proceeds without split awareness for that ticker — realized P&L on post-split closes will be wrong; operator notified. |
| Missing FX rate                                         | Cash flow row writes with `amount=NULL`; NAV walk skips that day's contribution to cost-basis NAV; anomaly row with `{anomaly_type: 'missing_fx', currency, date}`.                                |
| `futu_deal_id` collision                                | `ON CONFLICT DO NOTHING` — first row wins; log at INFO.                                                                                                                                            |
| Late-arriving fill in scope+ticker                      | Step 4 detects via cursor comparison and rematches all subsequent SELLs for that ticker. No operator action required.                                                                              |
| Backward walk hits a ticker-reopen with no lot history  | Walk writes anomaly `{anomaly_type: 'missing_lot_history', ticker, reopen_date}`, zeros that ticker's avg_cost prior to reopen date. Realized P&L on the ticker is still accurate (forward FIFO).  |
| Reconciliation delta > threshold for 3 consecutive days | Escalate from yellow banner to orange error badge; surface in sidebar notifications.                                                                                                               |

## Daily snapshot retention — never lose anything

Postgres is the durable source of truth. Once captured, no data point
relies on Futu's API still having it available. This protects against
the realistic case where Futu rotates older history out of their query
windows (some broker APIs retain only 90 days of orders/fills).

**What gets snapshotted every EOD sync run:**

| Stream                | Table                     | Idempotency key                                                  | Coverage                                                                                            |
| --------------------- | ------------------------- | ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Fills                 | `futu_trades`             | `(scope, futu_deal_id)`                                          | every fill since cursor                                                                             |
| Cash flow events      | `futu_cash_flow`          | `(scope, futu_flow_id)`                                          | every event since cursor                                                                            |
| Corporate actions     | `corporate_actions`       | `(ticker, action_type, effective_date)`                          | full universe refresh                                                                               |
| Current account state | `account_snapshots`       | new row per sync run                                             | full `fetch_portfolio()` envelope (positions + market values + raw account info) preserved as JSONB |
| Daily NAV             | `nav_history`             | `(scope, date)`                                                  | one row per trading day, source='reported' going forward                                            |
| FX rates              | `fx_daily`                | `(currency, date)`                                               | forward-filled daily                                                                                |
| Sync metadata         | `futu_reconciliation_log` | append + `(scope, recon_date) WHERE kind='daily'` partial unique | one daily row + any anomalies                                                                       |

**Guarantees:**

1. **No silent drops.** Every API result is persisted via
   `INSERT ... ON CONFLICT DO NOTHING` keyed on broker-supplied IDs.
   No row is ever dropped without a corresponding anomaly entry in
   `futu_reconciliation_log`.
2. **Resilience to Futu API retention.** Even if Futu purges trade
   history after N days, our `futu_trades` / `futu_cash_flow` /
   `corporate_actions` tables retain the full record from 2024-05-01
   forward. The longer the system runs, the less we depend on Futu's
   query window.
3. **Position-level MTM preserved daily going forward.** The
   `account_snapshots.payload` JSONB blob captures every position's
   `nominal_price` and `market_value` on every sync run.
   Consequence: the historical MTM blind spot (open positions valued
   at cost basis in the pre-deployment period) **only ever applies to
   the pre-deployment period and shrinks every day** as more snapshots
   accumulate. Two years from now, the cost-basis caveat applies only
   to a fixed historical slice; the rolling window of "real MTM
   history" expands daily.
4. **Multi-day outage recovery.** Cursor logic gap-fills any number of
   missed days from Futu's history endpoints, bounded only by Futu's
   own retention. If Futu retains 90 days and we're offline 100 days,
   the last 10 days are lost permanently — but `futu_reconciliation_log`
   records the gap so the loss is explicit, not silent. **Mitigation:**
   the on-demand sync route (`POST /futu/history/sync`) lets the
   operator force a catch-up at any time without waiting for launchd.

**What this means for analytics:** any analytic that needs per-position
EOD MTM history (e.g., markout curves, holding-period attribution,
unrealized P&L over time) can be derived from
`account_snapshots.payload` without re-querying Futu. A future feature
that wants a denormalized `futu_position_snapshots` table can populate
it from the JSONB archive at any point — no data loss in between.

## Observability

A new `futu_api_stats.py` module mirroring `uw_api_stats.py` — thread-safe
singleton recording per-call latency, rate-limit hits, cooldowns, error
counts for `history_deal_list_query`, `history_order_list_query`,
`get_acc_cash_flow`. Exposed via FastAPI `/futu-stats` and consumed by a
new `useFutuStats.ts` hook surfaced in the sidebar stats widget.

## Dependencies

- `futu-api>=9.0` — already in `pyproject.toml`. We use
  `OpenSecTradeContext` (existing) for trades + cash flows + accinfo,
  and the **same SDK's `OpenQuoteContext.get_rehab`** for corporate
  actions. No new dep.
- HTTP client for FRED CSV — `httpx` (already a project dep).
- Optional: `pandas_market_calendars` for the NYSE trading-day helper —
  decision deferred to plan.

No new external runtime dependencies. **No `ib_async` dependency for
this feature** — IB Gateway is not touched at any point in the sync,
backwalk, or daily-forward paths.

## Open implementation details (defaulted, surface in plan)

- Whether dev OpenD points at the live Futu account or a paper/sim
  account. Either works; the `broker_account` scope value distinguishes.
- Exact taxonomy of Futu's `cash_flow_type` enum values vs. our normalized
  `flow_type`. The mapping table above is the starting guess; verify
  against actual `get_acc_cash_flow` responses during implementation and
  expand the mapping as new enum values surface.
- Whether `get_acc_cash_flow` exposes a `deal_id` field on fee events.
  The fee-dedup strategy assumes it does; if not, the consumer-side
  dedup falls back to amount + same-day matching (looser, document in
  implementation).
- **FRED CSV format and direction.** Verified during review:
  `DEXHKUS` CSV is `observation_date,DEXHKUS` with values like
  `2024-05-01,7.8234` = HKD per USD. Conversion uses division
  (`usd = original / rate`). A `test_fx_daily_direction.py` golden
  test pins the math: 7.8 HKD/USD applied to a 780 HKD cash flow
  yields exactly $100 USD.
- **Futu paging window for `history_deal_list_query`.** Assumed 90
  days; never verified. Implementer should empirically test: pass a
  91-day window, observe whether SDK paginates internally, errors,
  or truncates. Adjust the sync algorithm step 2 window if needed.
- **Futu `get_rehab` coverage of historical splits.** Assumed
  complete for US stocks back to 2024-05-01. Implementation must spot-check
  against a known split in the account's history (e.g., NVDA 2024-06-10
  10:1) and assert the split appears with the correct
  ratio/effective_date. If `get_rehab` returns no row for a known
  split, that's an anomaly row + operator action — no fallback in
  scope.
- Exact "deployment date" semantics for live: not stored as config;
  inferred at query time as
  `MIN(date) FROM xenon.nav_history WHERE broker='FUTU' AND source='reported'`.
- NYSE calendar source: add a thin wrapper in `src/xenon/utils/market_calendar.py`
  (no existing helper in `time_norm.py`). `pandas_market_calendars` is a
  candidate dep; revisit during implementation.
- Whether the dev FastAPI launches its own scheduled-sync background task
  on lifespan (analogous to the VCG/CRI loop) in addition to launchd.
  Default: yes, behind `XENON_FUTU_HISTORY_LOOP=1` env var defaulting to
  off in tests and on in dev/prod. Singleflight is enforced by the
  shared `LOCK_KEY_FUTU_HISTORY_SYNC` advisory lock so launchd + lifespan
  loop coexist safely.

## Future work (out of scope)

- Options support on the Futu side (if the account ever holds options).
- Intra-day equity curve.
- HK / CN market trades.
- Per-currency NAV breakdown alongside USD.
- Cross-broker per-trade view (currently per-broker only).
- Moving Futu OpenD itself to the Mac mini headless.
