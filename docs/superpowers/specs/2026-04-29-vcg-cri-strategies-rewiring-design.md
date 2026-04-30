# VCG-R + CRI Strategies Rewiring — Design

**Date:** 2026-04-29
**Backlog item:** `docs/todo-backlog.md` § 7 — Activate existing CRI / VCG strategies
**Strategy specs (unchanged by this work):** `docs/trading/strategy-vcg.md`, `docs/trading/strategies.md` (Strategy 6 — CRI)

---

## 1. Summary

VCG-R (Volatility-Credit Gap, revised) and CRI (Crash Risk Index) scanners exist. **VCG-R already writes to `vcg_series`** via `POST /vcg/scan` (`server.py:594`). **CRI does not currently write to `cri_series`** — its CLI prints JSON to stdout (`scanners/cri.py:1494`), and `POST /regime/scan` archives the payload to `data/cri.json` and `scan_results/` (`server.py:572`). The `cri_series` schema also has a column-naming mismatch with the scanner output (schema reads `crash_trigger.fired` / `cta.forced_reduction`; scanner emits `triggered` / `forced_reduction_pct`).

This spec wires both scanners into the order-entry path as a **risk-budget override** that throttles or blocks user-initiated trades when the regime classifier signals a hedging window, with per-trade override and full audit logging. **A new Phase 0 (§8.0) is required first** to repair CRI persistence and the column mismatch — without Phase 0, `regime_state` would be empty or miss CRI trigger data.

The math, thresholds, and signal definitions are not changed by this work. Only the integration is.

### 1.1 Errata corrected from initial review (2026-04-29)

This spec was revised after a code-anchored audit. The corrections are listed once here so reviewers do not have to diff against an earlier version:

1. **CRI persistence is now a Phase 0 prerequisite** (§8.0). Scanner writes `cri_series` rows; column-naming mismatch fixed.
2. **Throttle has a precise contract** (§3.2, §4.5). It is **not** a silent quantity rewrite. Throttle imposes a halved per-order risk-budget cap (1.25% of bankroll) and returns HTTP 422 `resize_required` when the incoming order exceeds it. Cover-ratio tightening (1.0 → 1.25) is a separate orthogonal effect on TIER_2.
3. **EDR is THROTTLE, not OK** (§3.2 row, §4.5 step). Self-contradiction in v1 fixed.
4. **`regime_overrides` keys on `submission_id` + scope** (§4.3), with FK to `order_submissions` (DEFERRABLE), `account_env`/`broker`/`broker_account` columns, and `perm_id`/`ib_order_id` filled by post-fill UPDATE.
5. **Modify gating is bounded** (§4.6). Pure price modifies and quantity-decreases bypass the gate; quantity increases, side changes, and replacements run through it.
6. **Notification path uses `xenon.db.events.emit()` outbox**, not a fictional `PushNotification` symbol (§4.8). The outbox + notify trigger already exists (`9b645325b50d_add_outbox_notify_trigger`).
7. **Scheduler uses `pg_try_advisory_lock`** for multi-worker safety (§4.1, §6), mirroring the UW daily job pattern (`server.py:335`).
8. **Web `/api/regime` route is rewritten to proxy FastAPI** in Phase 0 (§4.9, §8.0) — removes `data/cri.json` reads and client-side CRI recomputation in `RegimePanel.tsx`.

## 2. Goals and non-goals

**In scope.**

- **Phase 0 corrections** (§8.0): CRI scanner emits boolean `crash_trigger.fired` + `cta.forced_reduction` fields; a CRI persistence path writes `cri_series` rows; web `/api/regime` route proxies FastAPI and stops reading `data/*.json`; scheduler acquires a Postgres advisory lock for single-worker semantics; notifications go through the existing `xenon.db.events.emit()` outbox.
- Scheduler hookup so both scanners run on a 30-minute cadence during market hours, writing to the existing `vcg_series` and `cri_series` tables.
- A Postgres view `regime_state` projecting the latest row of each scanner into a single thin shape.
- A new audit table `regime_overrides` recording every override decision, keyed on `submission_id` and scoped per `(account_env, broker, broker_account)`.
- A FastAPI dependency `get_regime_state` with a 30-second in-process TTL cache.
- A `RegimeGate.veto(order)` helper called from order routes (`POST /orders/place`, `POST /orders/modify` for quantity-increase / side-change / replacement only, the wizard combo-submit path).
- A `GET /regime` endpoint backing the UI banner.
- A regime-transition emitter via `xenon.db.events.emit()` (existing outbox pattern).
- Web UI: extending `RegimePanel.tsx` with a per-scanner tier strip; rewiring `/api/regime` to proxy FastAPI; surfacing throttle/block decisions inline in the order wizard with an override toggle.

**Out of scope.**

- Auto-staged hedge orders (HYG put / SPX put-spread auto-drafts). Carved out as a phase-2 follow-up spec.
- Any change to the VCG-R or CRI signal **math** — only the scanner output **fields** change in Phase 0 to align with the schema and add a CRI persistence path.
- Portfolio-wide auto-rebalancing or auto-deleveraging.
- Sunsetting either scanner — both pass the Four Gates as-is and have published backtests.
- A new notification subsystem. Reuse the existing outbox; downstream consumers (mobile push, Slack, etc.) attach to the outbox separately.
- Holiday calendar. Inherits the same gap as the UW analyzer (weekday holidays treated as open).
- Pure-price modifies and quantity-decreases through `POST /orders/modify` — explicitly not gated (§4.6).
- Cancels and refreshes — explicitly not gated (the way out of a regime should not be locked).

## 3. Architecture

```
┌─────────────────── scheduler (server.py lifespan) ────────────────────┐
│  is_market_open() → run xenon-vcg-scan + xenon-cri-scan every 30 min  │
│  writes to existing tables: vcg_series, cri_series                    │
└───────────────────────────────────────────────────────────────────────┘
                              │ (no new write path)
                              ▼
┌──────────────────── PG: existing + new ───────────────────────────────┐
│  vcg_series  (existing) ─┐                                            │
│  cri_series  (existing) ─┴─→ regime_state  (NEW VIEW: latest of each) │
│  regime_overrides (NEW TABLE: trade_id, vcg_tier, cri_tier,           │
│                              binding_side, override_reason, ts, …)    │
└───────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────── FastAPI (src/xenon/api) ──────────────────────────┐
│  get_regime_state()   FastAPI dependency, 30 s in-process TTL cache   │
│       │                                                               │
│       ├──→ /regime                  UI banner + scanner attribution   │
│       │                                                               │
│       ├──→ RegimeGate.veto(order)   called from order routes:         │
│       │       OK | THROTTLE(cap, cover) | BLOCK(reason)               │
│       │       THROTTLE → 422 resize_required when over cap.           │
│       │       BLOCK is bypassable with `?override=true&reason=…`      │
│       │       which writes a row to regime_overrides.                 │
│       │                                                               │
│       └──→ tier-transition hook     scheduler diffs prev↔current      │
│                                     tiers; on change, calls           │
│                                     xenon.db.events.emit() (outbox).  │
└───────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────── web/ (Next.js) ───────────────────────────────────┐
│  RegimePanel.tsx (existing, extended) — VCG-R + CRI tier strip,       │
│       binding side, scan freshness                                    │
│  Order wizard / blotter — surfaces THROTTLE/BLOCK inline with         │
│       override toggle + reason input; reads /regime once per render   │
└───────────────────────────────────────────────────────────────────────┘
```

