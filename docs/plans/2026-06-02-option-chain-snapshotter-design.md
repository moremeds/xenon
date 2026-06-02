# Option Chain Snapshotter — Design Doc

**Date:** 2026-06-02
**Status:** Design / proposal — not scheduled. Promote to
`docs/plans/YYYY-MM-DD-option-chain-snapshotter-IMPL.md` when picking up.
**Owner:** chenxi
**Related:**

- `docs/architecture/production-database-strategy.md` (DB-first invariant)
- `~/projects/unusual-whales/CLAUDE.md` (`option_wizard` DB context, role `argon_app`)
- Memory: `feedback_ib_async_in_fastapi.md`, `feedback_broker_bugs_paper_first.md`,
  `feedback_live_e2e_surfaces_contract_bugs.md`, `feedback_verify_data_source_capabilities.md`

---

## Why this matters

Today the only persisted per-contract option time-series on the macmini stack
is `option_wizard.uw_scan.option_intraday_buckets` — per-minute UW bars,
but only for contracts that _traded_. That dataset is rich for flow research
but cannot answer:

- "What did SPX's full IV surface look like at 2026-04-15 10:30?"
- "What was the bid/ask on an OTM SPX put two weeks before that FOMC?"
- "Replay the SPX chain at every 10-min tick to backtest a 0DTE vertical entry."
- "Was VIX skew inverted at the moment of the regime change on day X?"

This proposal adds **full-chain IB snapshots** (every strike × every expiry,
every cycle) for **four index tickers — SPX, NDX, RUT, VIX** (CBOE cash-settled
European-style index options), plus 1-min underlying OHLCV bars per ticker,
persisted to a new TimescaleDB-backed Postgres database `option_chain` on the
macmini. It is research/archive infrastructure; xenon's order-path is not
affected and the dataset is not a runtime input to any trading decision in v1.

Index-only scope (vs the original 102-ticker watchlist) keeps the contract
universe at ~33k and the throughput budget within striking distance of the
10-min cadence target. Stock-watchlist scope is **explicitly deferred to v2**
until throughput economics improve (line booster purchase, offline IV
computation, or chain scoping by ATM band).

---

## Scope

**In scope:**

- New Postgres DB `option_chain` on macmini, TimescaleDB extension required.
- Five tables under schema `archive`: `snapshot_config`, `option_universe`,
  `snapshot_run`, `option_chain`, `underlying_ohlcv`.
- Long-running Python service `option_chain_snapshotter` on macmini,
  supervised by launchd, isolated from xenon's order client.
- **Universe: 4 index tickers — SPX, NDX, RUT, VIX** (CBOE cash-settled,
  secType `IND`, European-style). Hardcoded in seed migration; not synced
  from any external source in v1.
- **Cadence: 10 min target for all 4 tickers** during RTH, ×3 outside RTH.
  Effective cadence under throughput budget is documented honestly in the
  Throughput section.
- Continuous priority-queue poller — _not_ wall-clock cycles.
- IB connection pool: 2× `IB()` clients (clientIds 99–100), round-robin
  dispatch (affinity not useful at 4-ticker scale), per-connection
  semaphore of 36 lines (≤72 account-wide).
