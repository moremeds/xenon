# XENON — Todo Backlog

Ideas captured for future planning. **Not active work.** Items here have not been
scoped, sized, or scheduled. Promote to `docs/plans/YYYY-MM-DD-<slug>.md` when
ready to execute.

When the user says "todo" (e.g. "todo: explore X"), append to the **Inbox** at the
bottom of this file with the current date — do not silently drop ideas, and do
not start work on them unless explicitly asked.

---

## 1. Rule-based portfolio management (alerts + order placement)

Surface portfolio events the system can react to deterministically before the
user has to notice them manually. Start with alerts; promote the well-behaved
ones to auto-actions once observed running clean.

- **Expiry watchdog** — alert when any open option position is ≤ 2 weeks from
  expiry, prompt roll-vs-flat decision. Candidate for auto-roll once stable.
- Future categories: stop-loss breach, target reached, position drift past size
  cap, IV crush flagged on long premium, earnings inside expiry window.
- Open question: alert channel (web banner, push, email, Discord?) and where
  the rules live (DSL? Python config? UI builder?).

## 2. Order types (IB now, Futu later)

Expand beyond plain limit/market.

- **MOC (market-on-close)** — simplest, ship first.
- **Trailing stop-loss / profit-take** — auto-attach to any new fill that opens
  a position, with sensible defaults per asset class. Bracket-style.
- Cancel/replace logic when underlying moves; don't let stops orphan.

## 3. Apex integration (backtesting + strategies + signals)

Wire the Apex project as an external signal source — primary value is
**signals**, secondary is shared strategy/backtest infra.

- Define the signal contract (schema, freshness, scoring) before deciding
  transport (HTTP pull vs. event push vs. shared DB).
- Decide whether Xenon owns the executor for Apex-originated signals or
  whether Apex emits structured trade ideas only.

## 4. Signal graphs (Z-score on ATR, etc.)

Visualize the signals we already compute — start with the Z-score-on-ATR view
referenced in the YouTube source. Goal: see _why_ a signal fired, not just the
fact it fired.

- Pull the YouTube reference into `docs/reference/` so the spec is grounded.
- Likely lives next to the existing scanner output pages in `web/`.

## 5. UW polling + OI alerts

Standing loop against `xenon-uw-analyze` to track changes over time, then alert
on meaningful **OI deltas** instead of just one-shot snapshots.

- Already have the 30-min snapshot TTL and 20k/day budget — design loop
  cadence inside that budget (see CLAUDE.md → UW API budget controls).
- Alert thresholds: OI step-change, expiry-bucket migration, sweep-vs-block
  composition shift.

## 6. Fundamentals (Massive.io + FMP)

Deepen the fundamental layer beyond what the scanners currently use.

- PE / PEG / margin trend / FCF yield / debt schedule from FMP.
- Massive.io: catalogue what it actually exposes, decide which fields earn a
  cache slot vs. on-demand fetch.
- Output: a fundamentals panel on the ticker page, plus a fundamentals gate
  the structured-options flow can opt into.

## 7. Activate existing CRI / VCG strategies

We have CRI scan + VCG history infrastructure in the repo but no live trading
loop on top of either. Audit what exists, decide what's signal vs. noise, and
either wire them into the alert/order pipeline or sunset the dead code.

- Inventory: `src/xenon/scanners/`, any `cri_*` / `vcg_*` modules, scheduler
  state in `server.py`.
- Decision points: which strategy clears the Four Gates as-is? Which needs
  parameter rework? Which gets deleted?

---

## Inbox

New unsorted ideas land here with a date. Sort into a numbered section above
when scoping starts.

<!-- 2026-MM-DD — short title — one-line description -->

- 2026-04-27 — **Add a priority level to backlog items** — tag each numbered
  section (and inbox entry on promotion) with a priority so planning sessions
  can sort by impact, not by capture order. Open question: P0/P1/P2 vs.
  High/Med/Low; whether to track effort separately.
  **Notes:** This is a meta-todo about backlog structure. The rule of
  "do not start work on it" applies even to design decisions — capture the
  question, don't pre-bake the answer. Apply retroactively to items 1–7 once
  a scheme is chosen.