### 3.1 Invariants

1. **Single source of truth for regime state.** UI banner and order gate both read `regime_state` via `get_regime_state`. No client-side tier recomputation.
2. **Independent attribution preserved.** `regime_state` returns `vcg_tier` and `cri_tier` as separate columns; whichever is stricter binds the gate, but the row says _which one_ did. The UI shows the binding side; the override row records both.
3. **Order-path allowlist stays canonical.** `RegimeGate.veto` is called from the same set of routes guarded by `scripts/checks/order_path_caller_allowlist.py`. No new entry points to `xenon.execution.ib_place_order`.
4. **Defined-risk hedges always pass.** Orders matching canonical hedge structures from `docs/trading/options-structures.json` (HYG put, SPX put-spread, VIX call-spread, etc., enumerated in §4.5) are never blocked, only annotated.
5. **Stale-data fail-safe.** If the latest scanner row is older than `XENON_REGIME_MAX_AGE_S` (default 90 min), `regime_state.tier` reports `UNKNOWN`. `UNKNOWN` is treated as `THROTTLE` (half-Kelly), not `OK` and not `BLOCK`.
6. **Override is logged, not silent.** Every `BLOCK→override` decision writes a row to `regime_overrides` with the original reason, the user-supplied justification, the binding scanner, and the resulting `trade_id`.

### 3.2 Throttle level mapping (β across the board, from Q3)

| Regime tier                           | RegimeGate decision                                                                             | Web wizard surface                                              |
| ------------------------------------- | ----------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| `NORMAL` (no signal)                  | `OK`                                                                                            | no banner                                                       |
| `EDR` (VCG only, watch)               | `THROTTLE(soft)` — risk-budget halved; cover-ratio unchanged                                    | "VCG-R EDR — risk-budget halved on this trade"                  |
| `TIER_2` (VCG Tier 2 or CRI HIGH)     | `THROTTLE(strict)` — risk-budget halved **and** covered-call cover-ratio tightened (1.0 → 1.25) | "Risk-budget halved; covered-call ratio tightened"              |
| `TIER_1` (VCG Tier 1 or CRI CRITICAL) | `BLOCK` for non-hedge entries                                                                   | "All new entries blocked except defined-risk hedges. Override?" |
| `PANIC` (VIX ≥ 48)                    | `BLOCK` for non-hedge entries (same as TIER_1)                                                  | "Panic regime — order routes hardened"                          |
| `UNKNOWN` (stale data)                | `THROTTLE(soft)`                                                                                | "Regime data stale (>90 min). Sized conservatively."            |

#### 3.2.1 What `THROTTLE` actually does (precise contract)

Throttle is a **server-side cap with a friendly resize protocol**, not a silent quantity rewrite and not advisory text.

1. **Risk-budget cap.** The gate computes `max_loss_cap_usd = base_pct × bankroll`, where `base_pct = 0.0125` for THROTTLE (half of the Four Gates default `0.025`) and `bankroll = AccountScope.net_liq_usd`. If the order's `max_loss_usd` (computed from defined-risk structures: premium paid for longs; `width × contracts × 100 - net_credit` for verticals; full notional for any naked structure that somehow slipped through) exceeds `max_loss_cap_usd`, the route returns **HTTP 422** with body `{"decision": "resize_required", "max_loss_cap_usd": ..., "binding_tier": ..., "reason": ...}`. The wizard shows a "Trim to fit?" prompt that pre-fills the largest contract count whose `max_loss_usd ≤ cap` and lets the user resubmit with one click. **No `regime_overrides` row** — resize is not an override, just a re-prompt.
2. **Cover-ratio tightening (TIER_2 only).** Existing predicate in `src/xenon/api/guards.py` requires `long_shares ≥ 1.0 × short_call_contracts × 100` for short-call structures. Under TIER_2, that ratio is parameterized to `1.25`. Orders failing the tightened predicate are rejected with the existing covered-call guard's HTTP code (currently 422); the rejection message names the binding tier so the user understands why.
3. **No quantity rewrite.** The gate never silently mutates the order body. The user always sees, acknowledges, and resubmits the trimmed quantity.

`BLOCK` returns **HTTP 409** with body `{"decision": "block", "binding_tier": ..., "binding_side": ..., "reason": ...}`; the wizard renders an inline modal with a `≥10 char` reason field. Confirm resubmits the original payload with `?override=true` and `override_reason=...`. The server writes a `regime_overrides` row in the same transaction as the submission reservation; if either fails, both roll back (§6).

`OK` and `THROTTLE(soft)` paths run no extra cover-ratio logic; the existing per-order guards are untouched.

### 3.3 Architectural choices, justified

- **View, not table, for `regime_state`.** Eliminates two-writer drift; `vcg_series` and `cri_series` remain the only writers.
- **In-process 30-s TTL cache, not Redis.** Single-process FastAPI; per-request PG cost is small, but typing in the order wizard fires repeated `/regime` reads.
- **Audit log as its own table.** Override decisions are first-class data and reflect user intent — they only make sense as a real table.
- **Outbox-based notifications.** No new notification subsystem; reuse the existing `xenon.db.events.emit()` outbox + notify trigger (`9b645325b50d_add_outbox_notify_trigger`). Single emitter for v1: regime tier transition. Downstream consumers (mobile push, Slack, etc.) attach to the outbox separately and are out of scope here.

## 4. Components

### 4.1 Scheduler hookup

**File:** `src/xenon/api/server.py` (existing `lifespan` async context manager, around line 238).

```python
_VCG_CRI_LOCK_KEY = 0xVCG_CRI  # 64-bit constant; see migration for value

async def _vcg_cri_scan_loop():
    """Run VCG-R and CRI scans every 30 minutes during market hours.

    Phase 0 ensures CRI writes to cri_series via a persistence helper invoked
    after the CLI returns its JSON; VCG already writes to vcg_series via
    POST /vcg/scan logic.

    Multi-worker safety: at startup (under FastAPI lifespan), the loop
    attempts pg_try_advisory_lock(_VCG_CRI_LOCK_KEY). Workers that fail to
    acquire the lock log "vcg_cri loop already running on another worker"
    and exit cleanly — only one worker scans. Mirrors UW daily job pattern
    at server.py:335.
    """
    async with _pg_try_advisory_lock(_VCG_CRI_LOCK_KEY) as got_lock:
        if not got_lock:
            log.info("vcg_cri loop already running on another worker; this worker is no-op")
            return
        last_seen: tuple[str, str] | None = None  # (vcg_tier, cri_tier)
        while True:
            if _is_market_open_now():
                try:
                    await _run_vcg_scan_and_persist()  # writes vcg_series
                    await _run_cri_scan_and_persist()  # Phase 0: writes cri_series
                    new_state = await _read_latest_regime_tiers()
                    if last_seen is not None and new_state != last_seen \
                       and "UNKNOWN" not in (last_seen + new_state):
                        await xenon.db.events.emit(
                            kind="regime_transition",
                            payload=_build_transition_payload(last_seen, new_state),
                        )
                    last_seen = new_state
                except Exception:
                    log.exception("vcg_cri scan tick failed")
                    # do not abort loop; retry on next tick
            await asyncio.sleep(30 * 60)
```