- Snapshot includes `modelGreeks` tick (IV + delta/gamma/vega/theta).
  Greeks captured inline at IB's tick arrival — not recomputed downstream
  (per user requirement: offline IV reconstruction is hard to make match
  IB's pricing assumptions exactly; capture-once is the audit-grade record).
- Market-hours gating: 04:00–20:00 ET Mon–Fri, cadence ×3 outside RTH,
  exchange-calendar honored for holidays.
- Read grants on the new DB for `xenon_prod`, `xenon_dev`, `argon_app`.

**Out of scope (explicit non-goals):**

- **Stock/ETF universe** (the 102 `uw_scan.watchlist` tickers) — deferred to
  v2, gated on a throughput economics decision (line booster purchase,
  offline IV computation, or ATM-band scoping).
- **`option_wizard` cross-DB read** — v1 hardcodes the 4-ticker config seed.
  No watchlist sync module.
- Real-time streaming (no `reqMktData` non-snapshot mode).
- Backfill of historical chains (we capture from go-live forward).
- Push alerting (Slack/email/webhook on staleness) — deferred to v1.1.
- Any consumer of the dataset — backtester, IV-surface viewer, etc.
  Build the archive first; consumers ship separately.
- VX futures options (`secType=FOP`) — only VIX index options are in v1.

---

## Architecture

```
                          macmini
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│  IB Gateway (live)  ←─── clientId=42 (orders, xenon-stack)   │
│         ▲                                                    │
│         │   pool: clientIds 99-100, per-conn sem 36,         │
│         │   account-wide cap ≤72 of 100 lines                │
│         │                                                    │
│  ┌──────┴──────────────┐                                     │
│  │ option-chain-       │                                     │
│  │ snapshotter         │              ┌────────────────────┐ │
│  │                     │              │ option_chain DB    │ │
│  │ Components:         │  COPY writes │   .archive.config  │ │
│  │ • IBConnectionPool  │ ───────────► │   .archive.univ.   │ │
│  │ • PriorityQueue     │              │   .archive.runs    │ │
│  │ • UniverseExpander  │              │   .archive.chain   │ │
│  │ • SnapshotWorker×2  │              │   .archive.ohlcv   │ │
│  │ • OhlcvWorker       │              └────────────────────┘ │
│  │ • Persister         │                                     │
│  └─────────────────────┘                                     │
│                                                              │
│   Universe: SPX, NDX, RUT, VIX (CBOE index options)          │
│   Hardcoded in seed migration — no external watchlist read   │
└──────────────────────────────────────────────────────────────┘
   ▲                                          ▲
   │ launchd / single-instance guard          │ read grants:
   │ KeepAlive=true                           │ xenon_prod, xenon_dev,
   │                                          │ argon_app (read-only)
```

### Process model

- **One process per snapshotter instance.** Single-instance guard via PID
  file at `/tmp/option-chain-snapshotter.pid` AND a Postgres advisory lock
  on a known key — refuses to start if another instance holds either.
  Prevents the IB clientId collision pattern (IB silently disconnects the
  earlier client when a duplicate clientId connects).
- **Internal concurrency** uses `asyncio` + ib_async's `*Async` variants
  exclusively (per memory `feedback_ib_async_in_fastapi.md` — never wrap
  ib_async sync calls in `asyncio.to_thread`).
- **Workers:** one `SnapshotWorker` task per connection in the pool
  (2 workers, all pulling from the shared priority queue), plus one
  `OhlcvWorker` task — fires every **60s**, fans out
  `reqHistoricalData(barSize='1 min', durationStr='120 S', whatToShow='TRADES')`
  for the 4 underlying indexes (parallel, well under IB's 6-simultaneous
  historical-request limit), wall time ~2s per cycle. Note: VIX/SPX/NDX/RUT
  are `IND` secType — IB returns their level as historical bars but `volume`
  is always 0 for cash indexes (no trading volume on the index itself).
- **Persister** is a separate task with a bounded asyncio queue (100k row
  ring buffer). Snapshot workers push; persister batches into 5k-row
  COPYs. Decouples IB-bound work from DB-bound work.
- **Persister back-pressure policy:** snapshot worker calls
  `queue.put()` with a **5-second timeout**. On timeout: row is dropped,
  counter `persister_drops_total` incremented, structured-error event
  logged with `(ticker, snapshot_ts)` for forensics. The persister is
  expected to drain faster than snapshotters produce; sustained drops
  signal a DB-side outage and should page in v1.1.

---

## Schema

DB owner: `option_chain_writer` (new role, owned by snapshotter).
Read grants: `xenon_prod`, `xenon_dev`, `argon_app`.

```sql
CREATE DATABASE option_chain
    OWNER option_chain_writer
    ENCODING 'UTF8';

\c option_chain
CREATE EXTENSION timescaledb;
CREATE SCHEMA archive AUTHORIZATION option_chain_writer;

-- 1. PER-TICKER POLLER CONFIG
CREATE TABLE archive.snapshot_config (
    ticker          TEXT PRIMARY KEY,
    cadence_seconds INT NOT NULL DEFAULT 1800,        -- 30 min default
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    contract_scope  TEXT NOT NULL DEFAULT 'full',     -- 'full' | future scoping options
    notes           TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. DAILY OPTION UNIVERSE CACHE
--    Per Pass-2 finding C-2: same (ticker, expiry, strike, right) can map to
--    multiple contracts via tradingClass (SPX vs SPXW for weeklies). The full
--    contract identity also depends on exchange + multiplier + localSymbol.
--    Use con_id as the universe PK; the (ticker, ...) tuple is a lookup key,
--    not an identity key.
CREATE TABLE archive.option_universe (
    universe_date    DATE NOT NULL,
    con_id           BIGINT NOT NULL,                 -- qualified once per day; identity
    ticker           TEXT NOT NULL,
    trading_class    TEXT NOT NULL,                   -- SPX, SPXW, NDX, NDXP, RUT, RUTW, VIX, VIXW
    exchange         TEXT NOT NULL,                   -- typ. CBOE for indexes
    multiplier       INTEGER NOT NULL,                -- 100 for SPX/NDX/RUT, 100 for VIX
    local_symbol     TEXT NOT NULL,                   -- IB localSymbol (canonical wire form)
    expiry           DATE NOT NULL,
    strike           NUMERIC(14,4) NOT NULL,
    right            CHAR(1) NOT NULL,                -- 'C' | 'P'

    -- Per Pass-2 finding C-9: dead-conId state needs durable persistence,
    -- not in-process. Otherwise launchd restart loses the disable list.
    status           TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'disabled_temp' | 'disabled_day'
    failure_count    INTEGER NOT NULL DEFAULT 0,
    disabled_until   TIMESTAMPTZ,                     -- NULL when active
    last_error_code  INTEGER,                         -- IB error code (e.g. 354, 200, 162)

    -- Per Pass-3 finding A-1: two-step commit prevents poller from reading
    -- partial mid-refresh rows. Refresh writes with FALSE, flips to TRUE
    -- atomically on success. Poller reads only WHERE committed = TRUE.
    universe_date_committed BOOLEAN NOT NULL DEFAULT FALSE,

    discovered_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (universe_date, con_id)
);
CREATE INDEX ON archive.option_universe (ticker, universe_date, expiry, strike, right);
CREATE INDEX ON archive.option_universe (status, disabled_until)
    WHERE status <> 'active';
CREATE INDEX ON archive.option_universe (universe_date_committed, universe_date DESC)
    WHERE universe_date_committed;

-- 3. SNAPSHOT RUN LEDGER
CREATE TABLE archive.snapshot_run (
    id                   BIGSERIAL PRIMARY KEY,
    ticker               TEXT NOT NULL,
    started_at           TIMESTAMPTZ NOT NULL,
    finished_at          TIMESTAMPTZ,
    contracts_attempted  INT,
    contracts_persisted  INT,
    duration_ms          INT,
    ib_lines_peak        INT,
    status               TEXT NOT NULL,               -- 'ok' | 'partial' | 'failed' | 'timeout'
    error                TEXT
);
CREATE INDEX ON archive.snapshot_run (ticker, started_at DESC);

-- 4. THE BIG ONE — full chain snapshots (hypertable)
--    Per Pass-2 finding C-2: PK uses con_id (the canonical IB contract identity),
--    not normalized option terms — same (ticker,expiry,strike,right) can be
--    multiple contracts.
--    Per Pass-2 finding C-12: snapshot_ts marks the sweep grouping; per-tick
--    timestamps preserve sub-sweep accuracy for surface replay.
--    Per Pass-2 finding C-13: open_interest is best-effort under snapshot=True
--    (IB ignores generic-tick list for snapshot mode). Often NULL.
CREATE TABLE archive.option_chain (
    snapshot_ts      TIMESTAMPTZ NOT NULL,            -- sweep start time (run grouping)
    con_id           BIGINT      NOT NULL,            -- contract identity

    -- Denormalized identity (for query convenience without joining option_universe)
    ticker           TEXT        NOT NULL,
    trading_class    TEXT        NOT NULL,
    expiry           DATE        NOT NULL,
    strike           NUMERIC(14,4) NOT NULL,
    right            CHAR(1)     NOT NULL,

    -- Per-row timestamps (true as-of for surface reconstruction)
    request_ts       TIMESTAMPTZ NOT NULL,            -- when reqMktData fired
    quote_ts         TIMESTAMPTZ,                     -- when bid/ask both arrived
    greeks_ts        TIMESTAMPTZ,                     -- when modelGreeks tick arrived

    bid              NUMERIC(12,4),
    ask              NUMERIC(12,4),
    bid_size         INTEGER,
    ask_size         INTEGER,
    last             NUMERIC(12,4),
    last_size        INTEGER,
    volume           BIGINT,
    open_interest    BIGINT,                          -- best-effort; often NULL in snapshot mode

    iv               REAL,
    delta            REAL,
    gamma            REAL,
    vega             REAL,
    theta            REAL,
    underlying_px    NUMERIC(12,4),                   -- spot at greek-tick arrival

    run_id           BIGINT NOT NULL REFERENCES archive.snapshot_run(id),

    PRIMARY KEY (snapshot_ts, con_id)
);
SELECT create_hypertable('archive.option_chain', 'snapshot_ts',
                        chunk_time_interval => INTERVAL '1 day');
SELECT add_compression_policy('archive.option_chain', INTERVAL '7 days');
CREATE INDEX ON archive.option_chain (ticker, trading_class, expiry, snapshot_ts DESC);
CREATE INDEX ON archive.option_chain (con_id, snapshot_ts DESC);

-- 5. UNDERLYING 1-MIN BARS (hypertable)
CREATE TABLE archive.underlying_ohlcv (
    bar_ts      TIMESTAMPTZ NOT NULL,
    ticker      TEXT NOT NULL,
    bar_size    TEXT NOT NULL DEFAULT '1 min',
    open        NUMERIC(14,4),
    high        NUMERIC(14,4),
    low         NUMERIC(14,4),
    close       NUMERIC(14,4),
    volume      BIGINT,
    PRIMARY KEY (bar_ts, ticker, bar_size)
);
SELECT create_hypertable('archive.underlying_ohlcv', 'bar_ts',
                        chunk_time_interval => INTERVAL '7 days');
SELECT add_compression_policy('archive.underlying_ohlcv', INTERVAL '30 days');

-- READ-ONLY VIEW for operator staleness check
CREATE VIEW archive.v_staleness AS
SELECT
    c.ticker,
    c.cadence_seconds,
    EXTRACT(EPOCH FROM (now() - last_run.finished_at))::INT AS seconds_since_last,
    last_run.contracts_persisted,
    last_run.status,
    CASE WHEN now() - last_run.finished_at > make_interval(secs => c.cadence_seconds * 2)
         THEN 'stale' ELSE 'fresh' END AS health
FROM archive.snapshot_config c
LEFT JOIN LATERAL (
    SELECT * FROM archive.snapshot_run r
    WHERE r.ticker = c.ticker AND r.status IN ('ok','partial')
    ORDER BY r.finished_at DESC LIMIT 1
) last_run ON true
WHERE c.enabled;

GRANT CONNECT ON DATABASE option_chain TO xenon_prod, xenon_dev, argon_app;
GRANT USAGE ON SCHEMA archive TO xenon_prod, xenon_dev, argon_app;
GRANT SELECT ON ALL TABLES IN SCHEMA archive TO xenon_prod, xenon_dev, argon_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA archive
    GRANT SELECT ON TABLES TO xenon_prod, xenon_dev, argon_app;
```

### Throughput budget — index-only universe

**Budget side — two scenarios, fork resolved on day-1 (Pass-2 finding C-1):**

If explicit `cancelMktData()` releases the market-data line _before_
IB's `tickSnapshotEnd` event (~11s), the per-snapshot wall time is
dominated by greek arrival:

- ~4s/snapshot (1s bid/ask + 3s modelGreeks empirical), 72 line-cap
- Sustained throughput: **~18 contracts/sec**
- Per RTH day: ~420k contracts/day

If `cancelMktData()` does _not_ release early (IB holds the line until
`tickSnapshotEnd`):

- ~11s/snapshot effective (IB's documented snapshot completion timer)
- Sustained throughput: **~6.5 contracts/sec**
- Per RTH day: ~150k contracts/day

The day-1 IB behavior probe (rollout step 1) measures this directly.
**Until measured, plan for the pessimistic case** (~6.5 cps, ~70 min
effective sweep). Throughput regression test floor adjusts to whichever
the probe lands on.

**Demand side at 10-min cadence for 4 indexes:**

- SPX: ~24k contracts (verify against `reqSecDefOptParams` day 1)
- NDX: ~5k
- RUT: ~3k
- VIX: ~1-3k
- **Universe total: ~33k contracts**

- @ 10 min during RTH: 33k × 39 = **~1.29M contracts/day**
- Outside RTH (9.5h @ 30-min effective): ~33k × 19 = **~0.63M contracts/day**
- Total demand: **~1.92M contracts/day → ~82 cps during RTH**

**Demand is 4.5×–12× the budget during RTH** depending on which throughput
scenario lands. Cadence is **aspirational**; the continuous priority-queue
pattern degrades gracefully. Realistic v1 expectations:

- **Optimistic case (line release works):** ~30 min effective sweep (not 10)
- **Pessimistic case (line held to tickSnapshotEnd):** ~70 min effective sweep
- v_staleness `cadence_seconds × 2 = 20 min` will read `stale` for all
  4 most of the time. **Bump the threshold to 4× cadence (40 min)** as
  the v1 working definition of healthy until throughput grows.

Mitigations (none chosen for v1; documented for v1.1 planning):

| Lever                                                                    | Effect                      | Cost                                    |
| ------------------------------------------------------------------------ | --------------------------- | --------------------------------------- |
| Use `reqHistoricalData(barSize='1 min', whatToShow='BID_ASK')` for chain | bypasses streaming line cap | needs IB doc verification; no IV/greeks |
| Buy IB market-data line booster tier                                     | 100 → 500 lines             | ~$30-100/month, account admin           |
| Per-ticker scope: `atm_band_25pct` for SPX                               | SPX universe ~70% smaller   | drops far-OTM SPX tails                 |

**Why we are keeping `modelGreeks` despite the throughput hit:** IB's
greeks/IV use IB's internal pricing assumptions (their dividend stream
estimate, risk-free curve, early-exercise model where applicable).
Recomputing offline cannot exactly match these — even small disagreements
in r/q inputs shift IV by basis points. Capture-once at IB tick arrival
is the audit-grade record; we can additionally compute our own IV later
for cross-validation, but we keep IB's as canonical.

### Storage envelope (under realistic throughput)

Sized to **actual** sustained throughput. Two scenarios per the throughput
section above; day-1 probe picks the actual one:

**Optimistic (early line release works, 18 cps):**

- 18 cps × 16h × 3600s = **~1.04M contracts/day** uncompressed
- Row size ~280B (incl new per-row timestamps + trading_class) → **~290 MB/day raw**
- After Timescale columnar compression (typical 10×): **~29 MB/day**
- Annual: **~10 GB/year compressed**

**Pessimistic (line held to tickSnapshotEnd, 6.5 cps):**

- 6.5 cps × 16h × 3600s = **~375k contracts/day** uncompressed
- ~105 MB/day raw → ~11 MB/day compressed → **~4 GB/year compressed**

Either case: comfortable on macmini local disk. Retention policy
(drop chunks older than N years) deferred to v1.1.

---

## IB connection management

### Pool topology

- **Pool of 2** connections via xenon's existing `IBClient` wrapper class
  (`src/xenon/clients/ib_client.py`) — NOT raw `ib_async.IB()` instances.
  The wrapper provides reconnect handling, error mapping, and clientId
  registry integration that we'd otherwise duplicate (per Pass-2 finding
  CL-3).
