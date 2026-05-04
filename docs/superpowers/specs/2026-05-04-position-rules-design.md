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

- `UNIQUE (broker, account_env, broker_account, position_key, rule_kind)` — at most one active row per (position, rule). Insert `SUPERSEDED` rows when policy changes; the unique index is partial on non-terminal states (or we manage via app logic — TBD in implementation).
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
  "legs": [
    { "sec_type": "OPT", "symbol": "GOOG", "expiry": "20260417",
      "strike": 315.0, "right": "C", "action": "BUY",
      "ratio": 1, "fill_price": 5.20, "con_id": 123456789 }
  ]
}
```

**`config` shape** is rule_kind-specific. Each `rules/{kind}.py` exports a Pydantic `ConfigModel`. The `bracket_policies` queries module validates inserts against these models — typo'd seed rows fail at insert, not at handler eval.

**Postgres-level CHECK constraints (v1):**

- `position_protection.rule_kind IN ('stop_loss','trailing_tp','take_profit_fixed','combo_tp_alert')`
- `position_protection.asset_class IN ('stock','long_option','debit_combo','credit_spread','covered_call','unclassified')`
- `bracket_policies.rule_kind` and `bracket_policies.asset_class` — same enum values

The CHECK constraints are intentionally narrow — adding a v2 rule_kind requires an Alembic migration, which is the right level of friction for "we're growing the engine."

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

**Manual leg-by-leg construction (operator opens a credit spread one leg at a time through `/orders/place` rather than the combo wizard):** the classifier sees leg 1 with no parent BAG and would naïvely classify it as `LONG_OPTION` or `UNCLASSIFIED`, then leg 2 fills and the position is now a credit spread but leg-1's rule is wrong. Two layered defenses:

1. The atomicity gate (§6.3) holds classification on any single-leg fill that has a sibling order at the same scope/symbol/expiry within `manual_assembly_window_s` (default 60s).
2. v1 policy: **multi-leg structures should go through combo wizard or a parent-BAG order**. Manual leg-by-leg construction is unsupported and surfaces a `manual_multi_leg_unsupported` operator notification when detected. This matches existing combo wizard policy and the user's actual workflow.

### 6.3 Multi-leg fill atomicity

`record_fill()` fires per execution, not per parent order. A 4-leg condor produces 4 separate hook invocations. Without an atomicity gate, partial-state classification would arm wrong rules.

Gate logic:

```
fill arrives →
  parent_order_id = lookup_parent(fill.submission_id)            # via order_submissions
  if parent is single-leg:
      classify + arm immediately
  else (parent is multi-leg BAG):
      mark fill as 'partial' in state
      check: have all expected legs of parent now filled?
        no  → return without classifying (next fill will retry)
        yes → fetch all sibling fills, build combined position, classify, arm
```

Implementation notes:

- Parent leg-count comes from `order_submissions.legs_count` (existing column on the BAG order row) — no new state needed.
- Reconciliation-discovered fills (post-restart) use the same gate against `order_submissions` history.
- The classifier is called **once per parent order** when the last leg fills.
- Single-leg orders bypass the gate entirely.

This also handles the manual leg-by-leg edge case (§6.2) when paired with `manual_assembly_window_s`.

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
for each position in IBClient.positions(scope):
  if no position_protection row exists in non-terminal state:
    emit outbox event kind='unprotected_position_detected'
    increment health.unprotected_count
    notify operator via macOS toast (rate-limited daily)
```

The operator's remediation is `xenon-position-rules sweep --apply --account-scope <…>` or per-position arming. The UI portfolio page surfaces these via the per-position shield badge in `UNCLASSIFIED`/none state with a tooltip "out-of-band fill — sweep to protect".

### 6.6 Failure modes — arm hook

The hook is wrapped in a try/except that catches everything except `KeyboardInterrupt`/`SystemExit`. Failures are categorised:

| Failure                                                                                  | Handling                                                                                                                                                                                                                                                                          |
| ---------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Classifier raises (e.g., malformed leg shape)                                            | Log + outbox event `kind='arm_hook_classify_error'`; fill record is not affected.                                                                                                                                                                                                 |
| `INSERT INTO position_protection` raises (constraint violation other than `ON CONFLICT`) | Log + outbox event; fill record durable.                                                                                                                                                                                                                                          |
| **DB connection lost mid-hook**                                                          | Degenerate case: `record_fill()` itself would have failed before the hook ran, since the fill INSERT and hook share the same DB transaction. The hook is called only if the fill was already durably written. So "DB down → hook silently skipped" is impossible by construction. |
| Notify path raises (macOS not available)                                                 | Swallow; existing `_default_notify` does this already.                                                                                                                                                                                                                            |

The hook **never raises** to the caller. `record_fill()` always returns successfully if the fill was durably written.

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

- **Native-order liveness check** (every tick, not boot-only): for ARMED rows with `native_order_perm_id`, the handler queries IB-side order state. If broker reports `Cancelled` / `Inactive` / `ApiCancelled`, the operator manually killed the child in TWS — row transitions to `CANCELED`, any orphaned siblings are cleaned, no re-arm. This closes the "user cancelled in TWS, system thinks it's still protected" silent-failure window.
- **Mark / spot coalescing**: `mark_cache` and `spot_cache` are scoped to a single tick. If 30 credit-spread rows reference SPY, the underlying spot is read once per tick across all of them. With ~50 positions this keeps per-tick IB API calls in the low-double-digits — well within IB's ~50/sec pacing limit.
- **Order of operations matters**: liveness check before mark read, because a cancelled native bracket is more urgent than a fresh trigger evaluation.

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

