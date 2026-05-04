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

| Asset class                                                        | Hard SL                                                                                                                           | Take-profit                                               |
| ------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| **Stocks**                                                         | −8% from entry, MKT at trigger                                                                                                    | 5% trail off MFE, activates immediately                   |
| **Long single-name options**                                       | **−20% from entry** (rigid red line), MKT at trigger                                                                              | 25% trail off MFE, activates at +30%                      |
| **Multi-leg debit combos**                                         | 50% of max-loss (synthetic monitor), MKT at trigger                                                                               | 25% trail off MFE in P&L $, activates at +25% of max-gain |
| **Credit spreads** (bull put short / bear call short)              | **either trigger, whichever first**: ① spread debit-to-close ≥ 2× credit received, ② underlying touches/breaches the short strike | **fixed close at 50% of max credit** (no trail in v1)     |
| **Covered calls**                                                  | n/a — `arm()` returns `FAILED(reason="covered_call_unsupported_v1")`; operator notified                                           | n/a                                                       |
| **Unclassified** (ratio, jade lizard, calendar, condor, butterfly) | n/a — operator notified at fill, no auto-arm                                                                                      | n/a                                                       |

These are _defaults_. The seed inserts wildcard rows (broker / env / account = NULL); per-account overrides land as more-specific rows when the user wants them. Defaults are tunable via `psql UPDATE bracket_policies` — UI later.

### 3.2 Backfill behavior — the "don't disturb existing positions" guarantee

- **New fills going forward** auto-arm via the post-fill hook (§6).
- **Existing positions** require the operator to run `xenon-position-rules sweep` explicitly. The CLI defaults to `--dry-run`; the user must add `--apply` to actually create rows. Optional `--interactive` prompts per position. The sweep is the only operator action that touches existing book on day 1 of v1.

### 3.3 Auto-place vs alert-only

The `bracket_policies.auto_place` boolean controls trigger behavior:

- `auto_place = TRUE` (v1 default for all new rule_kinds) → MKT-flatten on trigger.
- `auto_place = FALSE` → emit alert + macOS notification, state stays `ARMED`. Matches the existing `combo_tp_alert` semantics from the wizard pipeline.

This makes the new system orthogonal to the existing combo-wizard Risk Alert flow (spec §9.2). Wizard guards the entry decision pre-fill; this engine guards the position state post-fill. Different lifecycle, different store, no conflict.

## 4. Architecture

### 4.1 Reuse audit summary

The codebase already has 80% of the abstraction this work needs. Existing assets used:

| Component                            | Location                                                   | Reuse                                                                                    |
| ------------------------------------ | ---------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `MonitorDaemon` orchestrator         | `src/xenon/monitor_daemon/daemon.py`                       | Hosts the new handler                                                                    |
| `BaseHandler` ABC                    | `src/xenon/monitor_daemon/handlers/base.py`                | Parent class for `PositionRulesHandler`                                                  |
| `wizard_stop_monitor` handler        | `src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py` | Reference template; reroutes to new table in Migration B                                 |
| `combo_wizard/protect.py`            | `src/xenon/execution/combo_wizard/`                        | Reference for retries, Gate-4, signed pricing; reroutes to new table in Migration B      |
| `wizard_protection` table            | `src/xenon/db/schema.py`                                   | Renamed and reshaped to `position_protection` (Migration B); empty per user confirmation |
| `orders_store.record_fill`           | `src/xenon/execution/orders_store.py`                      | Single integration point for the post-fill arming hook                                   |
| `xenon-ib-place-order` CLI           | `src/xenon/execution/ib_place_order.py`                    | Subprocess write path for native bracket arming + MKT-flatten                            |
| `xenon-ib-order-manage` CLI          | `src/xenon/execution/ib_order_manage.py`                   | Subprocess for cancel during disarm                                                      |
| `account_scope.py`                   | `src/xenon/execution/account_scope.py`                     | Routes broker/env/account; selects which CLI to invoke                                   |
| `IBClient` / `FutuClient` singletons | `src/xenon/clients/`                                       | In-process read path for marks, spot, position state                                     |
| `events.outbox` + LISTEN/NOTIFY      | `src/xenon/db/events.py`                                   | Audit log + live UI updates                                                              |
| Postgres advisory lock pattern       | `src/xenon/api/services/advisory_lock.py`                  | Daemon singleton enforcement                                                             |

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
    └── combo_tp_alert.py  ~50  LOC — wraps existing wizard_stop_monitor logic