Any pre-existing CRI scheduler discovered in Phase 1's audit step is consolidated into this loop. The audit also documents whether the existing `POST /vcg/scan` and `POST /regime/scan` HTTP triggers continue to exist as user-facing manual-refresh endpoints (they do, but they will share the same persistence helpers introduced in Phase 0 rather than duplicating write paths).

### 4.2 Postgres view: `regime_state`

**Migration:** new Alembic migration under `src/xenon/db/migrations/versions/`.

```sql
CREATE OR REPLACE VIEW regime_state AS
WITH latest_vcg AS (
    SELECT
        scanned_at,
        tier  AS vcg_tier_raw,    -- 1, 2, 3, or NULL
        regime AS vcg_regime,     -- DIVERGENCE, WATCH, ACTIVE, TRANSITION, PANIC
        ro,
        edr,
        bounce,
        sign_ok,
        sign_suppressed,
        pi_panic,
        vix
    FROM vcg_series
    ORDER BY scanned_at DESC
    LIMIT 1
),
latest_cri AS (
    SELECT
        recorded_at,
        cri_score,
        crash_trigger_fired,
        cta_forced_reduction,
        vix AS cri_vix
    FROM cri_series
    ORDER BY recorded_at DESC
    LIMIT 1
)
SELECT
    -- Per-scanner raw inputs
    v.scanned_at      AS vcg_scanned_at,
    v.vcg_tier_raw,
    v.vcg_regime,
    v.ro              AS vcg_ro,
    v.edr             AS vcg_edr,
    v.bounce          AS vcg_bounce,
    v.sign_ok         AS vcg_sign_ok,
    v.pi_panic        AS vcg_pi_panic,
    v.vix             AS vcg_vix,
    c.recorded_at     AS cri_scanned_at,
    c.cri_score,
    c.crash_trigger_fired,
    c.cta_forced_reduction,
    c.cri_vix
FROM latest_vcg v CROSS JOIN latest_cri c;
```

The view is intentionally raw — tier classification (NORMAL / EDR / TIER_2 / TIER_1 / PANIC / UNKNOWN) is computed in Python (`regime_state.py`, §4.4) where it can be unit-tested in isolation and matches the table in §3.2 exactly.

### 4.3 Postgres table: `regime_overrides`

The audit table is keyed on **`submission_id`** (the pre-broker reservation ID from `xenon.order_submissions`, see `src/xenon/execution/orders_store.py:93`) rather than IB `orderId` / `permId`, which are only known post-submit. Broker IDs are filled in via UPDATE after submission, mirroring the existing `orders_store` two-phase pattern at `orders_store.py:324`.

```sql
CREATE TABLE regime_overrides (
    id                BIGSERIAL PRIMARY KEY,
    ts                TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    user_id           TEXT NOT NULL,

    -- AccountScope columns (mirror PR #52 pattern)
    account_env       TEXT NOT NULL,         -- "paper" | "live"
    broker            TEXT NOT NULL,         -- "ib" (Futu is read-only)
    broker_account    TEXT NOT NULL,         -- broker account id

    -- Order linkage
    submission_id     TEXT NOT NULL,         -- xenon.order_submissions.submission_id
    client_attempt_id TEXT,                  -- idempotency key from request
    perm_id           BIGINT,                -- IB permId, filled post-submit by UPDATE
    ib_order_id       BIGINT,                -- IB orderId, filled post-submit by UPDATE

    -- Audit content
    route             TEXT NOT NULL,         -- e.g. "POST /orders/place"
    vcg_tier          TEXT,                  -- TIER_1 / TIER_2 / EDR / NORMAL / UNKNOWN
    cri_tier          TEXT,
    binding_side      TEXT NOT NULL,         -- "vcg" | "cri" | "both"
    block_reason      TEXT NOT NULL,         -- the gate's reason string
    user_reason       TEXT NOT NULL,         -- user-supplied justification (≥10 chars)
    order_payload     JSONB NOT NULL,        -- full order body, redacted of secrets

    -- FK is DEFERRABLE so we can write the audit row in the same transaction as
    -- the order_submissions row (audit insert may fire before submissions commit).
    CONSTRAINT fk_regime_overrides_submission
        FOREIGN KEY (submission_id)
        REFERENCES order_submissions(submission_id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX ix_regime_overrides_ts ON regime_overrides (ts DESC);
CREATE INDEX ix_regime_overrides_submission ON regime_overrides (submission_id);
CREATE INDEX ix_regime_overrides_user_ts ON regime_overrides (user_id, ts DESC);
CREATE INDEX ix_regime_overrides_scope_ts
    ON regime_overrides (account_env, broker_account, ts DESC);
```

**Linkage protocol.**

1. Order route reserves a `submission_id` (existing `orders_store.reserve_submission`).
2. If the gate returns `BLOCK` and the request carries `?override=true`, the route opens a transaction, inserts the `regime_overrides` row with the reserved `submission_id`, then proceeds with broker submission.
3. After IB returns, `orders_store.mark_ib_order_id` runs as today; an additional helper `mark_regime_override_perm_id(submission_id, perm_id, ib_order_id)` updates the audit row's `perm_id` and `ib_order_id` columns.
4. If broker submission fails after the audit row was written: the transaction rolls back both the `order_submissions` reservation and the `regime_overrides` row. The deferred FK ensures this is consistent.

Blotter linkage is a join on `submission_id` (or `perm_id` once filled).

### 4.4 `regime_state.py` — dependency + classifier

**File:** `src/xenon/api/services/regime_state.py` (new).

```python
@dataclass(frozen=True)
class RegimeState:
    vcg_tier: TierLabel        # NORMAL | EDR | TIER_2 | TIER_1 | PANIC | UNKNOWN
    cri_tier: TierLabel        # NORMAL | TIER_2 | TIER_1 | UNKNOWN
    binding_tier: TierLabel    # max(vcg, cri) by ordinal
    binding_side: str          # "vcg" | "cri" | "both" | "none"
    vcg_scanned_at: datetime | None
    cri_scanned_at: datetime | None
    is_stale: bool             # max age > XENON_REGIME_MAX_AGE_S
    panic_active: bool         # VIX >= 48 on either scanner
    raw: dict                  # full row for /regime clients

async def get_regime_state(scope: AccountScope = Depends(get_account_scope)) -> RegimeState:
    """Returns current regime state. Cached 30 s in-process per-process."""
```

Classification rules (must match §3.2 table):