- 2026-04-27 — **What-if option calculator (held-greeks repricing)** — given a
  hypothetical underlying price, hold IV and the rest of the greeks constant,
  recompute the option value via first-order Taylor expansion using delta,
  ½·gamma·ΔS², theta·Δt, and vega·ΔIV. Extend to spread/combo by summing per-leg
  with sign. Concrete first use case: **premarket / after-hours pricing
  approximation** — the option chain is closed but the underlying trades; reprice
  user positions off the premarket/AH last with a manual IV override slot for
  later. Lives next to the existing position table (`PositionTable.tsx`,
  `WorkspaceSections.tsx`), reuses the credit/debit sign convention from
  `web/CLAUDE.md`. Open question: pull greeks from the last IB snapshot or
  recompute from BS each time?
  **Notes:** Forms a natural staircase with the shock-analysis entry below —
  this is the engine, that's the engine driven by macro inputs. Ship this first;
  it's immediately useful for the overnight P&L sanity-check moment when traders
  most want to know "how am I doing right now" but no quotes are streaming. The
  Taylor expansion is the standard hack for this gap and is accurate enough at
  ATM strikes; accuracy degrades on deep OTM where gamma is non-linear, which is
  worth flagging in the UI. ib_insync caches yesterday's greeks so no extra
  fetch needed for the v1.

- 2026-04-27 — **Portfolio shock analysis (macro → individual)** — extension of
  the what-if calculator above. Inputs: VIX level, SPX level, 10Y/20Y rates, fed
  funds / base rate. Derive each holding's shocked IV via a beta-to-VIX +
  term-structure model, shocked underlying via beta-to-SPX (and rate sensitivity
  for rate-linked names), then reprice every option position with the
  held-greeks formula above. Output: per-position and portfolio-total P&L impact
  under the chosen macro scenario. Useful for stress-testing before FOMC, CPI,
  earnings clusters. Open questions: where do per-name betas come from (rolling
  regression vs. FMP/Massive cached field)? How to model skew shift, not just
  ATM IV?
  **Notes:** Shares infrastructure with todo #6 (FMP/Massive.io fundamentals) —
  per-name betas are the cheapest if the fundamentals layer ships first and
  caches them. Calling out the dependency here so future planning is honest
  about real cost: shock analysis ≈ what-if calculator + per-name beta layer +
  scenario UI. The skew-shift question matters more than it sounds: a
  VIX-only model will badly under-estimate damage on positions that live on the
  put-skew wing (long puts, put spreads), which is exactly where stress
  scenarios hurt most.

- 2026-04-27 — **Bug: uw-analyze periodic refresh not firing** — the
  `/uw-analyze` page is documented to refresh periodically (cache-first load,
  then background SSE refresh on the 30-min TTL during open hours), but the
  periodic update doesn't appear to be happening in practice. User reports
  feature is "stated but not actually working". Repro: open `/uw-analyze`
  during market hours, leave it open past the TTL window (default 30 min via
  `XENON_UW_TTL_OPEN_S`), observe whether the snapshot updates without manual
  refresh.
  **Notes:** Multiple suspect surfaces — could be any of (a) the SSE channel
  isn't wired to a recurring tick, only a one-shot load, (b) the closed-market
  gate in `UwAnalyzeCache.get_or_run()` is over-firing during open hours
  (timezone bug? holiday-calendar absence already noted in root CLAUDE.md),
  (c) the `useUwAnalyze.ts` hook doesn't poll/listen continuously, (d) the
  TTL check exists but the refresh path itself is short-circuited by the
  cross-mount snapshot cache (commit 6cd7b49) returning warm data without
  re-fetching. Investigate in this order: confirm with browser devtools whether
  the SSE connection stays open and whether ticks arrive; only then look at
  the backend gate. Files to start from: `web/lib/hooks/useUwAnalyze.ts`,
  `src/xenon/api/services/uw_analyze_cache.py`, `src/xenon/api/routes/uw_analyze.py`.
  Watch the daily UW budget while debugging — a misfiring polling loop could
  blow through the 20k/day cap in a few hours.