- **clientIds registered in xenon's `CLIENT_IDS` dict** (per Pass-2 finding
  C-14), in the daemon range (70-99), not picked ad-hoc:
  ```python
  # Add to src/xenon/clients/ib_client.py::CLIENT_IDS
  "option_chain_snapshotter_a": 95,
  "option_chain_snapshotter_b": 96,
  ```
  Connect via `IBClient.connect(client_name="option_chain_snapshotter_a")`.
- **Dispatch: round-robin**, not affinity. With 4 tickers, ticker→connection
  affinity would pin 2 tickers to each connection — a wedged connection
  then stalls half the universe. Round-robin spreads any single ticker's
  contracts across both connections.

### Admission control — single atomic acquire path

Per Pass-2 findings C-4 (no msg/sec pacing), C-10 (asyncio.Semaphore not
safely resizable), and C-11 (TOCTOU race between separate semaphores):
**one atomic limiter, not two.**

```
ResizableLimiter (per connection):
    .acquire() -> Lease
        - Awaits until BOTH (a) per-conn slots available AND
          (b) account-wide line ledger has free slot AND
          (c) account-wide token bucket has 1 msg credit
        - Decrements all three atomically inside an asyncio.Lock
        - Returns a Lease object that owns the slot/credit
    Lease.release() (called in `finally`, exactly once):
        - Returns slot to per-conn counter
        - Returns slot to account ledger
        - (Token bucket credits regenerate via background task)
    .resize(new_cap):
        - Only changes future admissions. Current leases unaffected.
        - Implemented as `self._cap = new_cap`, woken by Condition.notify_all().
```