- `vcg_tier`: derived from `vcg_tier_raw` and `vcg_regime`. `pi_panic >= 1.0` → `PANIC`. `tier_raw == 1` → `TIER_1`. `tier_raw == 2` → `TIER_2`. `edr == 1` → `EDR`. else `NORMAL`. `vcg_scanned_at` older than max-age → `UNKNOWN`.
- `cri_tier`: `crash_trigger_fired = TRUE` or `cri_score >= 75` → `TIER_1`. `cri_score >= 50` → `TIER_2`. else `NORMAL`. Stale → `UNKNOWN`.
- `binding_tier`: `max` by ordinal `NORMAL < EDR < TIER_2 < TIER_1 < PANIC`. `UNKNOWN` is treated as `EDR` for ordinal purposes (ordinal pegged to "throttle, don't block").

Cache: a `functools`-style 30-s TTL keyed on `(account_env, broker_account)` from the `AccountScope`. Tests opt out by setting `XENON_REGIME_CACHE_TTL_S=0`.

### 4.5 `regime_gate.py` — the order-path veto

**File:** `src/xenon/api/services/regime_gate.py` (new).

```python
class GateDecision(Enum):
    OK = "ok"
    THROTTLE = "throttle"
    BLOCK = "block"

@dataclass(frozen=True)
class GateResult:
    decision: GateDecision
    reason: str                  # "" when OK
    bind: str                    # "vcg" | "cri" | "both" | "none"
    # THROTTLE-only fields (zero/None when decision != THROTTLE):
    max_loss_cap_usd: float | None    # halved per-order risk-budget cap
    cover_ratio: float | None         # 1.25 for TIER_2, 1.0 otherwise

class RegimeGate:
    @staticmethod
    def veto(order: PreflightRequest, state: RegimeState,
             bankroll_usd: float) -> GateResult: ...
```

Decision tree (matches §3.2):

1. If `state.binding_tier in {TIER_1, PANIC}` and `not _is_hedge(order)` → `BLOCK(reason="<tier> — non-hedge entries blocked")`.
2. Else if `state.binding_tier == TIER_2` → `THROTTLE(strict)` with `max_loss_cap_usd = 0.0125 × bankroll_usd, cover_ratio = 1.25`.
3. Else if `state.binding_tier in {EDR, UNKNOWN}` → `THROTTLE(soft)` with `max_loss_cap_usd = 0.0125 × bankroll_usd, cover_ratio = 1.0`.
4. Else (`NORMAL`) → `OK`.

The order route, after receiving `GateResult`:

- **OK:** continues unchanged.
- **THROTTLE:** computes the order's `max_loss_usd`. If exceeds `max_loss_cap_usd` → HTTP 422 `resize_required`. The TIER_2 `cover_ratio = 1.25` is plumbed into the existing covered-call guard via a parameter (the guard already exists in `src/xenon/api/guards.py`; this work parameterizes its current hard-coded `1.0`).
- **BLOCK:** HTTP 409 unless `?override=true` with valid reason — in which case the route writes the audit row and proceeds.

`_is_hedge(order)` matches against a hard-coded structure set drawn from `docs/trading/options-structures.json`:

- HYG put (single or debit vertical), JNK put, LQD put, SPX/SPY put or put-spread, VIX call or call-spread.
- The match is symbol-aware (only counts as a hedge if the underlying is in the credit/equity-index hedge set).
- Long-only or defined-risk debit verticals only — no short calls, no naked structures even on hedge underlyings.
- For multi-leg combos: structural classification wins. If `options-structures.json` classifies the whole structure as a hedge, the whole order passes; otherwise it gates normally regardless of leg-count.

#### 4.5.1 Computing `max_loss_usd` from a `PreflightRequest`

Helper `_max_loss_usd(order)` lives next to `_is_hedge`:

- Long single leg: `premium_per_contract × contracts × 100`.
- Debit vertical: `(width − net_debit) × contracts × 100` capped at `width × contracts × 100`. Use `net_debit` if present in payload, else compute from per-leg `limit_price`.
- Credit spread / iron condor / butterfly (defined-risk): `(width − net_credit) × contracts × 100`.
- Anything not classified as defined-risk: `inf` — falls back to "exceeds cap" path. Naked structures should already be rejected by Gate 4 / the covered-call predicate; this is a belt-and-suspenders default.

Bankroll: `AccountScope.net_liq_usd` from the existing scope dependency. Test override: `XENON_REGIME_BANKROLL_USD_OVERRIDE` env var.

### 4.6 Order route integration points

The gate runs only on **new exposure**: brand-new orders, replacements, side-changes, and quantity increases. Pure price modifies and quantity decreases bypass the gate (they reduce or reshape existing risk; gating them would punish exits during the regime).