- **Daemon singleton:** `pg_try_advisory_lock(LOCK_KEY_POSITION_RULES)`. Two instances → one wins, the other no-ops. Lock is connection-scoped (released by Postgres when the daemon's PG connection closes); a hung daemon process holding the connection will hold the lock — operationally surfaced via the health endpoint's `last_tick_age_seconds` going stale.
- **Row-level CAS:** every state transition uses `UPDATE … WHERE state=$expected RETURNING …`.
- **Boot reconcile** runs once on daemon start before the first tick. **If `IBClient` is not connected at boot, reconcile is deferred** — rows stay in their current state, the handler tick begins normally, and reconcile is re-attempted on the first successful IB connection event (next bullet). Boot reconcile steps:
  1. `ARMED` rows with `native_order_perm_id` set: verify IB order live; if not → `PENDING_ARM` (re-arm) or `CANCELED` (position also gone).
  2. `TRIGGERED` rows: cross-check IB executions for matching close — filled → `CLOSED`; working → leave; not found → revert to `ARMED`.
  3. `PENDING_ARM` rows: leave; first tick retries arm.
- **Reconnect-triggered reconcile**: `IBClient` exposes a `connected` event. The handler subscribes once at startup; on every connection-restored event (after a disconnect of any duration) it re-runs the boot reconcile sequence before resuming normal ticks. This catches drift accumulated during long Gateway outages (e.g., a user-cancellation in TWS that happened while we were disconnected).
- **Quarter-end re-arm sweep** (separate daily cron at 16:30 ET): IB cancels untriggered GTC orders at end of calendar quarter ([IBKR docs](https://interactivebrokers.github.io/tws-api/bracket_order.html)). Sweep scans `ARMED` rows with `native_order_perm_id`, transitions stale ones to `PENDING_ARM`.
- **Daily out-of-band fill sweep** (same daily cron, immediately after re-arm): scans `IBClient.positions()` for entries with no matching `position_protection` row in non-terminal state; emits `kind='unprotected_position_detected'` outbox events and increments `health.unprotected_count`. See §6.5.

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

**`GET /position-rules/health` response shape:**

```jsonc
{
  "schema_version": 1,
  "daemon_alive": true,
  "advisory_lock_held": true,
  "last_tick_at": "2026-05-04T14:23:11Z",
  "last_tick_age_seconds": 18,
  "rule_counts_by_state": {
    "PENDING_ARM": 0,
    "ARMED": 12,
    "TRIGGERED": 1,
    "FAILED": 0,
    "CANCELED": 47,
    "CLOSED": 1842,
    "SUPERSEDED": 8,
  },
  "stale_quote_skips_last_hour": 0,
  "unprotected_position_count": 0,
  "ib_connected": true,
  "scope": { "broker": "IB", "account_env": "live", "broker_account": "U..." },
}
```

The global UI indicator computes its color from: green if `daemon_alive && advisory_lock_held && last_tick_age_seconds < 60 && rule_counts_by_state.FAILED == 0 && unprotected_position_count == 0`; amber if any of those soft-fail; red if `last_tick_age_seconds > 300` or `!daemon_alive`.

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

`scripts/checks/frozen_config_at_arm.py` — static check enforcing the frozen-config invariant. Implementation: AST-based scan of every file under `src/xenon/execution/brackets/rules/`. Fails the build if any module imports from `xenon.db.queries.bracket_policies` or contains string-literal references to the `bracket_policies` table. Rule modules must operate exclusively on the `config` dict passed in by the handler — which is sourced from `position_protection.config`, frozen at insert time. Prevents the class of bug where a `psql UPDATE bracket_policies` would silently change the threshold of an already-armed position mid-flight.

## 14. Migration sequence

Single PR, single Alembic revision, atomic. **Forward-only** (see rollback note below).

```
Migration A (additive):
  CREATE TABLE xenon.position_protection
  CREATE TABLE xenon.bracket_policies
  INSERT 9 seed rows into bracket_policies
  CREATE indexes (partial + lookup)
  CREATE CheckConstraints (broker, account_env, state, rule_kind, asset_class enums)

Migration B (combo-wizard repointing — same alembic revision):
  ASSERT (SELECT COUNT(*) FROM xenon.wizard_protection) = 0
    -- aborts migration if non-zero; safety net even though user confirms empty
  DROP TABLE xenon.wizard_protection

Code edits (same PR):
  src/xenon/db/schema.py                                  drop wizard_protection table def; add position_protection + bracket_policies
  src/xenon/db/queries/combo_wizard.py                    point INSERT/UPDATE/SELECT at position_protection (filter rule_kind='combo_tp_alert')
  src/xenon/execution/combo_wizard/protect.py             write rule_kind='combo_tp_alert' rows to position_protection (with auto_place=FALSE)
  src/xenon/monitor_daemon/handlers/wizard_stop_monitor.py  DELETE — logic absorbed into rules/combo_tp_alert.py
  src/xenon/monitor_daemon/run.py                         remove WizardStopMonitorHandler registration; register PositionRulesHandler
  src/xenon/db/tests/test_combo_wizard.py                 update fixtures (table name + new shape)
  src/xenon/db/tests/test_schema.py                       drop wizard_protection assertion, add position_protection + bracket_policies
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
- **Re-anchoring on add-on fills** — v1 freezes the entry-price anchor at first-fill (§6.4). For ramp-add positions (operator builds size over multiple fills), this means later adds are protected at the first-fill anchor — possibly far from the new average cost. v2 may detect adds and offer a re-anchor prompt; v1 punts to the operator's manual sweep workflow. Decision: do nothing automatically, surface the situation in the UI ("position size changed by N% since arm" indicator on the drawer).
- **`manual_assembly_window_s` value** — §6.2 / §6.3 use 60s as the window in which sibling single-leg fills get held for atomicity gating. Profile-driven; if false-hold rate is significant, tune lower; if false-pair rate (separate single-leg fills wrongly grouped) is significant, tune lower aggressively.

## 18. Decisions log (during brainstorm)

| ID  | Decision                                                                                                                                                                                                                | Reasoning                                                                                                                                   |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| Q1  | All asset classes; IB only v1                                                                                                                                                                                           | Futu replicates IB stack later                                                                                                              |
| Q2  | New fills auto; existing positions via explicit sweep                                                                                                                                                                   | Don't disturb live book on day 1                                                                                                            |
| Q3  | Hybrid policy resolution (asset-class defaults, scope overrides)                                                                                                                                                        | Tighten via Gate-3 cap if needed                                                                                                            |
| Q4  | Trailing TP activates after threshold for options/combos; immediate for stocks                                                                                                                                          | "Don't trail until I'm actually winning"                                                                                                    |
| Q5  | Concrete defaults: stocks −8% / 5%, long options −20% / 25% / +30% activation, debit combos 50%-of-max-loss / 25% trail / +25% activation, credit spreads (2× credit OR strike-touch) / 50% TP, covered calls refuse v1 | User-tuned during brainstorm; will be data-driven once simulator ships                                                                      |
| D1  | Generalize `wizard_protection` → `position_protection`                                                                                                                                                                  | Cleaner; table is empty                                                                                                                     |
| D2  | Auto-place vs alert-only is per-policy flag, not per-system                                                                                                                                                             | Preserves wizard's existing `combo_tp_alert` semantics                                                                                      |
| D3  | Opaque deterministic `position_key` string                                                                                                                                                                              | No prior convention; reversible via descriptor JSONB                                                                                        |
| D4  | Single PR for migration A+B                                                                                                                                                                                             | Empty table; mechanical reshape; atomic is acceptable                                                                                       |
| R1  | `wizard_stop_monitor.py` is **deleted** in Migration B; `rules/combo_tp_alert.py` absorbs its logic; `PositionRulesHandler` is the only handler                                                                         | Self-review found: keeping both handlers would double-evaluate `combo_tp_alert` rows. One handler, dispatch-by-rule_kind                    |
| R2  | `config` is frozen at **insert time (PENDING_ARM)**, not arm time; CI guard enforces                                                                                                                                    | Self-review found wording inconsistency. Insert-time freeze is what the post-fill hook actually does; arm-time was misleading               |
| R3  | Multi-leg fill atomicity gate via parent BAG order_id lookup; classification deferred until all legs filled                                                                                                             | Self-review found: per-execution `record_fill` would partial-classify combos and arm wrong rules                                            |
| R4  | Entry-price anchor frozen in `state_data.anchor_price` at insert; add-on fills don't re-anchor                                                                                                                          | Self-review found "entry price" was undefined for ramp-add positions; pinned to first-fill weighted average                                 |
| R5  | Native-order liveness check runs every tick (not boot-only) for ARMED rows with `native_order_perm_id`                                                                                                                  | Self-review found: user-cancellation in TWS would silently leave system thinking position is protected                                      |
| R6  | Alert-only debounce via `min_realert_interval_s` (default 1h); edge-triggered fire on threshold crossing                                                                                                                | Self-review found: per-tick alert re-fire on sustained breach would be operationally awful                                                  |
| R7  | Out-of-band fills (TWS direct, mobile app, assignment) detected via daily reconciliation pass; surfaced as `unprotected_position_detected` outbox events                                                                | Self-review found: post-fill hook only fires for fills through `orders_store`; positions opened outside that path were silently unprotected |

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
- [ ] **14 days of "clean operation" on paper** before flipping `XENON_POSITION_RULES_ENABLED=1` on live (real-money). "Clean operation" defined explicitly as **all** of:
  - Zero rows reach `FAILED` state for non-structural reasons (`naked_short_blocked` and `corporate_action_suspected` are structural, allowed)
  - Zero unexpected triggers (a trigger that fires when the operator believes the threshold should not have been hit — judged manually against the daily outbox event review)
  - At least 1 successful end-to-end trigger → MKT-flatten → CLOSED cycle observed in paper (proves the write path actually works under live IB conditions)
  - At least 1 successful boot-reconcile observed (daemon kill + restart with ARMED rows, recovers cleanly)
  - At least 1 successful native-bracket attach + IB-side liveness verification observed
  - `health.unprotected_position_count` returned to zero within 1 daily-sweep cycle of any out-of-band fill detected
  - Quote staleness skip rate (`stale_quote_skips_last_hour` / `rule_counts_by_state.ARMED`) stays below 5% in aggregate during RTH
- [ ] `docs/reference/order-path-incident-history.md` gets a new row referencing this design (per CLAUDE.md convention for order-path changes).

---

_End of design document._