- **Per-connection cap: 36** (configurable). 2 × 36 = 72 account-wide,
  leaves ~28 lines for xenon's order-quote traffic.
- **Account-wide token bucket: 50 msg/sec, burst 100** (configurable).
  Every IB message — `reqMktData`, `cancelMktData`, `reqSecDefOptParams`,
  `reqHistoricalData`, `reqContractDetails` — costs 1 token. Account-wide,
  not per-conn. AIMD operates on `msg_per_sec_cap`, not the line cap.
- **AIMD behavior on pacing violation (codes 100, 165):** halve
  `msg_per_sec_cap` (MD) immediately, then additive-increase +1/30s back
  to ceiling. Implemented via `ResizableLimiter.resize()` — no leaked
  permits, no deadlock.

### Snapshot lifecycle (per contract)

Per Pass-2 findings C-1 (line release timing), C-6 (8s vs tickSnapshotEnd):

1. `lease = await limiter.acquire()` — admission control.
2. `request_ts = now()` recorded.
3. `ticker = ib.reqMktData(contract, genericTickList='', snapshot=True, regulatorySnapshot=False)`.
4. Wait on `ib.pendingTickersEvent` and/or `ticker.snapshotEndEvent` until:
   - bid + ask both present → `quote_ts = now()`
   - `modelGreeks` tick arrives → `greeks_ts = now()`, IV/delta/gamma/vega/theta filled
   - OR `tickSnapshotEnd` fires → snapshot complete, persist whatever we have
   - OR 12s hard timeout (above IB's documented 11s `tickSnapshotEnd`) → persist whatever we have
5. **Explicitly call `ib.cancelMktData(contract)`.** Whether this releases
   the line early or IB holds it until the 11s `tickSnapshotEnd` window
   is the headline empirical question (see Open Question #7). v1 throughput
   estimate of 18 cps **assumes early release**; if it doesn't work the
   real throughput is ~6.5 cps.
6. `lease.release()` (in `finally`).
7. Row queued to persister with `(snapshot_ts, request_ts, quote_ts, greeks_ts)`.

### Per-connection failure isolation

Per Pass-2 finding C-8 (per-conn disconnect must not pause the global queue):

- `IB.disconnectedEvent` per connection → that connection's workers
  pause and enter exponential reconnect backoff (1s → 60s cap).
- Other connection's workers **keep draining the queue** at their own
  pace. Round-robin dispatch is dynamically aware: if conn A is down,
  the dispatcher uses conn B only.
- Outstanding leases on the dead connection are released (returning slots
  to the account ledger so the surviving conn can use them) and their
  in-flight contracts are re-queued with `priority=high`.

---

## Operational behavior

### Market-hours gating

Default schedule: **04:00–20:00 ET Mon–Fri**.

- Inside RTH (09:30–16:00 ET): use the per-ticker cadence (10 min for all 4).
- Outside RTH but inside the 04:00–20:00 window: **multiply cadence by 3**
  (effective 30 min for all 4). Note: index options technically only trade
  during RTH on CBOE; outside-RTH snapshots will return stale quotes from
  the last RTH session. Worth capturing for completeness; flag the data
  as `is_rth=false` if downstream consumers care.
- Outside the window: poller idle, only universe-refresh job runs.
- Honor exchange-calendar holidays — fully idle. (Library: verify
  `exchange-calendars` is in xenon's deps; if not, add it.)

### Daily universe refresh

Runs once at **08:30 ET** (60 min before RTH open, configurable). Per
Pass-2 finding C-3, `reqSecDefOptParams` for `IND` secType requires the
underlying conId, not just the symbol.

```
For each ticker in {SPX, NDX, RUT, VIX}:
  1. underlying_contract = Index(symbol=ticker, exchange='CBOE', currency='USD')
  2. await ib.qualifyContractsAsync(underlying_contract)
     → resolves underlying_conId (cached for the trading day)
  3. params = await ib.reqSecDefOptParamsAsync(
         underlyingSymbol=ticker,
         futFopExchange='',                      # equity/index option, not FOP
         underlyingSecType='IND',                # NOT 'STK'
         underlyingConId=underlying_contract.conId,
     )
     → returns list of (exchange, tradingClass, multiplier, expirations, strikes)
     → ONE ticker may return MULTIPLE tradingClass entries
       (e.g. SPX returns SPX + SPXW; VIX returns VIX + VIXW)
  4. For each (tradingClass, exchange, multiplier) × expirations × strikes
     × {C,P}, build an Option contract with explicit tradingClass set.
  5. await ib.qualifyContractsAsync(*all_contracts) in batches
     → resolves per-contract conId
  6. UPSERT into archive.option_universe keyed on (universe_date, con_id)
     with full identity (ticker, tradingClass, exchange, multiplier,
     localSymbol, expiry, strike, right). status='active', failure_count=0.
```

**Time budget: must complete by 09:25 ET.** Empirical estimate: 4 tickers
× ~5s reqSecDefOptParams + ~60s qualifyContractsAsync for ~8k contracts
each = ~5 min total. Headroom against 55-min budget. If refresh hasn't
finished by 09:25 ET, abort gracefully — snapshot loop falls back to
_yesterday's_ `option_universe` rows (so steady-state coverage is
preserved), and an operator alert fires (`universe_refresh_overflow`
counter increments).

Universe rows are append-only by `universe_date`; never delete yesterday's
even when an expiry rolls off. Audit trail for "when did this conId first
appear / disappear from the chain."

**Universe-refresh / snapshot-loop race (Pass-3 finding A-1):** snapshot
workers MUST NOT read partial today's-universe rows while the refresh is
mid-write. Two-step commit pattern:

1. Refresh writes new rows with a sentinel column `universe_date_committed = false`.
2. Snapshot poller reads `WHERE universe_date_committed = true` only.
3. Refresh atomically flips the flag to `true` in a single transaction
   on success.
4. If refresh aborts, poller transparently falls back to the previous day's
   committed universe (also `committed=true`), picking the most recent
   `universe_date` with `committed=true` per conId.

Add column to `archive.option_universe`:

```sql
ALTER TABLE archive.option_universe ADD COLUMN universe_date_committed BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX ON archive.option_universe (universe_date_committed, universe_date DESC) WHERE universe_date_committed;
```

**0DTE rollover (Pass-3 finding A-5):** SPX/SPXW 0DTE contracts settle
and disappear from IB's chain at exactly 16:00 ET. The first snapshot
attempt after 16:00 on 0DTE conIds will hit the `error 200`
(no security definition) path. Suppress this as a normal-event log line
(not `partial`-counting) when the contract's expiry was today AND the
current time is past 16:00 ET. Don't flag for `disabled_day` either —
just stop trying that conId for the rest of the day silently.

**Intraday conId recovery (durable per Pass-2 finding C-9):** the dead-conId
state lives in `archive.option_universe.status` / `disabled_until` /
`failure_count` / `last_error_code` — NOT in memory. On `error()` callback:

- First failure of a conId today: `status='disabled_temp'`,
  `disabled_until = now() + 90min`, `failure_count = 1`.
- Second failure of the same conId today: `status='disabled_day'`,
  `disabled_until = NULL`, `failure_count = 2`. Won't retry until next
  08:30 ET refresh.
- launchd restart preserves state (reads from PG on boot).

### Failure modes & circuit breakers

| Failure                                                | Detection                                                                                                                | Response                                                                                                                                                                                                                                                                                                     |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| IB Gateway disconnect (per connection)                 | `IB.disconnectedEvent` on conn-N                                                                                         | **Local pause only**: pause workers bound to conn-N, exponential backoff reconnect (1s → 60s cap). Other connection keeps draining the global queue. Release outstanding leases on conn-N back to the account ledger so the surviving conn can use the slots. Re-queue in-flight contracts at high priority. |
| IB pacing violation (codes 100, 165)                   | Error event                                                                                                              | **AIMD on token bucket**: halve `msg_per_sec_cap` (MD) immediately, additive-increase +1/30s back to ceiling. Implemented via `ResizableLimiter.resize()`. (Per-conn line cap stays at 36.)                                                                                                                  |
| Postgres write timeout (>5s)                           | psycopg timeout                                                                                                          | Persister enters **high-water IB pause** (workers stop calling `limiter.acquire()`). Pause clears at low-water mark. Sustained timeouts (>30s) → drop oldest rows from the ring + structured-error event. Snapshot_run rows wait for persister ACK before transitioning to `ok` or `partial`.                |
| Contract qualification fails (delisted, missing conId) | `error()` callback                                                                                                       | Update `option_universe.status` in PG: first failure → `disabled_temp` (90 min), second same-day → `disabled_day`. State survives launchd restart.                                                                                                                                                           |
| `modelGreeks` tick never arrives                       | `tickSnapshotEnd` event OR 12s hard timeout                                                                              | Persist row with whatever ticks arrived (bid/ask/volume usually good; IV/greeks may be NULL). `greeks_ts` stays NULL when greeks didn't arrive. Increment `partial` count.                                                                                                                                   |
| Ticker stuck in queue **measured threshold**           | Watchdog scan every 60s; threshold = `2 × p95_observed_sweep_seconds` (from rolling stats), NOT `2 × configured_cadence` | Force-cancel only truly-hung in-flight reqIds past a hard 30s request timeout. Re-queue ticker at high priority. **Spec accepts ~30 min effective sweep** so the watchdog must not fire on normal backlog.                                                                                                   |
| Index option (SPX/NDX/RUT/VIX) hang                    | Same as above; per memory `feedback_ib_async_in_fastapi.md`, `reqTickers` hangs on index options                         | Regression-tested specifically; we use `reqMktDataAsync(snapshot=True)`, not `reqTickers`/`reqTickersAsync`. All 4 v1 tickers are `IND` secType so this path is the _entire_ product.                                                                                                                        |
| IB error 354 (data not subscribed) per ticker          | `error()` callback with `errorCode=354`                                                                                  | Per Open Question #2 — fail loudly during the day-1 preflight check, not silently mid-day. If hit mid-day, disable that ticker entirely until operator restart (don't auto-recover, the subscription state needs human action).                                                                              |

### Observability

- **Operator dashboard query:** `SELECT * FROM archive.v_staleness WHERE health = 'stale';`
- **Structured logs** (JSON, one line per snapshot completion + every
  error) to `/var/log/xenon/option-chain-snapshotter.log`, rotated by
  `newsyslog`.
- **Push alerting deferred to v1.1** — once we see what the noise floor
  looks like, define a useful threshold (e.g. `health='stale'` for > N
  tickers for > M minutes).

### Production safety (Pass-3 additions)

**Persister back-pressure water marks (Pass-3 finding A-3):**

- **High-water = 80% of ring (80k rows)** → snapshot workers' `limiter.acquire()`
  starts blocking new admissions. Existing leases drain normally.
- **Low-water = 30% of ring (30k rows)** → admissions resume.
- The 50-percentile hysteresis is deliberate; preventing flap when the
  persister is doing steady-state catch-up.

**Combined-recovery thundering herd (Pass-3 finding A-4):**

After a simultaneous IB-disconnect + PG-disconnect event resolves, both
recover at roughly the same time and the persister ring is near-full.
The naive behavior — all 72 workers fire reqMktData at once while the
persister tries to flush 100k buffered rows — would saturate both IB and
PG simultaneously and likely re-trigger the outage. Mitigation:

- **Cold-restart token bucket**: on either reconnect (IB or PG),
  `msg_per_sec_cap` resets to 25% of configured ceiling, then
  additive-increases per the AIMD schedule. Workers ramp up gradually
  rather than burst.
- **Persister cold-restart batching**: COPY batch size drops from 5k
  to 500 rows for the first 60 seconds after PG reconnect. Lets PG
  re-warm caches without a huge first-COPY stalling everything.

**Watchdog vs reconnect-backoff coordination (Pass-3 finding A-6):**

The watchdog scans every 60s for in-flight reqIds past their 30s timeout.
The reconnect backoff schedule is 1s → 60s. **The watchdog MUST skip
tickers whose connection is currently in reconnect backoff** — otherwise
a momentarily-disconnected conn whose ticker's in-flight reqIds will be
re-queued by the watchdog right as reconnect succeeds, causing
duplicate work. Implementation: watchdog reads each connection's
`status` field; only force-cancels reqIds on `connected` connections.

**Cold-start universe behavior (Pass-3 finding A-2):**

On the very first run (no prior `option_universe` rows for any
`universe_date_committed = true`), the snapshot loop must idle until the
first universe refresh completes. The lifespan order:

1. Snapshotter boots.
2. Universe refresh runs synchronously at boot (in addition to the
   08:30 ET cron) IF no committed universe row exists for today OR
   yesterday.
3. Once refresh commits, snapshot loop starts pulling tickers.

**Day-1 probe bail criteria (Pass-3 finding A-2):**

If the day-1 IB behavior probe (rollout step 1) reveals:

- **< 3 cps achievable** under any line/cancel mode → HALT rollout. Spec
  must be revised. Likely root cause: per-snapshot wall time much higher
  than 11s, or line release behavior worse than tickSnapshotEnd auto-release.
- **Pacing errors at < 25 msg/sec** → revise `msg_per_sec_cap` ceiling
  downward and re-estimate effective cadence.
- **`reqSecDefOptParams` returns 0 expirations** for any of SPX/NDX/RUT/VIX
  → fail loudly; verify subscription state per Open Question #2.
- **Multiple tradingClasses NOT returned for SPX (only `SPX`, no `SPXW`)**
  → verify the underlier qualification was correct; SPXW must appear or
  weekly chain coverage is silently incomplete.

### Process supervision

- **launchd plist:** `~/Library/LaunchAgents/com.xenon.option-chain-snapshotter.plist`
- `KeepAlive = { SuccessfulExit = false }` — restart only on crash, not on
  intentional successful shutdown.
- `RunAtLoad = true`
- **`ThrottleInterval = 60`** (Pass-2 finding C-15) — minimum 60s between
  restart attempts. Prevents restart-storm when prestart fails on pending
  migrations or DB outage. Without this, `KeepAlive=true` can hammer the
  service at ~1Hz indefinitely.
- `ExitTimeOut = 30` — give the service 30s to drain the persister queue
  on SIGTERM before SIGKILL.
- `StandardOutPath = /var/log/xenon/option-chain-snapshotter.log`
- `StandardErrorPath = /var/log/xenon/option-chain-snapshotter.err`
- **Pre-start hook**: `scripts/infra/option-chain-prestart.sh` validates
  (a) migrations are at head, (b) `option_chain` DB reachable, (c) IB
  Gateway port 4001 open. On failure: exits non-zero AND writes a
  `last-prestart-failure.txt` file so the next restart can short-circuit
  if the same condition persists (avoids hammering a known-broken state).

**Single-instance guard (Pass-2 finding C-15):** PID file at
`/var/run/option-chain-snapshotter.pid` (NOT /tmp — /tmp is cleared on
reboot which loses our state for stale-PID detection). Stale-PID
validation:

1. Read PID from file.
2. If process doesn't exist (`kill -0 pid` fails) → stale, proceed.
3. If process exists, verify it's actually the snapshotter by checking
   `/proc/<pid>/comm` (Linux) or `ps -o comm= -p <pid>` (macOS) matches
   `xenon-option-chain-snapshotter`. If not → PID was reused by some other
   process → stale, proceed.
4. If alive AND command matches → real duplicate → refuse to start.
5. Additionally: hold a Postgres advisory lock
   `LOCK_KEY_OPTION_CHAIN_SNAPSHOTTER = 7343001` (per CL-1, next in the
   xenon sequence after `LOCK_KEY_VCG_CRI = 7342001`) via the existing
   `pg_try_advisory_lock` pattern in `xenon.api.services.advisory_lock`.
   Lock auto-releases on process death — definitive answer if PID-file
   check is ambiguous.

---

## Code layout

```
src/xenon/
└── option_chain_snapshotter/
    ├── __init__.py
    ├── __main__.py              # entry point, argparse, launchd target
    ├── config.py                # env / CLI flags, defaults, hardcoded 4-ticker universe
    ├── pool.py                  # IBConnectionPool (2 conns), round-robin dispatch
    ├── queue.py                 # PriorityQueue (ticker, due_at)
    ├── universe.py              # daily refresh, reqSecDefOptParams loop
    ├── snapshot_worker.py       # per-connection worker
    ├── ohlcv_worker.py          # underlying 1-min bars worker (4 indexes)
    ├── persister.py             # bounded queue + COPY batching
    ├── storage.py               # psycopg connection + table accessors
    ├── hours.py                 # market hours / holiday calendar
    └── CLAUDE.md                # module overview
```

No `watchlist_sync.py` — universe is hardcoded in seed migration
`001_initial_schema.py`:

```python
TICKERS = [
    ("SPX", 600, True, "full"),  # ticker, cadence_seconds, enabled, scope
    ("NDX", 600, True, "full"),
    ("RUT", 600, True, "full"),
    ("VIX", 600, True, "full"),
]
```

DB migrations live in a new directory (separate alembic environment from
xenon's main migrations because this DB has a different owner):

```
scripts/migrations/option_chain/
├── alembic.ini
├── env.py
└── versions/
    └── 001_initial_schema.py
```

**Migration application:**

- Run manually at deploy:
  `uv run alembic -c scripts/migrations/option_chain/alembic.ini upgrade head`
- NOT wired into `scripts/infra/dev.sh` (which only manages xenon's main DB)
  — operator-driven schema changes for this DB.
- `launchd` plist runs the migration check pre-start via a `ProgramArguments`
  wrapper script (`scripts/infra/option-chain-prestart.sh`) that fails the
  service start if migrations are pending. Snapshotter never runs against
  an out-of-date schema.

**Configuration env vars (added to `.env`):**

| Var                            | Purpose                                             | Example                                                            |
| ------------------------------ | --------------------------------------------------- | ------------------------------------------------------------------ |
| `OPTION_CHAIN_DATABASE_URL`    | Write connection for snapshotter                    | `postgresql://option_chain_writer:...@127.0.0.1:5432/option_chain` |
| `OPTION_CHAIN_IB_HOST`         | IB Gateway host (defaults to macmini's `127.0.0.1`) | `127.0.0.1`                                                        |
| `OPTION_CHAIN_IB_PORT`         | IB Gateway port (live: 4001)                        | `4001`                                                             |
| `OPTION_CHAIN_POOL_SIZE`       | Number of pool connections (default 2)              | `2`                                                                |
| `OPTION_CHAIN_LINE_CAP`        | Account-wide line ceiling (default 72)              | `72`                                                               |
| `OPTION_CHAIN_MSG_PER_SEC_CAP` | Account-wide IB message rate ceiling (default 50)   | `50`                                                               |
| `OPTION_CHAIN_LOG_LEVEL`       | Snapshotter log level                               | `INFO`                                                             |

clientIds are NOT env-controlled — they come from `CLIENT_IDS` registry in
`src/xenon/clients/ib_client.py` (per Pass-2 finding C-14). The
snapshotter calls `IBClient.connect(client_name="option_chain_snapshotter_a")`
and `..._b`, registered to ids 95 and 96 in the daemon range.

No `OPTION_WIZARD_DATABASE_URL` — universe is hardcoded; no cross-DB read.

Console entry point in `pyproject.toml`:

```toml
[project.scripts]
xenon-option-chain-snapshotter = "xenon.option_chain_snapshotter.__main__:main"
```

---

## Testing strategy

Five layers, none optional:

| #   | Layer                                                                                                                                                                                                             | Path                                                                        | CI?              |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- | ---------------- |
| 1   | Unit (no IB, no PG) — queue scheduling, backoff state machine, line-cap semaphore math, round-robin dispatch                                                                                                      | `scripts/tests/test_option_chain_snapshotter_*.py`                          | yes              |
| 2   | IB-mocked integration — `MockIB` per xenon's existing patterns; verify row shape, partial-on-timeout, round-robin dispatch, regression test for index-option snapshot path                                        | same dir                                                                    | yes              |
| 3   | Postgres integration — hypertable create, compression policy, `v_staleness` health states. Uses `pg_test_engine` fixture. TimescaleDB required in CI Postgres container                                           | same dir                                                                    | yes              |
| 4   | Paper-IB live — snapshotter against paper IB on macbook for SPX + VIX → real rows land in test DB. Catches IB API contract drift that mocks miss; specifically validates IND-secType + cash-settled European path | `scripts/tests/test_option_chain_live_paper.py`, marked `@pytest.mark.live` | manual / nightly |
| 5   | Macmini canary — 30-min warmup post-deploy, compare row counts to envelope, alert if off >2×                                                                                                                      | runbook step, not a test                                                    | n/a              |

Specific regression tests required (driven by past incident memory and
the throughput-budget finding):

- **Throughput regression test (NEW, required given the cadence/budget gap):**
  Mocked-IB integration test that simulates a known 1k-contract universe,
  runs for 60 simulated seconds, asserts observed
  `contracts_persisted / elapsed_s ≥ documented_floor`. Floor is set
  **after day-1 probe** (rollout step 1) to `0.8 × probe_p50_cps` — i.e.
  CI fails if a future change drops throughput >20% below measured
  baseline. Documented in `option_chain_snapshotter/CLAUDE.md` and updated
  whenever the probe re-runs.
- **Persister back-pressure test:** drive the persister queue to 100k
  in-flight rows by stubbing out DB writes, then assert (a) snapshot
  worker's `put` calls time out at 5s, (b) drops are counted in the metric,
  (c) `persister_drops_total > 0` is visible to the test.
- **AIMD ramp-back test (Pass-2 finding C-10):** inject a synthetic pacing
  violation against the `ResizableLimiter`, assert the cap halves
  immediately, assert it additive-increases by 1 every 30s, assert no
  permit leak (outstanding leases unaffected), assert it converges at the
  configured ceiling without oscillating.
- **conId-disabled intraday-recovery test (Pass-2 finding C-9):** insert
  `option_universe` row with `status='disabled_temp'` and
  `disabled_until=now()+90min`. Advance clock 91 min via test fixture,
  assert next snapshot attempts the conId. On second failure assert
  `status='disabled_day'`, `disabled_until=NULL`. Restart-survival check:
  recreate the process, assert disabled state persisted.
- **Universe-refresh overflow test:** stub `reqSecDefOptParams` to take
  20 min per ticker (4 × 20 min > 09:25 deadline), assert refresh aborts
  gracefully and snapshotter falls back to yesterday's universe.
- **NEW from Pass 2: SPX/SPXW tradingClass de-collision test (C-2):**
  feed `reqSecDefOptParamsAsync` mock returning both `SPX` and `SPXW`
  classes for same expiry/strike pair; assert two distinct conIds land
  in `option_universe`, assert subsequent snapshots produce two rows
  in `option_chain` rather than overwriting.
- **NEW from Pass 2: token bucket pacing test (C-4):** issue 200
  `reqMktData` calls in a tight loop against the limiter, assert
  observed message rate ≤ `msg_per_sec_cap`. No IB pacing errors raised
  by the mock.
- **NEW from Pass 2: atomic acquire / no TOCTOU test (C-11):** spawn 100
  concurrent tasks racing to `limiter.acquire()`, assert observed
  concurrent leases never exceed `cap`, no exception leaks a slot.
- **NEW from Pass 2: per-conn disconnect isolation test (C-8):**
  simulate `IB.disconnectedEvent` on conn-A; assert conn-B's workers
  keep draining the queue; assert conn-A's outstanding leases released
  back to account ledger; assert in-flight contracts re-queued at
  high priority.
- **NEW from Pass 2: tickSnapshotEnd timeout test (C-6):** mock IB to
  deliver bid/ask at 1s, modelGreeks at 9s, `tickSnapshotEnd` at 11s;
  assert row persists with all greeks set (no truncation at the old
  8s timer); assert `greeks_ts` ≈ 9s mark, not NULL.
- **NEW from Pass 2: watchdog threshold sanity (C-5):** with rolling
  p95-observed-sweep at 28 min and configured cadence at 10 min, assert
  watchdog does NOT fire on tickers at 25-30 min idle. Only fires on
  truly-stuck (in-flight reqId past 30s).
- **NEW from Pass 2: persister ACK before run status (C-7):** drive a
  scenario where snapshot fanout completes but persister has not yet
  COPY'd. Assert `snapshot_run.status` stays `running` until persister
  ACK; flips to `ok` or `partial` only after the COPY commits.
- **NEW from Pass 2: clientId registry test (C-14):** assert
  `CLIENT_IDS["option_chain_snapshotter_a"] == 95` and `..._b == 96`;
  assert connecting via `IBClient.connect(client_name=...)` resolves
  the right id. Regression-guards against ad-hoc allocation drift.
- `feedback_ib_async_in_fastapi.md` → test that `reqMktDataAsync(snapshot=True)`
  works for SPX (Index contract). Asserts the code path uses
  `reqMktDataAsync`, not `reqTickers` / `reqTickersAsync` (the latter hangs
  on index options per prior incident).
- `feedback_broker_bugs_paper_first.md` → live-paper tests gated on
  `--live` flag, never run against real-money account.
- `feedback_live_e2e_surfaces_contract_bugs.md` → layer 4 (paper-IB live)
  is mandatory before merging; mocked tests proven insufficient for IB.
- `committed_db` marker on tests that fork the snapshotter as a subprocess.

---

## Migration / rollout plan

Phased, smallest-blast-radius first.

0. **Pre-work** —
   - Install TimescaleDB extension on macmini Postgres
     (Homebrew: `brew install timescaledb` then `timescaledb-tune`),
     create `option_chain_writer` role, create DB, run initial migration
     (which seeds the 4-ticker `snapshot_config`), verify hypertables +
     compression policy. Smoke-query the empty view.
   - **Add `exchange-calendars` to `pyproject.toml`** (per Pass-2 finding
     CL-2; confirmed not currently present). Run `uv sync --frozen`.
   - **Add `LOCK_KEY_OPTION_CHAIN_SNAPSHOTTER = 7343001`** to
     `src/xenon/api/services/advisory_lock.py` (per Pass-2 finding CL-1).
   - **Register clientIds** in `src/xenon/clients/ib_client.py::CLIENT_IDS`:
     `option_chain_snapshotter_a = 95`, `option_chain_snapshotter_b = 96`.
   - Confirm OPRA + CBOE One + NDX + Russell market-data subscriptions
     in IBKR Client Portal.
1. **Day-1 IB behavior probe (NEW, blocks rollout per Pass-2 finding C-1):**
   Run a one-shot script against paper IB that:
   - Connects via the new clientIds.
   - Calls `reqContractDetails(Index('SPX','CBOE'))` and confirms IND
     underlier qualifies (per C-3).
   - Calls `reqSecDefOptParamsAsync` with `underlyingSecType='IND'` and
     the qualified conId. Confirms multiple tradingClasses returned
     for SPX (expecting SPX + SPXW).
   - Issues 50 `reqMktDataAsync(snapshot=True)` calls in a row to liquid
     SPX strikes, measures (a) time to bid/ask, (b) time to modelGreeks,
     (c) time to `tickSnapshotEnd`, (d) **whether explicit
     `cancelMktData()` releases the line before `tickSnapshotEnd`** —
     this last point determines the 18 cps vs 6.5 cps fork. Record
     p50/p95 values.
   - Issues 200 calls at peak rate to probe IB pacing limits — record
     when error 100/165 fires. Sets the `msg_per_sec_cap` ceiling for
     production.
   - If line release doesn't work as assumed, **revise the spec's
     throughput number** before proceeding.
2. **Snapshotter skeleton on VIX only** — process, pool (2 conns),
   queue, persister, but enable for **VIX only** (smallest chain,
   safest first canary). 10-min cadence. Verify 24h of clean snapshots
   end-to-end.
3. **Expand to SPX** — enable SPX; this is the throughput stress test
   (SPX is ~24k of the 33k universe). Observe effective cadence and
   `v_staleness` — confirm sweep time matches the day-1 probe
   measurement (not aspirational 10 min).
4. **Enable NDX + RUT** — full 4-ticker universe. No further surprises
   expected; same code path as SPX.
5. **launchd handoff** — install plist, kill foreground process, confirm
   launchd-managed instance is running and healthy.

Rollback: stop launchd job, drop the DB (`option_chain`), uninstall
extension. No xenon code is impacted — the snapshotter is its own service.

---

## Open questions

1. **Existing TimescaleDB on macmini?** — Not verified; needs `brew install`
   and `CREATE EXTENSION` if absent. If TimescaleDB is unavailable, fallback
   is vanilla `PARTITION BY RANGE (snapshot_ts)` with daily partitions
   (loses columnar compression — storage grows ~10×).
2. **IB index-option market-data subscriptions** — these are _distinct
   subscriptions_ from the OPRA equity-options bundle:
   - **SPX/SPXW**: CBOE One Equity (covers SPX/VIX index data + greeks).
   - **NDX**: Nasdaq Global Index Service or equivalent NDX add-on.
   - **RUT**: Russell Indexes (often bundled with CBOE One).
   - **VIX**: included with CBOE One in most tiers.
     Without each, snapshots return `error 354 - requested market data is
not subscribed` for that ticker. Confirm subscription state in
     IBKR Client Portal → Market Data Subscriptions before universe refresh
     day 1.
3. **macmini disk headroom** — confirm free space ≥20 GB before committing
   (envelope: ~10 GB/year compressed for the 4-index universe).
4. **launchd vs Docker compose** — macmini already runs xenon under Docker
   compose; should the snapshotter live there too rather than a launchd
   plist? Open for revisit during IMPL phase.
5. **`exchange-calendars` dep** — verified NOT in `pyproject.toml`
   (Pass-2 finding CL-2). Add it as part of pre-work step 0.
6. **Per-snapshot wall-time empirical measurement** — covered by the
   day-1 IB behavior probe (rollout step 1). Measures p50/p95 for bid/ask,
   modelGreeks, `tickSnapshotEnd`, and the critical question of whether
   early `cancelMktData` releases the market-data line. Throughput budget
   adjusts based on probe outcome.
7. **NEW from Pass 2: open_interest under snapshot mode (C-13)** —
   IB documentation suggests `snapshot=True` ignores the generic-tick
   list, which would leave `open_interest` permanently NULL on the
   `option_chain` table. Probe (rollout step 1) explicitly checks
   whether OI ticks arrive in snapshot mode. If they don't, options:
   (a) leave `open_interest` column nullable and accept it's mostly
   NULL, (b) add a separate slow non-snapshot OI collection job (~1×/day)
   to backfill OI per conId for end-of-day analysis. Defer (b) to v1.1.

---

## v1.1 deferrals (intentionally not in v1)

- Push alerting (Slack/email/webhook on stale tickers)
- Retention policy (drop chunks > N years)
- Per-ticker contract scoping (e.g. `atm_band_25pct` for tickers we want
  cheaper)
- Cross-DB views from xenon → option_chain for runtime consumers
- Backfill from any third-party historical chain dataset
- A consumer — IV surface viewer, backtester, etc.
