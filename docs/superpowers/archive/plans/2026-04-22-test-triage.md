# Test-suite triage — 2026-04-22

Full-scope run during PR-C/D burn-in. Source logs: `output/pytest-full.txt`,
`output/vitest-full.txt`. Playwright `--list` can't run headless without a dev
server; skip/fixme enumeration done via `grep` over `web/e2e/`.

## Headline

| Suite      | Passed | Failed | Skipped | Verdict                               |
| ---------- | ------ | ------ | ------- | ------------------------------------- |
| pytest     | 2495   | 0      | 90      | **green**, skips all env-gated        |
| Vitest     | 1642   | **24** | 1       | **broken** — 8 test files regress     |
| Playwright | —      | —      | 8       | `.skip`/`.fixme`, no run this session |

The Vitest breakage is the surprise. Triage below.

## Vitest failures (24) — 8 files

Grouped by likely root cause. Detail in `output/vitest-full.txt`.

### Group A — upstream-error propagation drift (10 fails, 2 files)

Tests assert old behavior where the web route **wrapped** the FastAPI error in
a generic 500 / `XenonApiError` text. After F6 (`orders-upstream-preserved`
contract) the web route now passes the upstream status + detail through.

| File                                          | Fails | Action                                                                      |
| --------------------------------------------- | ----- | --------------------------------------------------------------------------- |
| `api-routes-extended.test.ts`                 | 8     | **Fix tests** — update assertions to match upstream-preserved contract      |
| `order-place-route-error-propagation.test.ts` | 1     | Likely same — verify it actually asserts _old_ behavior (name suggests new) |

Verify first: read one case and confirm whether it's asserting pre-F6 or
post-F6. If post-F6, this is a real regression, not a test-rot fix.

### Group B — WS mock setup broken (4 fails, 1 file)

| File                           | Symptom                                                       |
| ------------------------------ | ------------------------------------------------------------- |
| `ticker-search-filter.test.ts` | `ws.simulateOpen()` — `ws` is `undefined` → mock not attached |

Root cause is test-file local. Either the WS mock was renamed or the setup
hook isn't running. Fix is likely 3 lines.

### Group C — timeouts (3 fails, 1 file)

| File                            | Symptom                             |
| ------------------------------- | ----------------------------------- |
| `uw-analyze.component.test.tsx` | 3 tests hit 5000ms `Test timed out` |

Classic symptom of a `setPortfolio([])` path that never promotes to "ready".
Either raise `testTimeout` or advance fake timers. Investigate `setPortfolio`
promotion logic — this may be a real UI bug (scaffold-first promotion path).

### Group D — Futu Day P&L source (1 fail, 1 file)

| File                                        | Action      |
| ------------------------------------------- | ----------- |
| `metric-cards-futu-day-pnl-source.test.tsx` | Investigate |

Likely related to the Futu staleness work — copy drift or computed intraday
P&L not wired. One-hour task.

### Group E — IB realtime server (2 fails, 2 files)

| File                                | Symptom                                |
| ----------------------------------- | -------------------------------------- |
| `ib-index-stream-contracts.test.ts` | Typed contracts for cold-start restore |
| `ib-realtime-restart-modes.test.ts` | ESM imports vs require() for launchd   |

These feel like snapshot-of-implementation tests that drifted from the actual
server. Investigate whether the server code or the test is out of date. The
`launchd` one is macOS-specific — may not even be exercised in CI.

### Group F — FastAPI-gated E2E (5 fails, 1 file)

| File                | Symptom                                   |
| ------------------- | ----------------------------------------- |
| `order-e2e.test.ts` | `(requires FastAPI)` suite ran and failed |

`fastApiHarness.available` returned true but the harness didn't actually
deliver. Either the harness env probe is wrong or the FastAPI test-mode stub
broke. **Likely same mechanism that masked the F3 stub bug pre-hotfix**
(see `project_pr_cd_handover.md`).

---

## Vitest skips (1)

| File                       | Why                       | Action                                                                         |
| -------------------------- | ------------------------- | ------------------------------------------------------------------------------ |
| `route-cache-meta.test.ts` | `describe.skip` — removed | **Delete the file** — it's a tombstone for `cache_meta` which no longer exists |

## Playwright `.skip` / `.fixme` (8)

| File                                | Kind          | Note                                                                  |
| ----------------------------------- | ------------- | --------------------------------------------------------------------- |
| `regime-cor1m-live-route.spec.ts`   | `test.skip()` | Conditional: skips when live CRI cache has no numeric COR1M. **Keep** |
| `share-pnl.spec.ts` (×4)            | `test.skip()` | Conditional visibility guards. **Keep**                               |
| `risk-reversal-midprice.spec.ts`    | `test.fixme`  | MIDPRICE badge — investigate, un-fixme or delete                      |
| `price-bar-quote-telemetry.spec.ts` | `test.fixme`  | BID/MID/ASK order on shared ticker modal — un-fixme or delete         |
| `spread-price-bar.spec.ts`          | `test.fixme`  | Net spread prices — un-fixme or delete                                |
| `ilf-chart-price.spec.ts`           | `test.fixme`  | ILF seeds above $30 — un-fixme or delete                              |

All four `.fixme` cases describe behavior that _should_ hold. Either the
behavior isn't implemented yet (un-fixme → write impl → green) or the spec
was wrong (delete). Owner decision per case.

## Pytest skips (90)

All 90 are `MENTHORQ_USER / MENTHORQ_PASS not set` in
`test_menthorq_integration.py`. **Keep.** Env-gated integration tests —
expected skip when creds absent. No action.

---

## Recommended execution order

1. **Group F + A first** — these directly touch the order-route contract
   that just shipped in PR #29. If any of these are _real_ regressions, we
   need to know during burn-in, not after.
2. **Group B, D** — small, bounded fixes.
3. **Group C** — investigate UI promotion path; could be real bug.
4. **Group E** — lowest-value; only fix if CI is actually running them.
5. **Delete** `route-cache-meta.test.ts` (tombstone).
6. **`.fixme` sweep** — per-case decision, batch into one PR.

## Timebox

2 days total across groups. If any group escalates past half a day,
pause and reassess — burn-in monitoring takes priority over test polish.