- 2026-04-27 — **🔴 TOP PRIORITY — Bug: single-leg orders rejected with "quote expired"** —
  placing a single-leg option order raises a "quote expired" alert and the
  order does not submit. Previously raised, not fixed. User has hit this
  repeatedly. **Blocks real trading on the IB tab.** Repro: open the order
  form for any single-leg option position, fill in size/price, click submit,
  observe the "quote expired" alert.
  **Notes:** Almost certainly the long tail of the **quote_token saga** —
  per session memory: PR #34 introduced `quote_token`, #35 reverted it, #47
  re-attempted, then commit `654d72d2` removed the gate. `web/CLAUDE.md` even
  warns "do not re-ship as-is" on that surface. If the alert is still firing
  after the gate was removed, three live hypotheses: (a) a _different_
  staleness check is misreading a quote as expired (snapshot-age, contract
  qualification, bid/ask sanity), (b) the gate wasn't fully removed and a
  surviving path still enforces it, or (c) the alert text is being raised by
  a different validation entirely and is just _labelled_ "quote expired" —
  the label is a tell, not a diagnosis. Investigation must start by
  **grepping the literal "quote expired" string** across `web/` to enumerate
  every emitter, then trace which one fires for single-leg specifically. Do
  _not_ assume the same code path as the previous fix. Cross-references in
  the codebase: `654d72d2`, PRs #34 / #35 / #47, position-row order button
  (`a7cbbbc4`), `/orders/quote` snapshot resolver (`8ef479ab`). Tag this P0 /
  "drop everything" once a priority scheme is picked.
  **Status (2026-04-30, cross-ref pass):** Hypothesis (b) ruled out — the
  literal string "quote expired" no longer appears anywhere in `web/` or
  `src/`. The gate from `654d72d2` is fully gone. The alert text "Quote
  expired; refreshing." today comes from `web/lib/orderReasonCodes.ts:40`
  rendering the `STALE_QUOTE` reason code, which is emitted by
  `src/xenon/execution/quote_guard.py:120,124` (the freshness/tick-grid/band/
  market-hours guard from commit `2a489060`) and surfaced via `server.py:2173`.
  So the live theory is hypothesis (a): the new guard's freshness check is
  rejecting valid single-leg quotes. Investigation narrows to: snapshot age
  TTL, tick-grid validation, band check, market-hours gate inside
  `quote_guard.check`. Still open.