src/xenon/monitor_daemon/handlers/               EXTEND
└── position_rules.py      ~150 LOC — PositionRulesHandler(BaseHandler)

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

Approximately **1100 new LOC + 1 alembic migration + 1 CI guard**. No deletions beyond the empty `wizard_protection` table.

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
| `config`               | JSONB NOT NULL                        | Frozen at arm time                                                                          |
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

- `UNIQUE (broker, account_env, broker_account, position_key, rule_kind)` — at most one active row per (position, rule). Insert `SUPERSEDED` rows when policy changes; the unique index is partial on non-terminal states (or we manage via app logic — TBD in implementation).
- Partial index on `(state, broker, account_env, broker_account) WHERE state IN ('PENDING_ARM','ARMED')` — handler hot path.
- Index on `(broker, account_env, broker_account, position_key)` — "rules guarding this position?" lookup.
- CheckConstraints on `broker`, `account_env`, `state` enums.

### 5.2 `xenon.bracket_policies` (NEW — defaults seed)

| Column           | Type                                  | Notes                                             |
| ---------------- | ------------------------------------- | ------------------------------------------------- |
| `policy_id`      | BigInt PK autoincrement               |                                                   |
| `broker`         | Text nullable                         | NULL = wildcard                                   |
| `account_env`    | Text nullable                         | NULL = wildcard                                   |
| `broker_account` | Text nullable                         | NULL = wildcard                                   |
| `asset_class`    | Text NOT NULL                         |                                                   |
| `rule_kind`      | Text NOT NULL                         |                                                   |
| `enabled`        | Bool NOT NULL default TRUE            | Kill switch                                       |
| `auto_place`     | Bool NOT NULL default TRUE            | TRUE = MKT-flatten on trigger; FALSE = alert-only |
| `config`         | JSONB NOT NULL                        | Default config merged into row at arm time        |
| `created_at`     | TIMESTAMPTZ NOT NULL default `tz_now` |                                                   |
| `updated_at`     | TIMESTAMPTZ NOT NULL default `tz_now` |                                                   |

**Constraint:** `UNIQUE(broker, account_env, broker_account, asset_class, rule_kind)` — handle NULL collations via `COALESCE(...,'*')` expression index.

**Resolution:** most-specific-wins. Lookup via:

```sql
SELECT rule_kind, enabled, auto_place, config
FROM xenon.bracket_policies
WHERE asset_class = $1
  AND (broker IS NULL OR broker = $2)
  AND (account_env IS NULL OR account_env = $3)
  AND (broker_account IS NULL OR broker_account = $4)
ORDER BY (broker IS NOT NULL)::int DESC,
         (account_env IS NOT NULL)::int DESC,
         (broker_account IS NOT NULL)::int DESC
```

Then deduplicate by `rule_kind` in Python (first row wins), filter `enabled=TRUE`.

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

### 5.4 v1 seed for `bracket_policies` (9 rows)

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

## 6. Post-fill arming hook

### 6.1 Integration point

Single integration: extend `orders_store.record_fill()`. All fill paths converge here (per `src/xenon/CLAUDE.md`); hooking lower is duplication, hooking higher misses reconciliation-discovered fills.

```
orders_store.record_fill(...)
   │
   ├── (existing) write fill row, update submission state
   │
   └── (NEW) brackets.arm_hook.on_fill(scope, fill_record):
         ├── classify_position(fill_record)        → AssetClass
         ├── if asset_class in {UNCLASSIFIED, COVERED_CALL}: notify operator + return
         ├── resolve_policies(scope, asset_class)  → list[(rule_kind, config)]
         ├── if no matching enabled policies: notify operator + return
         └── for each (rule_kind, config):
               INSERT INTO position_protection (..., state='PENDING_ARM', config=<frozen>)
               ON CONFLICT (broker, env, account, position_key, rule_kind) DO NOTHING
```

The hook **never raises** — failures are logged and emitted as outbox events; the fill record itself remains durable. Idempotent via the unique index.

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
        for row in rows:
            rule = RULE_REGISTRY[row.rule_kind]
            scope = AccountScope.from_row(row)
            position = resolve_position(scope, row.position_key, row.position_descriptor)

            if row.state == "PENDING_ARM":
                result = rule.arm(scope, position, row.config, row.state_data)
                apply_arm_result(row, result)            # → ARMED | FAILED | retry

            elif row.state == "ARMED":
                marks = read_marks(scope, position)      # in-process IBClient/FutuClient
                if not marks_fresh(marks):               # staleness gate
                    record_stale_skip(row); continue
                decision = rule.evaluate(scope, position, row.config, row.state_data, marks)
                apply_evaluation(row, decision, scope)   # may → TRIGGERED → executor.flatten_mkt

            elif row.state == "TRIGGERED":
                reconcile_triggered(row, scope)          # poll broker for close-order terminal
        return {"evaluated": len(rows)}
