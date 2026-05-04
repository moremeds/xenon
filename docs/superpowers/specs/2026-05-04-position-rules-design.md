# Position Rules Engine — Auto-Bracket Protective Orders (v1)

**Date:** 2026-05-04
**Status:** Design — pending implementation plan
**Branch:** `feature/position-rules-design-spec`
**Related backlog:** Item #1 (Rule-based portfolio management), item #2 (Order types — Trailing stop-loss / profit-take), Inbox 2026-05-04 (Bracket-rule backtest / markout simulator — HIGH PRIORITY)

---

## 1. Problem & motivation

Every position Xenon trades through Interactive Brokers should have, by default, an associated stop-loss order and a trailing take-profit order — automatically attached, broker-side where supported, otherwise enforced by an in-process monitor. Triggers fire as MKT orders against a hard threshold ("rigid red line"). This is a **system-level rule**, not a per-strategy signal.

The user has lost meaningful money on **credit spreads** (short bull put, short bear call) that drifted into max loss without an enforced exit. Long calls that pulled back hard never recovered because long-call delta drops as the underlying drops — the option price becomes structurally insensitive to a bounce. Both cases would have been bounded by a mechanical, no-rationalization stop.

This document specifies the v1 rule engine that fixes that, designed so future rules ("close credit combo before 2-week expiry", "close long premium when IV crushes after earnings", "alert on size drift past cap") plug into the same framework without architectural change.

## 2. Non-goals (v1)

| #   | Non-goal                                      | Reason                                                                                               |
| --- | --------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| 1   | Per-fill override at order entry              | Defaults from `bracket_policies` apply uniformly; revisit after the markout simulator validates them |
| 2   | Web UI for editing defaults                   | Tune via `psql UPDATE` until simulator data exists                                                   |
| 3   | Slack / Discord / email / SMS notifications   | macOS toast only; single-operator system                                                             |
| 4   | Futu writes                                   | `FutuExecutor.flatten_mkt` raises `SyntheticOnly`; activates when `xenon-futu-place-order` CLI ships |
| 5   | Markout simulator over historical trades      | High-priority backlog; depends on intraday option-bar backfill + combo historical reconstruction     |
| 6   | Partial-position rebalancing                  | Partial closes are treated as full close → `CANCELED`; user re-arms manually                         |
| 7   | Exotic structure auto-arm                     | Jade lizards, ratios, calendars, condors → `UNCLASSIFIED` → operator-handled                         |
| 8   | Covered-call auto-arm                         | Closing the stock leg alone trips Gate-4; needs atomic combo close (v2)                              |
| 9   | STP-LMT instead of STP-MKT                    | User explicit: rigid red line = MKT; STP-LMT defeats the purpose                                     |
| 10  | `outsideRth=True` on native brackets          | Default RTH-only; pre/post-market liquidity is unsafe for tight stops                                |
| 11  | Per-rule_kind polling intervals               | Single 30s tick for all rule_kinds in v1                                                             |
| 12  | Laddered TPs                                  | One SL + one TP per position; `ib_insync` chokes on multiple TPs in a bracket                        |
| 13  | Delta-aware / IV-aware option stops           | Hard % on premium; convention; simulator decides v2 refinement                                       |
| 14  | Auto-roll instead of close                    | Rolling is strategic, not defensive                                                                  |
| 15  | Additional rule_kinds beyond the four shipped | Plug-in interface is open; v2+ rule_kinds ship additively                                            |

## 3. User-facing behavior

### 3.1 Default policy table (seeded into `bracket_policies`)

| Asset class                                                        | Hard SL                                                                                                                                                                                                              | Take-profit                                               |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| **Stocks**                                                         | −8% from entry, MKT at trigger                                                                                                                                                                                       | 5% trail off MFE, activates immediately                   |
| **Long single-name options**                                       | **−20% from entry** (rigid red line), MKT at trigger                                                                                                                                                                 | 25% trail off MFE, activates at +30%                      |
| **Multi-leg debit combos**                                         | 50% of max-loss (synthetic monitor), MKT at trigger                                                                                                                                                                  | 25% trail off MFE in P&L $, activates at +25% of max-gain |
| **Credit spreads** (bull put short / bear call short)              | **either trigger, whichever first**: ① spread debit-to-close ≥ 2× credit received, ② underlying touches/breaches the short strike                                                                                    | **fixed close at 50% of max credit** (no trail in v1)     |
| **Covered calls**                                                  | n/a — classifier returns `COVERED_CALL` → arm_hook short-circuits with operator notification; no `position_protection` row is inserted, `arm()` is never called. v2 enables auto-arm once atomic combo-close exists. | n/a                                                       |
| **Unclassified** (ratio, jade lizard, calendar, condor, butterfly) | n/a — operator notified at fill, no auto-arm                                                                                                                                                                         | n/a                                                       |

These are _defaults_. The seed inserts wildcard rows (broker / env / account = NULL); per-account overrides land as more-specific rows when the user wants them. Defaults are tunable via `psql UPDATE bracket_policies` — UI later.

### 3.2 Backfill behavior — the "don't disturb existing positions" guarantee

- **New fills going forward** auto-arm via the post-fill hook (§6).
- **Existing positions** require the operator to run `xenon-position-rules sweep` explicitly. The CLI defaults to `--dry-run`; the user must add `--apply` to actually create rows. Optional `--interactive` prompts per position. The sweep is the only operator action that touches existing book on day 1 of v1.

### 3.3 Auto-place vs alert-only

The `bracket_policies.auto_place` boolean controls trigger behavior:

- `auto_place = TRUE` (v1 default for all new rule_kinds) → MKT-flatten on trigger.
- `auto_place = FALSE` → emit alert + macOS notification, state stays `ARMED`. Matches the existing `combo_tp_alert` semantics from the wizard pipeline.

**Alert-only debounce.** A row in `ARMED` whose threshold remains breached would otherwise re-fire alerts every 30s tick. v1 debounces with `min_realert_interval_s` (default `3600` = 1 hour) tracked in `state_data.last_alert_at`. Edge-triggered: alert fires the first tick the threshold crosses; suppressed while breach is sustained until either the interval expires or the threshold un-crosses (which resets `last_alert_at` to NULL so the next crossing re-fires). The row stays `ARMED` until operator close or external position-flat detection moves it to `CANCELED`.

This makes the new system orthogonal to the existing combo-wizard Risk Alert flow (spec §9.2). Wizard guards the entry decision pre-fill; this engine guards the position state post-fill. Different lifecycle, different store, no conflict.

## 4. Architecture

### 4.1 Reuse audit summary

The codebase already has 80% of the abstraction this work needs. Existing assets used:

| Component                            | Location                                                   | Reuse                                                                                                                                                                |
| ------------------------------------ | ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MonitorDaemon` orchestrator         | `src/xenon/monitor_daemon/daemon.py`                       | Hosts the new handler                                                                                                                                                |
| `BaseHandler` ABC                    | `src/xenon/monitor_daemon/handlers/base.py`                | Parent class for `PositionRulesHandler`                                                                                                                              |
| `wizard_stop_monitor` handler        | `src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py` | **Replaced** by `rules/combo_tp_alert.py` in Migration B (file deleted). Crossing-detection logic (`_crossed`, notify path) is lifted verbatim into the rule module. |
| `combo_wizard/protect.py`            | `src/xenon/execution/combo_wizard/`                        | Reference for retries, Gate-4, signed pricing; rewires INSERT to `position_protection` with `rule_kind='combo_tp_alert'` in Migration B                              |
| `wizard_protection` table            | `src/xenon/db/schema.py`                                   | Renamed and reshaped to `position_protection` (Migration B); empty per user confirmation                                                                             |
| `orders_store.record_fill`           | `src/xenon/execution/orders_store.py`                      | Single integration point for the post-fill arming hook                                                                                                               |
| `xenon-ib-place-order` CLI           | `src/xenon/execution/ib_place_order.py`                    | Subprocess write path for native bracket arming + MKT-flatten                                                                                                        |
| `xenon-ib-order-manage` CLI          | `src/xenon/execution/ib_order_manage.py`                   | Subprocess for cancel during disarm                                                                                                                                  |
| `account_scope.py`                   | `src/xenon/execution/account_scope.py`                     | Routes broker/env/account; selects which CLI to invoke                                                                                                               |
| `IBClient` / `FutuClient` singletons | `src/xenon/clients/`                                       | In-process read path for marks, spot, position state                                                                                                                 |
| `events.outbox` + LISTEN/NOTIFY      | `src/xenon/db/events.py`                                   | Audit log + live UI updates                                                                                                                                          |
| Postgres advisory lock pattern       | `src/xenon/api/services/advisory_lock.py`                  | Daemon singleton enforcement                                                                                                                                         |

### 4.2 New code (delta)

```
src/xenon/execution/brackets/                    NEW
├── __init__.py
├── policies.py            ~100 LOC — defaults resolver, asset-class detection
├── triggers.py            ~80  LOC — pure trigger-evaluation functions
├── position_key.py        ~60  LOC — opaque deterministic key encoding
├── arm_hook.py            ~80  LOC — called from orders_store.record_fill
└── rules/
    ├── base.py            ~40  LOC — RuleEvaluator Protocol
    ├── stop_loss.py       ~120 LOC
    ├── trailing_tp.py     ~140 LOC
    ├── take_profit_fixed.py ~80 LOC
    └── combo_tp_alert.py  ~80  LOC — lifts crossing-detection from wizard_stop_monitor (deleted in Migration B)

src/xenon/monitor_daemon/handlers/               EXTEND + DELETE
├── position_rules.py      ~150 LOC — PositionRulesHandler(BaseHandler)
└── wizard_stop_monitor.py    DELETE — logic absorbed into rules/combo_tp_alert.py

src/xenon/db/                                    EXTEND
├── schema.py              add position_protection + bracket_policies
├── queries/position_protection.py  NEW          ~120 LOC
├── queries/bracket_policies.py     NEW          ~60  LOC
└── migrations/versions/                         NEW alembic revision

src/xenon/execution/orders_store.py              EXTEND
                            +1 callback at record_fill()

scripts/checks/                                  EXTEND
└── frozen_config_at_arm.py NEW                  ~40 LOC — CI guard

CLI entry point                                  NEW
└── xenon-position-rules    list / show / cancel / sweep / health
```

Approximately **1100 new LOC + 1 alembic migration + 1 CI guard**. Migration B deletes the empty `wizard_protection` table and `src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py` (its `_crossed` + notify logic moves into `rules/combo_tp_alert.py`; the handler itself is replaced by the new `PositionRulesHandler` for all rule_kinds including `combo_tp_alert`).

### 4.3 Layered architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ POLICY LAYER  — broker-agnostic                                  │
│   bracket_policies (Postgres) + spec resolver                    │
│   "given asset_class + scope, compute default rule specs"        │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│ LIFECYCLE LAYER  — broker-agnostic                               │
│   position_protection (Postgres) — one row per (position, rule)  │
│   events.outbox (existing) — audit log + LISTEN/NOTIFY           │
│   FSM: PENDING_ARM → ARMED → TRIGGERED → CLOSED / CANCELED       │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│ MONITOR LAYER  — broker-agnostic, CANONICAL TRIGGER              │
│   PositionRulesHandler(BaseHandler) inside MonitorDaemon         │
│   Polls ARMED rows every 30s; evaluates triggers via rule plugins│
│   Source of truth — fires regardless of native bracket presence  │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│ EXECUTOR LAYER  — broker-specific (only branch on broker here)   │
│   Reads:  in-process via IBClient / FutuClient singletons        │
│   Writes: subprocess to existing CLIs                            │
│            xenon-ib-place-order  /  xenon-ib-order-manage        │
│            (Futu equivalent when available)                      │
│   No new BrokerExecutor abstract class — the CLI/JSON contract   │
│   is the abstraction; account_scope.broker picks which CLI       │
└──────────────────────────────────────────────────────────────────┘
```

### 4.4 Broker symmetry

The user requirement is that policy/lifecycle/monitor are shared between IB and Futu; only the order-placement endpoint differs. This is satisfied by:

- The handler reads `account_scope.broker` exactly once per position (when constructing the executor invocation).
- `if scope.broker == 'IB'` shells `xenon-ib-place-order`; `elif scope.broker == 'FUTU'` shells `xenon-futu-place-order` (when that CLI lands).
- No `if broker == ...` branches anywhere in policy, lifecycle, or trigger code.

When Futu gets a write CLI, the handler gains one `elif` line in one method; nothing else changes.

