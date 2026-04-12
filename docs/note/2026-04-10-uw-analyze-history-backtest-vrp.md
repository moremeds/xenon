# 2026-04-10 — uw-analyze: history persistence, backtestability, VRP z-score gaps

Discussion notes covering three related questions about `/uw-analyze`:

1. Is per-refresh history retained?
2. Is it feasible to backtest the bias judgments against that history?
3. What does "VRP z-score unavailable — regime defaulted to cautious" actually mean?

---

## 1. Per-record history retention

**Yes — every materialized refresh is archived to disk, not just the latest.**

- **Live cache** (`data/uw_analyze_cache.json`): one entry per ticker, holding only `current` + a light `previous` stub (ts + derived only). Overwritten on each refresh.
- **History archive** (`data/uw_analyze_history/<TICKER>/YYYYMMDD-HHMMSS-ffffff.json`): one file per refresh, written via `_archive_snapshot` at `scripts/api/services/uw_analyze_cache.py:373` _after_ `_persist()` succeeds. Each file contains the full `current` snapshot (report, display, flow_alerts, derived, dark_pool_summary, options_flow_summary) plus that refresh's `materialized_changes` and `archived_at`.

Key properties:

- **Microsecond-stamped filenames** prevent collisions on back-to-back forced refreshes and stay lex-sortable for `load_history()` (`uw_analyze_cache.py:347`, `:420`).
- **No retention policy yet** (`:402`) — guidance is to add a janitor only if the directory exceeds ~500K files or `load_history` p99 > 50ms. At ~1.5K files/day for a 20-ticker portfolio, that's months out.
- **Archive failures are swallowed** (`:417`) — disk-full on history must not break `/uw-analyze`.
- **No HTTP route reads it yet** (`:431`) — `load_history()` exists for debug/future time-series consumers.

The in-memory `materialized_changes` list per entry is bounded to the last 10 diffs (`_MAX_MATERIALIZED_CHANGES`, `:52`), but the archive preserves each refresh independently, so full history is on disk even though the live cache only shows the latest.

### Insights

- The split (light live cache + fat per-refresh archive) is a deliberate memory bound — the comment at `:644` notes dropping the embedded prev `report`/`display` cut ~95% of doubled per-entry cost after RSS surged past 7 GB.
- Ordering matters: archive _after_ persist guarantees the history is always a subset of committed cache states. Reverse order would risk phantom snapshots if persist failed.
- Filename = full UTC timestamp lets `load_history()` filter by `since` _before_ parsing JSON, so cost scales with returned count, not archive size (`:443`).

---

## 2. Backtesting bias judgments against history

**Feasible, with meaningful caveats.** The archive gives you the raw material, but it's not a clean backtest dataset out of the box.

### Working in your favor

- **Per-refresh snapshots are immutable and timestamped** to microsecond precision — exactly the shape a backtester needs.
- **Bias inputs captured in full**: `report`, `display`, `derived` (gex_sign, flip strike, walls, flow_score, net_call/put_premium, spot), plus `dark_pool_summary` and `options_flow_summary`. The portfolio bias service (`uw_analyze_portfolio_bias.py`) is the judgment layer on top — re-runnable deterministically against any archived snapshot.
- **No survivorship bias at the snapshot level**: every refresh lands on disk regardless of outcome.
- **Diffs are pre-materialized** (`materialized_changes`) — useful for event-study tests (e.g. "when gex_sign flips POSITIVE→NEGATIVE, what's the 1d/5d forward return?").

### What's missing / harder

1. **No price ground truth in the archive.** Need a separate source of forward returns (IB historical bars, yfinance, UW historicals) joined on `(ticker, archived_at)`. The archive's `report.price` is the spot at snapshot time — fine as the entry mark, but T+1/T+5/T+20 bars must come from elsewhere.

2. **Sampling cadence is irregular and demand-driven.** The cache only refreshes when something asks for the ticker. TTL is 5min open / 30min closed but only _if_ polled. Implications:
   - Per-ticker time series have uneven spacing.
   - Selection bias: tickers you watched closely have dense history; ones you ignored have sparse history.
   - Mitigation: resample to a fixed grid (daily close snapshot) and accept missing cells, OR backfill via a one-time historical run if UW endpoints support `as_of`.