```

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

| Module                       | Behavior                                                                                                                                                                                                         |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rules/stop_loss.py`         | `arm`: stocks + long_options try IB-native STP via subprocess; combos return `SYNTHETIC_ONLY`. `evaluate`: pure compare mark to threshold.                                                                       |
| `rules/trailing_tp.py`       | `arm`: same pattern; tracks MFE in `state_data`. `evaluate`: updates MFE every tick, fires when mark drops by `trail_pct` from MFE _after_ `activation_pct` is met.                                              |
| `rules/take_profit_fixed.py` | Credit-spread-only in v1. `arm`: returns `SYNTHETIC_ONLY` (no native bracket on BAG combos). `evaluate`: triggers when `debit_to_close ≤ (1 - close_at_credit_pct) × credit_received`.                           |
| `rules/combo_tp_alert.py`    | Wraps existing `wizard_stop_monitor` logic. Migration B reroutes its read/write to `position_protection` (filtered to `rule_kind='combo_tp_alert'`). Preserves alert-only semantics (`auto_place=FALSE` always). |

## 10. Failure modes & safety policies

### 10.1 Read-side (cannot get a mark)

| Failure                                    | Policy                                                                                            |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------- |
| `IBClient` disconnected / Gateway down     | Skip the tick, log, retry next tick. ARMED rows with native brackets remain defended broker-side. |
| Quote stale (last_update > 60s during RTH) | Skip evaluation. Track `state_data.consecutive_stale_ticks`; alert at 5 misses.                   |
| `con_id` lookup fails                      | Skip + log; re-resolve next tick.                                                                 |
| Combo leg unavailable                      | Skip the combo entirely; do not synthesize.                                                       |

### 10.2 Trigger-time (write fails)

| Failure                                            | Policy                                                                                                         |
| -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Subprocess error from `xenon-ib-place-order`       | State stays `TRIGGERED`. Exponential backoff (5s, 30s, 2min, 10min). After 4 failures → `FAILED` + loud alert. |
| Naked-short guard refuses (covered-call edge case) | Immediate `FAILED(reason="naked_short_blocked")`. No retry; structural.                                        |
| Subprocess OK but no parseable `perm_id`           | Leave `TRIGGERED`; reconcile via `IBClient.openOrders` next tick.                                              |
| Position already flat (raced manual close)         | IB returns "no position"; reconcile to `CANCELED`.                                                             |

### 10.3 Position-state surprises

| Surprise                                   | Policy                                                                                             |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| User manually cancels child STP/LMT in TWS | Detected via reconcile → state `CANCELED`. **Do not re-place.**                                    |
| Position closed externally                 | Position not in IB `positions()` → `CANCELED`. Cancel orphaned native children.                    |
| Position partially closed                  | v1: treat as full close → `CANCELED`. Operator re-arms manually.                                   |
| Same position re-opened after close        | New `position_protection` row inserted by post-fill hook. Old row's `CANCELED` remains as audit.   |
| Two rules trigger same tick                | CAS on close: first MKT wins; other rule transitions to `SUPERSEDED` because position is now flat. |

### 10.4 Concurrency / restart

- **Daemon singleton:** `pg_try_advisory_lock(LOCK_KEY_POSITION_RULES)`. Two instances → one wins, the other no-ops.
- **Row-level CAS:** every state transition uses `UPDATE … WHERE state=$expected RETURNING …`.
- **Boot reconcile** runs once on daemon start before the first tick:
  1. `ARMED` rows with `native_order_perm_id` set: verify IB order live; if not → `PENDING_ARM` (re-arm) or `CANCELED` (position also gone).
  2. `TRIGGERED` rows: cross-check IB executions for matching close — filled → `CLOSED`; working → leave; not found → revert to `ARMED`.
  3. `PENDING_ARM` rows: leave; first tick retries arm.
