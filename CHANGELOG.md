# Changelog

All notable changes to Xenon are documented here. Format loosely based on
[Keep a Changelog](https://keepachangelog.com/) with semver-ish versioning.

## [0.1.0] - 2026-04-15

### Added — Trend Scanner: bearish pipeline, catalyst stage, pre-market prep

Fourteen-commit feature branch addressing all nine findings from the
Codex+Gemini+Claude tribunal review of `feat/ta-integration`. Plan:
`docs/superpowers/plans/2026-04-14-trend-scanner-tribunal-fixes.md`.

#### Signal accuracy

- Breakout detection now requires `close >= high_20d` — consolidation
  narrowness alone no longer flags as breakout (Finding #3).
- Volume profile isolates up-day vs down-day volume via new
  `up_day_volume_ratio`, weighted 2× so distribution patterns penalize
  the trend score (Finding #9).
- Stage B rejects unsupported overhead walls: call wall within 2% above
  spot with no supportive put wall below now hard-fails like severe
  pinning (Finding #8).
- Snapshot exposes `high_20d`, `low_20d`, `low_52w`, `up_day_volume_ratio`
  — additive schema change, enables both breakout gate and bearish mirror.

#### Scope expansion

- **Bearish pipeline** runs alongside bullish (Finding #1). Stage A split
  into direction-neutral data fetch + direction-specific gate. Mirrored
  `passes_bearish_gate`, `detect_breakdown`,
  `has_unsupported_underhead_wall`. Structure / OI / flow scoring branch
  on direction. Live scan emits bullish AND bearish candidates per ticker.
- **Stage C catalyst check** via UW headlines + earnings/FDA/guidance
  flags (Finding #4). Degrades gracefully when headlines unavailable.
  Weight 0.10 in final ranking; weights rebalanced to explicit 5-key
  dict: `{trend: 0.30, structure: 0.25, volatility: 0.20, flow: 0.15,
catalyst: 0.10}`.
- **Analysis-only scoping**: `suggested_trade` field removed from
  `TrendCandidate`; replaced with advisory `structure_hint` (defined-risk
  long-side only). Every candidate auto-flagged
  `four_gates_not_applied`. Cross-layer change — Python model + DuckDB
  schema + TypeScript types + web components kept in lockstep
  (Finding #2).

#### Defensive hardening

- SPY pre-cache crash guard: scan no longer aborts when SPY is cold
  and IB is unavailable (Finding #7).
- Staleness check unified: `ta_premarket_prep.classify_tickers`
  delegates to `TAService._is_stale` — audit and scanner now share one
  truth. New `TAService.read_only(conn)` factory encapsulates the audit
  construction (Finding #6).
- Pre-market prep warms the full triple-source scanner universe
  (static + UW flow + IB scanner), not just the S&P 500 static slice
  (Finding #5). Persists to `data/ta_premarket_universe.json` with
  UTC timestamps + honest `source_counts` telemetry. Scanner reuses
  it if <2h old.
- `--audit-only` stays strictly offline — no UW/IB connection attempts.
- UW client cleanup symmetric with IB disconnect; refresh phase wrapped
  in try/finally.

### Fixed

- `fetch_catalysts` tolerates `earnings_days=None` (UW may not resolve
  earnings date for every ticker) — was raising `TypeError` on the
  `0 <= None` comparison. Regression test added.
- Scanner top-level error handler now logs with `exc_info=True` so
  future regressions surface at stderr instead of being silently
  flattened to a one-line message.

### Known follow-ups (documented, not blocking)

See `docs/superpowers/plans/2026-04-15-trend-scanner-post-verification-followups.md`:

- `UWClient.get_headlines` not implemented — catalyst score stuck at
  neutral 0.5 until added.
- `TAService.bulk_refresh` 10-min IB pacing sleep drops the socket —
  universe larger than one pacing window cannot be warmed.
- SPY/VIX should be prioritized first in refresh ordering so
  `market_context.regime` isn't `unknown` on partial refreshes.

### Test coverage

2220 passed / 90 skipped / 0 failed across `scripts/tests/` at the tip
of the branch.

## [0.0.1] - Initial

Project scaffold prior to this changelog.