**Honest caveat (codex review N-S4): the broker-symmetry claim has hidden read-side asymmetry.** Per `src/xenon/CLAUDE.md`, the current `FutuClient` is **read-only and does not subscribe to market data**: "Futu (read-only) — positions snapshot from local Futu OpenD. Never write, never subscribe to market data." The synthetic monitor's design is mark-driven — it needs a quote stream to evaluate triggers. So even after Futu gets a write CLI, the synthetic trigger path can't run for Futu positions until either (a) Futu also exposes quote data through OpenD, or (b) we accept that Futu protection is _native-bracket-only_ (when Futu's API exposes brackets) with no synthetic safety net.

Two implications for v1:

1. The `BrokerExecutor`-style abstraction we declined (§4.1, last bullet) genuinely doesn't exist; the asymmetry is real, not just unwritten. We acknowledge this rather than pretend.
2. v1 spec'ing **Futu writes raise `SyntheticOnly`** is correct; the path to enabling Futu auto-arm is separate from the IB v1 work. When that future work happens, we'll either spec a `MarketDataProvider` interface alongside the executor split, or accept Futu = native-only.

This caveat does not block v1 (which is IB-only), but it does revise the "minor `elif` line" claim above — getting Futu to full parity is a larger lift than v1 lets on.

## 5. Data model

### 5.1 `xenon.position_protection` (replaces `wizard_protection`)

| Column                 | Type                                  | Notes                                                                                       |
| ---------------------- | ------------------------------------- | ------------------------------------------------------------------------------------------- |
| `protection_id`        | BigInt PK autoincrement               |                                                                                             |
| `broker`               | Text NOT NULL                         | CheckConstraint `('IB','FUTU')`                                                             |
| `account_env`          | Text NOT NULL                         | CheckConstraint `('paper','live','sim','legacy_unknown')`                                   |
| `broker_account`       | Text NOT NULL                         |                                                                                             |
| `position_key`         | Text NOT NULL                         | Opaque deterministic; see §5.3                                                              |
| `position_descriptor`  | JSONB NOT NULL                        | Human-readable shape for debug + UI                                                         |
| `asset_class`          | Text NOT NULL                         | `stock` / `long_option` / `debit_combo` / `credit_spread` / `covered_call` / `unclassified` |
| `rule_kind`            | Text NOT NULL                         | `stop_loss` / `trailing_tp` / `take_profit_fixed` / `combo_tp_alert`                        |
| `state`                | Text NOT NULL default `PENDING_ARM`   | FSM (§7)                                                                                    |
| `config`               | JSONB NOT NULL                        | Frozen at row insert (PENDING_ARM); never re-read from `bracket_policies` after that        |
| `state_data`           | JSONB NOT NULL default `{}`           | Runtime: MFE, last quote, retry counts                                                      |
| `native_order_perm_id` | BigInt nullable                       | IB perm_id of broker-side STP/LMT if armed                                                  |
| `native_order_state`   | Text nullable                         | Last-known broker-side status                                                               |
| `armed_at`             | TIMESTAMPTZ nullable                  |                                                                                             |
| `triggered_at`         | TIMESTAMPTZ nullable                  |                                                                                             |
| `closed_at`            | TIMESTAMPTZ nullable                  |                                                                                             |
| `last_evaluated_at`    | TIMESTAMPTZ nullable                  |                                                                                             |
| `created_at`           | TIMESTAMPTZ NOT NULL default `tz_now` |                                                                                             |
| `updated_at`           | TIMESTAMPTZ NOT NULL default `tz_now` | Auto-updated via trigger or per-write                                                       |

**Constraints / indexes:**

- **Partial unique index** on `(broker, account_env, broker_account, position_key, rule_kind) WHERE state IN ('PENDING_ARM','ARMED','TRIGGERED')` — at most one active row per (position, rule). Terminal rows (`CLOSED`, `CANCELED`, `FAILED`, `SUPERSEDED`) do **not** participate in the constraint, so a position can be closed and re-opened without the old terminal rows blocking new inserts. (Codex review N-S2 closed a previously-deferred decision; see §18 R8.) Policy changes generate a new row in `PENDING_ARM` and the prior active row transitions to `SUPERSEDED` via app logic — no conflict because `SUPERSEDED` is terminal.
- Partial index on `(state, broker, account_env, broker_account) WHERE state IN ('PENDING_ARM','ARMED')` — handler hot path.
- Index on `(broker, account_env, broker_account, position_key)` — "rules guarding this position?" lookup.
- CheckConstraints on `broker`, `account_env`, `state` enums.

### 5.2 `xenon.bracket_policies` (NEW — defaults seed)

| Column           | Type                                  | Notes                                                                       |
| ---------------- | ------------------------------------- | --------------------------------------------------------------------------- |
| `policy_id`      | BigInt PK autoincrement               |                                                                             |
| `broker`         | Text nullable                         | NULL = wildcard                                                             |
| `account_env`    | Text nullable                         | NULL = wildcard                                                             |
| `broker_account` | Text nullable                         | NULL = wildcard                                                             |
| `asset_class`    | Text NOT NULL                         |                                                                             |
| `rule_kind`      | Text NOT NULL                         |                                                                             |
| `enabled`        | Bool NOT NULL default TRUE            | Kill switch                                                                 |
| `auto_place`     | Bool NOT NULL default TRUE            | TRUE = MKT-flatten on trigger; FALSE = alert-only                           |
| `config`         | JSONB NOT NULL                        | Default config copied into the new `position_protection` row at insert time |
| `created_at`     | TIMESTAMPTZ NOT NULL default `tz_now` |                                                                             |
| `updated_at`     | TIMESTAMPTZ NOT NULL default `tz_now` |                                                                             |

**Constraint:** `UNIQUE(broker, account_env, broker_account, asset_class, rule_kind)` — handle NULL collations via `COALESCE(...,'*')` expression index.

**Resolution:** most-specific-wins. Specificity ranks `broker_account > account_env > broker` (an account-specific override should beat a broker-wide override that leaves account NULL). Lookup via:

```sql
SELECT rule_kind, enabled, auto_place, config
FROM xenon.bracket_policies
WHERE asset_class = $1
  AND (broker IS NULL OR broker = $2)
  AND (account_env IS NULL OR account_env = $3)
  AND (broker_account IS NULL OR broker_account = $4)
ORDER BY
  -- weighted specificity score: broker_account most specific, then env, then broker
  (CASE WHEN broker_account IS NOT NULL THEN 4 ELSE 0 END
 + CASE WHEN account_env    IS NOT NULL THEN 2 ELSE 0 END
 + CASE WHEN broker         IS NOT NULL THEN 1 ELSE 0 END) DESC,
  policy_id ASC      -- stable tiebreak for rows of equal specificity
```

Then deduplicate by `rule_kind` in Python (first row wins), filter `enabled=TRUE`.

The naive `(broker IS NOT NULL)::int DESC, (account_env IS NOT NULL)::int DESC, (broker_account IS NOT NULL)::int DESC` ordering is **wrong** — it sorts lexicographically by NOT-NULL flags rather than by specificity, so a `(broker='IB', env=NULL, account=NULL)` broker-wide override ranks above `(broker=NULL, env=NULL, account='U1234')` account-specific override. The weighted-score ORDER BY above gives correct precedence.

### 5.3 `position_key` encoding

Opaque, deterministic. Computed by `xenon.execution.brackets.position_key.compute(scope, fill_record_or_descriptor)`:

```
stock         → "STK::{symbol}"
long_option   → "OPT::{symbol}::{expiry}::{strike}::{right}"
covered_call  → "CC::{symbol}::{call_expiry}::{call_strike}"
debit_combo   → "COMBO::{sha256(canonical legs)}::{first_symbol}"
credit_spread → "CS::{symbol}::{short_expiry}::{short_strike}::{long_strike}::{right}"
```

Canonical leg ordering (for hashes): sort by `(right, strike, expiry, action)`. Prevents key drift from leg-order permutations.

The `position_descriptor` JSONB carries the full leg list — combos hash one-way, but the descriptor preserves reversibility for debug and for the future markout simulator.

### 5.4 v1 seed for `bracket_policies` (8 rows)

```sql
-- All wildcards on (broker, account_env, broker_account)
INSERT INTO xenon.bracket_policies (asset_class, rule_kind, auto_place, config) VALUES
  ('stock',          'stop_loss',          TRUE, '{"threshold_pct": -0.08, "anchor": "entry_price"}'),
  ('stock',          'trailing_tp',        TRUE, '{"trail_pct": 0.05, "activation_pct": 0.0, "anchor": "mfe"}'),
  ('long_option',    'stop_loss',          TRUE, '{"threshold_pct": -0.20, "anchor": "entry_price"}'),
  ('long_option',    'trailing_tp',        TRUE, '{"trail_pct": 0.25, "activation_pct": 0.30, "anchor": "mfe"}'),
  ('debit_combo',    'stop_loss',          TRUE, '{"threshold_pct_of_max_loss": 0.50, "anchor": "synthetic_mark"}'),
  ('debit_combo',    'trailing_tp',        TRUE, '{"trail_pct": 0.25, "activation_pct_of_max_gain": 0.25, "anchor": "mfe_pnl_dollars"}'),
  ('credit_spread',  'stop_loss',          TRUE, '{"trigger_kind": "either", "mark_multiple_of_credit": 2.0, "underlying_breach_short_strike": true, "anchor": "synthetic_mark"}'),
  ('credit_spread',  'take_profit_fixed', TRUE, '{"close_at_credit_pct": 0.50, "anchor": "synthetic_mark"}');
-- NOTE: no covered_call rows in v1. Classifier returns COVERED_CALL; policy resolution finds
-- zero matching rows → arm_hook treats this exactly like UNCLASSIFIED (operator-notify, no insert).
-- Covered-call auto-arm returns in v2 once atomic combo-close is wired.
```

Note: `combo_tp_alert` is not in the seed because it's set per-session by the existing `combo_wizard/protect.py` pipeline, with `auto_place = FALSE` to preserve the wizard's existing alert-only semantics.

### 5.5 JSONB schemas + CHECK constraints

Two JSONB columns (`position_descriptor`, `config`, `state_data`) are otherwise schemaless — easy to typo, hard to debug. v1 ships explicit Pydantic models per shape and a CI guard validating seed rows + a Postgres CHECK on rule_kind enum.

**`position_descriptor` shape** (frozen):

```jsonc
{
  "asset_class": "credit_spread",
  "opened_at": "2026-05-04T14:23:11Z",
  "opener_user_id": "user_2A...",
  "source": "fastapi_orders_place" | "combo_wizard" | "sweep_cli" | "reconcile_discovered",
  "first_fill_id": 9876,
  "anchor_price": 1.42,                         // see §6.4 — frozen entry-price anchor
  "anchor_currency": "USD",

  // Quantity model (§N-S3) — frozen at insert; never re-derived.
  "opened_qty": 3,            // number of units (shares for stock, contracts for option, spreads for combo)
  "protected_qty": 3,         // qty the protective close should target — equals opened_qty in v1
  "multiplier": 100,          // contract multiplier for options/combos (1 for stocks)
  "qty_unit": "spread",       // 'share' | 'contract' | 'spread'

  "legs": [
    { "sec_type": "OPT", "symbol": "GOOG", "expiry": "20260417",
      "strike": 315.0, "right": "C", "action": "BUY",
      "ratio": 1, "fill_price": 5.20, "con_id": 123456789 }
  ]
}
```

**Quantity rule for the close (§10.5 'MKT, full size' clarified):** at trigger time, the rule reads a fresh `IBClient.positions()` snapshot for the position_key. The actual MKT close size is `min(protected_qty, current_broker_qty)` and is submitted with `tif='DAY'` and the IB `OutsideRTH=False` flag plus the **`Order.firmQuoteOnly` is irrelevant; we use a reduce-only equivalent:** the close direction is deterministic (opposite of opening action) and quantity is bounded by the live position. If `current_broker_qty == 0` (position already flat), the row reconciles to `CANCELED` with no order submission. If `current_broker_qty < protected_qty` (partial manual close happened), v1 closes whatever's left and transitions to `CLOSED`; the size mismatch is logged as `state_data.partial_position_at_close=true` for the operator review.

**`config` shape** is rule_kind-specific. Each `rules/{kind}.py` exports a Pydantic `ConfigModel`. The `bracket_policies` queries module validates inserts against these models — typo'd seed rows fail at insert, not at handler eval.

**Postgres-level CHECK constraints (v1):**

- `position_protection.rule_kind IN ('stop_loss','trailing_tp','take_profit_fixed','combo_tp_alert')`
- `position_protection.asset_class IN ('stock','long_option','debit_combo','credit_spread','covered_call','unclassified')`
- `bracket_policies.rule_kind` and `bracket_policies.asset_class` — same enum values

The CHECK constraints are intentionally narrow — adding a v2 rule_kind requires an Alembic migration, which is the right level of friction for "we're growing the engine."

### 5.6 `xenon.position_close_claims` (NEW — duplicate-close prevention)

Codex review (N-C1, N-C2, N-C3) surfaced three real-money hazards:

- **N-C1 — duplicate close from native + synthetic.** `stop_loss.arm()` may attach a broker-side STP. The synthetic monitor also evaluates the threshold every tick. If the native fills first and the synthetic observes the same threshold breach in the same tick window before broker state propagates, the daemon submits a _second_ MKT — flattening then reopening short.
- **N-C2 — duplicate close from two rules.** `stop_loss` and `trailing_tp` are different `position_protection` rows with different `protection_id`s. Per-row CAS does not coordinate them. Both can transition `ARMED → TRIGGERED` from the same active-row snapshot and both submit MKTs.
- **N-C3 — non-idempotent retry.** §10.2 retries `xenon-ib-place-order` on subprocess error. If the first attempt's order was accepted by IB but the subprocess timed out before returning a parseable `perm_id`, the retry submits another MKT.

**Single fix that closes all three:** a position-level claim table. Any flattening attempt — synthetic or native-reconciliation — must successfully claim the position before submitting a close order. Claim is deterministic and survives crashes.

| Column                                    | Type                                  | Notes                                                                        |
| ----------------------------------------- | ------------------------------------- | ---------------------------------------------------------------------------- |
| `claim_id`                                | BigInt PK autoincrement               | Generates the deterministic `orderRef`                                       |
| `broker`, `account_env`, `broker_account` | Text NOT NULL                         | Standard scope; CheckConstraints                                             |
| `position_key`                            | Text NOT NULL                         | Same shape as `position_protection.position_key`                             |
| `claimed_by_protection_id`                | BigInt NOT NULL                       | The `position_protection` row that owns this claim (for audit)               |
| `claim_kind`                              | Text NOT NULL                         | `synthetic_close` \| `native_reconcile_close`                                |
| `status`                                  | Text NOT NULL default `PENDING`       | `PENDING` → `SUBMITTED` → `FILLED` \| `FAILED` \| `ABANDONED`                |
| `order_ref`                               | Text NOT NULL                         | `f"xenon-pr-{claim_id}"` — used as IB `Order.orderRef` for retry idempotency |
| `broker_perm_id`                          | BigInt nullable                       | Set after IB returns perm_id                                                 |
| `attempts`                                | Int NOT NULL default 0                | Subprocess retry counter                                                     |
| `claimed_at`                              | TIMESTAMPTZ NOT NULL default `tz_now` |                                                                              |
| `submitted_at`                            | TIMESTAMPTZ nullable                  | Set when subprocess succeeded                                                |
| `terminal_at`                             | TIMESTAMPTZ nullable                  | Set when status moves to FILLED/FAILED/ABANDONED                             |
| `last_error`                              | Text nullable                         |                                                                              |

**Constraints / indexes:**

- `UNIQUE (broker, account_env, broker_account, position_key) WHERE status IN ('PENDING','SUBMITTED')` — partial unique. At most one in-flight claim per position; terminal claims (`FILLED`/`FAILED`/`ABANDONED`) coexist in the audit history.
- `UNIQUE (order_ref)` — full unique; `order_ref` is the deterministic broker-side handle.
- Index on `(broker, account_env, broker_account, status)` — daemon's claim cleanup hot path.

**Close protocol (replaces the old "submit MKT directly" pattern in §8/§10.2):**

```
1. INSERT INTO position_close_claims
       (..., position_key, claimed_by_protection_id, claim_kind='synthetic_close',
        status='PENDING')
   ON CONFLICT (broker, env, account, position_key) WHERE status IN ('PENDING','SUBMITTED')
   DO NOTHING
   RETURNING claim_id;

2. If RETURNING is empty:
       another close is already in flight for this position — abandon.
       The position_protection row transitions to SUPERSEDED (the other claim wins).
       Reconciliation will mark this row CLOSED when the other claim's order fills.

3. If RETURNING returns claim_id:
       order_ref = f"xenon-pr-{claim_id}"
       UPDATE claim SET order_ref=order_ref;

       # Idempotency: before submitting, check if this orderRef is already alive on broker
       existing = IBClient.find_open_orders(orderRef=order_ref) ∪ executions(orderRef=order_ref)
       if existing:
           UPDATE claim SET status='SUBMITTED', broker_perm_id=existing.perm_id
           skip submission, transition position_protection → TRIGGERED
       else:
           call xenon-ib-place-order with orderRef in the JSON payload
           UPDATE claim SET status='SUBMITTED', attempts=attempts+1, submitted_at=now,
                            broker_perm_id=<from subprocess response if available>

4. Reconciliation polls IB by order_ref:
       FILLED:  claim → FILLED, position_protection → CLOSED
       CANCELED at IB: claim → ABANDONED (with reason), position_protection → FAILED
       still working: leave for next tick
       not found AND no executions: subprocess truly failed; retry on next tick (attempts++)
       max attempts (4) → claim → FAILED, position_protection → FAILED + alert
```

This protocol guarantees:

- **At most one MKT submission per position per close-attempt cycle** — N-C1 + N-C2 closed by the partial unique constraint.
- **Idempotent retries** — N-C3 closed by the orderRef-first lookup before re-submitting.
- **Crash-safe** — a daemon crash mid-protocol leaves the claim in `PENDING` or `SUBMITTED`; boot reconcile resumes from claim state, never from "nothing committed."

The native bracket case is handled symmetrically: when the daemon detects a native bracket has been filled (via the §8 per-tick liveness check), it inserts a claim with `claim_kind='native_reconcile_close'` and immediately marks it `FILLED` with the native order's perm_id. If a synthetic close was racing the native fill, the synthetic's `INSERT … ON CONFLICT` returns empty → synthetic abandons cleanly. No duplicate close.

## 6. Post-fill arming hook

### 6.1 Integration point — outbox-consumer-driven (revised after codex review WF-1)

Codex flagged the original "call arm_hook inline inside `record_fill()`" design as unsafe: `record_fill()` (`orders_store.py:553–628`) wraps the fill INSERT and outbox emit in a single `engine.begin()` transaction. **A constraint error inside the arm_hook would abort the whole transaction, rolling back the durable fill record itself.** That's the opposite of the desired guarantee.

Fix: **arm_hook is an outbox consumer, not an inline call.** This leverages the existing `events.outbox` + LISTEN/NOTIFY plumbing (`src/xenon/CLAUDE.md` — "Database events use `events.py` plus the Postgres outbox trigger; emit durable events by writing outbox rows; subscribe with the LISTEN/NOTIFY helpers for reactive services").

```
orders_store.record_fill(...)             [unchanged]
   │
   └── single tx { INSERT order_fills ; emit_outbox_in_txn(CHANNEL_FILL_RECORDED, …) }
                                                   │
                                                   ▼ LISTEN/NOTIFY
                                  ┌──────────────────────────────┐
                                  │ NEW: arm_consumer worker     │
                                  │ subscribes to fill_recorded  │
                                  │ runs in its own transaction  │
                                  │ separate from record_fill's  │
                                  └──────────────────┬───────────┘
                                                     ▼
                                  brackets.arm_hook.on_fill_event(payload):
                                    ├── load fill row by exec_id (idempotent)
                                    ├── atomicity gate (§6.3) — defer if combo incomplete
                                    ├── classify_position(...)
                                    ├── if asset_class in {UNCLASSIFIED, COVERED_CALL}:
                                    │       notify operator; do not insert
                                    ├── resolve_policies(scope, asset_class)
                                    ├── if no matching enabled policies:
                                    │       notify operator; do not insert
                                    └── for each (rule_kind, config):
                                          INSERT INTO position_protection
                                            (..., state='PENDING_ARM', config=<frozen at insert>)
                                          ON CONFLICT (broker, env, account, position_key, rule_kind)
                                          DO NOTHING
```

Properties this gives us:

- **Fill durability is independent of arming.** Any failure inside the arm_consumer — DB hiccup, classifier bug, constraint violation, notify failure — cannot touch the already-committed fill record.
- **At-least-once delivery.** Outbox events are durably persisted. The consumer can crash mid-handle and replay on restart. Idempotency is guaranteed by the unique index on `(scope, position_key, rule_kind)` + `ON CONFLICT DO NOTHING`.
- **Eventually consistent, not synchronous.** Arming happens microseconds-to-seconds after the fill commits, not in the same transaction. The latency budget is generous because the _handler tick_ (every 30s) is what actually attaches native brackets — the consumer just inserts `PENDING_ARM` rows. A 2-second consumer lag is invisible.
- **Same event drives unrelated consumers.** Future workers (markout simulator, alerts pipeline, etc.) subscribe to the same `fill_recorded` channel without reshuffling `orders_store`.

**Worker placement.** The arm consumer runs as a registered handler inside the existing `MonitorDaemon` — same daemon process, same advisory-lock-protected singleton. It's not a `BaseHandler` subclass (those are interval-driven); it's a NOTIFY-driven async task launched alongside the handler ticks. Reuses the existing `events.py` LISTEN helpers.

**Replay semantics.** Boot reconcile (§10.4) checks the outbox for `fill_recorded` events newer than the last consumer ack; any unprocessed events are replayed. Combined with `ON CONFLICT DO NOTHING`, double-processing is benign.

**Synchronous fallback (operator path).** The `xenon-position-rules sweep --apply` CLI does not go through the outbox — it inserts directly because it's already an explicit operator action with synchronous feedback. Same `INSERT … ON CONFLICT` semantics; no race with the consumer because the consumer also targets unique rows.

### 6.2 Asset-class classifier scope

The classifier reads the _full position context_, not just the fill, because a single short-call fill can be (a) the start of a covered call, (b) part of a credit spread, or (c) something else entirely. Logic:

```python
def classify_position(fill_record, full_position) -> AssetClass:
    legs = derive_legs(full_position)
    if combo_wizard_session_id := fill_record.metadata.get("combo_wizard_session_id"):
        # Defer to combo wizard's existing structure classification — don't reclassify
        return wizard_session_payload(combo_wizard_session_id).asset_class
    if len(legs) == 1 and legs[0].sec_type == "STK":
        return AssetClass.STOCK
    if len(legs) == 1 and legs[0].sec_type == "OPT" and legs[0].action == "BUY":
        return AssetClass.LONG_OPTION
    if covered_call_pattern(legs, full_position):
        return AssetClass.COVERED_CALL
    if credit_spread_pattern(legs):
        return AssetClass.CREDIT_SPREAD
    if debit_combo_pattern(legs):
        return AssetClass.DEBIT_COMBO
    return AssetClass.UNCLASSIFIED
```

`UNCLASSIFIED` is a legitimate v1 outcome — no rows inserted, operator notified.

**Manual leg-by-leg construction (operator opens a credit spread one leg at a time through `/orders/place` rather than the combo wizard):** the classifier sees leg 1 with no parent BAG and would naïvely classify it as `LONG_OPTION`. Leg 2 fills moments later and the position is now a credit spread but leg-1's rule is already armed wrong.

**v1 handling — pure detection signal, NOT deferred classification.** The `manual_assembly_window_s` (default 60s) is used only to _detect_ the situation, not to delay arming:

1. When the classifier sees a single-leg OPT fill with no `combo_wizard_session_id`, it queries `order_submissions` and `order_fills` for sibling orders/fills at the same `(scope, symbol, expiry)` opened within the trailing window.
2. If a sibling is detected → emit `manual_multi_leg_unsupported` operator notification + return `UNCLASSIFIED` (no insert). The operator runs `xenon-position-rules sweep --apply` after the position is fully assembled.
3. If no sibling → classify as single-leg `LONG_OPTION` and arm normally.

The window is **not** used to defer classification (§6.3 already declares non-wizard manual combos unsupported in v1, so there's nothing to wait for). It exists purely so we can warn the operator at fill-time rather than silently arm the wrong rule.

### 6.3 Multi-leg fill atomicity (revised after codex review WF-2)

`record_fill()` fires per execution, not per parent order. A 4-leg condor produces 4 separate `fill_recorded` outbox events; the arm consumer (§6.1) sees them serially. Without an atomicity gate, partial-state classification would arm wrong rules.

**Codex review surfaced that the original §6.3 gate was unimplementable:** it referenced an `order_submissions.legs_count` column that doesn't exist (`schema.py:198`). The current schema's `order_submissions` row has single-leg fields only — no per-leg manifest. The leg list lives transiently in IB's `LegList` and persistently only in `wizard_combo_attempts.combo_legs` (combo-wizard sessions only).

**Revised gate:** two distinct paths based on the fill's origin.

**Path A — combo-wizard-routed fills (auto-armed in v1):**

```
fill arrives → consumer sees fill_recorded outbox event →
  if fill.metadata.combo_wizard_session_id is set:
    attempt = SELECT combo_legs, expected_leg_count
              FROM xenon.wizard_combo_attempts WHERE attempt_id = …
    sibling_fills = SELECT * FROM order_fills
                    WHERE combo_attempt_id = attempt_id
    if len(sibling_fills) < expected_leg_count:
        return without classifying — next sibling fill's event re-checks
    else:
        # all legs filled → classify once, arm once
        full_position = build_position_from_legs(combo_legs, sibling_fills)
        proceed to classify + insert
```

`wizard_combo_attempts.combo_legs` is the authoritative leg manifest for combo-wizard combos. It already exists in the schema and is populated by combo wizard at attempt creation. No new tables needed.

**Path B — single-leg fills (auto-armed in v1):**

```
if fill is from a single-leg order_submission (security_type ∈ {STK, OPT}):
    classify immediately on the fill
    if STK + opens new position: AssetClass.STOCK
    if OPT + action=BUY: AssetClass.LONG_OPTION
    if OPT + action=SELL: check for covered-call coverage; if uncovered, refuse (Gate-4 should already have blocked)
```

**Path C — non-wizard manually-constructed multi-leg fills (NOT auto-armed in v1):**

If a fill has neither a `combo_wizard_session_id` nor a single-leg `order_submission`, OR it has multiple legs but the legs are spread across multiple `order_submissions` (operator opening leg-by-leg through `/orders/place`), the consumer:

1. Detects the ambiguity: `combo_wizard_session_id` absent **and** the parent submission is not single-leg (or the fill belongs to a recognized BAG-via-`/orders/place` path with no leg manifest).
2. Emits `arm_hook_unsupported_combo_path` outbox event with operator notification.
3. Does **not** insert any `position_protection` row.
4. The position appears in the next daily out-of-band sweep (§6.5) as `unprotected_position_detected`. The operator runs `xenon-position-rules sweep --apply` to manually arm it.

This is the v1 "dumb but safe" cut for the manual leg-by-leg case codex flagged in N-S10. It also matches existing combo wizard policy: multi-leg structures should go through the wizard or a parent-BAG order with a known manifest. v2 may extend `order_submissions` with a `legs_manifest JSONB` column to cover Path C.

**Reconciliation-discovered fills (post-daemon-restart):** the consumer replays unprocessed `fill_recorded` events on boot (§6.1 replay semantics). For combo-wizard fills, the gate logic above applies identically — `wizard_combo_attempts` is the source of truth regardless of _when_ the fill was committed. For single-leg fills, replay is trivially idempotent.

### 6.4 Entry-price anchor semantics

"Entry price" referenced throughout §3.1 and the seed config is defined precisely as: **the position's first-fill price at the moment the protection row was inserted**, frozen in `position_protection.state_data.anchor_price` at PENDING_ARM creation. Subsequent fills (add-ons) **do not move the anchor** — the existing rule keeps its original threshold. The unique index `(scope, position_key, rule_kind)` plus `ON CONFLICT DO NOTHING` ensures add-on fills do not create new rules.

Per asset class, "first-fill price" resolves as:

| Asset class     | Anchor                                                                                                              |
| --------------- | ------------------------------------------------------------------------------------------------------------------- |
| `stock`         | volume-weighted average fill price across all child executions of the parent order                                  |
| `long_option`   | volume-weighted average fill price                                                                                  |
| `debit_combo`   | net debit paid (signed positive)                                                                                    |
| `credit_spread` | net credit received (signed negative in IB convention) — also stored as `anchor_credit` for clarity in trigger math |
| `covered_call`  | n/a (refused)                                                                                                       |

Implication: **add-ons to an existing position are NOT auto-protected with their own rule.** The original rule still applies, sized to the original entry. If the operator wants protection at the new average cost, they must manually cancel the existing rule and re-arm via `xenon-position-rules sweep --apply` (recomputes anchor from current weighted cost). This is the v1 "dumb but safe" behavior; v2 may add re-anchoring on add-on detection.

### 6.5 Coverage assumption — out-of-band fills

The post-fill arming hook only fires for fills that flow through `orders_store.record_fill()`. Fills entered via:

- IB mobile app
- TWS direct entry (not through Xenon)
- Broker-side options assignment / exercise
- Corporate-action-induced position changes

…bypass the hook entirely. Those positions appear in `IBClient.positions()` but have no `position_protection` row. v1 detects this via a daily reconciliation task (runs alongside the §10.4 quarter-end re-arm sweep, after market close):

```
positions = IBClient.positions(scope)

# T5 sanity gate: a partial/empty positions response would falsely classify
# real positions as 'out-of-band' and spam the operator. Bail out cleanly instead.
expected_min = max(1, last_known_position_count(scope) * 0.7)   # 70% of yesterday
if not IBClient.connected:                                       # connection broken mid-call
    abort_sweep(reason='ib_disconnected'); return
if len(positions) < expected_min and last_known_position_count(scope) > 0:
    abort_sweep(reason='positions_response_suspiciously_small',
                got=len(positions), expected_min=expected_min)
    notify_operator_loud('daily out-of-band sweep aborted — IB positions() returned fewer rows than expected')
    return                                                        # reschedule next day; no false alarms

for each position in positions:
  if no position_protection row exists in non-terminal state:
    emit outbox event kind='unprotected_position_detected'
    increment health.unprotected_count
    notify operator via macOS toast (rate-limited daily)

write last_known_position_count(scope) = len(positions)           # for tomorrow's gate
```

The 70% floor is a conservative anti-flap heuristic — IB occasionally returns short responses during market-data-farm transitions. A genuine 30%+ position drop in a single day is real news worth a loud manual confirmation rather than auto-flagging every remaining position as "unprotected." First-run on a fresh deploy has `last_known_position_count = 0`, which the gate skips (you can't false-alarm against an unknown baseline).

The operator's remediation is `xenon-position-rules sweep --apply --account-scope <…>` or per-position arming. The UI portfolio page surfaces these via the per-position shield badge in `UNCLASSIFIED`/none state with a tooltip "out-of-band fill — sweep to protect".

### 6.6 Failure modes — arm consumer (revised after codex review WF-1)

With the outbox-consumer-driven model from §6.1, the consumer runs in its **own** transaction, fully isolated from `record_fill()`. Failures here cannot affect fill durability.

| Failure                                                                                  | Handling                                                                                                                                                                                                                  |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Classifier raises (e.g., malformed leg shape)                                            | Catch; log; emit outbox event `kind='arm_hook_classify_error'`; ack the source `fill_recorded` event. The fill itself was already committed by `record_fill()`.                                                           |
| `INSERT INTO position_protection` raises (constraint violation other than `ON CONFLICT`) | Catch; log; do **not** ack the source event. Outbox redelivery will retry the consumer; if the failure is deterministic, it will re-fail until operator intervention or a max-attempt counter trips and we DLQ the event. |
| **DB unavailable when consumer runs**                                                    | The consumer can't ack the source event; outbox redelivery retries on next reconnect. Fill record itself remains durable since `record_fill()` had already committed. No silent drop.                                     |
| Notify path raises (macOS not available)                                                 | Swallow; existing `_default_notify` already does this.                                                                                                                                                                    |
| Atomicity gate decides "wait for more legs" (combo wizard, partial)                      | Ack the current event; later sibling fill events drive the eventual classification. No state machine action needed — the gate is purely "do we have all legs yet?".                                                       |
| Manual non-wizard multi-leg detected (Path C, §6.3)                                      | Emit `arm_hook_unsupported_combo_path` event; ack source event; no `position_protection` insert. Operator runs `sweep --apply` to manually arm.                                                                           |

**Outbox dead-letter policy.** The consumer wraps each event in a counter (`processing_attempts`). After 5 failed attempts on the same event, the event is moved to a `events.outbox_dlq` table (existing pattern in `events.py` if present, otherwise added by this work) with full error context. Operator review surfaces these via the health endpoint's `dlq_count` field (added in §12.2).

## 7. State machine

```
                ┌─────────────┐
                │ insert      │  (post-fill hook OR sweep CLI)
                └──────┬──────┘
                       ▼
              ┌────────────────┐
              │ PENDING_ARM    │
              └─┬────────────┬─┘
        arm OK  │            │  arm fails (max retries exhausted)
                ▼            ▼
        ┌─────────────┐  ┌─────────┐
        │   ARMED     │  │ FAILED  │  terminal — operator alert
        └─┬───┬───┬───┘  └─────────┘
          │   │   └── policy change → new row PENDING_ARM, this row → SUPERSEDED
          │   │
   user manual cancel       trigger fires
   OR position closed       │
   externally               ▼
          │         ┌─────────────┐
          │         │ TRIGGERED   │
          │         └──────┬──────┘
          ▼                │  MKT-flatten ack OR boot reconcile to broker
   ┌──────────┐            ▼
   │ CANCELED │          ┌──────────┐
   └──────────┘          │  CLOSED  │
                          └──────────┘
```

**Transitions use optimistic CAS** — `UPDATE position_protection SET state='X' WHERE protection_id=$1 AND state='Y' RETURNING ...`. Zero rowcount → another tick already handled it. No double-trigger possible.

**Every transition writes one `events.outbox` row** with `kind='position_rule_transition'`. UI subscribes via existing LISTEN/NOTIFY plumbing for live updates without polling.

## 8. Handler loop semantics

```python
class PositionRulesHandler(BaseHandler):
    name = "position_rules"
    interval_seconds = 30
    requires_market_hours = True  # rule_kinds may override (future expiry-window)

    def execute(self) -> dict:
        rows = list_active_rows(state_in=("PENDING_ARM", "ARMED", "TRIGGERED"))
        mark_cache: dict[ContractKey, Mark] = {}            # per-tick coalescing (§S3)
        spot_cache: dict[Symbol, SpotPrice] = {}            # underlying spot per-tick coalescing
        for row in rows:
            rule = RULE_REGISTRY[row.rule_kind]
            scope = AccountScope.from_row(row)
            position = resolve_position(scope, row.position_key, row.position_descriptor)

            if row.state == "PENDING_ARM":
                result = rule.arm(scope, position, row.config, row.state_data)
                apply_arm_result(row, result)            # → ARMED | FAILED | retry

            elif row.state == "ARMED":
                # 1. Native-order liveness check (per-tick, not boot-only)
                if row.native_order_perm_id is not None:
                    live = verify_native_order_live(scope, row.native_order_perm_id)
                    if not live:
                        on_native_order_externally_canceled(row, scope)   # → CANCELED + cleanup
                        continue

                # 2. Read marks (cached within this tick)
                marks = read_marks(scope, position, cache=mark_cache, spot_cache=spot_cache)
                if not marks_fresh(marks):               # staleness gate (§10.1)
                    record_stale_skip(row); continue

                # 3. Evaluate trigger
                decision = rule.evaluate(scope, position, row.config, row.state_data, marks)
                apply_evaluation(row, decision, scope)   # may → TRIGGERED → executor.flatten_mkt

            elif row.state == "TRIGGERED":
                reconcile_triggered(row, scope)          # poll broker for close-order terminal
        return {"evaluated": len(rows)}
```

**Per-tick semantics — explicit:**

- **Native-order liveness check** (every tick, not boot-only): for ARMED rows with `native_order_perm_id`, the handler queries IB-side order state. If broker reports `Cancelled` / `Inactive` / `ApiCancelled`, the operator manually killed the child in TWS — row transitions to `CANCELED`, any orphaned siblings are cleaned, no re-arm. This closes the "user cancelled in TWS, system thinks it's still protected" silent-failure window. If broker reports `Filled`, the native bracket fired — handler inserts a `position_close_claims` row with `claim_kind='native_reconcile_close'` and `status='FILLED'` (this is the broker-side close we just observed), then transitions the row to `CLOSED`.
- **Mark / spot coalescing**: `mark_cache` and `spot_cache` are scoped to a single tick. If 30 credit-spread rows reference SPY, the underlying spot is read once per tick across all of them. With ~50 positions this keeps per-tick IB API calls in the low-double-digits — well within IB's ~50/sec pacing limit.
- **Order of operations matters**: liveness check before mark read, because a cancelled-or-filled native bracket is more urgent than a fresh trigger evaluation.
- **Trigger → close-claim protocol** (replaces "submit MKT directly"): when `rule.evaluate(...)` returns `TRIGGERED`, `apply_evaluation` follows the §5.6 close protocol — first insert a `position_close_claims` row with the partial-unique constraint on `(scope, position_key)`. If the INSERT returns no `claim_id`, another rule or the native-reconciler already owns the close — this rule's row transitions to `SUPERSEDED` (audit-only); the in-flight close handles the position-flat. If the INSERT returns a `claim_id`, the rule submits `xenon-ib-place-order` with `orderRef = f"xenon-pr-{claim_id}"`, capturing `perm_id` into the claim row. Per §5.6, retry-after-subprocess-error first searches IB by orderRef for an already-submitted order. **No rule ever submits a MKT close without first holding a claim.**
- **Same-tick multi-row narrative — explicit (B7).** Two rules guard the same position (e.g., `stop_loss` and `trailing_tp` rows for one long option). Both are in `ARMED`; the loop processes them serially. If row A's `evaluate()` returns `TRIGGERED` (its threshold breached), `apply_evaluation` succeeds the close-claim INSERT, transitions A → `TRIGGERED`, submits MKT. Row B is processed next in the same tick:
  1. Liveness check (no native_order_perm_id assumed for this case) — passes.
  2. Mark read uses the per-tick cache → returns the same mark A saw.
  3. `evaluate()` is allowed to run; if B's threshold is also breached, B returns `TRIGGERED`.
  4. `apply_evaluation` for B attempts the close-claim INSERT — fails ON CONFLICT against A's claim → B transitions to `SUPERSEDED` immediately, with `reason='claim_held_by_other_rule'` recorded in `state_data`. No second MKT is submitted.

  Net effect: at most one MKT per position per tick, regardless of how many rules see the breach simultaneously. Row B's evaluation is not skipped (so its breach is auditable in the outbox), but its close-action is short-circuited at the claim layer.

- **Engine-internal vs operator-manual closes — explicit (B6).** The close-claim protocol applies **only to closes initiated by this engine** — synthetic-monitor triggers and native-bracket reconciliation. Closes the operator submits manually via `/orders/place` or TWS do **not** go through the claim table. They are detected by the §10.3 "Position closed externally" path (after the 2-tick gate) and reconcile the `position_protection` row to `CANCELED`. Implementation rule: **`position_close_claims` is touched only by `src/xenon/execution/brackets/rules/*.py` and the per-tick liveness check; never by `xenon-ib-place-order` itself or by the FastAPI `/orders/place` route.**

## 9. Rule plug-in interface

```python
# src/xenon/execution/brackets/rules/base.py

class RuleEvaluator(Protocol):
    rule_kind: ClassVar[str]

    def arm(self, scope, position, config, state_data) -> ArmResult: ...
        # NATIVE_ARMED(perm_id) | SYNTHETIC_ONLY | RETRY(reason) | FAILED(reason)

    def evaluate(self, scope, position, config, state_data, marks) -> Decision: ...
        # NO_OP | TRIGGERED(reason, context) | UPDATE_STATE(new_state_data)

    def disarm(self, scope, position, native_perm_id) -> None: ...
        # idempotent; called when row → CANCELED / SUPERSEDED / CLOSED
```

### 9.1 v1 implementations

| Module                       | Behavior                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rules/stop_loss.py`         | `arm`: stocks + long_options try IB-native STP via subprocess; combos return `SYNTHETIC_ONLY`. `evaluate`: pure compare mark to threshold.                                                                                                                                                                                                                                                                                                              |
| `rules/trailing_tp.py`       | `arm`: same pattern; tracks MFE in `state_data`. `evaluate`: updates MFE every tick, fires when mark drops by `trail_pct` from MFE _after_ `activation_pct` is met.                                                                                                                                                                                                                                                                                     |
| `rules/take_profit_fixed.py` | Credit-spread-only in v1. `arm`: returns `SYNTHETIC_ONLY` (no native bracket on BAG combos). `evaluate`: triggers when `debit_to_close ≤ (1 - close_at_credit_pct) × credit_received`.                                                                                                                                                                                                                                                                  |
| `rules/combo_tp_alert.py`    | Lifts the `_crossed` / notify path from `wizard_stop_monitor.py` (which is **deleted** in Migration B). Combo wizard's `combo_wizard/protect.py` writes rows here with `rule_kind='combo_tp_alert'` and `auto_place=FALSE`. The new `PositionRulesHandler` evaluates these rows alongside every other rule_kind via `RULE_REGISTRY` dispatch — no second handler runs. Preserves spec §9.2 alert-only semantics for combo-wizard sessions specifically. |

## 10. Failure modes & safety policies

### 10.1 Read-side (cannot get a mark)

| Failure                                    | Policy                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `IBClient` disconnected / Gateway down     | Skip the tick, log, retry next tick. ARMED rows with native brackets remain defended broker-side.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| Quote stale (last_update > 60s during RTH) | Skip evaluation. **Distinguish two sub-cases via `IBClient.connected` flag:** (a) **stale + IB connected** → "silent market" (halt, illiquid name, low-volume window). Increment `state_data.consecutive_stale_ticks`; alert operator at 10 misses (5 min) with reason `silent_market_suspected`; do NOT escalate to FAILED — many illiquid options legitimately have multi-minute quote gaps. (b) **stale + IB disconnected** → connection failure. Same counter but alert at 3 misses (90s) with reason `ib_connection_stale`; this is more actionable. Counter resets on the first fresh quote. |
| `con_id` lookup fails                      | Skip + log; re-resolve next tick.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| Combo leg unavailable                      | Skip the combo entirely; do not synthesize.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |

### 10.2 Trigger-time (write fails)

The §5.6 close-claim protocol changes the failure model: every retry is keyed by the deterministic `order_ref` (= `xenon-pr-{claim_id}`), so re-submission first looks up the broker side by `orderRef` before issuing another MKT.

| Failure                                                          | Policy                                                                                                                                                                                                                                                                                                     |
| ---------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `INSERT INTO position_close_claims` returns empty                | Another close (synthetic or native-reconcile) already owns this position. Row transitions to `SUPERSEDED`. The other claim's terminal state will reconcile this row to `CLOSED` or `FAILED` accordingly.                                                                                                   |
| Subprocess error from `xenon-ib-place-order`                     | Claim stays `PENDING`/`SUBMITTED`; `attempts++`. Exponential backoff (5s, 30s, 2min, 10min). **Before each retry**, the rule queries IB by `orderRef`; if the order is already on the broker, attach `perm_id` and skip resubmission. After 4 failed attempts → claim `FAILED`, row `FAILED` + loud alert. |
| Naked-short guard refuses (covered-call edge case)               | Immediate claim `FAILED(reason="naked_short_blocked")`. No retry; structural.                                                                                                                                                                                                                              |
| Subprocess OK but no parseable `perm_id`                         | Leave claim `SUBMITTED`; reconcile via `IBClient.openOrders` lookup by `orderRef` next tick. Captures real `perm_id` once it appears.                                                                                                                                                                      |
| Position already flat (raced manual close)                       | IB returns "no position"; close claim moves to `ABANDONED(reason="position_already_flat")`; row reconciles to `CANCELED`.                                                                                                                                                                                  |
| Native bracket fired between liveness-check and synthetic submit | The §5.6 unique constraint on the claim table catches this: synthetic's INSERT fails ON CONFLICT against the native-reconcile claim that the liveness check inserted. Synthetic's row transitions to `SUPERSEDED`. No duplicate close. (This is the codex N-C1 fix.)                                       |

### 10.3 Position-state surprises

| Surprise                                               | Policy                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| User manually cancels child STP/LMT in TWS             | Per-tick liveness check (§8) detects `Cancelled` from broker → state `CANCELED`. **Do not re-place.**                                                                                                                                                                                                                                                                                                                                                                    |
| Position closed externally                             | Position not in IB `positions()` for **2 consecutive ticks** AND IB connected for both → `CANCELED`. Cancel orphaned native children. The 2-tick gate avoids false-positive cancellations from transient API blips (partial responses, market-data-farm flips, brief reconnects). The intermediate state is tracked in `state_data.position_missing_ticks` (resets to 0 on any tick that observes the position). Single-tick absence + IB-disconnected = skip per §10.1. |
| Position partially closed                              | v1: at trigger, the close-claim queries fresh `IBClient.positions()` and submits MKT for `min(protected_qty, current_broker_qty)`. If `current_broker_qty == 0` → claim `ABANDONED(reason="position_already_flat")`, row → `CANCELED`. If `0 < current_broker_qty < protected_qty` → close the remaining size, `state_data.partial_position_at_close=true` for operator review, row → `CLOSED`. (See §5.5.)                                                              |
| Same position re-opened after close                    | New `position_protection` row inserted by post-fill hook. Old row's `CANCELED` remains as audit. Partial-unique constraint on `(scope, position_key, rule_kind)` over **non-terminal states only** (§17 R8) makes this work: terminal `CANCELED`/`CLOSED` rows don't block new inserts.                                                                                                                                                                                  |
| Two rules trigger same tick                            | The §5.6 close-claim partial-unique constraint catches this at the position level: first rule's INSERT into `position_close_claims` succeeds; second rule's INSERT returns empty → second rule transitions to `SUPERSEDED` and reconciles to whatever terminal state the first rule's claim reaches. **No duplicate MKT submission possible.**                                                                                                                           |
| Native bracket fires concurrent with synthetic trigger | Same close-claim partial-unique constraint catches this. The §8 native-liveness check inserts a `claim_kind='native_reconcile_close'` claim immediately upon observing `Filled`. If a synthetic close was racing, its INSERT fails ON CONFLICT → synthetic abandons. (Codex N-C1 fix.)                                                                                                                                                                                   |
| Subprocess retry after broker accepted the order       | Before re-submission, rule queries IB by `orderRef = f"xenon-pr-{claim_id}"`. If the order is already on the broker, attach the existing `perm_id` to the claim and skip resubmission. (Codex N-C3 fix; see §5.6 step 3.)                                                                                                                                                                                                                                                |

### 10.4 Concurrency / restart

- **Daemon singleton:** `pg_try_advisory_lock(LOCK_KEY_POSITION_RULES)`. Two instances → one wins, the other no-ops. Lock is connection-scoped (released by Postgres when the daemon's PG connection closes); a hung daemon process holding the connection will hold the lock — operationally surfaced via the health endpoint's `last_tick_age_seconds` going stale.
- **Row-level CAS:** every state transition uses `UPDATE … WHERE state=$expected RETURNING …`.
- **Position-level CAS via `position_close_claims`** (§5.6): partial unique on `(scope, position_key) WHERE status IN ('PENDING','SUBMITTED')`. Closes N-C1 + N-C2 race classes.
- **Boot reconcile** runs once on daemon start before the first tick. **If `IBClient` is not connected at boot, reconcile is deferred** — rows stay in their current state, the handler tick begins normally, and reconcile is re-attempted on the first successful IB connection event (next bullet). Boot reconcile steps:
  1. **Reconcile in-flight close claims first**: every `PENDING`/`SUBMITTED` row in `position_close_claims` is cross-checked against IB by `order_ref`. Filled → claim `FILLED`, owning `position_protection` row → `CLOSED`. Still working at broker → leave. Not found at broker AND no executions → revert claim to `PENDING` (next handler tick will retry submission via the §5.6 step-3 idempotent path). This must run before the per-row reconcile below, because a position with an in-flight claim should not be re-armed.
  2. `ARMED` rows with `native_order_perm_id` set: verify IB order live; if not → `PENDING_ARM` (re-arm) or `CANCELED` (position also gone).
  3. `TRIGGERED` rows whose corresponding claim is terminal but state machine didn't catch the transition: snap to the claim's outcome (`CLOSED` if claim FILLED, `FAILED` if claim FAILED, `CANCELED` if claim ABANDONED).
  4. `PENDING_ARM` rows: leave; first tick retries arm.
- **Reconnect-triggered reconcile**: `IBClient` exposes a `connected` event. The handler subscribes once at startup; on every connection-restored event (after a disconnect of any duration) it re-runs the boot reconcile sequence before resuming normal ticks. This catches drift accumulated during long Gateway outages (e.g., a user-cancellation in TWS that happened while we were disconnected).
- **Quarter-end re-arm sweep** (separate daily cron at 16:30 ET): IB cancels untriggered GTC orders at end of calendar quarter ([IBKR docs](https://interactivebrokers.github.io/tws-api/bracket_order.html)). Sweep scans `ARMED` rows with `native_order_perm_id`, transitions stale ones to `PENDING_ARM`.
- **Daily out-of-band fill sweep** (same daily cron, immediately after re-arm): scans `IBClient.positions()` for entries with no matching `position_protection` row in non-terminal state; emits `kind='unprotected_position_detected'` outbox events and increments `health.unprotected_count`. See §6.5.

#### 10.4.1 Phase 0 prerequisite — DST-correct market hours (codex N-S5)

Codex review found `src/xenon/monitor_daemon/daemon.py:80` uses a hardcoded `MARKET_OPEN_HOUR=9, MARKET_OPEN_MINUTE=30` against UTC-5 with no DST handling. Result: during EDT (mid-March → early November), every `requires_market_hours=True` handler — including the new `PositionRulesHandler` — starts ticking an hour late and stops an hour late. **The synthetic trigger is asleep for the first hour of the trading day during ~7 months of every year.** This is unacceptable for a stop-loss engine.

**Phase 0 fix (must land before Phase 3):** replace the UTC-5 arithmetic in `MonitorDaemon.is_market_hours()` with `zoneinfo.ZoneInfo("America/New_York")` localization. Add a unit test that asserts `is_market_hours(datetime(2026, 3, 9, 9, 35, tzinfo=timezone.utc))` returns `True` (a date in early March, post-EDT-start where UTC 9:35 is 5:35 ET → closed) and `is_market_hours(datetime(2026, 3, 9, 13, 35, tzinfo=timezone.utc))` returns `True` (UTC 13:35 = 9:35 ET → open during EDT). Without this fix, the entire v1 paper-smoke gate would fail half the year.

The Phase 0 fix lands as a small, focused PR ahead of the main feature branch — the existing `wizard_stop_monitor` and `fill_monitor` handlers (which both have `requires_market_hours=True`) are already affected by this bug; fixing it now benefits the broader codebase, not just this work.

### 10.5 Slippage policy — explicit

**Trigger fires → MKT, full size, RTH-only by default.**

- ✅ Always exits.
- ⚠ Realized fill may be worse than threshold (e.g., long option SL=−20% may fill at −35% on overnight gap).

We do **not** use STP-LMT in v1 — the user's "rigid red line" requirement explicitly chose unconditional exit over fill-protection. `outsideRth=False` on all native brackets; flip per-policy if user later opts in.

**Weekend / overnight gap behavior — explicit:**

- The synthetic monitor handler does not tick outside RTH (`requires_market_hours=True` per `BaseHandler` semantics).
- Native brackets on IB use `outsideRth=False` per default policy.
- **Combined effect:** a 10% Sunday-night futures gap or a 3am crypto-correlated equity move does not trigger anything until 9:30 ET Monday RTH open. The native bracket fires at the opening auction print (or any RTH print thereafter); the synthetic handler resumes ticking ~30s into the open.
- This is a deliberate trade-off: pre/post-market liquidity is too thin for a tight stop to fill cleanly, and we'd rather take the open-print fill than be filled in pre-market on a 5-share crossed quote.
- Users who want overnight protection must flip `outsideRth=True` on the relevant `bracket_policies.config` rows manually. v1 does not auto-detect "I have a long position over the weekend, give me overnight protection."

### 10.6 Naked-short (Gate-4) interaction

Almost all protective closes reduce exposure → guard always allows. Edge case: covered-call stop on the stock leg leaves the short call uncovered. v1 refuses to arm covered calls (§3.1).

### 10.7 Corporate actions

A ticker rename, split, merger, or spin-off changes the IB contract underneath us. The `position_descriptor.legs[*].con_id` becomes stale; `position_key` no longer matches a real position; reading marks fails. Per memory `[no corporate-action guessing]` we do **not** auto-update the descriptor — manual operator intervention is required.

v1 detects via the daily reconciliation pass: for every ARMED row, if the position no longer appears in `IBClient.positions()` _but a similar-symbol position with a different `con_id` does_, the row transitions to `FAILED(reason="corporate_action_suspected")` with a loud operator alert. The new position appears in the daily out-of-band sweep (§6.5) and the operator re-arms manually.

Failing loud beats silently re-keying a stale row — which would either match nothing (defensive but invisible) or match the wrong contract (catastrophic).

## 11. Backfill — existing-positions sweep

Explicit CLI, default-dry-run:

```
xenon-position-rules sweep [--dry-run | --apply]
                            [--asset-class STK|OPT|...]
                            [--account-scope ...]
                            [--interactive]
                            [--rate-limit-per-min N]
```

- No-arg = dry-run, prints per-position table of intended inserts.
- `--apply` required to insert; rows go in as `PENDING_ARM`.
- `--interactive` prompts y/n per position.
- `--rate-limit-per-min` defaults to 30. **Applies to native-arming attempts only** (the IB-side STP/LMT submissions), not the Postgres INSERTs. Inserting all rows in a 50-position sweep is fast and unbounded; the rate limit only governs the rate at which the handler then dequeues `PENDING_ARM` rows for native arming on subsequent ticks. Keeps IB API pacing under 50/sec even for large sweeps.

**Re-arm semantics (B4) — explicit.** When the operator wants to re-arm a position whose protection was previously `CANCELED` (manual cancel via UI/CLI, or external-close detection that turned out to be a false positive):

- The sweep CLI **always inserts a NEW row** with state `PENDING_ARM`. It never updates a terminal `CANCELED`/`CLOSED`/`FAILED`/`SUPERSEDED` row.
- The partial-unique constraint over non-terminal states (R8) allows the new INSERT because the old row's terminal state is excluded from the unique index.
- The old row is preserved verbatim as audit. The drawer UI shows both rows, with the older terminal row collapsed by default and a "history" expander.
- If the operator runs sweep against a position that already has an `ARMED` row, the INSERT hits ON CONFLICT and is silently skipped (with the dry-run output noting "already armed").

Operator-side rule: **never UPDATE a CANCELED row to PENDING_ARM** — always INSERT. This keeps the audit trail intact and avoids ambiguity about which threshold/anchor was active when. The CLI/UI exposes only INSERT-shaped operations; there is no "edit existing rule" surface in v1.

The post-fill hook (§6) fires automatically for new fills; the sweep is the _only_ operator-initiated action that touches existing book.

**Live-trading auth requirement (codex N-S6).** Both the FastAPI cancel route (§12.2) and the sweep `--apply` CLI mutate real-money protection state. v1 fails closed in live mode if Clerk auth is unconfigured or unable to identify the operator:

- FastAPI: `POST /position-rules/{id}/cancel` and `POST /position-rules/sweep` (the apply-mode equivalent) both require `account_scope.account_env == 'live'` to flow through `xenon.api.guards.require_live_trading_permission`. If `CLERK_SECRET_KEY` / `CLERK_JWKS_URL` are unset (per `src/xenon/api/CLAUDE.md:56` warning that auth can pass through when config is absent), the route returns HTTP 503 with `reason_code='live_trading_auth_unconfigured'`. Paper mode bypasses this guard so dev environments aren't blocked.
- CLI: `xenon-position-rules sweep --apply --account-scope <…>` requires `XENON_TRADING_MODE=live` AND a non-empty `XENON_OPERATOR_USER_ID` env var; otherwise it errors out with the same `reason_code`. The dry-run path is exempt (no state change).
- The new `position_close_claims` rows store `claimed_by_protection_id` for every close — the audit trail names _who_ (which protection row, traceable to which fill, traceable to which user) initiated each MKT.

## 12. Surfacing & observability

### 12.1 UI (web frontend)

- **Per-position shield badge** in the existing portfolio table. Color encodes state: green `ARMED`, amber `PENDING_ARM`, orange `TRIGGERED`, red `FAILED`, gray `CANCELED`/none, neutral `UNCLASSIFIED`.
- **Click badge → side drawer** showing rule list, thresholds, current mark, MFE for trailing, raw config, **Cancel rule** button per row.
- **Global health indicator** in sidebar/header: `🛡 N armed · last tick Xs ago`, color-coded.

### 12.2 FastAPI

| Endpoint                           | Purpose                                                                                  |
| ---------------------------------- | ---------------------------------------------------------------------------------------- |
| `GET /position-rules`              | List rules for current `AccountScope`                                                    |
| `GET /position-rules/health`       | Daemon liveness, last tick, state counts, claim counts, market_window                    |
| `POST /position-rules/{id}/cancel` | Operator override → `CANCELED` (live mode requires N-S6 auth gate)                       |
| `POST /position-rules/sweep`       | Apply-mode sweep (live mode requires N-S6 auth gate; dry-run is GET-style and unguarded) |

Auth via existing Clerk + the live-trading guard described in §11. Mutation endpoints fail closed in live mode if Clerk is unconfigured.

**`GET /position-rules/health` response shape:**

```jsonc
{
  "schema_version": 1,
  "daemon_alive": true,
  "advisory_lock_held": true,
  "last_tick_at": "2026-05-04T14:23:11Z",
  "last_tick_age_seconds": 18,
  "market_window": "open", // 'open' | 'closed' | 'pre_open' | 'post_close'
  "next_market_event_at": "2026-05-04T20:00:00Z", // RTH close, or next RTH open if closed
  "rule_counts_by_state": {
    "PENDING_ARM": 0,
    "ARMED": 12,
    "TRIGGERED": 1,
    "FAILED": 0,
    "CANCELED": 47,
    "CLOSED": 1842,
    "SUPERSEDED": 8,
  },
  "claim_counts_by_status": {
    "PENDING": 0,
    "SUBMITTED": 1,
    "FILLED": 287,
    "FAILED": 2,
    "ABANDONED": 14,
  },
  "in_flight_claims": 1, // PENDING + SUBMITTED — should be 0 most of the time
  "stale_quote_skips_last_hour": 0,
  "unprotected_position_count": 0,
  "ib_connected": true,
  "outbox_dlq_count": 0, // arm_consumer dead-letter queue depth (§6.6)
  "scope": { "broker": "IB", "account_env": "live", "broker_account": "U..." },
}
```

**Global UI indicator color logic** (corrected for codex N-M1 — handler intentionally doesn't tick outside RTH):

- `red`: `!daemon_alive` OR (`market_window == "open"` AND `last_tick_age_seconds > 300`) OR `outbox_dlq_count > 0` OR `claim_counts_by_status.FAILED > 0`
- `amber`: any of `rule_counts_by_state.FAILED > 0`, `unprotected_position_count > 0`, `in_flight_claims > 0`, `stale_quote_skips_last_hour > 5`, `!ib_connected` (during open market)
- `green` otherwise
- **Outside RTH (`market_window != "open"`):** the staleness threshold is suppressed entirely; a `last_tick_age_seconds` of several hours is **expected**. Indicator stays green if everything else is healthy. Tooltip explains: "Market closed — synthetic monitor resumes at <next_market_event_at>; native brackets remain armed."

### 12.3 events.outbox payload

```jsonc
{
  "kind": "position_rule_transition",
  "payload_version": 1, // bump on shape change; consumers branch
  "protection_id": 12345,
  "position_key": "OPT::GOOG::20260417::315::C",
  "rule_kind": "stop_loss",
  "old_state": "ARMED",
  "new_state": "TRIGGERED",
  "reason": "mark_below_threshold",
  "context": { "mark": 6.42, "threshold": 7.2 },
  "scope": { "broker": "IB", "account_env": "live", "broker_account": "U..." },
}
```

This is the audit log. Every transition produces exactly one row. The `payload_version` field is **mandatory from v1** so future shape changes don't silently break downstream consumers (notably the future markout simulator). The future markout simulator reads this stream.

### 12.4 Notifications (macOS only in v1)

| Event                    | Channel                                                   |
| ------------------------ | --------------------------------------------------------- |
| `TRIGGERED` (auto-place) | macOS toast: "Stop fired: <position> — closing at MKT"    |
| `TRIGGERED` (alert-only) | macOS toast: "Risk Alert: <position> crossed <threshold>" |
| `FAILED`                 | macOS toast + persistent UI sidebar banner                |
| `UNCLASSIFIED` fill      | macOS toast (low-urgency)                                 |

Uses existing `_default_notify` helper from `wizard_stop_monitor`.

### 12.5 CLI mirror

```
xenon-position-rules list   [--scope=...] [--state=...] [--rule_kind=...]
xenon-position-rules show   <protection_id>
xenon-position-rules cancel <protection_id> [--force]
xenon-position-rules sweep  ...    # §11
xenon-position-rules health
```

Same JSON-out pattern as existing CLIs. `cancel` and `sweep` are the only mutating commands.

## 13. Testing strategy

### 13.1 Five-layer pyramid

```
Live paper smoke     — manual checklist               (acceptance gate)
E2E browser          — Playwright, ~6 tests           (golden paths only)
FastAPI routes       — fastapiHarness, ~12 tests
Subprocess contract  — pytest, ~8 tests               (memory: live E2E catches CLI drift)
Postgres integration — pytest + real PG, ~20 tests
Pure unit            — pytest, ~50 tests              (95% line coverage target)
```

### 13.2 Pure unit tests (`scripts/tests/test_position_rules/`)

- `test_triggers.py` per rule_kind — fed `(config, state_data, marks)` → assert `Decision`
- `test_classify_position.py` — 5 asset classes × variants + UNCLASSIFIED for exotics; specifically the manual leg-by-leg path returns `manual_multi_leg_unsupported` rather than misclassifying
- `test_policies.py` — most-specific-wins resolution; explicit case for "account-specific override beats broker-wide override" (codex N-S1 regression test against the weighted-score ORDER BY)
- `test_position_key.py` — deterministic, leg-order-invariant for combos
- `test_state_machine.py` — every legal transition × every illegal one (CAS rejection); new transitions added by codex review: `ARMED → SUPERSEDED` via claim conflict; `TRIGGERED → CLOSED` after subprocess retry that found existing orderRef
- `test_market_hours_dst.py` — **Phase 0 test** asserting `MonitorDaemon.is_market_hours()` is correct across the EST/EDT boundary (codex N-S5). At least four cases: 9:35 ET on a winter day (open), 9:35 ET on a summer day (open), 8:35 ET on a winter day (closed), 16:05 ET on a summer day (closed). **T6 addition — also include an integration-level test** that constructs a `MonitorDaemon` instance with a fake clock pinned to a March 2026 EDT date (post-DST-start, pre-conversion in winter-locale developer machines) and verifies `run_once()` actually fires registered handlers' `is_due()` paths during EDT 9:30-16:00 ET. Pure unit-test of `is_market_hours()` is necessary but not sufficient — the integration version catches "we returned True from is_market_hours but the calling code converts the timestamp incorrectly" regressions.
- `test_close_claim_logic.py` — pure-function tests for the §5.6 claim insert + orderRef derivation + retry-by-orderRef lookup logic (mocked broker calls).

### 13.3 Postgres integration (`scripts/tests/test_position_rules_db/`)

- Migration up/down clean (incl. new `position_close_claims` table, partial unique index)
- `arm_consumer` idempotency: replay same `fill_recorded` outbox event twice → exactly one `position_protection` row inserted
- `arm_consumer` DLQ: simulate persistent constraint failure → event lands in `events.outbox_dlq` after 5 attempts; subsequent processing skips it
- Row-level CAS race correctness (state transitions)
- **Position-level claim contention** (codex N-C1, N-C2): two concurrent `INSERT INTO position_close_claims` against the same `(scope, position_key)` — exactly one returns claim_id, the other returns empty. Repeat with a third inserter using `claim_kind='native_reconcile_close'` to verify the synthetic-vs-native race yields the same single-claim outcome.
- **Retry idempotency** (codex N-C3): the rule submits with `orderRef`; broker has the order but subprocess timed out before parsing perm_id; rule retries → broker-lookup-by-orderRef finds the order → no second submission.
- Outbox emission on every transition (incl. `payload_version=1`)
- Partial index hot-path query plan (EXPLAIN)
- Partial-unique on `(scope, position_key, rule_kind) WHERE state IN ('PENDING_ARM','ARMED','TRIGGERED')`: terminal `CANCELED` row does **not** block subsequent re-arm of the same position (codex N-S2 regression test).

### 13.4 Subprocess contract (`scripts/tests/test_position_rules_subprocess/`)

Per memory `[live E2E surfaces contract bugs]`: explicit tests that the JSON we pass to `xenon-ib-place-order` matches the CLI's accepted schema, that stdout is parseable, that exit codes map correctly. Run with `XENON_API_TEST_MODE=1`.

### 13.5 FastAPI routes (`web/tests/positionRules.test.ts`)

Per memory `[TestClient skips lifespan]`: pre-seed `app.state` in autouse conftest. Cover happy path + scope-filter + idempotency for cancel.

### 13.6 E2E browser (`web/tests/e2e/positionRules.spec.ts`)

Portfolio renders shield badge with correct color → click → drawer → cancel → live update via LISTEN/NOTIFY → badge re-renders.

### 13.7 Paper-account smoke (`docs/runbooks/position-rules-paper-smoke.md`)

Mandatory before live promotion (per memory `[paper-first for IB order bugs]`). Checklist covers:

- stock SL+TP arming, trail update, manual TWS cancel handling, sweep CLI re-arm, credit spread dual-trigger, daemon kill+restart reconcile;
- **codex N-C1 scenario** — open a long stock with both a native STP and the synthetic monitor armed; force the underlying through the threshold during a paper RTH window; confirm exactly one MKT close hits IB (verified via Flex-Query log) and `position_close_claims` shows one claim with `claim_kind='native_reconcile_close'` and one synthetic-attempt row in `SUPERSEDED`;
- **codex N-C2 scenario** — open a long option with both `stop_loss` and `trailing_tp` armed; force the price below SL while also above the TP activation (rare but constructable); confirm exactly one MKT close, one claim, one rule reaches `CLOSED`, the other reaches `SUPERSEDED`;
- **codex N-C3 scenario** — paper IB Gateway: kill the gateway connection mid-MKT-submission (after broker accepted but before subprocess returned); restart; confirm the next handler tick finds the order via `orderRef` lookup and does NOT submit another MKT.

**Authority of paper smoke vs integration tests (T1/T2 caveat).** The N-C1 and N-C3 scenarios above require precise timing that paper-account testing cannot reliably reproduce — racing the native bracket fill against a synthetic-monitor tick window, or killing the subprocess at the exact moment broker accepts. **Integration tests (§13.3) are authoritative for these correctness guarantees.** Paper smoke is a _best-effort_ additional pass that confirms the integration-test result holds against real IB Gateway round-trips. If paper smoke can't construct a particular race (e.g., the native fill always happens cleanly before the synthetic tick), the operator marks the line "verified via integration test only" and moves on. Do not block live promotion on a paper-smoke scenario that cannot be deterministically reproduced.

### 13.8 New CI guards + audit scripts

**`scripts/checks/frozen_config_at_arm.py`** — static check enforcing the frozen-config invariant. Implementation: AST-based scan of every file under `src/xenon/execution/brackets/rules/`. Fails the build if any module imports from `xenon.db.queries.bracket_policies` or contains string-literal references to the `bracket_policies` table. Rule modules must operate exclusively on the `config` dict passed in by the handler — which is sourced from `position_protection.config`, frozen at insert time. Prevents the class of bug where a `psql UPDATE bracket_policies` would silently change the threshold of an already-armed position mid-flight.

**`scripts/checks/no_duplicate_close_audit.py`** (T4) — Phase 6/7 deliverable. Joins `position_close_claims` against IB Flex Query executions over a configurable date window. Asserts: every `position_protection` row that reached `CLOSED` has exactly one matching `position_close_claims` row with `status='FILLED'`; every claim with `status='FILLED'` has exactly one matching IB execution by `orderRef`; the count of distinct close orders for any (broker, account, position*key, day) is at most one. Outputs JSON-shaped violations for the operator review. Lands as part of Phase 7 (paper smoke) so the 14-day "zero duplicate MKT closes" acceptance criterion (§20) is \_verifiable*, not just declarative.

**Daily ops review tooling (T3).** `xenon-position-rules events --since=24h [--rule_kind=...] [--state-transitions=...]` — JSON-out CLI that surfaces every `position_rule_transition` outbox event in a window, with operator-annotation slot stored in a sidecar `position_rules_review` Postgres table (rows: `protection_id`, `event_id`, `reviewed_by`, `reviewed_at`, `verdict ∈ {expected, unexpected, structural}`, `note`). Drives the §20 acceptance criterion "zero unexpected triggers": the reviewer marks each trigger expected/unexpected; the 14-day count is automatic. Without this tool, "zero unexpected triggers" is unscaled; with it, the burn-in gate becomes one daily 5-minute review.

## 14. Migration sequence

**Phase 0 prerequisite (separate small PR, lands first):** fix `MonitorDaemon.is_market_hours()` DST handling per §10.4.1 / codex N-S5. This is its own commit because it benefits all existing handlers (`wizard_stop_monitor`, `fill_monitor`, `flex_token_check`, `preset_rebalance`) — not just the new work — and shouldn't ride on a feature flag. Adds `test_market_hours_dst.py` per §13.2.

The main feature work is then a single PR, single Alembic revision, atomic. **Forward-only** (see rollback note below).

```
Migration A (additive):
  CREATE TABLE xenon.position_protection
  CREATE TABLE xenon.bracket_policies
  CREATE TABLE xenon.position_close_claims          -- §5.6 (codex N-C1/N-C2/N-C3 fix)
  INSERT 8 seed rows into bracket_policies
  CREATE indexes (partial + lookup):
    - position_protection: partial unique on (scope, position_key, rule_kind) WHERE state IN ('PENDING_ARM','ARMED','TRIGGERED')
    - position_protection: partial WHERE state IN ('PENDING_ARM','ARMED') for handler hot path
    - position_close_claims: partial unique on (scope, position_key) WHERE status IN ('PENDING','SUBMITTED')
    - position_close_claims: full unique on order_ref
    - position_close_claims: (scope, status) for cleanup
  CREATE CheckConstraints (broker, account_env, state, rule_kind, asset_class enums)
  CREATE TABLE events.outbox_dlq                    -- arm-consumer DLQ from §6.6 (only if not already present)

Migration B (combo-wizard repointing — same alembic revision):
  ASSERT (SELECT COUNT(*) FROM xenon.wizard_protection) = 0
    -- aborts migration if non-zero; safety net even though user confirms empty
  DROP TABLE xenon.wizard_protection

Code edits (same PR):
  src/xenon/db/schema.py                                  drop wizard_protection table def; add position_protection + bracket_policies + position_close_claims
  src/xenon/db/queries/combo_wizard.py                    point INSERT/UPDATE/SELECT at position_protection (filter rule_kind='combo_tp_alert')
  src/xenon/execution/combo_wizard/protect.py             write rule_kind='combo_tp_alert' rows to position_protection (with auto_place=FALSE)
  src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py  DELETE — logic absorbed into rules/combo_tp_alert.py
  src/xenon/monitor_daemon/run.py                         remove WizardStopMonitorHandler registration; register PositionRulesHandler + arm_consumer
  src/xenon/execution/orders_store.py                     no functional change to record_fill itself (keeps single tx around fill+outbox); arm_consumer is the new subscriber
  src/xenon/db/tests/test_combo_wizard.py                 update fixtures (table name + new shape)
  src/xenon/db/tests/test_schema.py                       drop wizard_protection assertion, add position_protection + bracket_policies + position_close_claims
  scripts/migrations/migrate_to_postgres.py               drop the now-stale wizard_protection block
```

Per memory `[zero-break shim refactors]`, this is technically the riskier "single atomic" approach the user normally avoids. Justified here because (a) the table is empty, (b) the rename is mechanical, (c) the test surface catches semantic drift before merge. PR review pays attention.

**Rollback strategy — explicit.** The Alembic `downgrade()` recreates an empty `wizard_protection` table for schema parity, but **cannot restore data** since the migration runs against an empty table by precondition. If something goes catastrophically wrong post-deploy and a real rollback is needed:

1. **Preferred:** restore the Postgres database from the snapshot taken immediately before the migration ran (production deploy procedure includes a snapshot precondition).
2. **Fallback:** `alembic downgrade <prev>` recreates `wizard_protection` empty; the codebase rolls back to the prior commit; combo-wizard pipeline resumes against an empty table (which it was already running against, so no data loss).
3. The new `position_protection` rows are abandoned (left as terminal CANCELED via app code, then `DROP TABLE`). The outbox audit log preserves the event history regardless.

The pre-deploy snapshot is non-negotiable. Even though the table is empty, the migration touches a load-bearing combo-wizard pipeline; a behavioral regression that's not data-loss is still a real risk.

## 15. Build phasing (TDD progression)

```
Phase 0 — Existing-code fix (small separate PR, lands first):
  Tests first → test_market_hours_dst.py
  Fix → MonitorDaemon.is_market_hours() uses zoneinfo.ZoneInfo("America/New_York")
  Benefits all existing market-hours-gated handlers; gates Phase 3+ of main work

Phase 1 — Pure (no IB, no PG):
  Tests first → triggers.py + state machine + classify_position + position_key + close-claim logic
Phase 2 — Postgres:
  Tests first → schema + migration + queries + arm_hook + outbox emission
Phase 3 — Handler (mocked executor):
  Tests first → PositionRulesHandler loop + CAS + boot reconcile + staleness gate
Phase 4 — Subprocess executor:
  Tests first → IBExecutor real impl + cancel + arm-native (xenon-ib-place-order subprocess)
Phase 5 — FastAPI:
  Tests first → /position-rules endpoints + auth + health
Phase 6 — UI:
  Tests first → badge + drawer + indicator + LISTEN/NOTIFY wiring
Phase 7 — Paper smoke:
  Manual checklist; gates the feature flag flip to enabled-on-live
```

Feature flag: `XENON_POSITION_RULES_ENABLED` defaults to `0`. Enabled per-environment after that environment's smoke checklist passes. Live (real-money) account flips the flag last.

## 16. Future work (links to backlog)

- **Markout simulator** — `docs/todo-backlog.md` Inbox 2026-05-04 [HIGH PRIORITY]. Replays historical entries against the rule engine; tunes defaults from data instead of intuition.
- **`expiry_window_close` rule_kind** — close credit combo at DTE ≤ 14 (user's stated future requirement). Same plug-in interface; new module under `rules/`.
- **`earnings_window_close` / `iv_crush_close` rule_kinds** — backlog item #1 categories.
- **Per-position UI override** — operator picks custom SL/TP at order entry. Depends on simulator validating that overrides are useful in practice.
- **Defaults editor UI** — replaces psql `UPDATE bracket_policies`. v2.
- **Futu write path** — `FutuExecutor.flatten_mkt` activates once `xenon-futu-place-order` CLI ships.
- **Exotic structure expansion** — wire `docs/trading/options-structures.json` into the classifier so jade lizard / ratio / calendar get auto-arm.
- **Atomic combo close for covered calls** — v2; needs combo-construction logic the existing CLI doesn't yet expose.

## 17. Open questions / deferred decisions

- **Polling cadence per rule_kind** — single 30s for v1; future per-rule_kind (e.g., 5s for trailing on hot positions, 60s for cold). Profile-driven decision after v1 has live data.
- **Re-anchoring on add-on fills** — v1 freezes the entry-price anchor at first-fill (§6.4). For ramp-add positions (operator builds size over multiple fills), this means later adds are protected at the first-fill anchor — possibly far from the new average cost. v2 may detect adds and offer a re-anchor prompt; v1 punts to the operator's manual sweep workflow. Decision: do nothing automatically, surface the situation in the UI ("position size changed by N% since arm" indicator on the drawer).
- **`manual_assembly_window_s` value** — §6.2 use 60s as the window in which sibling single-leg fills get held for atomicity gating. Profile-driven; if false-hold rate is significant, tune lower; if false-pair rate (separate single-leg fills wrongly grouped) is significant, tune lower aggressively.
- **Claim cleanup cadence** — §5.6 terminal claims (`FILLED`/`FAILED`/`ABANDONED`) accumulate as audit history. Decision deferred: keep indefinitely (small volume — at most one per closed position lifetime), or archive to a `position_close_claims_archive` after N days. v1 keeps indefinitely; revisit at 1-year mark.

(Codex review N-S2 closed the previously-deferred unique-constraint question — see R8 below.)

## 18. Decisions log (during brainstorm)

| ID  | Decision                                                                                                                                                                                                                | Reasoning                                                                                                                                                                                           |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Q1  | All asset classes; IB only v1                                                                                                                                                                                           | Futu replicates IB stack later                                                                                                                                                                      |
| Q2  | New fills auto; existing positions via explicit sweep                                                                                                                                                                   | Don't disturb live book on day 1                                                                                                                                                                    |
| Q3  | Hybrid policy resolution (asset-class defaults, scope overrides)                                                                                                                                                        | Tighten via Gate-3 cap if needed                                                                                                                                                                    |
| Q4  | Trailing TP activates after threshold for options/combos; immediate for stocks                                                                                                                                          | "Don't trail until I'm actually winning"                                                                                                                                                            |
| Q5  | Concrete defaults: stocks −8% / 5%, long options −20% / 25% / +30% activation, debit combos 50%-of-max-loss / 25% trail / +25% activation, credit spreads (2× credit OR strike-touch) / 50% TP, covered calls refuse v1 | User-tuned during brainstorm; will be data-driven once simulator ships                                                                                                                              |
| D1  | Generalize `wizard_protection` → `position_protection`                                                                                                                                                                  | Cleaner; table is empty                                                                                                                                                                             |
| D2  | Auto-place vs alert-only is per-policy flag, not per-system                                                                                                                                                             | Preserves wizard's existing `combo_tp_alert` semantics                                                                                                                                              |
| D3  | Opaque deterministic `position_key` string                                                                                                                                                                              | No prior convention; reversible via descriptor JSONB                                                                                                                                                |
| D4  | Single PR for migration A+B                                                                                                                                                                                             | Empty table; mechanical reshape; atomic is acceptable                                                                                                                                               |
| R1  | `wizard_stop_monitor.py` is **deleted** in Migration B; `rules/combo_tp_alert.py` absorbs its logic; `PositionRulesHandler` is the only handler                                                                         | Self-review found: keeping both handlers would double-evaluate `combo_tp_alert` rows. One handler, dispatch-by-rule_kind                                                                            |
| R2  | `config` is frozen at **insert time (PENDING_ARM)**, not arm time; CI guard enforces                                                                                                                                    | Self-review found wording inconsistency. Insert-time freeze is what the post-fill hook actually does; arm-time was misleading                                                                       |
| R3  | Multi-leg fill atomicity gate via parent BAG order_id lookup; classification deferred until all legs filled                                                                                                             | Self-review found: per-execution `record_fill` would partial-classify combos and arm wrong rules                                                                                                    |
| R4  | Entry-price anchor frozen in `state_data.anchor_price` at insert; add-on fills don't re-anchor                                                                                                                          | Self-review found "entry price" was undefined for ramp-add positions; pinned to first-fill weighted average                                                                                         |
| R5  | Native-order liveness check runs every tick (not boot-only) for ARMED rows with `native_order_perm_id`                                                                                                                  | Self-review found: user-cancellation in TWS would silently leave system thinking position is protected                                                                                              |
| R6  | Alert-only debounce via `min_realert_interval_s` (default 1h); edge-triggered fire on threshold crossing                                                                                                                | Self-review found: per-tick alert re-fire on sustained breach would be operationally awful                                                                                                          |
| R7  | Out-of-band fills (TWS direct, mobile app, assignment) detected via daily reconciliation pass; surfaced as `unprotected_position_detected` outbox events                                                                | Self-review found: post-fill hook only fires for fills through `orders_store`; positions opened outside that path were silently unprotected                                                         |
| R8  | Partial unique on `position_protection (scope, position_key, rule_kind) WHERE state IN ('PENDING_ARM','ARMED','TRIGGERED')` — terminal rows do **not** participate in uniqueness                                        | Codex N-S2: full unique would block re-arm of a re-opened position; partial-unique on non-terminal makes "same position closed and reopened" work cleanly                                           |
| R9  | NEW table `position_close_claims` with partial unique on `(scope, position_key) WHERE status IN ('PENDING','SUBMITTED')` is the source of truth for "no two MKT closes for the same position"                           | Codex N-C1, N-C2: per-row CAS on `position_protection` does not coordinate the native bracket vs synthetic monitor, nor two rules on the same position. Position-level claim does.                  |
| R10 | Deterministic `order_ref = f"xenon-pr-{claim_id}"` on every IB MKT submission; subprocess retry first searches IB by `orderRef` before resubmitting                                                                     | Codex N-C3: subprocess timeout after broker accept could submit a duplicate MKT. orderRef-first lookup makes retries idempotent.                                                                    |
| R11 | Arm hook is **outbox-consumer-driven**, not inline in `record_fill()`'s transaction                                                                                                                                     | Codex WF-1: `record_fill()` is a single `engine.begin()` around fill INSERT + outbox emit. Inline arm_hook → constraint error aborts fill durability. Move to outbox subscriber on `fill_recorded`. |
| R12 | Multi-leg atomicity gate uses `wizard_combo_attempts.combo_legs` (combo wizard only); non-wizard manual multi-leg = `arm_hook_unsupported_combo_path` → operator runs `sweep --apply`                                   | Codex WF-2: original §6.3 referenced `order_submissions.legs_count` which doesn't exist in current schema. v1 narrows auto-arm to wizard combos + single-legs; non-wizard combos go through sweep.  |
| R13 | Policy resolution uses weighted specificity score: `broker_account=4, account_env=2, broker=1`; ties broken by `policy_id ASC`                                                                                          | Codex N-S1: naïve `(field IS NOT NULL)::int DESC` ordering ranks broker-wide overrides above account-specific ones. Weighted score gives correct precedence.                                        |
| R14 | Quantity model — `protected_qty`, `opened_qty`, `multiplier`, `qty_unit` frozen in `position_descriptor` at insert; close uses `min(protected_qty, current_broker_qty)` snapshot                                        | Codex N-S3: original spec said "MKT, full size" without defining size for ramp-add or partial-close cases. Frozen `protected_qty` + fresh broker-qty min handles all scenarios.                     |
| R15 | Phase 0 separate PR fixes existing `MonitorDaemon.is_market_hours()` DST handling via `zoneinfo.ZoneInfo("America/New_York")`                                                                                           | Codex N-S5: existing daemon hardcodes UTC-5; synthetic monitor would be asleep first hour of every EDT trading day. Fix benefits all market-hours-gated handlers.                                   |
| R16 | Live-mode mutating endpoints/CLIs require explicit live-trading auth permission; fail closed if Clerk unconfigured                                                                                                      | Codex N-S6: real-money cancel + sweep should not pass through if auth is misconfigured. Fail closed in live, allow in paper.                                                                        |
| R17 | Health endpoint reports `market_window` (open/closed); UI indicator suppresses staleness-red outside RTH                                                                                                                | Codex N-M1: with `requires_market_hours=True`, `last_tick_age_seconds > 300` outside RTH is _expected_, not a failure. Indicator must distinguish.                                                  |
| R18 | Outbox `payload_version: 1` mandatory from v1                                                                                                                                                                           | Self-review S5: lock the field shape from day 1; consumers branch on version when shape evolves. (Closed previous open Q.)                                                                          |

## 19. Research / external sources

Findings that shaped the design:

- [IBKR Bracket Orders TWS API](https://interactivebrokers.github.io/tws-api/bracket_order.html) — confirms native bracket + TRAIL semantics, OCA linkage, GTC support.
- [Supa.is — IBKR Bracket 2026 Guide](https://www.supa.is/article/interactive-brokers-bracket-order-oco-stop-loss-take-profit-tws-2026) — confirms quarterly auto-cancel of untriggered GTC orders. Drove the §10.4 quarter-end re-arm cron.
- [ib_insync issue #216 — adjustable stop bug](https://github.com/erdewit/ib_insync/issues/216) — drove the decision to implement activation-threshold logic ourselves rather than using IB's adjustable-stop feature.
- [ib_insync issue #85 — market bracket price offset](https://github.com/erdewit/ib_insync/issues/85) — drove the "submit children only after fill confirmation" pattern.
- [QuantConnect Lean — TrailingStopRiskManagementModel](https://github.com/QuantConnect/Lean/blob/master/Algorithm.Framework/Risk/TrailingStopRiskManagementModel.py) — reference architecture for the synthetic monitor.
- [mattsta/icli](https://github.com/mattsta/icli) — closest open-source prior art for auto-attached IB stops.

## 20. Acceptance criteria

v1 ships when all of the following hold:

- [ ] **Phase 0 DST fix landed and merged** ahead of feature work; `test_market_hours_dst.py` green; existing handlers (wizard_stop_monitor, fill_monitor) verified to tick at the correct ET boundaries year-round.
- [ ] All Phase 1-6 test layers green in CI on the feature branch.
- [ ] Paper-account smoke checklist (§13.7) executed end-to-end with operator sign-off, **including the codex N-C1, N-C2, N-C3 scenarios** (duplicate-close races, retry idempotency).
- [ ] Migration applied to a paper Postgres, all combo-wizard tests still pass post-migration; `position_close_claims` indexes verified via EXPLAIN.
- [ ] `xenon-position-rules list/show/cancel/sweep/health` CLIs work against the paper environment.
- [ ] FastAPI `/position-rules` endpoints respond correctly through Clerk auth; **mutating endpoints fail closed in live mode when `CLERK_*` envs are unset** (codex N-S6 regression test).
- [ ] Web UI shield badge + drawer + cancel + live update verified in browser; global health indicator correctly reports green during RTH-closed hours rather than red (codex N-M1).
- [ ] **14 days of "clean operation" on paper** before flipping `XENON_POSITION_RULES_ENABLED=1` on live (real-money). "Clean operation" defined explicitly as **all** of:
  - Zero rows reach `FAILED` state for non-structural reasons (`naked_short_blocked` and `corporate_action_suspected` are structural, allowed)
  - Zero unexpected triggers (judged manually against the daily outbox event review)
  - **Zero duplicate MKT closes** observed across the 14 days — verified by joining `position_close_claims` against IB Flex Query: every `position_protection` row that reached `CLOSED` has exactly one `claim` row with `status='FILLED'` and exactly one matching IB execution
  - **Zero `outbox_dlq` events** for the arm consumer
  - At least 1 successful end-to-end trigger → MKT-flatten → CLOSED cycle observed in paper
  - At least 1 successful boot-reconcile observed (daemon kill + restart with ARMED rows AND in-flight `SUBMITTED` claim, recovers cleanly without duplicate submission)
  - At least 1 successful native-bracket attach + per-tick IB-side liveness verification observed
  - At least 1 successful "subprocess timed out after broker accept" scenario in paper, with the retry attaching the existing `perm_id` via `orderRef` lookup rather than submitting a second MKT
  - `health.unprotected_position_count` returned to zero within 1 daily-sweep cycle of any out-of-band fill detected
  - Quote staleness skip rate (`stale_quote_skips_last_hour` / `rule_counts_by_state.ARMED`) stays below 5% in aggregate during RTH
- [ ] `docs/reference/order-path-incident-history.md` gets a new row referencing this design (per CLAUDE.md convention for order-path changes).

---

_End of design document._