| Route / call site                                       | Gate runs?                               | Notes                                                                                                                                                                     |
| ------------------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /orders/place` (`server.py:1901`)                 | **Always**                               | Primary entry point.                                                                                                                                                      |
| `submit_combo` / wizard combo-submit (`session.py:327`) | **Always**                               | Calls `_orders_place_from_body` directly; gate must run inside that handler so both the HTTP path and in-process calls are covered. (`feedback_in_process_route_bypass`.) |
| `POST /orders/modify` (`server.py:2270`)                | **Conditional** — see modify rules below | Modify body has only IDs + price + quantity + sequence (`server.py:2296`). Original structure must be loaded from `order_submissions` to evaluate `_is_hedge`.            |
| Wizard reprice (`session.py:398`)                       | **Never**                                | Price-only change by construction.                                                                                                                                        |
| `POST /orders/cancel`                                   | **Never**                                | Cancels are the way out; never block.                                                                                                                                     |
| `POST /orders/refresh`                                  | **Never**                                | Read-only.                                                                                                                                                                |

#### 4.6.1 Modify gating rules

Inside `POST /orders/modify`, after loading the original `order_submissions.payload`:

1. **Pure price change** (`new_quantity == old_quantity`, no side change): skip gate.
2. **Quantity decrease** (`new_quantity < old_quantity`, same side): skip gate.
3. **Quantity increase** (`new_quantity > old_quantity`): run gate against the **delta** order (a synthetic order representing the increment), not the full new quantity. Throttle cap and cover-ratio apply to the delta as if it were a new entry.
4. **Side change / structure change**: treat as a new entry — gate the entire new order.

If the gate returns `BLOCK` on a quantity-increase or side-change modify, the modify route returns 409 with the same body shape as `/orders/place`'s 409, and the same `?override=true` protocol applies.

#### 4.6.2 Override protocol

`?override=true` query param + `override_reason` body field (preferred over a header — the field is part of the JSON payload that's already audited via `order_payload`). Override is rejected if `reason` is empty or shorter than 10 chars (HTTP 400). The audit row is written in the same transaction as the order-submission reservation; if either fails, both roll back (deferred FK from §4.3).

### 4.7 `/regime` endpoint

**File:** `src/xenon/api/routes/regime.py` (new).

`GET /regime` — returns the `RegimeState` payload as JSON for the UI. Cache-Control: `private, max-age=30`. No auth changes; uses the same Clerk middleware as the rest of the API.

`GET /regime/overrides?limit=50` — paginated audit trail for the current user. Sorted by `ts DESC`. Used by a future "override review" UI; included in v1 because the table is cheap and it lets the user dogfood the audit log immediately.

### 4.8 Tier-transition emission via outbox

**Hook in `_vcg_cri_scan_loop` (§4.1).** When the prev → current tier tuple changes **and** neither side is `UNKNOWN`, emit through the existing outbox:

```python
xenon.db.events.emit(
    kind="regime_transition",
    payload={
        "type": "regime_transition",
        "from": {"vcg": "NORMAL", "cri": "NORMAL"},
        "to":   {"vcg": "TIER_2", "cri": "NORMAL"},
        "binding_side": "vcg",
        "vix": 29.4,
        "vcg": 2.74,
        "cri_score": 42,
        "ts": "2026-04-29T15:00:00Z"
    },
)
```

This writes a row to `events.outbox` (`src/xenon/db/schema.py:720`) and the existing notify trigger (`9b645325b50d_add_outbox_notify_trigger`) fans it out to LISTEN/NOTIFY consumers. Downstream consumers (mobile push, Slack webhook, etc.) attach by listening on the outbox channel and filtering `kind = 'regime_transition'`. Adding such a consumer is **out of scope** for this spec — the contract here ends at the outbox row.

Stale-data transitions (any change involving `UNKNOWN`) are explicitly suppressed at the emit site so the outbox does not flood on scheduler hiccups.

### 4.9 Web changes

- **`web/app/api/regime/route.ts`** — **Phase 0 rewrite.** Currently reads `data/cri.json` and `data/cri_scheduled` files (`route.ts:2, :172`). New behavior: proxy `xenonFetch('/regime')` and return the FastAPI payload unchanged. No file reads, no client-side recomputation. Mirrors the migrated VCG / portfolio routes per CLAUDE.md "Runtime Data Read Paths".
- **`web/components/RegimePanel.tsx`** — **Phase 0 + Phase 3.** Phase 0 removes the live client-side CRI recomputation at `RegimePanel.tsx:232` (display-only overlays may remain, but they must not feed the binding tier). Phase 3 extends the panel with a horizontal strip showing `[VCG-R: TIER_2]  [CRI: NORMAL]`, freshness ages, binding-side highlight, all sourced from `/api/regime` → FastAPI `/regime`.
- **Order wizard** (`web/components/order-wizard/*` — exact files enumerated in Phase 1 audit) — on submit, intercepts:
  - **HTTP 409 `decision=block`** → renders an inline modal: "Order blocked by VCG-R Tier 2. Reason: …. Override (requires justification)?" with a textarea (≥10 chars) and a confirm button. On confirm, resubmits with `?override=true` and `override_reason` in the body.
  - **HTTP 422 `decision=resize_required`** → renders a "Trim to fit?" prompt. Pre-fills the wizard with the largest contract count whose `max_loss_usd ≤ max_loss_cap_usd`. User clicks Apply, wizard resubmits without an override flag.
- **Order blotter** — adds an "Overridden" tag to rows submitted via override, sourced from a join on `regime_overrides.submission_id` (resolved via `order_submissions.submission_id`).

## 5. Data flow walkthroughs

### 5.1 Normal scan, no signal

1. 30-min tick → loop calls `xenon-vcg-scan --json` → writes row to `vcg_series` (tier null, ro=0, edr=0).
2. Loop calls `xenon-cri-scan --json` → writes row to `cri_series` (cri_score=22).
3. Loop reads `regime_state` view → `(vcg_tier=NORMAL, cri_tier=NORMAL)`. No transition vs `last_seen`. No notification.
4. UI banner reads `/regime` → renders muted "regime: normal" strip. Order routes get `OK` from `RegimeGate.veto`.

### 5.2 VCG-R Tier 2 fires, user submits an unhedged short call, overrides

1. Scan writes new `vcg_series` row with `tier=2, regime='ACTIVE', vcg=2.71, vix=29.4`.
2. Loop sees transition `(NORMAL, NORMAL) → (TIER_2, NORMAL)` → outbox event emitted (`kind="regime_transition"`).
3. User opens wizard, builds a single short call on AAPL (no covering shares). Hits Submit.
4. `POST /orders/place` → `get_regime_state` returns `binding_tier=TIER_2, binding_side="vcg"` → `RegimeGate.veto` returns `THROTTLE(strict)` with `cover_ratio=1.25`. The route plumbs the tightened cover-ratio into the existing covered-call guard, which rejects the short call (no cover).
5. Wizard shows "blocked by naked-short guard (tightened by VCG-R Tier 2)". User reduces to a defined-risk vertical (long-put + short-put debit spread). Resubmit → gate returns `THROTTLE(strict)` again; route computes `max_loss_usd` from the vertical's width and contract count, finds it `≤ max_loss_cap_usd`, lets the order proceed. No `regime_overrides` row (no override of a BLOCK; the original rejection was the cover-ratio guard, not the gate's BLOCK path).

### 5.3 CRI CRITICAL fires, user wants a non-hedge anyway

1. Scan writes `cri_series` row with `cri_score=82, crash_trigger_fired=true`.
2. Transition `(NORMAL, NORMAL) → (NORMAL, TIER_1)` → outbox event emitted.
3. User submits a long-call directional bet. `RegimeGate.veto` → `BLOCK("CRI CRITICAL — non-hedge entries blocked")`. HTTP 409 returned.
4. Wizard renders block modal with reason. User types "earnings catalyst already priced in, contrarian play, sized small" (>10 chars). Confirms.
5. Wizard resubmits with `?override=true&override_reason=...`. Server opens a transaction: reserves `submission_id`, writes `regime_overrides` row referencing that `submission_id`, submits the order, marks IB ids back onto the override row post-fill. Both rows commit together. Blotter row tagged "Overridden".

### 5.4 Stale scan data

1. Scheduler hung for 2 hours (e.g. `xenon-vcg-scan` exited non-zero repeatedly). Latest `vcg_series` row is 2h old, `cri_series` is 30 min old.
2. `regime_state` view returns the old VCG row anyway.
3. `RegimeState.is_stale = True` because `vcg_scanned_at` exceeds `XENON_REGIME_MAX_AGE_S`. `vcg_tier` becomes `UNKNOWN`. `binding_tier` resolves to `UNKNOWN` (treated as EDR-equivalent ordinal).
4. `RegimeGate.veto` → `THROTTLE(soft)` with the standard halved cap and `cover_ratio=1.0`. UI banner shows "regime data stale (>90 min) — sized conservatively". Outbox event is **not** emitted for stale-data transitions (suppression rule from §4.8 / §6).

## 6. Error handling

| Failure mode                                                                                         | Behavior                                                                                                                                                                                                                                                              |
| ---------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `xenon-vcg-scan` or `xenon-cri-scan` exits non-zero                                                  | Log structured error, do not write row, do not abort loop. Next tick retries. After 3 consecutive failures, emit a `kind="scanner_stuck"` outbox event (rate-limited to once per day per scanner).                                                                    |
| `regime_state` view returns zero rows (no scans ever ran)                                            | `RegimeState.vcg_tier = cri_tier = UNKNOWN`, `is_stale = True`. Same as §5.4.                                                                                                                                                                                         |
| `regime_overrides` insert fails inside the order transaction                                         | Whole transaction rolls back — order is **not** submitted. HTTP 500 to caller. Deferred FK from §4.3 ensures audit row + `order_submissions` reservation either both commit or both roll back. This is intentional — no order through without audit.                  |
| Outbox `events.emit()` fails on transition                                                           | Log, do not retry, do not abort the scan loop. The UI banner is the durable surface; the outbox event is best-effort signal for downstream consumers.                                                                                                                 |
| User passes `?override=true` with empty / short / missing reason                                     | HTTP 400 "Override reason required (min 10 chars)". No audit row, no submission.                                                                                                                                                                                      |
| Stale-data transition (`UNKNOWN` involved on either side)                                            | Outbox emit suppressed. UI banner shows "regime data stale" but no transition event. Avoids flooding the outbox on scheduler hiccups.                                                                                                                                 |
| Multiple workers attempt `_vcg_cri_scan_loop` (e.g. gunicorn `--workers 2`, dev + prod sharing a DB) | Each worker calls `pg_try_advisory_lock(_VCG_CRI_LOCK_KEY)` at startup; only the lock holder runs the loop, others log "loop already running on another worker" and exit cleanly. Mirrors `server.py:335` UW-daily pattern. Lock is auto-released on connection drop. |
| `cri_series` insert race (Phase 0's CRI persistence helper writes from two paths simultaneously)     | The CRI persistence helper is the **only** writer post-Phase-0 (manual `/regime/scan` and the scheduled loop both call it). It uses `INSERT ... ON CONFLICT DO NOTHING` keyed on `recorded_date` to make duplicate ticks no-ops.                                      |
| Modify with quantity-increase during BLOCK regime                                                    | Synthetic delta-order is constructed and run through the gate; if non-hedge, returns 409 same as a new `/orders/place`. Override path applies symmetrically.                                                                                                          |
| AccountScope mismatch (paper vs live)                                                                | `regime_overrides` carries `account_env` + `broker` + `broker_account` columns; FK to `order_submissions` (which has the same scope columns); audit log is per-scope by construction.                                                                                 |

## 7. Testing strategy

### 7.1 Unit tests

- **`tests/test_regime_state_classifier.py`** — pure-function table tests for the classifier in §4.4. One row per cell of §3.2 plus stale-data rows. No DB.
- **`tests/test_regime_gate.py`** — `RegimeGate.veto` table tests. Combinations: `(binding_tier, is_hedge)` cross-product. Asserts on `GateResult.decision`, `max_loss_cap_usd`, and `cover_ratio`. Also tests `_max_loss_usd` for each defined-risk structure type from §4.5.1. No DB.
- **`tests/test_is_hedge_predicate.py`** — assert that all canonical hedge structures from `docs/trading/options-structures.json` match, and that naked structures on hedge underlyings (e.g. naked HYG short call) do **not** match.

### 7.2 Integration tests (PG required)

- **`tests/test_regime_state_view.py`** — uses the existing test PG (`DATABASE_URL_TEST`). Inserts fixture rows into `vcg_series` and `cri_series`, asserts `regime_state` view returns the right shape, and that LIMIT 1 + ORDER BY behave correctly when newer rows arrive.
- **`tests/test_regime_overrides_audit.py`** — round-trip insert + read; assert that the order route writes a row when override is used; assert that a failed insert prevents order submission.

### 7.3 Order-route integration tests

- Extend `web/tests/fastapiHarness.ts` with a `setRegimeState(...)` helper that pre-seeds `app.state` (mirrors the `TestClient skips lifespan` memory note — pre-seeding is the only correct test path).
- New tests: `web/tests/order-place-regime-block.test.ts`, `order-place-regime-throttle.test.ts`, `order-place-regime-override.test.ts`.

### 7.4 E2E (Playwright)

One golden-path test: `web/e2e/regime-gate-flow.spec.ts`. Sets test PG to TIER_1 state via fixtures, opens wizard, submits non-hedge order, asserts block modal appears, types a reason, submits override, asserts order goes through and blotter shows "Overridden" tag.

### 7.5 CI guards

- **Lands in Phase 4** (the first phase that introduces the gate; guard must protect the very first gated PR onward).
- New `scripts/checks/order_path_regime_gate_called.py` — static check: every `POST /orders/{place,modify}` handler in `src/xenon/api/server.py`, and every in-process call site enumerated in Phase 1's audit (e.g. `submit_combo`, `_orders_place_from_body`), must have a lexical call to `RegimeGate.veto` in the function body (or a documented allowlist exemption for non-new-exposure modify paths). Fails CI if a new order entry point is added without the gate.
- New check inside `scripts/checks/no_json_fallback_on_order_path.py` (or sibling file): assert `web/app/api/regime/route.ts` does not call `readDataFile` / `readFile` / `JSON.parse(fs.readFileSync(...))`. Locks in the Phase 0 rewrite so the file-read regression cannot return.
- Extends the existing order-path guards rather than duplicating the allowlist mechanism.

## 8. Implementation plan

Phased so each phase can land as one PR with full CI green. Each phase ends with a verify checklist in the PR description. **Phase 0 is mandatory before any later phase** — without it, regime_state is empty or wrong.

### Phase 0 — Persistence + plumbing prerequisites

The audit found that the v1 spec assumed integrations that don't yet exist. This phase makes them real.

1. **CRI scanner output normalization.** Modify `src/xenon/scanners/cri.py` to emit boolean `crash_trigger.fired` and `cta.forced_reduction` fields alongside (not replacing) the existing `triggered` and `forced_reduction_pct` numeric fields. The schema-generated columns at `src/xenon/db/schema.py:325, 333` then resolve correctly. One unit test asserts both old and new fields appear in CLI JSON output.
2. **CRI persistence path.** New helper `xenon.scanners.cri.persist(payload, *, conn)` writes a `cri_series` row from a parsed CLI payload. `INSERT ... ON CONFLICT (recorded_date) DO NOTHING` so duplicate ticks within a day are idempotent (matches the `recorded_date` generated column in the schema). Wire into both:
   - `POST /regime/scan` (`server.py:572`) — replace the JSON-archive-only path with persist + archive (archive can stay for transitional readers; remove in a follow-up once nothing reads `data/cri.json`).
   - The new scheduler loop (§4.1).
3. **Multi-worker advisory lock helper.** New `src/xenon/api/services/advisory_lock.py` exposing `_pg_try_advisory_lock(key)` async context manager. Used by both the existing UW-daily worker guard pattern (refactor `server.py:335` to use it) and the new VCG/CRI loop. Single helper, two callers.
4. **Outbox emit helper for regime transitions.** Confirm `xenon.db.events.emit()` accepts the `regime_transition` `kind` (no schema change expected; `events.outbox.payload` is JSONB). One Vitest fixture to seed an outbox row and assert `LISTEN` consumers see it via the existing notify trigger.
5. **Web `/api/regime` rewrite.** Replace the file-reading implementation in `web/app/api/regime/route.ts` with `xenonFetch('/regime')`. Strip the data file paths and the client-side CRI recompute branch in `RegimePanel.tsx:232` (display-only overlays may stay, but they cannot feed binding tier).
6. **Audit deliverable.** While doing the above, write `docs/plans/2026-04-29-vcg-cri-rewiring-audit.md` documenting:
   - The pre-existing scheduler topology (CRI loop / VCG triggers).
   - All in-process callers of `submit_combo`, `_orders_place_from_body`, `ib_place_order`.
   - The canonical hedge predicate set distilled from `options-structures.json` (input to §4.5 `_is_hedge`).
   - Whether any other readers of `data/cri.json` exist that the Phase 0 web rewrite would break.

**Verify:** CRI scanner CLI now emits both old and new fields; `cri_series` rows accumulate after a manual `POST /regime/scan`; `web/app/api/regime/route.ts` returns the FastAPI payload byte-for-byte; the regime page renders without any client-side recomputation.

### Phase 1 — PG view + audit table (DB only)

1. New Alembic migration adding `regime_state` view (§4.2 DDL) and `regime_overrides` table with deferred FK to `order_submissions` (§4.3 DDL).
2. SQLAlchemy table reflection for `regime_overrides` in `src/xenon/db/schema.py`.
3. Tests: §7.2.

**Verify:** `alembic upgrade head` clean on dev DB; `SELECT * FROM regime_state` returns the expected shape; `regime_overrides` insert with a non-existent `submission_id` fails at COMMIT time (deferred FK).

### Phase 2 — Classifier + dependency + `/regime` endpoint

1. `src/xenon/api/services/regime_state.py` — RegimeState dataclass, classifier, `get_regime_state` Depends with 30-s in-process TTL cache (§4.4).
2. `src/xenon/api/routes/regime.py` — `GET /regime` and `GET /regime/overrides`.
3. Tests: §7.1, §7.3 (skeleton — no gate yet).
4. Frontend: `web/components/RegimePanel.tsx` extension consuming the new endpoint shape and rendering the per-scanner tier strip.

**Verify:** open the regime page in the browser; confirm tier strip renders correctly for both signals; no order-path effects yet.

### Phase 3 — RegimeGate + order-route integration + CI guard

The CI guard moves into this phase (was Phase 6 in v1) so the very first gated PR is itself protected.

1. `src/xenon/api/services/regime_gate.py` — `veto`, `_is_hedge`, `_max_loss_usd` (§4.5, §4.5.1).
2. Parameterize the existing covered-call cover-ratio predicate in `src/xenon/api/guards.py` to accept the cover ratio as an argument (default 1.0; gate passes 1.25 on TIER_2).
3. Wire `RegimeGate.veto` into `POST /orders/place`, `POST /orders/modify` (with the modify-rules from §4.6.1), and every in-process call site enumerated in Phase 0 step 6.
4. 422 / 409 client handling in the wizard (resize prompt; override modal).
5. Tests: §7.1 (gate), §7.3, §7.4.
6. Land `scripts/checks/order_path_regime_gate_called.py` and the `/api/regime` no-file-reads check (§7.5); wire into `.github/workflows/ci.yml::order-path-guards`.

**Verify:** the manual smoke tests from §5.2 and §5.3 reproduce on dev; CI guards green on this PR; CI red on a synthetic PR that adds a new order entry point without the gate.

### Phase 4 — Scheduler consolidation + outbox emit

1. `_vcg_cri_scan_loop` in `server.py` lifespan, using the Phase 0 advisory-lock helper. Remove any pre-existing CRI loop in favor of this consolidated supervisor.
2. Tier-transition diff + outbox emit via `xenon.db.events.emit('regime_transition', ...)` (§4.8). Stale-data transitions suppressed.
3. Tests: integration test inserting fixture rows into `vcg_series` / `cri_series` and asserting outbox row appears.
4. Update CLAUDE.md startup checklist to reflect the consolidated loop.

**Verify:** run dev server for an extended window; observe scans 30 min apart; insert a fixture row to force a tier transition and assert an outbox row appears with `kind = 'regime_transition'` and the correct payload.

### Phase 5 — Docs + backlog closeout

1. Update `CLAUDE.md` Order-Path Guards section with the new regime gate guard.
2. Update `docs/todo-backlog.md` § 7 to mark this work shipped, link to this design doc, and record any deferred follow-ups (e.g. removing `data/cri.json` archive once no readers remain, mobile-push consumer attaching to the outbox).
3. Optional follow-up spec stub for Phase 2 — auto-staged hedge orders (out of scope here).

**Verify:** documentation reads accurately to a fresh contributor; backlog item #7 closed with link to this spec.

## 9. Self-review

Run inline against the four checks from the brainstorming skill.

### 9.1 Placeholder scan

- No `TBD` / `TODO` / `???` strings remain.
- One stub: §4.6 says "exact files to be enumerated in audit step" for the wizard component path. This is intentional — the audit (phase 1) is the right place to enumerate; pre-committing to a path now would risk being wrong. **Acceptable.**

### 9.2 Internal consistency

- §3.2 throttle table, §4.4 classifier rules, §4.5 gate decision tree, and §5 walkthroughs all agree on tier names (`NORMAL`, `EDR`, `TIER_2`, `TIER_1`, `PANIC`, `UNKNOWN`) and decisions (`OK`, `THROTTLE`, `BLOCK`). EDR is now uniformly `THROTTLE(soft)` across all sections (v1 contradiction fixed).
- §3.1 invariant 4 ("hedges always pass") is implemented in §4.5 decision tree step 1.
- §6 says override insert failure rolls back the order transaction; §5.3 walkthrough writes the override row inside the same transaction as the submission reservation (deferred FK in §4.3 makes this work).
- §4.1 outbox emit + §4.8 outbox emit + §6 stale-suppression all agree: transitions involving `UNKNOWN` do not emit; ordinary tier changes do.

### 9.3 Scope check

- Six phases (0–5) of one connected feature. Phase 0 is the persistence/plumbing prerequisite that the v1 review surfaced; without it the rest of the plan operates on missing data.
- Phase-2 (auto-staged hedge orders) is explicitly carved out (§2 non-goals) for a future spec.
- **In scope for one implementation plan.**

### 9.4 Ambiguity check

- "Throttle" pinned in §3.2.1 to "halved per-order risk-budget cap (1.25% of bankroll), with TIER_2 also bumping the covered-call cover-ratio from 1.0 to 1.25". HTTP 422 `resize_required` protocol; no silent quantity rewrite.
- "Defined-risk hedge" enumerated in §4.5; multi-leg combo rule pinned to "structural classification wins".
- "Stale" pinned to `XENON_REGIME_MAX_AGE_S` = 90 min default (§3.1 invariant 5).
- "Tier transition" pinned to `(vcg_tier, cri_tier)` tuple change with explicit `UNKNOWN`-suppression (§4.8, §6).
- "Quantity-increase modify" pinned to delta-order gating (§4.6.1).
- "Override audit failure" pinned to whole-transaction rollback, no order through without audit row (§6).

### 9.5 Audit corrections applied (2026-04-29 review)

The Codex-style audit identified eight spec-correctness issues. All resolved inline:

| #   | Finding (audit summary)                                                       | Resolution                                                                                                          |
| --- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| 1   | CRI does not write to `cri_series`; column-name mismatch                      | New Phase 0 (§8.0) adds CRI persistence path + scanner field normalization.                                         |
| 2   | "half-Kelly throttle" had no execution contract                               | §3.2.1 defines throttle precisely (risk-budget cap + 422 resize_required + cover-ratio).                            |
| 3   | EDR contradicted itself between §3.2 table and §4.5 decision tree             | EDR is now uniformly `THROTTLE(soft)`.                                                                              |
| 4   | `regime_overrides` lacked submission/scope linkage                            | §4.3 redesigned around `submission_id` + `account_env`/`broker`/`broker_account` + deferred FK.                     |
| 5   | Modify gating boundaries unclear                                              | §4.6.1 enumerates modify rules; pure-price and quantity-decrease bypass gate.                                       |
| 6   | `PushNotification` symbol does not exist                                      | §4.8 now uses `xenon.db.events.emit()` outbox.                                                                      |
| 7   | Scheduler had no multi-worker guard                                           | §4.1 + Phase 0 use `pg_try_advisory_lock`, sharing helper with the UW-daily worker pattern.                         |
| 8   | Web `/api/regime` route reads files; `RegimePanel` recomputes CRI client-side | §4.9 + Phase 0 rewrite the route to proxy FastAPI; remove client-side recomputation; CI guard locks the rewrite in. |

### 9.6 Tribunal review — accepted design decisions (Phases 0/1/2/4 review, 2026-04-30)

After the Codex tribunal pass on 18 commits across Phases 0/1/2/4, four findings were intentionally accepted as-designed rather than fixed:

- **CROSS JOIN partial-feed → UNKNOWN/UNKNOWN** (tribunal ISSUE-4). When only one feed has data, `xenon.regime_state` returns zero rows by design (CROSS JOIN with two `LIMIT 1` CTEs). The classifier maps a missing row to `vcg_tier=UNKNOWN`, `cri_tier=UNKNOWN`, which the binding logic surfaces as `EDR` (THROTTLE). For a risk gate, "we don't have both signals → throttle, don't go permissive" is the conservative interpretation. Alternative (LEFT JOIN with NULL handling) was considered and rejected as bias-toward-permissive.
- **`events.outbox` thin payload** (tribunal ISSUE-6). The `regime_transition` outbox row stores only the tier tuple + scanner timestamps, not the full classifier inputs. `events.outbox.payload` is `JSONB` so additional fields can be appended without a migration if a downstream consumer (mobile/Slack/email — out of scope per §10) needs richer context. Forward-compat by schema, not by pre-emptive payload bloat.

Two findings handled separately:

- **`regime_overrides` FK scope binding** (tribunal ISSUE-5) — **deferred to Phase 3.** The deferred FK in §4.3 references `order_submissions.submission_id` but does not enforce that the override row's `(broker, account_env, broker_account)` matches the parent's. No production code path inserts into `regime_overrides` yet — only tests do — so the cleanest fix (composite FK requiring full scope match) lands when the gate's override-write path lands in Phase 3.
- **CRI malformed-score → NORMAL** (tribunal ISSUE-2) — **fixed in Phase 0.** `save_cri_scan` now raises `ValueError` on missing/None/NaN/non-finite `cri.score`. A bad scan no longer biases the gate toward permissive (which would surface as `cri_tier=NORMAL`, the safest tier, and unblock trading). Tests in `scripts/tests/test_save_cri_scan.py`.

---

## 10. Open questions for user verification

These didn't reach a question gate during brainstorming; default decisions are stated. Flag any you want changed.

1. **Notification consumer.** Default: this spec stops at the outbox row. Adding a downstream consumer (mobile push, Slack, email) is a separate piece of work that attaches to the existing notify trigger. If you want a specific consumer wired in v1, name it and I'll fold it into Phase 4.
2. **Scan cadence.** Default: 30 min during market hours, paused outside (matching CRI's documented cadence). Alternative: tighten to 15 min during ACTIVE/PANIC regimes (more responsive, ~2× scanner cost).
3. **`UNKNOWN` ordinal.** Default: pegged to EDR (throttle, don't block). Alternative: peg to `TIER_2` (block non-hedges on stale data). I think the default is right — locking the user out because the scheduler hiccupped is worse than letting one trade through at half size.
4. **Override TTL.** No default. Should an override be a one-shot (this trade only) or sticky for some window (e.g. 15 min)? Sticky is dangerous (you forget you overrode); one-shot is annoying if you submit a basket. Recommendation: **one-shot.** Confirm.
5. **Multi-leg combo edge.** If a wizard-built combo has one hedge leg + one directional leg, is the whole order a hedge? Recommendation: **structural classification wins**. If `options-structures.json` classifies the whole structure as a hedge, the whole order passes. If as anything else (synthetic, ratio, etc.), gate normally. The strategy docs are unambiguous that the hedge instruments are the _whole_ HYG put / SPX put-spread, not "a combo containing a put leg".

---

## 11. Changelog

- 2026-04-29 — Initial design.
- 2026-04-30 — **Tribunal review (v3).** Codex+Gemini+Claude tribunal pass on the 18 commits implementing Phases 0/1/2/4. Six issues fixed inline (advisory-lock txn commit, supervisor observability, market-hours gate, broker-scope filter on overrides listing, regime-state view id-tiebreaker migration, save_cri_scan malformed-score rejection). Four issues addressed via §9.6: ISSUE-4 (CROSS JOIN partial-feed) and ISSUE-6 (thin outbox payload) accepted as-designed; ISSUE-5 (FK scope binding) deferred to Phase 3 where the override-write code path lands.
- 2026-04-29 — **Audit-driven revision (v2).** Eight findings from a code-anchored review applied inline; see §9.5 for the full mapping. Notable structural changes: introduced **Phase 0** (CRI persistence + plumbing prerequisites); rewrote §3.2 throttle contract to `risk-budget cap + HTTP 422 resize_required`; redesigned `regime_overrides` around `submission_id` + scope columns + deferred FK to `order_submissions`; replaced fictional `PushNotification` symbol with `xenon.db.events.emit()` outbox; added `pg_try_advisory_lock` multi-worker guard sharing helper with UW-daily; bounded modify gating to new-exposure cases (price-only and quantity-decrease bypass); added `/api/regime` no-file-reads CI guard. Sections rewritten or substantially edited: §1 (summary + errata), §2 (in/out scope), §3.2 + §3.2.1 (throttle contract + EDR fix), §3.3 (push channel → outbox), §4.1 (advisory lock + outbox emit), §4.3 (audit table redesign), §4.5 + §4.5.1 (gate result shape + max_loss helper), §4.6 + §4.6.1 + §4.6.2 (modify rules + override protocol), §4.8 (outbox-based emission), §4.9 (web rewrites), §6 (error table), §7.5 (CI guard timing), §8 (Phase 0 + reordered phases).
