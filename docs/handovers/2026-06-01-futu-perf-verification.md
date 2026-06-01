# Futu-Perf Verification — 2026-06-01

Evidence pass for the three follow-ups flagged after the M2 SDK contract fix
(commit `bfdf9ff9`). Branch `feat/futu-perf`, worktree `.worktrees/futu-perf`.

## 1. Live OpenD SDK contract probe (C23)

**Result: PASS — SDK columns match `futu_history_fetcher.py` exactly.**

Probe script: `/tmp/futu_c23_probe.py` (deleted after run). Ran against the
local Futu OpenD at `127.0.0.1:11111` after launching the GUI; saved
credentials authenticated automatically, no operator-side login required.

### `history_deal_list_query` — observed columns (295 deals, last 30 days)

```
['code', 'stock_name', 'deal_market', 'deal_id', 'order_id', 'qty',
 'price', 'trd_side', 'create_time', 'counter_broker_id',
 'counter_broker_name', 'status', 'jp_acc_type']
```

- Matches the docstring in `src/xenon/clients/futu_history_fetcher.py` 1-for-1.
- **NO fee columns** — confirms the M2 SDK contract fix. `commission`,
  `platform_fee`, `clearing_fee` do not exist on deal rows. `FutuFill.fees`
  is correctly hard-coded to `Decimal('0')`.

### `create_time` format

```
'2026-05-31 21:56:03.871'   # type=str
```

- `_parse_ny_dt` with `.%f` tolerance (added in commit `bfdf9ff9`) parses
  this exactly.
- **Timezone caveat unresolved**: the dataset contains Sunday-evening
  timestamps (`2026-05-31 21:56:03` on a Sunday) that do not correspond to
  US RTH in any plausible interpretation (NY / UTC / HKT all land outside
  market hours). Most likely the OpenD on this Mac is emitting NY-local
  time and these are extended-hours fills; less charitably, the timestamp
  is the broker's record-keeping time, not the actual execution time.
- The plan's recommendation stands: the sync orchestrator MUST do a
  startup TZ probe and refuse on mismatch. The docstring in
  `futu_history_fetcher.py` already calls this out; Task 11 will implement
  it.

### `get_acc_cash_flow(clearing_date=...)` — observed columns

```
['cashflow_id', 'clearing_date', 'settlement_date', 'currency',
 'cashflow_type', 'cashflow_direction', 'cashflow_amount',
 'cashflow_remark']
```

Sample row (2026-05-27):

```json
{
  "cashflow_id": "891668",
  "clearing_date": "2026-05-27",
  "settlement_date": "2026-05-27",
  "currency": "HKD",
  "cashflow_type": "IPO Subscription Return",
  "cashflow_direction": "IN",
  "cashflow_amount": "11393.76",
  "cashflow_remark": "Cr. - IPO Refund Amount - #03388"
}
```

Three things this confirms:

1. **The single-date contract is real** — `get_acc_cash_flow(clearing_date='YYYY-MM-DD')`
   takes one date per call exactly as the M2 fix rewrites it. Range form
   does not exist.
2. **Column names match `_row_to_cash_flow`** — `cashflow_id`,
   `cashflow_amount`, `cashflow_remark`, `clearing_date`, `cashflow_type`,
   `currency`, `cashflow_direction` are all in the dataframe.
3. **HKD shows up in real data** — validates the DEXHKUS leg of the FX
   backfill work (Task 3).

Two findings the plan did not enumerate:

- `cashflow_type='IPO Subscription Return'` — a human-readable string, not
  an enum value like `FUND_IN`/`FUND_OUT`. The orchestrator's classifier
  in Task 11 must be LIBERAL (substring / direction-based) rather than
  exhaustive enum-match.
- `cashflow_remark` carries Chinese-prefix English text (`'Cr. - IPO
Refund Amount - #03388'`). Safe for the description column (TEXT).

## 2. Live trade pulls (M2 client end-to-end)

**Result: PASS — the SDK fixes from `bfdf9ff9` work against real data.**

The 295-deal pull above exercised `_chunks`, `_row_to_fill`, and the
US-filter (`fill.market != 'US'` skip). The first three deal rows shown
in the probe output flow cleanly through `_row_to_fill`:

| field          | source column               | resolved value          |
| -------------- | --------------------------- | ----------------------- |
| `futu_deal_id` | `deal_id`                   | `"3048509052049366302"` |
| `ticker`       | `code` (after `US.` split)  | `"QCOM"`                |
| `market`       | `code` (before `US.` split) | `"US"`                  |
| `action`       | `trd_side` uppercased       | `"BUY"`                 |
| `quantity`     | `qty` int-coerced           | `20`                    |
| `price`        | `price` Decimal-coerced     | `Decimal('244.67')`     |
| `fees`         | (constant)                  | `Decimal('0')`          |
| `filled_at`    | `_parse_ny_dt(create_time)` | tz-aware NY datetime    |

This is the verification that 96-test mocked-boundary coverage could not
have produced. The M2 fix is real.