3. **Bias logic itself has changed over time.** If `uw_analyze_portfolio_bias.py` was tweaked between archive entries, "the bias the system _would_ emit today" ≠ "the bias actually shown to the user back then." For an honest backtest, re-derive bias from archived raw inputs using the current code rather than trusting any cached label.

4. **Sticky-field carry-over contaminates "what was true at T."** `_merge_sticky_fields` (`:125`) preserves last-known-good values across refreshes when UW returns None. Right call for a UI but means an archived snapshot at T may contain a `term_structure_label` that was fetched at T-2h. Some fields are "as-of last successful fetch ≤ T", not "as-of T".

5. **History is shallow.** Archive started with the flow-analysis overhaul branch (recent merge `c5c3a2f`). Weeks-to-low-months of data, not years. Statistical power for anything beyond very short-horizon, high-frequency signals will be thin.

6. **No retention janitor yet** (`:402`) — current archive is complete. If one is added, lock down the backtest dataset first (copy out, or change the janitor to tier-archive instead of delete).

### Pragmatic backtest design

1. **One-shot extractor**: walk `data/uw_analyze_history/*/`, load each snapshot, flatten to a Parquet/DuckDB table keyed `(ticker, archived_at)` with columns for every `derived.*` field + a few `display.*` fields.
2. **Forward-return joiner**: pull daily OHLC for the union of tickers from IB or UW historical, compute T+1d / T+5d / T+20d log returns relative to `report.price` at each snapshot.
3. **Re-derive bias**: import the current `uw_analyze_portfolio_bias` module, feed it each snapshot, store the resulting label alongside the row. → `(features, current_bias_label, forward_return)`.
4. **Evaluate**: hit-rate, mean forward return per bias bucket, IC of `flow_score` vs return, event study around `gex_sign` flips, etc.
5. **Sanity check**: compare against a holdout where you _don't_ re-derive — use any `materialized_changes` entries that recorded the bias label at write time, to make sure your re-derivation matches history where the code hasn't changed.

### Verdict

Feasible for **directional / hit-rate evaluation of the current bias logic** on actively-watched tickers. Not sufficient for **rigorous strategy backtesting** without (a) a forward-return data source, (b) a way to handle irregular sampling, and (c) more accumulated history. Cheapest first step is the one-shot extractor — within an hour you'll know whether the dataset is dense enough to bother with the rest.

### Insights

- The archive being strictly downstream of `_persist()` (`:667`) is what makes this trustworthy as a backtest source — you can never get a phantom snapshot that "the system saw" but never acted on.
- Biggest threat to backtest validity: _judgment-layer drift_. The bias function evolves but the archive doesn't tag which version produced any cached label. Re-deriving from raw `report`/`display` is the only honest way to compare across time.
- Demand-driven sampling is a sneaky form of selection bias — backtesting only on dense-history tickers will overstate edge on names you already had conviction about. Neutral universe (resample to daily, accept gaps, weight by inverse sampling density) helps neutralize it.

---

## 3. "VRP z-score unavailable — regime defaulted to cautious"

### The literal trigger

`scripts/uw_analyze.py:143-144` appends the note iff `vrp.vrp_zscore is None`. The z-score itself is built in `scripts/analysis/vrp.py:66-68`:

```python
vrp_raw = None
if td.iv is not None and td.rv is not None:
    vrp_raw = td.iv - td.rv

vrp_zscore = None
if vrp_raw is not None and td.vrp_history:
    vrp_zscore = _zscore(td.vrp_history, vrp_raw)
```

`_zscore` (`vrp.py:50-58`) requires **at least 10 history points**. So `vrp_zscore` is None when **any** of the following fails:

| #   | Missing piece                 | Where it would have come from                                         | Why it's commonly absent                                                                                                                                                                                                                                                   |
| --- | ----------------------------- | --------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `td.iv` (current implied vol) | UW volatility/IV endpoint, parsed in `ticker_data.py`                 | Endpoint 429/5xx, illiquid name, or no listed options surface                                                                                                                                                                                                              |
| 2   | `td.rv` (realized vol)        | UW or computed from price history                                     | Same — fetch failure or insufficient bars                                                                                                                                                                                                                                  |
| 3   | `td.vrp_history` non-empty    | `client.get_variance_risk_premium(ticker)` (`ticker_data.py:374-390`) | The `getattr(client, "get_variance_risk_premium", None)` is **conditional on Task 0 endpoint probe** — if the probe decided UW doesn't expose `/variance_risk_premium` for your token tier, the method isn't even attached, so `vrp_history` stays `None` for every ticker |
| 4   | `len(vrp_history) >= 10`      | Same endpoint, longer lookback                                        | Endpoint returned a stub series (new ticker, recent IPO, or UW backfill incomplete)                                                                                                                                                                                        |