- **Quarter-end re-arm sweep** (separate daily cron at 16:30 ET): IB cancels untriggered GTC orders at end of calendar quarter ([IBKR docs](https://interactivebrokers.github.io/tws-api/bracket_order.html)). Sweep scans `ARMED` rows with `native_order_perm_id`, transitions stale ones to `PENDING_ARM`.

### 10.5 Slippage policy — explicit

**Trigger fires → MKT, full size, RTH-only by default.**

- ✅ Always exits.
- ⚠ Realized fill may be worse than threshold (e.g., long option SL=−20% may fill at −35% on overnight gap).

We do **not** use STP-LMT in v1 — the user's "rigid red line" requirement explicitly chose unconditional exit over fill-protection. `outsideRth=False` on all native brackets; flip per-policy if user later opts in.

### 10.6 Naked-short (Gate-4) interaction

Almost all protective closes reduce exposure → guard always allows. Edge case: covered-call stop on the stock leg leaves the short call uncovered. v1 refuses to arm covered calls (§3.1).

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
- `--rate-limit-per-min` defaults to 30 — caps native arming attempts to avoid IB API throttle.

The post-fill hook (§6) fires automatically for new fills; the sweep is the _only_ operator-initiated action that touches existing book.

## 12. Surfacing & observability

### 12.1 UI (web frontend)

- **Per-position shield badge** in the existing portfolio table. Color encodes state: green `ARMED`, amber `PENDING_ARM`, orange `TRIGGERED`, red `FAILED`, gray `CANCELED`/none, neutral `UNCLASSIFIED`.
- **Click badge → side drawer** showing rule list, thresholds, current mark, MFE for trailing, raw config, **Cancel rule** button per row.
- **Global health indicator** in sidebar/header: `🛡 N armed · last tick Xs ago`, color-coded.

### 12.2 FastAPI

| Endpoint                           | Purpose                                  |
| ---------------------------------- | ---------------------------------------- |
| `GET /position-rules`              | List rules for current `AccountScope`    |
| `GET /position-rules/health`       | Daemon liveness, last tick, state counts |
| `POST /position-rules/{id}/cancel` | Operator override → `CANCELED`           |

Auth via existing Clerk; no special privileges beyond order-path access.

### 12.3 events.outbox payload

```jsonc
{
  "kind": "position_rule_transition",
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

This is the audit log. Every transition produces exactly one row. The future markout simulator reads this stream.

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
- `test_classify_position.py` — 5 asset classes × variants + UNCLASSIFIED for exotics
- `test_policies.py` — most-specific-wins resolution
- `test_position_key.py` — deterministic, leg-order-invariant for combos
- `test_state_machine.py` — every legal transition × every illegal one (CAS rejection)

### 13.3 Postgres integration (`scripts/tests/test_position_rules_db/`)

- Migration up/down clean
- `arm_hook` idempotency (`ON CONFLICT DO NOTHING`)
- CAS race correctness
- Outbox emission on every transition
- Partial index hot-path query plan (EXPLAIN)

### 13.4 Subprocess contract (`scripts/tests/test_position_rules_subprocess/`)

Per memory `[live E2E surfaces contract bugs]`: explicit tests that the JSON we pass to `xenon-ib-place-order` matches the CLI's accepted schema, that stdout is parseable, that exit codes map correctly. Run with `XENON_API_TEST_MODE=1`.

### 13.5 FastAPI routes (`web/tests/positionRules.test.ts`)

Per memory `[TestClient skips lifespan]`: pre-seed `app.state` in autouse conftest. Cover happy path + scope-filter + idempotency for cancel.

### 13.6 E2E browser (`web/tests/e2e/positionRules.spec.ts`)

Portfolio renders shield badge with correct color → click → drawer → cancel → live update via LISTEN/NOTIFY → badge re-renders.

### 13.7 Paper-account smoke (`docs/runbooks/position-rules-paper-smoke.md`)

Mandatory before live promotion (per memory `[paper-first for IB order bugs]`). Checklist covers: stock SL+TP arming, trail update, manual TWS cancel handling, sweep CLI re-arm, credit spread dual-trigger, daemon kill+restart reconcile.

### 13.8 New CI guard

`scripts/checks/frozen_config_at_arm.py` — static check that `rules/*.py` reads `config` only from the row passed in, never directly from `bracket_policies` mid-tick. Prevents retroactive-policy bugs.

## 14. Migration sequence

Single PR, single Alembic revision, atomic.

```
Migration A (additive):
  CREATE TABLE xenon.position_protection
  CREATE TABLE xenon.bracket_policies
  INSERT 9 seed rows into bracket_policies
  CREATE indexes (partial + lookup)
  CREATE CheckConstraints

Migration B (combo-wizard repointing — same alembic revision):
  ASSERT (SELECT COUNT(*) FROM xenon.wizard_protection) = 0
    -- aborts migration if non-zero; safety net even though user confirms empty
  DROP TABLE xenon.wizard_protection

Code edits (same PR):
  src/xenon/db/schema.py                                  drop wizard_protection table def
  src/xenon/db/queries/combo_wizard.py                    point queries at position_protection (filter rule_kind='combo_tp_alert')
  src/xenon/execution/combo_wizard/protect.py             write rule_kind='combo_tp_alert' rows to position_protection
  src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py read position_protection filtered to rule_kind='combo_tp_alert'
  src/xenon/db/tests/test_combo_wizard.py                 update fixtures
  src/xenon/db/tests/test_schema.py                       drop wizard_protection assertion, add position_protection
  scripts/migrations/migrate_to_postgres.py               drop the now-stale wizard_protection block
```

Per memory `[zero-break shim refactors]`, this is technically the riskier "single atomic" approach the user normally avoids. Justified here because (a) the table is empty, (b) the rename is mechanical, (c) the test surface catches semantic drift before merge. PR review pays attention.

## 15. Build phasing (TDD progression)

```
Phase 1 — Pure (no IB, no PG):
  Tests first → triggers.py + state machine + classify_position + position_key
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

- **Unique-constraint shape for `position_protection`** — partial unique on non-terminal states, or full unique with `SUPERSEDED` rows generated? Implementation-time decision; both work, picking based on which yields cleaner queries during phase 2.
- **Polling cadence per rule_kind** — single 30s for v1; future per-rule_kind (e.g., 5s for trailing on hot positions, 60s for cold). Profile-driven decision after v1 has live data.
- **Outbox payload schema versioning** — `kind='position_rule_transition'` v1 shape locked here; if schema evolves, add `version` field and consumers branch. Not a v1 concern.

## 18. Decisions log (during brainstorm)

| ID  | Decision                                                                                                                                                                                                                | Reasoning                                                              |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Q1  | All asset classes; IB only v1                                                                                                                                                                                           | Futu replicates IB stack later                                         |
| Q2  | New fills auto; existing positions via explicit sweep                                                                                                                                                                   | Don't disturb live book on day 1                                       |
| Q3  | Hybrid policy resolution (asset-class defaults, scope overrides)                                                                                                                                                        | Tighten via Gate-3 cap if needed                                       |
| Q4  | Trailing TP activates after threshold for options/combos; immediate for stocks                                                                                                                                          | "Don't trail until I'm actually winning"                               |
| Q5  | Concrete defaults: stocks −8% / 5%, long options −20% / 25% / +30% activation, debit combos 50%-of-max-loss / 25% trail / +25% activation, credit spreads (2× credit OR strike-touch) / 50% TP, covered calls refuse v1 | User-tuned during brainstorm; will be data-driven once simulator ships |
| D1  | Generalize `wizard_protection` → `position_protection`                                                                                                                                                                  | Cleaner; table is empty                                                |
| D2  | Auto-place vs alert-only is per-policy flag, not per-system                                                                                                                                                             | Preserves wizard's existing `combo_tp_alert` semantics                 |
| D3  | Opaque deterministic `position_key` string                                                                                                                                                                              | No prior convention; reversible via descriptor JSONB                   |
| D4  | Single PR for migration A+B                                                                                                                                                                                             | Empty table; mechanical reshape; atomic is acceptable                  |

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

- [ ] All Phase 1-6 test layers green in CI on the feature branch.
- [ ] Paper-account smoke checklist (§13.7) executed end-to-end with operator sign-off.
- [ ] Migration A+B applied to a paper Postgres, all combo-wizard tests still pass post-migration.
- [ ] `xenon-position-rules list/show/cancel/sweep/health` CLIs work against the paper environment.
- [ ] FastAPI `/position-rules` endpoints respond correctly through Clerk auth.
- [ ] Web UI shield badge + drawer + cancel + live update verified in browser.
- [ ] `XENON_POSITION_RULES_ENABLED=1` flag flipped on paper; flag stays at `0` for live until paper has at least 14 days of clean operation.
- [ ] `docs/reference/order-path-incident-history.md` gets a new row referencing this design (per CLAUDE.md convention for order-path changes).

---

_End of design document._