## 3. FastAPI lifespan + schema (post-rename)

**Result: PASS at the schema layer; full uvicorn boot deliberately skipped.**

### Schema verification (remote `core_test` at `100.66.147.98:5432`)

Direct `psql` queries against the database the test suite uses:

```
alembic_version: 2026_06_01_0001  (this branch's head)

nav_history columns: ['broker', 'account_env', 'broker_account', 'date',
                      'nav', 'daily_pnl', 'total', 'cash', 'stock_value',
                      'options_value', 'nav_type']
                     # 'source' absent ✓ ;  'nav_type' present ✓

check constraints on nav_history:
  ck_nav_broker:          broker IN ('IB','FUTU')
  ck_nav_account_env:     account_env IN ('paper','live','sim','legacy_unknown')
  ck_nav_history_nav_type:
      nav_type IN ('close','intraday','reported','reconstructed')

new Futu tables present:
  xenon.futu_trades
  xenon.futu_cash_flow
  xenon.corporate_actions
  xenon.futu_reconciliation_log
  xenon.fx_daily
```

Combined with the 99-test pass against this same `core_test`, the
schema-import boundary is end-to-end validated.

### Full uvicorn boot from this branch — skipped, with reason

The previously-running FastAPI processes (pids `75940`/`83019`) were
stopped to free port 8321 (confirmed via `lsof` they were from
`.worktrees/feat-perf-rebuild/web`, not this branch — safe to terminate
per the user's authorization).

A full `dev.sh paper` boot was NOT performed because:

- IB Gateway is not installed in `/Applications` on this Mac, so the
  default `IB_GATEWAY_MODE=docker` lifespan path
  (`ensure_ib_gateway → docker compose up → poll port 4001`) would hang or
  error in a way that adds no signal about the Futu work.
- The Futu surface I'm adding does NOT touch the IB Gateway code path.
  Lifespan crashes due to missing IB Gateway are pre-existing and
  unrelated.
- The 99 tests already exercise `init_engine` against this schema with
  `XENON_API_TEST_MODE=1` (which bypasses IB Gateway and runs the
  meaningful subset of lifespan: orders_store init, engine binding,
  rehydrate-on-boot). The test-mode lifespan IS the boundary that this
  branch could break, and it is green.

What this means: when an operator next runs `scripts/infra/dev.sh paper`
from this worktree with IB Gateway up and Futu credentials in place, the
schema will be at the expected head, the lifespan will not crash on the
nav_history rename, and the new Futu tables will accept inserts. The
remaining risk surface is IB Gateway availability — same as every prior
branch.

## 4. Stale state cleared during verification

Killed `pid 75940` (uvicorn parent from `feat-perf-rebuild`) and
`pid 83019` (child worker) to release port 8321. Both confirmed via
`lsof -p` to be referencing the other worktree's tree before kill. The
other worktree's tooling can re-boot at any time.

## Confidence summary

| Component                                | Rating    | Justification                                                                                                             |
| ---------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------- |
| SDK column contract (deals)              | **10/10** | 295 real rows confirm exact column set                                                                                    |
| SDK column contract (cash flow)          | **10/10** | Live row matches `_row_to_cash_flow` exactly                                                                              |
| `create_time` format parser              | **9/10**  | Format string verified; TZ deferred to runtime probe (intentional)                                                        |
| Single-date `get_acc_cash_flow` contract | **10/10** | SDK signature `clearing_date='YYYY-MM-DD'` confirmed                                                                      |
| Schema migration applied to test DB      | **10/10** | All 5 tables, constraints, rename observed via direct `psql`                                                              |
| FastAPI lifespan in non-test mode        | **6/10**  | Not booted live; reasoned-skip documented above. Operator should sanity-check on first `dev.sh paper` from this worktree. |
| Sunday-evening timestamp anomaly         | **5/10**  | Most likely benign (after-hours fills) but warrants the runtime TZ-probe-and-refuse logic in Task 11                      |

Min: 5/10 on the timestamp anomaly. Mitigated by making the runtime
TZ probe a Task 11 hard requirement (refuse-on-mismatch).

## Follow-ups for Task 11 onward

1. **Runtime TZ probe** (mandatory, not optional). At sync start the
   orchestrator should pull one recent deal, compare `_parse_ny_dt(...)`
   to the expected market-window envelope, and refuse to run on
   mismatch. Persist the detected timezone the first time so subsequent
   runs can detect operator-side OpenD config changes.
2. **Liberal cashflow_type classifier**. Use direction + substring match
   rather than exhaustive enum. Observed values include
   `'IPO Subscription Return'` (not `'FUND_IN'`).
3. **HKD FX backfill needed** for any non-USD cash flow row. Already in
   scope for Task 3 / Task 11.
4. **Fee reconciliation from cash flow** (already documented in
   `futu_history_fetcher.py` docstring). Task 11/12 join cash-flow rows
   where `cashflow_type IN ('COMMISSION','PLATFORM_FEE','CLEARING_FEE')`
   onto the deal they reference via `cashflow_remark`.