- 2026-04-27 — **🔴 TOP PRIORITY — Bug: naked-short guard blocks plain stock
  BUY orders** — attempted to **buy 1 share of QQQ** (the simplest possible
  long-stock order, no shorting involved) and the order was rejected with
  `"Naked short blocked: Naked short stock: no long shares held for QQQ"`.
  This is structurally wrong — Gate 4 (naked shorts) only applies to SELL /
  short-side orders; a BUY of stock can never be a naked short. Sister bug
  to the "quote expired" single-leg blocker above; group with it as P0
  order-placement reliability work.
  **Notes:** The principle, stated strongly: **the naked-short guard must be
  structurally unreachable from any BUY path** — direction-of-trade is the
  outermost gate, not a branch buried inside the guard. No state of the
  account, no inventory level, no contract type, no order-source path should
  ever cause a stock BUY to surface a naked-short rejection. If the guard
  _can_ be reached from a BUY path, the architecture is wrong even if today's
  inventory check happens to let it through. Acceptance criterion: a stock
  BUY for a ticker the user has never traded must place cleanly without ever
  evaluating any naked-short logic. Two likely root causes (in order of
  prior probability): (1) the guard runs unconditionally on every order
  before any side check — so an empty inventory looks like "naked" to it on
  any order, BUY or SELL; (2) BUY/SELL classification is inverted somewhere
  upstream so a real BUY arrives at the guard tagged as SELL. (1) reproduces
  on any fresh ticker; (2) would only fire on specific routing paths.
  Investigation: grep `"Naked short blocked"` and `"no long shares held"`
  across `src/xenon/` and `web/` to find every emitter, then verify the
  outermost guard wrapper short-circuits to a no-op when
  `order.action == "BUY"` (or the equivalent `side` field). Per session
  memory **"In-process
  route bypass"** — FastAPI Depends only fire on HTTP, in-process handler
  calls (`_orders_X_from_body`, `submit_combo`) skip every dep. The naked-short
  guard fires on a plain stock BUY _today_, which means it's already inside
  the inner handler (right place); the fix is to put the BUY-short-circuit
  there too, not at the route boundary. Reference: `src/xenon/CLAUDE.md` for
  the naked-short table. **Must ship in the same PR as the quote-expired fix
  above** — fixing one and shipping it leaves the surviving bug to mask QA
  signal on the other. Tests required: a regression test that asserts a
  stock BUY for a never-traded ticker reaches the broker layer without
  invoking the naked-short guard at all (mock the guard, assert it's not
  called), plus a parallel test for the `_orders_X_from_body` in-process
  path so the bypass class-of-bug doesn't recur.
  **Status (2026-04-30, deep cross-ref + live preflight test):**
  **Strong evidence the bug is already resolved. One live UI confirmation
  click still needed before closing.** Findings:
  - **Both BUY short-circuits were in code at the time of the report
    (2026-04-27).** TS UI guard `web/lib/nakedShortGuard.ts:207`
    (`if (order.action === "BUY") return { allowed: true }`) since
    commit `0913fcf3` (2026-04-07). Python preflight `preflight.py:301`
    (`if req.action == "BUY": return Verdict(accept=True)`) since
    commit `b6cadd376` (2026-04-21).
  - **Direct preflight test (2026-04-30) passes:** invoking
    `preflight.evaluate(PreflightRequest(action="BUY", ticker="QQQ",
security_type="STK", quantity=1, ...), PortfolioView(positions=[]))`
    returns `accept=True, reason_code=None` — no false naked-short
    rejection. Control test (`SELL 1 QQQ` on empty portfolio) correctly
    returns `INSUFFICIENT_SHARES`.
  - **Caller path is clean:** `OrderTab.tsx:425-444` (`handlePlace`)
    passes user-chosen `action` straight through to the guard with no
    inversion. The reactive warning at `OrderTab.tsx:397-412` is
    explicitly gated on `action === "SELL"` (returns null otherwise).
  - **The exact error text the user reported (`"Naked short blocked: "`
    prefix) exists nowhere in source today** — `grep` across `web/` and
    `src/` returned zero non-test matches. The TS guard's reason at
    `nakedShortGuard.ts:219` (`"Naked short stock: no long shares held
for ${sym}"`) survives, but the wrapping prefix is gone.
  - **In-process bypass path is safe:** `naked_short_audit.py` runs
    against IB open orders post-sync, not against incoming user orders,
    so it cannot reject a fresh BUY at submission time.
  - **Most likely closer:** PR #61 (`fix/order-placement-reliability`,
    2026-04-28) shipped five order-path fixes including reason-code
    rework — the error-prefix rewrite likely came from there.
    Action: user does a live **BUY 1 QQQ** click in the IB tab when next
    convenient. If it places cleanly → close this entry. If it still
    rejects with the same error → narrow hunt for an upstream
    sign-inversion (since the guards themselves are proven correct).