### How to tell which is biting you

Read `data_freshness` on `VRPState` (`vrp.py:74-79`):

- `"unavailable"` → **Cause #1**: `td.iv` is None. UW IV fetch failed.
- `"stale"` → **Cause #2/3/4**: `td.iv` present but `vrp_history` missing/too short, OR `td.rv` missing so `vrp_raw` couldn't be computed.
- `"live"` → z-score actually populated; wouldn't see the note.

In any cached entry: `report.regime.data_freshness`. For a quick triage across the archive, grep `data/uw_analyze_history/<TICKER>/*.json` for `"data_freshness"`.

### Most likely culprit in practice

`get_variance_risk_premium` is the prime suspect. The comment at `ticker_data.py:372` ("conditional on endpoint availability (Task 0 probe result)") means the system intentionally doesn't try to fetch VRP history if the probe at startup couldn't reach that endpoint. Two things flip the probe off and silently disable VRP z-scoring for _every_ ticker:

1. **UW token tier doesn't include `/variance_risk_premium`** — common on lower tiers; endpoint 401/403s, probe records "not available," runtime never re-checks.
2. **Probe ran during a UW outage** — endpoint was 5xxing at startup, probe marked unavailable, even though UW recovered the runtime won't re-attach the method until process restart.

If `data_freshness == "stale"` across many tickers simultaneously, that's the signature.

### Fixes by cause

| Cause                      | Fix                                                                                                                                                                                           |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Probe-disabled VRP history | Restart the API process; verify `client.get_variance_risk_premium` exists post-startup. If still missing, hit `/variance_risk_premium/AAPL` directly with `$UW_TOKEN` to confirm tier access. |
| Per-ticker fetch failure   | Check `logger.debug` output around `ticker_data.py:391` — exceptions swallowed at debug level, so bump analyser log level temporarily.                                                        |
| `<10` history points       | Either accept (illiquid/young name; cautious default is correct) or relax `_zscore`'s `< 10` floor to e.g. `< 5` to trade variance for coverage.                                              |
| Missing `iv`/`rv`          | Investigate the IV fetch path separately — unrelated to VRP history but kills the z-score the same way.                                                                                       |

### What "defaulted to cautious" actually means

The note is cosmetic — UI text only. The actual conservative behavior is in `classify_regime` (`vrp.py:147-156, 158-166`):

- Rule at `:147`: deeply negative GEX + flip far away → R2 even when `vrp_zscore is None`. The `or vrp.vrp_zscore < 0.3` clause treats unknown as below-threshold.
- Rule at `:158`: thin VRP (`vrp_zscore < 0.3`) → R1. Unknown VRP doesn't trigger this branch directly, but it also can't reach the R0 branch at `:168` which strictly requires `vrp_zscore > 0.5`.

Net effect: with `vrp_zscore = None` you can land in R1 or R2 but **never R0**. That's the "defaulted to cautious" the note warns about. The missing data isn't just cosmetic — it caps the most bullish regime classification entirely. If you see this note often, your hit rate on R0-gated trades is structurally zero.

### Insights

- The probe-conditional `getattr` pattern at `ticker_data.py:374` is a common stale-flag trap: a one-time startup check that never re-validates becomes load-bearing for every downstream decision. A periodic re-probe (e.g. once an hour) would self-heal after UW outages without a restart.
- The note is cosmetic but the regime classifier's `vrp_zscore is None` treatment is _not_ — it asymmetrically blocks R0 while leaving R2 reachable, which is the right safety bias but means missing VRP data quietly turns the system into a one-sided bear-only regime classifier. Worth surfacing in the UI more loudly than a footnote.
- `_zscore`'s `max(std, 0.01)` floor at `:58` prevents divide-by-zero but also caps the signal — on a near-constant VRP series you'll get artificially inflated z-scores. Fine for the current >10-sample minimum, but if you ever lower that floor, revisit this.
