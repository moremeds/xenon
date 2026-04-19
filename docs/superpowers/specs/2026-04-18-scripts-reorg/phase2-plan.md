# Phase 2 — Execution Plan

**Date:** 2026-04-19
**Authoritative design:** `phase2-design.md` in this directory.
**This doc:** execution-layer concerns — PR grouping, cadence, pre-flight, verification. Where it disagrees with the design, flag and resolve; don't silently diverge.

---

## Decisions Locked

| Area                        | Choice                                                          | Notes                                                                                          |
| --------------------------- | --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Scope                       | Phase 2 + Phase 1 loose ends                                    | Bundle the 1 remaining unbucketed file and a pre-flight smoke tool before the big moves start. |
| PR cadence                  | 5 grouped PRs, not 10+ per-bucket PRs                           | Soak only on production-path PRs.                                                              |
| `apex_refresh.py` placement | `scripts/fetchers/fetch_apex_data.py` → `xenon-fetch-apex-data` | `_data` disambiguates vs the broker/bucket/action name collision.                              |
| Smoke mechanism             | Bash script, `scripts/infra/dev/smoke_phase1_shims.sh`          | Re-runnable on laptop + VPS; doubles as mid-incident tool.                                     |

---

## Tribunal Review Findings — Resolved In-Line (2026-04-19)

A 3-way review (Codex + Gemini + Claude) surfaced 8 CRITICAL and 10 IMPORTANT issues against the first plan draft. Each is addressed in the section below where it lives:

| Finding                                                                                                                    | Where addressed                                                 |
| -------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- | -------------------------------------- |
| `scripts/api/server.py` has no `def main()` for `xenon-api` to call                                                        | PR 4 step 0 (prep extraction)                                   |
| `scripts/lib/` is JS-only, `scripts/infra/` is Node + Bash → can't move into `src/xenon/`                                  | PR 2 (carve-out) + PR 5 (slim layout)                           |
| Internal `subprocess.run([…, "scripts/X.py"])` callers in `risk_reversal.py`, `portfolio_report.py`, `cta_sync_service.py` | PR 4 step 4b (new)                                              |
| `_run_ib_script_with_recovery(script: str, args)` wrapper at `server.py:1697` takes script-name string; 7 call sites       | PR 4 step 3                                                     |
| `config/*.plist` files hardcode `.py` and `.sh` absolute paths; installers `cp` verbatim                                   | PR 4 step 6 (rewrite plist sources)                             |
| Plan's API verification hits non-existent routes (`/trend-scan/run`, `/cri-scan/run`)                                      | PR 4 verification                                               |
| `setup_exit_order_service.sh` is legacy — replaced by `setup_monitor_daemon.sh`                                            | PR 3 + PR 4 (target monitor-daemon)                             |
| Inline `python -c "from utils.market_calendar …"` in shell wrappers + setup installers                                     | PR 4 step 5 (new)                                               |
| `pyproject.toml` already exists with partial `[project]` block + `target-version = "py39"`                                 | PR 1 (expand, don't create)                                     |
| Rollback `git checkout … -- scripts/` reverts too much                                                                     | PR 4 rollback (explicit file list)                              |
| Docs grep gate impossible — many intentional `scripts/*.py` doc references                                                 | PR 4 verification (split runtime from docs) + PR 5 (docs sweep) |
| PR 5 missing `scripts/infra/dev/run_pytest_affected.py` (hardcodes `scripts/tests`)                                        | PR 5                                                            |
| `ta_premarket_prep` 6 AM ET trigger has no in-repo scheduler — external caller                                             | PR 3 (TA moved here, was PR 2)                                  |
| `data/reconciliation.json` startup gate doesn't map to actual writer                                                       | PR 3 verification (replaced)                                    |
| Entry-point smoke list incomplete                                                                                          | PR 4 (expanded)                                                 |
| `launchctl list                                                                                                            | grep` only proves "loaded", not healthy                         | PR 4 verification (per-service status) |
| Verification regex misses `.js` and `test_*` files                                                                         | PR 4 verification (widened)                                     |
| `web/tests/integration.test.ts` has hardcoded `["scripts/X.py", …]` test assertions                                        | PR 4 step 4a (enumerated)                                       |

---

## PR 0 — Prerequisites

**Goal:** start Phase 2 from a clean tree with a reusable smoke tool in hand.

### Changes

1. Bucket `apex_refresh.py`:
   - `git mv scripts/apex_refresh.py scripts/fetchers/fetch_apex_data.py`.
   - Rewrite sibling imports (`rg "from apex_refresh\b|import apex_refresh\b" scripts/ .github/`).
   - Update `.github/workflows/apex-data-refresh.yml:97` (`python scripts/apex_refresh.py …` → `python scripts/fetchers/fetch_apex_data.py …`).
   - Install Phase 1 shim at `scripts/apex_refresh.py` (runpy-based) so `docs/runbooks/apex-r2-cutover.md:26` and any out-of-tree caller keeps working until PR 4.
   - Update `scripts/CLAUDE.md:129` command-table row.
2. Ship `scripts/infra/dev/smoke_phase1_shims.sh`:
   - Invokes `--help` on every Phase 1 Python shim in the `phase1-design.md` §"Baseline" smoke list (~25 CLIs) plus the 2 shell wrappers (`run_cri_scan.sh --help`, `run_cta_sync.sh --help`) and `scripts/test_ib_realtime.py --help`.
   - Asserts exit code 0 per entry; prints `OK`/`FAIL` per entry; exits non-zero on any failure.
   - Source of truth for the list: keep the CLI array inside the script so adding/removing shims is a one-line edit.
   - Symlink at `scripts/smoke_phase1_shims.sh` so both old and new paths work.
3. **Create `scripts/tests/test_phase1_shim_compat.py`** (it does not exist yet; recent commit `e2a292ca` patched import paths elsewhere, not this file). Seed with one test per shim that runs `python3.13 scripts/<old>.py --help` via `subprocess` and asserts RC=0. Include `fetch_apex_data` and `test_ib_realtime` from day one.

### Verification

```bash
# From repo root:
bash scripts/infra/dev/smoke_phase1_shims.sh      # all OK, exit 0
python3.13 scripts/run_pytest_affected.py         # green
python3.13 -m pytest scripts/tests/test_phase1_shim_compat.py -xvs
python3.13 scripts/apex_refresh.py --help         # shim OK
python3.13 scripts/fetchers/fetch_apex_data.py --help  # new path OK
```

Run smoke on **both laptop and VPS**:

```bash
ssh vps 'cd /path/to/xenon && bash scripts/infra/dev/smoke_phase1_shims.sh'
```

Record output in the PR description as baseline-verified.

### Gate before proceeding

- [ ] Smoke script exits 0 on laptop.
- [ ] Smoke script exits 0 on VPS.
- [ ] Pytest green locally.
- [ ] GHA `apex-data-refresh` workflow runs successfully once (or equivalent dry-run) against the new path.

---

## PR 1 — Package scaffolding (zero behavior change)

**Goal:** `uv sync` works, `src/xenon` is importable, nothing else changes.

### State at plan-write time

`pyproject.toml` **already exists** (Phase 1 scaffolding shipped a partial `[project]` block, ruff `known-first-party` list, and `[tool.pytest.ini_options]`). PR 1 **expands** the existing file — does not create it.

### Changes

Per `phase2-design.md` Step 1, applied as edits to existing `pyproject.toml`:

- Add `[build-system] requires = ["hatchling"]` + `build-backend = "hatchling.build"`.
- Expand existing `[project]` block: add `name = "xenon"`, `version = "0.2.0"`, `requires-python = ">=3.13"`, `description`, `readme`. Migrate `scripts/requirements-api.txt` pins into `dependencies` (keep `requirements-api.txt` alive — deleted in PR 4).
- Add `[tool.hatch.build.targets.wheel] packages = ["src/xenon"]`.
- **Bump `[tool.ruff] target-version` from `"py39"` → `"py313"`** (currently lies; the project requires 3.13 elsewhere).
- Stub `src/xenon/__init__.py` (`"""Xenon package root."""`).
- `.python-version` pinned to `3.13`.
- Run `uv sync`; commit `uv.lock`.
- Declare `[project.optional-dependencies] test = [...]` and `dev = [...]` from existing pins (preserve `moto[s3]` already present).

**No `[project.scripts]` entries yet** — those arrive incrementally in PR 2–4.

**No `[project.scripts]` entries yet** — those arrive incrementally in PR 2–4.

### Verification

```bash
rm -rf .venv
uv sync --frozen                                   # clean install succeeds
.venv/bin/python -c "import xenon; print(xenon)"   # package importable
bash scripts/infra/dev/smoke_phase1_shims.sh       # Phase 1 shims still green
python3.13 scripts/run_pytest_affected.py          # pytest still passes
python3.13 -m pytest scripts/tests/ -x             # full suite green
curl http://localhost:8321/health                  # FastAPI unaffected (still uses system python)
```

### Gate before proceeding

- [ ] `uv sync --frozen` works from a freshly-deleted `.venv`.
- [ ] `uv.lock` present and committed.
- [ ] Phase 1 shims unchanged, smoke still green.
- [ ] No regressions in pytest or FastAPI.

---

## PR 2 — Leaf + foundational buckets (bundled move)

**Goal:** move every bucket with narrow blast radius — no scheduler dependency — in one PR. High-risk buckets deferred to PR 3.

### Buckets moved

- `shares/` (4 files)
- `clients/`, `utils/`, `analysis/`, `config/`, `monitor_daemon/`, `trade_blotter/` (all pure Python)

### Buckets carved-out — DO NOT move into `src/xenon/`

The original spec listed `lib/`, `infra/`, and `ta/` as PR 2 candidates. Audit found:

- **`scripts/lib/`** contains only `lru-cache.js` and `rate-limiter.js` — imported by `web/tests/lru-cache.test.ts`. Stays at `scripts/lib/` permanently (final slim layout). Move only the `__init__.py` if any is present; do not relocate `.js` files.
- **`scripts/infra/`** contains `cloud.sh`, `local.sh`, `docker_ib_gateway.sh`, the Node `ib_realtime/` server, plus `infra/dev/` (Python dev tools). Carve-out:
  - `scripts/infra/dev/*.py` (the Python content) → `src/xenon/infra/dev/` is wrong — these are dev tools, not package code. Move to `src/xenon/dev/` instead, OR leave at `scripts/infra/dev/` as standalone scripts. **Decision: leave at `scripts/infra/dev/` — they're invoked by path, not import, and don't need entry-point binaries.**
  - `scripts/infra/*.sh` and `scripts/infra/ib_realtime/*` stay where they are (final slim layout).
- **`scripts/ta/`** moved to PR 3, not PR 2 — `ta_premarket_prep.py` runs at 6 AM ET via an external (non-FastAPI) scheduler. Treat as production-path; soak required.

### Changes per moved bucket

1. `git mv scripts/<bucket> src/xenon/<bucket>` (Python-only buckets per the list above).
2. Rewrite bare-name imports to `from xenon.<bucket>.<name> import ...` (`rg -l '^(from|import) <bucket>\b' src/ tests/ scripts/` → sed).
3. Remove now-obsolete `sys.path.insert` blocks (the per-file Phase 1 fixes — `.parent.parent` gymnastics are unnecessary once `xenon` is editable-installed).
4. Update Phase 1 shim at `scripts/<old>.py` to forward via `runpy.run_module("xenon.<bucket>.<name>", run_name="__main__")` so CLI semantics survive.
5. Add `[project.scripts]` entry per CLI file in `pyproject.toml`. Run `uv sync` so binaries appear.

### Verification

After **each bucket** move inside the PR:

```bash
uv sync --frozen                                        # binaries regenerate
.venv/bin/pytest tests/ -x                              # pytest green
bash scripts/infra/dev/smoke_phase1_shims.sh            # old paths still work
rg "^(from|import) <bucket_just_moved>\b" src/ tests/   # must be zero
```

Verification after the whole PR:

```bash
uv sync --frozen
.venv/bin/xenon-generate-cta-share --help               # shares/
.venv/bin/xenon-generate-gex-share --help
.venv/bin/xenon-ta-cli AAPL --help                      # ta/
.venv/bin/xenon-ta-premarket-prep --help
# Sibling-import regression (foundational buckets are heavily imported):
.venv/bin/python -c "from xenon.utils import ib_connection; print('ok')"
.venv/bin/python -c "from xenon.clients import ib_client; print('ok')"
.venv/bin/python -c "from xenon.trade_blotter import flex_query; print('ok')"
bash scripts/infra/dev/smoke_phase1_shims.sh            # still green
python3.13 -m pytest scripts/tests/ -x
cd web && npm test && npx playwright test               # web unaffected (still calls shims)
```

### Gate before proceeding

- [ ] Every `[project.scripts]` entry for a moved bucket has a working `.venv/bin/xenon-*` binary.
- [ ] `rg '^(from|import) (shares|clients|utils|analysis|config|monitor_daemon|trade_blotter)\b' src/` returns zero results (note: `infra`, `ta`, `lib` **not** listed — carved out).
- [ ] Phase 1 smoke script still green (shims still forward correctly).
- [ ] Web test suite + Playwright green; `web/tests/lru-cache.test.ts` still resolves `scripts/lib/lru-cache.js`.
- [ ] No `sys.path.insert` lines left inside any moved file.

**No soak required** — none of these buckets are on a scheduler's critical path.

---

## PR 3 — Production-path buckets

**Goal:** move the scheduler-critical and broker-critical buckets. Soak after.

### Buckets moved

- `fetchers/` (includes the newly-bucketed `fetch_apex_data`)
- `scanners/` — both stages (2.8a direct moves + 2.8b `trend/` and `uw/` paired consolidations). Includes the `trend_scan.py` → `xenon.scanners.trend.cli` flow that runs at 8:30 AM ET.
- `ta/` — `ta_cli`, `ta_premarket_prep`, `ta_reseed_massive`, `ta_lib/*`. **Moved here (not PR 2) because `ta_premarket_prep.py` runs at 6 AM ET via an external scheduler; no in-repo scheduler caller exists, so production impact requires soak.** Before merging: audit the external trigger (user's laptop cron? VPS cron? GHA? launchd plist not in `config/`?) and update its invocation to use `.venv/bin/xenon-ta-premarket-prep`.
- `execution/` — IB + Futu + naked short audit. Broker writes.
- `reports/` — portfolio + evaluate + kelly. Consumed by `web/` via subprocess.
- `services/` — `cta_sync_service.py` and the **monitor-daemon stack** (`monitor_daemon/` package). `exit_order_service.py` is **legacy** — `setup_monitor_daemon.sh:22-37` explicitly removes the old exit-order plist. Plan to **retire** `exit_order_service.py` entry point rather than ship `xenon-exit-order-service` — confirm monitor-daemon has taken over exit-order behavior before deleting.

### Per-file pre-commit checklist (from `phase2-design.md` §"Per-File Pre-Commit Checklist")

Apply during this PR:

- [ ] `sys.path.insert(0, ...)` fixes for `risk_reversal`, `portfolio_report`, `portfolio_performance`, `evaluate`, `gex_scan`, `garch_convergence`, `fetch_news`, `ib_order_manage`, `naked_short_audit`, `ib_reconcile`, `verify_options_oi`, `ib_place_order`. After the move, remove the inserts entirely (editable install handles it).
- [ ] `blotter.py` hardcoded `trade_blotter` sibling path — remove entirely (xenon-qualified import replaces it).
- [ ] `ta_cli.py` / `ta_premarket_prep.py` `_project_root` walk — already handled in PR 2 since `ta/` moves there. Verify.
- [ ] `main()` extraction for any file with inline `if __name__ == "__main__":` logic.

### Verification

After each bucket move inside the PR (order: `fetchers/`, then `scanners/`, then `ta/`, then `execution/`, then `reports/`, then `services/`):

```bash
uv sync --frozen
.venv/bin/pytest tests/ -x
bash scripts/infra/dev/smoke_phase1_shims.sh
rg "^(from|import) <bucket>\b" src/ tests/ scripts/     # zero
```

End-of-PR integration verification (real FastAPI routes — see `scripts/api/server.py`):

```bash
# Entry points:
.venv/bin/xenon-fetch-flow AAPL --help
.venv/bin/xenon-fetch-apex-data --help
.venv/bin/xenon-trend-scan --top 5
.venv/bin/xenon-uw-scan --help
.venv/bin/xenon-cri-scan --json >/dev/null        # CLI; note FastAPI surfaces it as /regime/scan
.venv/bin/xenon-ib-sync --help
.venv/bin/xenon-ib-reconcile --help
.venv/bin/xenon-futu-sync --help
.venv/bin/xenon-evaluate AAPL
.venv/bin/xenon-kelly --help
.venv/bin/xenon-portfolio-report --help
.venv/bin/xenon-ta-cli AAPL
.venv/bin/xenon-ta-premarket-prep --help
.venv/bin/xenon-cta-sync-service --help
# Note: no xenon-exit-order-service — service retired in favor of monitor-daemon.

# FastAPI via old shim (PR 4 flips this to entry points):
curl -sf http://localhost:8321/health             # ib_gateway.port_listening: true
curl -sfX POST http://localhost:8321/trend-scan   # NOT /trend-scan/run — real route
curl -sfX POST http://localhost:8321/regime/scan  # NOT /cri-scan/run — real route
curl -sfX POST http://localhost:8321/vcg/scan
curl -sfX POST http://localhost:8321/gex/scan

# Broker wiring still intact:
python3.13 -m pytest scripts/tests/test_ib_execute.py scripts/tests/test_ib_reconcile.py -x

# Web + E2E:
cd web && npm test && npx playwright test
```

### Soak — 1 full trading day

- [ ] 8:30 AM ET trend scan completes successfully (`data/trend_scan.json` timestamp within the window).
- [ ] 6 AM ET TA prep completes successfully (log-verified — check external scheduler's log, not FastAPI).
- [ ] `monitor-daemon` service logs clean (`logs/monitor-daemon.log`; no uncaught tracebacks). **Do not look for `ScriptResult(ok=False)` in this log — the service uses bespoke logging, not `ScriptResult`.**
- [ ] Manual `xenon-ib-reconcile` invocation updates `data/reconciliation.json` with fresh timestamp (FastAPI startup does **not** run reconciliation; don't assert startup-triggered update).
- [ ] No new entries in `logs/cri-scan.err.log` or equivalent.

### Gate before PR 4

- [ ] Soak clean.
- [ ] Smoke still green on laptop + VPS.
- [ ] Web test suite green.
- [ ] No `sys.path.insert` lines in `src/xenon/`.

---

## PR 4 — `api/` move, call-site rewrite, shim deletion

**Goal:** complete the cutover. After this PR, no one should be calling `scripts/<anything>.py` paths except `.sh` files still living in `scripts/`.

### Changes (ordered commits inside the PR)

0. **Prep: extract `def main()` in `scripts/api/server.py`.** The current file ends with `if __name__ == "__main__":` followed by inline `uvicorn.run("scripts.api.server:app", ...)`. Before the move, refactor to:

   ```python
   def main() -> None:
       import uvicorn
       uvicorn.run("xenon.api.server:app", host="127.0.0.1", port=8321, reload=True)

   if __name__ == "__main__":
       main()
   ```

   The `uvicorn.run` target string also needs updating from `"scripts.api.server:app"` → `"xenon.api.server:app"` during the move (Commit 1). Without this prep, `[project.scripts] xenon-api = "xenon.api.server:main"` cannot generate a valid binary.

1. **Move `api/`**: `git mv scripts/api src/xenon/api`. Rewrite all `api/` internal imports to `xenon.*`. Add `[project.scripts] xenon-api = "xenon.api.server:main"` (the `main` extracted in Commit 0).

2. **Rewrite `scripts/api/subprocess.py`** (which now lives at `src/xenon/api/subprocess.py`): replace `run_script(name, args)` with `run_entry_point(entry, args)` that execs `.venv/bin/<entry>` directly (no `uv run`). Keep a thin `run_script()` legacy shim mapping old filename → entry-point name during this PR only; delete before merge.

3. **Update every `run_script` and `_run_ib_script_with_recovery` call site** in `src/xenon/api/server.py` (one commit per logical group — scanner runs, report runs, share-card runs, fetcher runs, IB runs).
   - Direct `run_script("X.py", ...)` literals: ~14 sites (see `scripts/api/server.py:139, 997, 1007, 1017, 1066, 1254, 1263, 1295, 1447, 1463, 1487, 1498, 1507, 1597, 1647`).
   - Wrapper: `_run_ib_script_with_recovery(script: str, args)` at `server.py:1697` — 7 call sites pass `"ib_orders.py"`, `"ib_place_order.py"`, `"ib_order_manage.py"` (×2), `"ib_option_chain.py"`. Change wrapper signature so callers pass entry-point names (`"xenon-ib-orders"`, etc.); wrapper then delegates to `run_entry_point()`.
   - Variable `run_script(script, args)` at `server.py:1741, 1795` — `script` is the same parameter flowing through the IB wrapper; no extra work once the wrapper is converted.

4. **Update `web/` TypeScript callers** — one commit per file:
   - **4a. Subprocess-invoking callers**:
     - `web/app/api/pi/route.ts:251` (`["scripts/evaluate.py", ...]`), `:404` (`["scripts/ib_sync.py"]`), `:419` (`["scripts/leap_scanner_uw.py"]`).
     - `web/app/api/ticker/ratings/route.ts:15` (`runScript("scripts/fetch_analyst_ratings.py", ...)`).
     - `web/app/api/menthorq/[command]/image/route.tsx:368` — UI string, not code; update the hint text.
     - `web/lib/runner.ts` (helper, if exists — if not, `runScript` definition lives inline in routes).
   - **4b. Test-assertion string-match callers** (these embed the script path as expected value, not as invocation; plan's "update assertion" covers both):
     - `web/tests/integration.test.ts:52-53` — `buildEvaluateCommand` asserts `["scripts/evaluate.py", ...]`.
     - `web/tests/integration.test.ts:113-116, 129` — literal path assertions in help-smoke fixture.
     - `web/tests/orders-manage.test.ts:160` — `path.resolve(__dirname, "../../scripts/ib_order_manage.py")` loads file content for contract tests; update to `src/xenon/execution/ib_order_manage.py`.
   - **4c. `web/package.json` + `web/README.md`**:
     - `web/package.json:20` — `"test:ib": "python3.13 ../scripts/test_ib_realtime.py"` → `"python3.13 ../scripts/infra/ib_realtime/test_ib_realtime.py"` (this file is a dev-smoke harness, not a `xenon-*` entry point — stays at filesystem path).
     - `web/README.md:291-293` — update `../scripts/test_ib_realtime.py` references to new path.

5. **Update shell scripts — both direct invocations AND inline Python snippets.**
   - **5a. Direct invocations** in `scripts/services/run_*.sh`, `scripts/benchmarks/autoresearch.sh`: replace `"$PYTHON_BIN" scripts/X.py` with `"$PROJECT_DIR/.venv/bin/xenon-X"` and add `export PATH="$PROJECT_DIR/.venv/bin:$PATH"` at the top.
   - **5b. Inline Python in shell wrappers** (`scripts/services/run_cri_scan.sh`, `run_data_refresh.sh`, `scripts/services/setup_cri_service.sh:189`, `scripts/services/setup_monitor_daemon.sh:84`): replace patterns like:
     ```bash
     IS_TRADING=$("$PYTHON_BIN" -c "import sys; sys.path.insert(0, 'scripts'); from utils.market_calendar import _is_trading_day; print(_is_trading_day())")
     ```
     with:
     ```bash
     IS_TRADING=$("$PROJECT_DIR/.venv/bin/python" -c "from xenon.utils.market_calendar import _is_trading_day; print(_is_trading_day())")
     ```
     The `sys.path.insert` is removed — editable install makes `xenon.*` importable without it.
   - Verify each rewritten wrapper under a fresh launchd context (unload + load) before moving on.

6. **Rewrite `config/*.plist` source files and re-install.**

   The plists hardcode absolute `.py` and `.sh` paths (e.g. `config/com.xenon.exit-order-service.plist:17` → `/Users/…/scripts/exit_order_service.py`). The setup installers **`cp` the plist verbatim** — they do not sed the `<string>` values. Required edits:
   - `config/com.xenon.cri-scan.plist:11` — now points to `scripts/services/run_cri_scan.sh` (Phase 1 moved); confirm still resolves.
   - `config/com.xenon.cta-sync.plist:11` — same pattern for `scripts/services/run_cta_sync.sh`.
   - `config/com.xenon.data-refresh.plist:11` — `scripts/services/run_data_refresh.sh`.
   - `config/com.xenon.exit-order-service.plist:17` — **retire**. Replace with `config/com.xenon.monitor-daemon.plist` (already exists). Delete the exit-order plist and drop its installer (`setup_exit_order_service.sh`).
   - `config/com.xenon.ibc-gateway.plist:10` — unrelated to this migration (vendor path); leave alone.
   - Any plist pointing to a `.py` path: rewrite to `.venv/bin/xenon-<entry>`.

   Then re-run each `scripts/services/setup_*_service.sh install` on laptop + VPS. `setup_monitor_daemon.sh install` **also uninstalls the legacy exit-order plist** (see its `install` command); verify that happens cleanly.

7. **Audit internal `subprocess.run([…, "scripts/X.py"])` callers** inside moved modules — these are Python-to-Python subprocess calls the plan's `web/` + FastAPI focus misses. Confirmed callers:
   - `scripts/reports/risk_reversal.py:43-44, 63-64` — `[sys.executable, str(SCRIPT_DIR / "fetch_flow.py"), ticker]` + `fetch_options.py`.
   - `scripts/reports/portfolio_report.py:905` — `subprocess.run(["python3", str(SCRIPT_DIR / "ib_sync.py"), "--sync"], ...)`.
   - `scripts/services/cta_sync_service.py:230` — `subprocess.run(fetch_cmd, ...)` where `fetch_cmd` is built from `python_bin + "scripts/fetch_menthorq_cta.py"`.
   - Audit the remaining `rg "subprocess\.(run|Popen).*scripts/" src/ scripts/` to catch anything new.

   Rewrite each to invoke the venv binary: `[str(VENV_BIN / "xenon-fetch-flow"), ticker]`. Import a shared `VENV_BIN = Path(__file__).resolve().parents[2] / ".venv" / "bin"` helper, or use `sys.prefix` if the caller runs under the venv.

8. **`requirements-api.txt` consumer audit** (spec Step 10): `rg -l "requirements-api\.txt" .`. Update every consumer to `uv sync --frozen --no-dev`.

9. **Tag recovery point**: `git tag phase2-pre-shim-deletion`. Push the tag before the next commit.

10. **Delete all Phase 1 shims** — one commit, explicit file list (do **not** rely on `git checkout … -- scripts/` for rollback — that reverts wrappers too):

    ```bash
    SHIM_FILES=(
      scripts/fetch_apex_data.py scripts/apex_refresh.py
      scripts/fetch_ticker.py scripts/fetch_flow.py scripts/fetch_options.py
      scripts/fetch_oi_changes.py scripts/fetch_analyst_ratings.py scripts/fetch_news.py
      scripts/fetch_menthorq_cta.py scripts/fetch_menthorq_dashboard.py
      scripts/fetch_x_watchlist.py scripts/fetch_x_xai.py
      scripts/ib_execute.py scripts/ib_place_order.py scripts/ib_order_manage.py
      scripts/ib_orders.py scripts/ib_option_chain.py scripts/ib_reconcile.py
      scripts/ib_sync.py scripts/naked_short_audit.py scripts/futu_sync.py
      scripts/portfolio_attribution.py scripts/portfolio_performance.py
      scripts/portfolio_report.py scripts/performance_explainer_report.py
      scripts/scenario_analysis.py scripts/scenario_report.py
      scripts/evaluate.py scripts/kelly.py scripts/risk_reversal.py
      scripts/blotter.py scripts/free_trade_analyzer.py scripts/verify_options_oi.py
      scripts/generate_cta_share.py scripts/generate_regime_share.py
      scripts/generate_vcg_share.py scripts/generate_gex_share.py
      scripts/scanner.py scripts/discover.py scripts/discover_forex_dom.py
      scripts/cri_scan.py scripts/vcg_scan.py scripts/gex_scan.py
      scripts/leap_iv_scanner.py scripts/leap_scanner_uw.py
      scripts/garch_convergence.py scripts/repair_cri_rvol_cache.py
      scripts/trend_scan.py scripts/uw_scan.py scripts/uw_analyze.py
      scripts/ta_cli.py scripts/ta_premarket_prep.py scripts/ta_reseed_massive.py
      scripts/cta_sync_service.py scripts/test_ib_realtime.py
      # scripts/exit_order_service.py — already deleted in Commit 6 (monitor-daemon retirement)
    )
    git rm "${SHIM_FILES[@]}"

    # Shell symlinks at old paths:
    find scripts -maxdepth 1 -type l -delete

    # Commit tracks every file by name; no globbing.
    ```

    The explicit file array avoids zsh/bash brace-expansion fragility and makes rollback surgical (restore only these paths).

11. **Delete `requirements-api.txt`**. Delete the legacy `run_script()` wrapper in `subprocess.py`.

### Verification — before shim deletion (commit 7 gate)

```bash
uv sync --frozen
.venv/bin/pytest tests/ -x

# Entry-point smoke — generate from pyproject.toml rather than hand-maintain:
python3.13 -c "
import tomllib, sys
scripts = tomllib.load(open('pyproject.toml','rb')).get('project',{}).get('scripts',{})
print('\n'.join(scripts.keys()))
" | while read entry; do
  .venv/bin/$entry --help >/dev/null 2>&1 || echo "BROKEN: $entry"
done | tee /tmp/entry-smoke.log
[ ! -s /tmp/entry-smoke.log ] || exit 1

# FastAPI end-to-end via new entry points — REAL routes, not /X/run:
.venv/bin/xenon-api &
API_PID=$!
sleep 3
curl -sf http://localhost:8321/health | grep '"ib_gateway"'
curl -sfX POST http://localhost:8321/trend-scan | grep -v '"error"'    # NOT /trend-scan/run
curl -sfX POST http://localhost:8321/regime/scan | grep -v '"error"'   # cri-scan surfaces here
curl -sfX POST http://localhost:8321/vcg/scan | grep -v '"error"'
curl -sfX POST http://localhost:8321/gex/scan | grep -v '"error"'
kill $API_PID

# Launchd verification — per-service health, not just "loaded":
for label in com.xenon.cri-scan com.xenon.cta-sync com.xenon.data-refresh com.xenon.monitor-daemon; do
  launchctl print "gui/$UID/$label" 2>/dev/null | grep -E "last exit code|state =" || echo "MISSING: $label"
done

# Per-installer status commands (each wrapper exposes a `status` verb):
bash scripts/services/setup_cri_service.sh status
bash scripts/services/setup_cta_sync_service.sh status
bash scripts/services/setup_data_refresh_service.sh status
bash scripts/services/setup_monitor_daemon.sh status

ssh vps 'for label in com.xenon.cri-scan com.xenon.cta-sync com.xenon.data-refresh com.xenon.monitor-daemon; do
  launchctl print "gui/$UID/$label" 2>/dev/null | grep -E "last exit code|state ="
done'

# Web + E2E:
cd web && npm test && npx playwright test

# Runtime caller audit — widened pattern; splits runtime callers from docs.
# Runtime callers MUST be zero:
rg "scripts/(fetch_|ib_|portfolio_|scenario_|generate_|trend_scan|uw_scan|uw_analyze|ta_cli|ta_premarket|ta_reseed|evaluate|kelly|blotter|risk_reversal|free_trade_analyzer|verify_options_oi|scanner|discover|cri_scan|vcg_scan|gex_scan|leap_|garch|repair_cri|naked_short_audit|futu_sync|exit_order_service|cta_sync_service|apex_refresh|test_ib_realtime)\.py" web/ src/ scripts/services/ scripts/benchmarks/ .github/
rg "scripts/ib_realtime_server\.js|scripts/lib/[a-z-]+\.js" web/
# Docs remain allowed to reference old paths for historical accuracy; cleaned in PR 5.
```

### Verification — after shim deletion

```bash
# Runtime-caller audit only (doc references addressed separately in PR 5):
rg "scripts/(fetch_|ib_|portfolio_|scenario_|generate_|trend_scan|uw_scan|uw_analyze|ta_cli|ta_premarket|ta_reseed|evaluate|kelly|blotter|risk_reversal|free_trade_analyzer|verify_options_oi|scanner|discover|cri_scan|vcg_scan|gex_scan|leap_|garch|repair_cri|naked_short_audit|futu_sync|exit_order_service|cta_sync_service|apex_refresh|test_ib_realtime)\.py" web/ src/ scripts/services/ scripts/benchmarks/ .github/
# Expect zero hits. Any hit = missed caller.

rg "scripts/ib_realtime_server\.js" web/        # JS shim references
rg "scripts/lib/[a-z-]+\.js" web/ src/          # JS utility references — scripts/lib/ stays

# Phase 1 smoke tool now obsolete — delete it.
rm scripts/infra/dev/smoke_phase1_shims.sh scripts/smoke_phase1_shims.sh

# Re-run every production path end-to-end:
.venv/bin/xenon-api &
sleep 3
curl -sf http://localhost:8321/health
curl -sfX POST http://localhost:8321/trend-scan            # real route
curl -sfX POST http://localhost:8321/regime/scan           # real route
curl -sf "http://localhost:8321/uw-analyze?ticker=AAPL"
kill %1

cd web && npm test && npx playwright test
```

### Soak — 3 full trading days

- [ ] Trend scan 8:30 AM ET: 3 consecutive weekday-greens (`data/trend_scan.json` timestamp within the window each day).
- [ ] TA prep 6 AM ET: 3 consecutive weekday-greens (check the external scheduler's log — FastAPI does not trigger this).
- [ ] Monitor-daemon logs (`logs/monitor-daemon.log`): no uncaught tracebacks. **Do not grep for `ScriptResult(ok=False)` here — the service uses bespoke logging.**
- [ ] CRI scan service: 30-min intervals all green (`launchctl print gui/$UID/com.xenon.cri-scan` shows `last exit code = 0`).
- [ ] FastAPI log grep for `run_entry_point.*ok=False`: zero hits during soak.
- [ ] No launchd errors in Console.app.
- [ ] Manual `.venv/bin/xenon-ib-reconcile` run each trading day updates `data/reconciliation.json` with fresh timestamp (reconciliation is manual/route-triggered, not startup-triggered).

### Rollback

**Do not use** `git checkout phase2-pre-shim-deletion -- scripts/` — that reverts every file in `scripts/`, including the updated shell wrappers and plist files, silently reintroducing broken state. Use the explicit file list from Commit 10 instead:

```bash
# Single-file restore (the common case):
git show phase2-pre-shim-deletion:scripts/<old>.py > scripts/<old>.py

# Full shim restore (explicit — no wildcards):
git show phase2-pre-shim-deletion -- $(printf 'scripts/%s\n' \
  fetch_apex_data.py apex_refresh.py fetch_ticker.py fetch_flow.py fetch_options.py \
  fetch_oi_changes.py fetch_analyst_ratings.py fetch_news.py fetch_menthorq_cta.py \
  fetch_menthorq_dashboard.py fetch_x_watchlist.py fetch_x_xai.py \
  ib_execute.py ib_place_order.py ib_order_manage.py ib_orders.py ib_option_chain.py \
  ib_reconcile.py ib_sync.py naked_short_audit.py futu_sync.py \
  portfolio_attribution.py portfolio_performance.py portfolio_report.py \
  performance_explainer_report.py scenario_analysis.py scenario_report.py \
  evaluate.py kelly.py risk_reversal.py blotter.py free_trade_analyzer.py \
  verify_options_oi.py generate_cta_share.py generate_regime_share.py \
  generate_vcg_share.py generate_gex_share.py scanner.py discover.py \
  discover_forex_dom.py cri_scan.py vcg_scan.py gex_scan.py leap_iv_scanner.py \
  leap_scanner_uw.py garch_convergence.py repair_cri_rvol_cache.py \
  trend_scan.py uw_scan.py uw_analyze.py ta_cli.py ta_premarket_prep.py \
  ta_reseed_massive.py cta_sync_service.py test_ib_realtime.py) | git apply
```

### Partial launchd-install rollback (VPS cutover fails mid-cycle)

If `setup_*_service.sh install` on VPS succeeds on some services but fails on others:

1. Immediately run `launchctl print gui/$UID/<label>` for every service in the list above; note which are still loaded.
2. For each half-migrated service: `launchctl unload ~/Library/LaunchAgents/<label>.plist`, restore the pre-cutover plist from `git show phase2-pre-shim-deletion:config/<label>.plist`, `launchctl load` the restored plist.
3. Verify with per-installer `status` commands.
4. Abort PR 4; fix root cause before re-attempting.

The tag is stable across later commits. Use it, not `HEAD~N`.

### Gate before PR 5

- [ ] 3-day soak clean.
- [ ] Entry-point smoke exhaustive pass.
- [ ] No launchd errors; both hosts re-installed successfully.
- [ ] `requirements-api.txt` deleted; every referenced consumer migrated.

---

## PR 5 — Tree cleanup + docs sweep

**Goal:** move the test tree, update the affected-test runner, trim `scripts/` to its slim target, and sweep doc references.

### Changes

1. **Move the test tree**: `git mv scripts/tests tests/`. Update `pyproject.toml` `[tool.pytest.ini_options] testpaths = ["tests"]`. Audit `scripts/tests/conftest.py` for `sys.path` assumptions before moving; remove any that duplicate editable-install behavior.

2. **Update the affected-test runner and its test** — both hardcode `scripts/tests`:
   - `scripts/infra/dev/run_pytest_affected.py:27-29, 47-50, 98-100` — change all `scripts/tests` references to `tests/`.
   - `scripts/tests/test_run_pytest_affected.py:6-18` — update expected-output assertions to match the new `tests/` paths.
   - Both live in the same commit as the `git mv` so the runner works immediately after the move.

3. **Do not flatten** `scripts/services/` or `scripts/infra/` further in this PR. The slim target layout already uses those subdirs, and flattening would require another pass at launchd plists. Defer to a future cleanup PR if desired.

4. **Remove the Phase 1 smoke tool**: `rm scripts/infra/dev/smoke_phase1_shims.sh scripts/smoke_phase1_shims.sh`.

5. **Docs sweep** (split from PR 4 — PR 4 verified only runtime callers; PR 5 cleans the narrative docs):
   - `scripts/CLAUDE.md` command table — replace every `python3.13 scripts/X.py` with `.venv/bin/xenon-X` (or delete if the command no longer exists).
   - Root `CLAUDE.md`, `README.md`, `web/README.md` — update bootstrap instructions to `uv sync` and command examples to `.venv/bin/xenon-*`.
   - `docs/workflows/implement.md`, `docs/trading/strategies.md`, `docs/runbooks/apex-r2-cutover.md` — update inline invocation examples. Historical references in CHANGELOG are **not** rewritten (they accurately describe what shipped at the time).
   - `docs/superpowers/plans/2026-04-17-apex-r2-etl-tribunal-followups.md` — the `scripts.apex_refresh` module references (monkeypatch targets) become `xenon.fetchers.fetch_apex_data`. Update only if those tasks haven't been completed; otherwise leave as historical.
   - Final grep: `rg "scripts/[a-z_]+\.py" docs/ README.md` — eyeball every hit, keep CHANGELOG references, update everything else.

### Verification

```bash
uv sync --frozen
.venv/bin/pytest tests/ -x                        # tests now discovered from root
.venv/bin/python scripts/infra/dev/run_pytest_affected.py  # affected runner resolves new path
cd web && npm test && npx playwright test

# Directory shape check:
ls scripts/ | wc -l                               # expected: ~15-22
find scripts -name "*.py" | grep -v infra/dev | wc -l   # expected: 0 (infra/dev holds non-CLI helpers)
find scripts -maxdepth 1 -name "*.sh"             # bash + service wrappers only

# Final doc grep — CHANGELOG + specs/plans entries allowed; everything else should be updated:
rg "scripts/[a-z_]+\.py" docs/ README.md \
  | grep -vE "CHANGELOG|docs/superpowers/(specs|plans)/2026-" \
  | wc -l                                         # expected: 0 (or flag each hit)
```

### Gate

- [ ] All tests green from root `tests/`.
- [ ] No `scripts/<*>.py` CLI files left (non-CLI dev helpers are fine in `scripts/infra/dev/`).
- [ ] README + CLAUDE.md bootstrap instructions tested from fresh clone.

---

## Success Criteria — rolled up

- `uv sync --frozen` from a fresh clone produces a working environment in <30 seconds.
- `.venv/bin/xenon-<name>` resolves for every production CLI.
- Zero references to retired `scripts/<file>.py` paths anywhere in `web/`, `docs/`, `src/`, `scripts/`.
- `pyproject.toml` is the single source of truth for dependencies and entry points.
- No `sys.path.insert` anywhere in `src/xenon/`.
- 8:30 AM ET trend scan + 6 AM ET TA prep + CRI scan 30-min intervals green for 5 consecutive weekdays post-merge.
- Web test suite + Playwright E2E suite green.
- `requirements-api.txt` deleted.

---

## Recovery Anchors

| Point             | Tag / Commit                                 | Use when                                                            |
| ----------------- | -------------------------------------------- | ------------------------------------------------------------------- |
| Pre-PR-0          | `master@<sha>`                               | Catastrophic regression in apex_refresh bucketing. Revert PR 0.     |
| Post-PR-1         | `master@<sha>`                               | Package scaffolding broken. Revert PR 1; `uv` not yet load-bearing. |
| Pre-shim-deletion | `phase2-pre-shim-deletion` (created in PR 4) | Missing-caller surfaced post-deletion. Restore shims from tag.      |
| Post-PR-4         | `master@<sha>`                               | Roll back to shim-less state if PR 5 test-move breaks discovery.    |