- 2026-04-27 — **OI-as-flow-attribution (overnight check, not intraday)** — OI
  is reported once per day end-of-day by OCC, so this is fundamentally an
  **overnight delta** feature, not an intraday tracker. Real use case: when
  a big options flow event prints today (sweep, block, unusual volume),
  cross-check the next session's OI to disambiguate **new positioning** (OI
  grows by roughly the flow size) from **churn / pass-through** (OI flat or
  down — institutions reshuffling existing positions, market-makers warehousing
  briefly). The flow signal alone is ambiguous; OI confirms (or doesn't)
  whether real conviction was put on.
  **Notes:** This is a meaningful reframing — earlier draft treated OI as
  something to "track" generically; the actual question is binary
  per-flow-event: _did that flow create position?_ Workflow: capture the
  notable flow events from a session, store them, run an overnight job after
  the next OCC settlement that pulls fresh OI and compares against
  prior-day OI for those exact contracts, surface a "confirmed / churned"
  verdict next morning. Concrete contracts to watch are produced by the UW
  flow scanner already in the codebase, so this doesn't need its own
  universe — it's a _post-processor_ on existing flow output. Overlaps with
  existing **todo #5 (UW polling and OI alerts)** but the framing is sharper:
  #5 says "alert on OI deltas," this says "use OI deltas to grade flow
  signals retroactively." Merge on promotion. Side benefit: builds a labelled
  dataset of flow → real-positioning over time, which is the kind of training
  data you'd want if anything in todo #3 (Apex backtesting) ever wants to
  learn flow-quality scoring. Source: UW historical OI endpoint or the
  `xenon-uw-analyze` snapshot (the latter is already cached daily). No
  intraday polling needed, so this does _not_ eat the 20k/day budget the way
  passive monitors do — it's a single batch run per ticker per day.

- 2026-04-27 — **Intraday IV tracking + alerts on large IV moves** — watch IV
  on tickers in the universe (or a configurable subset) intraday, alert when
  IV moves significantly relative to its own recent baseline. Goal: catch
  unusual vol expansion or compression _as it's happening_, before it shows
  up in price.
  **Notes:** Real questions hidden in "moves greatly": (a) which IV — ATM
  IV, IV30, term-structure point, surface-level? Probably ATM IV30 to start;
  it's the single most-quoted number and ib_insync surfaces it directly,
  (b) baseline for "great" — fixed bps move, Z-score against a rolling
  window, percentile of historical IV range? Z-score is the right answer
  long-term and connects to **todo #4 (signal graphs / Z-score on ATR)** —
  same statistical machinery, different input series. Worth co-designing
  the rolling-window infrastructure once and reusing it for both.
  (c) sample cadence — minute? 5-min? Cheaper and noisier vs. coarser and
  more reliable; the 5-min cadence aligns with the existing flow-cache TTL
  in root CLAUDE.md. Source: IB option-chain greeks (already streamed) is
  the cheap path; UW's IV endpoints are the richer-but-budgeted path. Default
  to IB to avoid eating UW budget on a passive monitor. Alert delivery
  channel ties back to **todo #1 (rule-based portfolio management)** — same
  alert plumbing, different trigger.

- 2026-04-28 — **Order-path regression prevention (layered automation)** —
  PR #61 (`fix/order-placement-reliability`, merged 2026-04-28) shipped
  five distinct order-path bug fixes in one branch and skipped the live
  paper smoke test. Build out a layered automation strategy so the same
  five regression classes (silent JSON fallback after Postgres migration,
  in-process bypass of route guards, reason-code overloading, optional
  fields that should be required, untested live broker contract drift)
  cannot recur. Six layers: edit-time Claude hook → pre-commit AST grep
  → CI path-filtered structural tests → pre-merge live paper smoke →
  nightly safety net → auto codex review on order-path PRs. Full design
  in **`docs/plans/2026-04-28-order-path-regression-prevention.md`**.
  **Notes:** Highest-leverage layer is the live paper smoke (Layer 4) —
  it's the only one that catches _unknown_ regression shapes. The other
  five lock in known shapes from the `[Postgres read-side gap]`,
  `[In-process route bypass]`, and `[Live E2E surfaces contract bugs]`
  memories, all of which have already burned this repo at least twice.
  Recommended starting point if anyone has half a day: Layer 2
  (pre-commit AST grep ~1h) + Layer 1 (edit-time hook ~30m) — together
  they lock in the two most-burned patterns in under two hours. Total
  full-coverage effort: ~3 person-days. Tradeoff: layered guards add
  inner-loop friction and flake surface, but for an order path that
  asymmetry is correct — false-positive retries cost minutes, a single
  naked-short order reaching IB costs real money. Provide
  `--no-verify` / `skip-smoke` bypasses for genuine emergencies. Depends
  on no other backlog item; can be picked up independently.
  **Status (2026-04-30, cross-ref pass):** **Layers 1+2 SHIPPED** in
  PR #63 (`018cf534 ci: order-path regression guards (Layers 1+2)`). The
  edit-time hook (`.claude/hooks/order-path-reminder.sh`) and the two CI
  guards (`scripts/checks/no_json_fallback_on_order_path.py`,
  `scripts/checks/order_path_caller_allowlist.py`) are live; root CLAUDE.md
  documents them under "Order-Path Guards (Layers 1+2)". Remaining work:
  Layers 3 (CI path-filtered structural tests), 4 (pre-merge live paper
  smoke — highest leverage, still pending), 5 (nightly safety net),
  6 (auto codex review on order-path PRs).

- 2026-04-30 — **Fractional-share quantity for stocks (e.g., sell 0.1 QQQ)** —
  Today the order-place flow truncates fractional stock quantities to
  integers. User wants to sell 0.1 QQQ; current behavior either rejects or
  silently rounds to 1 share. Options/combos must remain integer.
  **Notes:** Touches the schema, not just the input box. `src/xenon/db/schema.py`
  types `positions.quantity`, `trades.quantity`, `order_submissions.quantity`,
  `order_submissions.filled_qty` as `Integer` — all four need to migrate to
  `Numeric` (idempotent cast preserves existing whole-number rows).
  Pydantic models tightly type `int`: `PreflightRequest.quantity`,
  `RequestRow.quantity` (`src/xenon/execution/preflight.py:70`,
  `orders_store.py`); `ComboPreflightRequest.quantity` stays `int`.
  Backend choke points: `src/xenon/api/server.py:1845`, `:1876`, `:2333` all
  do `int(body.get("quantity", 0))`; `src/xenon/execution/ib_place_order.py:34`
  does `int(params["quantity"])` and passes to `totalQuantity` (ib_insync
  accepts float). Frontend choke points: 5 entry-point components —
  `web/components/PositionOrderModal.tsx` already does conditional
  `parseFloat` for `structure_type === "Stock"` at L99-104, BUT `Math.max(1, …)`
  at L127-128 clamps fractional → 1 (the bug already manifests here),
  `web/components/ticker-detail/OrderTab.tsx` (single-leg row L371, L515-516
  `min="1" step="1"`; combo row L741 stays integer),
  `web/components/InstrumentDetailModal.tsx` (L149, L239-240),
  plus `OptionsChainTab.tsx` and `BookTab.tsx`. Shared lib at
  `web/lib/order/components/OrderQuantityInput.tsx` and `hooks/useOrderValidation.ts`
  also enforce integer (`parseInt`, `Number.isInteger`); exported but unused —
  update for consistency. Naked-short audit math (`naked_short_audit.py`,
  `preflight.py:244` `new_uncovered_calls = uncovered_ratio * req.quantity`)
  should already work with float/Decimal but needs verification. IB-side:
  account-level fractional eligibility varies; IB rejects fractional on
  non-eligible instruments, so failure mode is surface-able. **Dependency:**
  Pairs naturally with the `orders_store.py` Decimal-typing cluster (Issues 1, 2,
  4, 5, 6, 9, 10, 14 from `bug_report_ib_postgres_activity_mirror.md`) — that
  PR is a logical prerequisite since it lays the Decimal foundation.
  **Estimate:** 1–2 day PR with TDD + browser verification per CLAUDE.md.
  **Status (2026-04-30, cross-ref pass):** Prerequisite ISSUE-5 from the
  `orders_store.py` cluster (vestigial `db_path` parameter) closed by PR #75
  `cb0903e4 chore(orders_store): remove vestigial db_path parameter`. The
  Decimal-typing issues (1, 2, 4, 6, 9, 10, 14) are still open.

- 2026-04-30 — **Share button broken on CRI and VCG pages** — clicking
  Share-to-X on the CRI and VCG pages does not produce a usable result. Sample
  payload returned by the VCG endpoint:
  `{preview_path: "/Users/chenxi/projects/xenon/reports/tweet-vcg-2026-04-30.html",`
  `card_paths: ["…tweet-vcg-2026-04-30-card-1.html", …]}`.
  **Notes:** The payload is the smoking gun — the API hands back **absolute
  filesystem paths**, not browser-addressable URLs, so anything in
  `ShareReportModal.tsx` that tries to `<iframe>` or `fetch()` them will fail
  silently. Two plausible fixes: (a) serve the `reports/` directory under a
  static route (e.g. `/api/share/file?path=…` with strict allowlist) and rewrite
  the response to URLs, or (b) inline the rendered HTML into the response and
  drop the disk write. Option (b) is cleaner for the share-modal use case
  since the disk artifact is only needed for the CLI flow that calls
  `subprocess.Popen(["open", preview_path])` (`generate_vcg_share.py:609`).
  **Suspected file sites:**
  `src/xenon/shares/generate_vcg_share.py` (and siblings: `generate_cta_share.py`,
  `generate_gex_share.py`, `generate_regime_share.py`),
  `web/app/api/vcg/share/route.ts`,
  `web/components/ShareReportModal.tsx`,
  `web/components/VcgPanel.tsx`. **CRI-specific gap:** there is no
  `web/app/api/cri/share/route.ts` and no `generate_cri_share.py` — "CRI broken"
  may mean the button is wired to a missing endpoint (returns 404), or it lives
  on a non-VCG panel I didn't locate. Triage step #1 is to identify which
  component renders the CRI share button and confirm its target endpoint.
  **Cross-reference:** `web/CLAUDE.md` "UI Verification" — fix must be E2E
  verified in chrome-cdp, not just unit-tested. Check all five share-enabled
  panels (Regime, GEX, CTA, VCG, internals) since they share `ShareReportModal`
  and likely share the bug. **Estimate:** half-day if the fix is option (b)
  inline-HTML; full day if standing up a static-serve route with auth.
  **Status (2026-04-30, cross-ref pass):** Bug confirmed structurally
  present. `preview_path` is still emitted as an absolute filesystem path
  by all four generators (`generate_gex_share.py:723`,
  `generate_cta_share.py:689`, `generate_vcg_share.py:604`,
  `generate_regime_share.py:701`), and `ShareReportModal.tsx:82` consumes
  it directly via `data?.preview_path`. No fix in flight.

- 2026-05-01 — **Regime-gate Phase 3 follow-ups (post-Codex deep review)** —
  Four panic-tier hotfixes shipped (PR #79 + #80) closing C-1 combo SELL
  bypass, C-2.1 BAG modify gate skipped, C-3 stale portfolio bypass, and
  C-4 hardcoded $100k bankroll. Five lower-stakes items remain from the
  same review:
  - **C-2.2 cancel-then-place data loss** — `web/app/api/orders/modify/route.ts:142-184`
    cancels the working order before calling `/orders/place` for a
    structure-change modify. If the replacement is regime-blocked, the
    user is left with no order. Fix: pre-gate the replacement via a
    server-side dry-run before the cancel call. Web-route refactor.
  - **C-2.3 modify override audit insert** — `/orders/modify` 409 now
    surfaces `override_required: true` but `override_supported: false`.
    Honoring an override on modify needs a parallel audit-insert path
    (place uses `orders_store.reserve_attempt` which doesn't apply on
    modify since the row already exists).
  - **C-5 hedge structure registry** — `_is_hedge_combo` only recognizes
    2-leg verticals. The spec (`§4.5`) points at the canonical
    `docs/trading/options-structures.json` registry. Multi-leg hedges
    (long put butterflies on SPX/SPY) are blocked at TIER_1 even though
    the design says they must pass.
  - **C-6 hardcoded `user_id="local"`** — `server.py:2774` attributes
    every order_submissions and regime_overrides row to the same
    pseudo-user; `/regime/overrides` filters by scope only, not by
    user. Compliance can't reconstruct "who overrode what". Also the
    permId=0 race in ib_place_order can leave override rows with
    `perm_id=NULL` even when the order acked.
  - **C-7 substring CI guard** — `scripts/checks/order_path_regime_gate_called.py`
    passes if the function source merely _contains_ one of four
    substrings. A comment like `# RegimeGate is bypassed here` would
    satisfy it. Upgrade to AST call-site analysis.

  **Notes:** Codex review output saved at `/tmp/codex-phase3-spec-review.txt`
  (ephemeral, regenerate via the deep-review prompt template if needed).
  Items are independent and can be picked up in any order. Pair with
  re-running the deep review after each lands.

- 2026-05-01 — **TWS-cancel mirroring** — `src/xenon/api/CLAUDE.md` line 44
  documents this as a known gap: the IB→Postgres activity poller does not
  transition `WORKING` snapshot-\* rows to `CANCELLED` when they vanish from
  `get_open_orders()`. Naïve disappearance-detection is unsafe (a fill also
  disappears). Right fix: combine the disappeared set with `xenon.order_fills`
  for the same `(perm_id, scope)` to disambiguate, plus an idle-grace window.
  **Suspected file site:** `src/xenon/api/services/ib_activity_mirror.py`.
  Tracked here to surface in a future planning session — the activity poller
  has been stable since #71.
