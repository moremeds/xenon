# Decision: Retire `ExitOrdersHandler`

**Date:** 2026-05-03
**Status:** Decided — retire in PG cutoff PR
**Decider:** chenxi
**Context:** PG migration clean-cutoff plan
(`docs/plans/2026-05-03-pg-migration-clean-cutoff.md`) requires deleting
`data/trade_log.json`. `ExitOrdersHandler` is the only remaining runtime
reader/writer of that file outside of `ib_sync` and the report tools.

## TL;DR

**Retire the handler.** The third feasibility tribunal flagged this as a
BLOCKER on the assumption it was actively scheduled and held production
state. Audit shows neither is true.

## Audit findings

### 1. Caller graph (verified at the file/line level)

| Site                                                    | What                                                                               | Status                                                 |
| ------------------------------------------------------- | ---------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `src/xenon/monitor_daemon/run.py:75`                    | `daemon.register(ExitOrdersHandler(ib_port=4001, client_id=71, max_gap_pct=0.40))` | Registered, but daemon must be invoked via CLI to fire |
| `src/xenon/monitor_daemon/__init__.py:16`               | Alternate registration with defaults                                               | Same — registration only                               |
| `pyproject.toml:52`                                     | Exposes `xenon-monitor-daemon` CLI entry point                                     | CLI exists                                             |
| `src/xenon/monitor_daemon/handlers/__init__.py:9,12`    | Exports `ExitOrdersHandler` from package                                           | Surface                                                |
| `scripts/tests/test_monitor_daemon/test_exit_orders.py` | 8 unit tests, mocked IB                                                            | Test surface                                           |

### 2. Production runtime: NOT scheduled

- `~/Library/LaunchAgents/*xenon*` — **no jobs found**
- `launchctl list | grep -i xenon` — empty
- `ps aux | grep xenon-monitor` — no processes running
- No nginx/cron/supervisord ref to `xenon-monitor-daemon` anywhere in repo

The CLI exists but **nobody invokes it as a long-running service**. There
is no scheduled cadence to break.

### 3. Persisted state: empty in production

`data/trade_log.json` contents (full file, 17 lines):

```json
{
  "trades": [
    {"id": 1, "ticker": "SPY", "date": "2026-01-15", "structure": "LONG_CALL",
     "entry_cost": 5.2, "exit_cost": 6.8, "pnl": 160, "realized_pnl": 160,
     "decision": "CLOSED", ...}
  ]
}
```

- **Zero** entries with `exit_orders` structure
- **Zero** entries with `status: PENDING`
- The only row is a CLOSED trade with realized P&L
- Handler reads `trade.exit_orders.{target,stop}.{status,price,contracts,contract_spec}` — that key path does not exist in any current row

There is no pending work to migrate.

### 4. Code-quality red flags

The handler would not pass current code review:

- **No AccountScope awareness** — would mix paper and live trades when both are active. Direct violation of broker scope policy (`docs/architecture/production-database-strategy.md`).
- **Hardcoded `ib_port=4001` and `client_id=71`** — bypasses the per-mode port resolution in `dev.sh` and the pool's clientId range (0–9 for pool, 20–49 for on-demand auto). 71 is outside the documented allocation in `docs/architecture/api-infrastructure.md`.
- **Bypasses the order-path allowlist** (`scripts/checks/order_path_caller_allowlist.py`) — calls `client.place_order()` directly via legacy `IBClient`, not via `xenon.execution.ib_place_order` which is the canonical placement path.
- **No `events.outbox` emission** — order placement is invisible to the activity mirror, blotter, and downstream consumers.
- **Per-order `client.sleep(2)`** in a loop over pending orders — would scale poorly if the feature ever had real volume.

A 1:1 PG port would carry all of these flaws forward and cement them in a
new `xenon.exit_orders_pending` table. That is worse than retirement.

## Options considered

### Option A — Retire (chosen)

Delete the handler and its references.

- Files removed:
  - `src/xenon/monitor_daemon/handlers/exit_orders.py`
  - `scripts/tests/test_monitor_daemon/test_exit_orders.py`
- Files modified:
  - `src/xenon/monitor_daemon/run.py` — remove import + register call + CLI list entry
  - `src/xenon/monitor_daemon/__init__.py` — remove import + register call
  - `src/xenon/monitor_daemon/handlers/__init__.py` — remove from exports + `__all__`

**Risk:** None for current production. Empty pending queue, no scheduled
runner, no AccountScope-correct callers. The CLI continues to work for
the surviving handlers (`fill_monitor`, `preset_rebalance`).

### Option B — Migrate state to a new PG table

Schema:

```sql
CREATE TABLE xenon.pending_exit_orders (
  id SERIAL PRIMARY KEY,
  trade_id INT NOT NULL,
  ticker TEXT NOT NULL,
  structure TEXT,
  order_type TEXT NOT NULL CHECK (order_type IN ('target', 'stop')),
  target_price NUMERIC NOT NULL,
  contracts INT NOT NULL,
  contract_spec JSONB NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PENDING', 'PLACED', 'CANCELLED')),
  order_id INT,
  placed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now(),
  broker TEXT NOT NULL,
  account_env TEXT NOT NULL,
  broker_account TEXT NOT NULL
);
```

**Cost:** ~1 day. Need new schema + alembic migration + queries module

- refactor handler to use AccountScope + ib_place_order + events.outbox
  emission + new test suite + backfill from trade_log.json (which is empty,
  so backfill is a no-op).

**Benefit:** Zero. The feature has no current users and no concrete
demand. Building infrastructure for hypothetical future use violates
YAGNI.

### Option C — Freeze behind a feature flag

Add `XENON_EXIT_ORDERS_HANDLER=0` default and let the handler stay but
no-op. **Rejected** — leaves dead code that has to be maintained,
type-checked, and pondered by future readers. The retire path is
cleaner.

## Decision

**Option A — retire.** Implementation lives in the PG cutoff PR alongside
the trade_log.json migration. If the auto-place-when-within-40% feature
becomes useful again, rebuild it as a Postgres-native feature with proper
AccountScope, ib_pool integration, allowlisted order placement path, and
outbox emission. Treat that as a fresh project — not a port.

## What this unblocks

The third feasibility tribunal flagged ExitOrdersHandler as a unanimous
BLOCKER assuming Option A "retire" would break scheduled work. This audit
falsifies that assumption. Plan v3 (or whatever replaces v2) can adopt
Option A safely.
