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
