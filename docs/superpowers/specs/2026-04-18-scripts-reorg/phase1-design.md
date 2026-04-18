# Phase 1 — `scripts/` Reorganization (Zero-Break File Moves)

**Date:** 2026-04-18
**Scope:** Move 58 loose top-level files into 8 verb-first buckets. Consolidate 4 paired `*_lib/` directories with their CLIs. Preserve all existing invocation paths via Python re-export shims and shell symlinks.
**Non-goals:** Rename `scripts/` to `src/`, adopt `uv`, declare `pyproject.toml` entry points, update external callers. All deferred to Phase 2 (`phase2-design.md`).

---

## Problem

`scripts/` contains **58 loose `.py`/`.sh`/`.js` files at the top level** plus 17 subdirectories. A `ls scripts/` returns ~75 entries with no semantic grouping. Four CLI/library pairs (`trend_scan.py` + `trend_scan_lib/`, `uw_scan.py`+`uw_analyze.py` + `uw_scan_lib/`, `ta_cli.py`+`ta_premarket_prep.py`+`ta_reseed_massive.py` + `ta_lib/`, `scanner_lib/`) are split across sibling directories as visible seams.

The goal: make the top level navigable by verb, consolidate paired dirs, without breaking any working invocation path (cron/launchd jobs, shell scripts, FastAPI `run_script()` calls, web frontend `subprocess.run`, sibling Python imports).

## Hard Constraints

1. **Zero runtime behavior change** at any invocation boundary reachable today. Every CLI reachable at `scripts/foo.py` remains reachable at `scripts/foo.py` for Phase 1.
2. **Atomic move + import-rewrite commits.** When a file moves, all sibling `from foo import bar` and test imports rewrite in the same commit. No cross-commit broken states on main.
3. **`scripts/` name preserved.** The rename to `src/` is Phase 2.
4. **Existing subdirs mostly untouched.** `api/`, `clients/`, `utils/`, `monitor_daemon/`, `analysis/`, `trade_blotter/`, `tests/`, `benchmarks/`, `config/`, `data/`, `lib/`, `api_status/` — no structural changes. Only `scanner_lib/`, `trend_scan_lib/`, `uw_scan_lib/`, `ta_lib/` move as part of paired consolidation.

---

## Final Taxonomy

```
scripts/
  ├── fetchers/          # NEW: data acquisition (pull-only, no broker writes)
  ├── scanners/          # NEW: multi-ticker scan/rank CLIs + their libs
  │   ├── _shared/       # ← was scanner_lib/
  │   ├── trend/         # ← was trend_scan.py + trend_scan_lib/
  │   └── uw/            # ← was uw_scan.py + uw_analyze.py + uw_scan_lib/
  ├── execution/         # NEW: broker-touching (IB + Futu + naked short audit)
  ├── reports/           # NEW: portfolio/performance/scenario/eval artifacts
  ├── shares/            # NEW: generate_*_share card generators
  ├── services/          # NEW: always-on daemons + installers + cron wrappers
  ├── ta/                # NEW: TA-Lib CLIs + consolidated ta_lib contents
  ├── infra/             # NEW: dev env + docker + IB realtime Node + dev tooling
  │   ├── ib_realtime/   # Node.js IB realtime pipeline (self-contained)
  │   └── dev/           # one-off Python dev tools
  │
  ├── analysis/          # UNCHANGED
  ├── api/               # UNCHANGED
  ├── api_status/        # UNCHANGED
  ├── benchmarks/        # UNCHANGED
  ├── clients/           # UNCHANGED
  ├── config/            # UNCHANGED
  ├── data/              # UNCHANGED
  ├── lib/               # UNCHANGED
  ├── monitor_daemon/    # UNCHANGED
  ├── tests/             # CONTENTS REORGANIZED to mirror source buckets (see below)
  ├── trade_blotter/     # UNCHANGED
  ├── utils/             # UNCHANGED
  │
  ├── CLAUDE.md
  └── requirements-api.txt
```

### Test Directory Mirror

`scripts/tests/` subdirs rename in lockstep with source consolidations (in the same commit):

| Old test dir                         | New test dir                         |
| ------------------------------------ | ------------------------------------ |
| `scripts/tests/test_trend_scan_lib/` | `scripts/tests/test_scanners_trend/` |
| `scripts/tests/test_ta_lib/`         | `scripts/tests/test_ta/`             |

Individual `test_*.py` files at the top level of `scripts/tests/` stay flat — they already track one module each, and flat is fine at this depth. Only the nested test subdirs mirror the source consolidation.

---

## Per-File Placement Manifest

### `fetchers/` (10 files)

| Old path                              | New path                                       |
| ------------------------------------- | ---------------------------------------------- |
| `scripts/fetch_ticker.py`             | `scripts/fetchers/fetch_ticker.py`             |
| `scripts/fetch_flow.py`               | `scripts/fetchers/fetch_flow.py`               |
| `scripts/fetch_options.py`            | `scripts/fetchers/fetch_options.py`            |
| `scripts/fetch_oi_changes.py`         | `scripts/fetchers/fetch_oi_changes.py`         |
| `scripts/fetch_analyst_ratings.py`    | `scripts/fetchers/fetch_analyst_ratings.py`    |
| `scripts/fetch_news.py`               | `scripts/fetchers/fetch_news.py`               |
| `scripts/fetch_menthorq_cta.py`       | `scripts/fetchers/fetch_menthorq_cta.py`       |
| `scripts/fetch_menthorq_dashboard.py` | `scripts/fetchers/fetch_menthorq_dashboard.py` |
| `scripts/fetch_x_watchlist.py`        | `scripts/fetchers/fetch_x_watchlist.py`        |
| `scripts/fetch_x_xai.py`              | `scripts/fetchers/fetch_x_xai.py`              |

### `scanners/` (~18 files + 4 paired-lib collapses)

**`scanners/_shared/`** ← rename of `scripts/scanner_lib/*`. Every `from scanner_lib.X` rewrites to `from scanners._shared.X` in the same commit. No shim — internal-only, never invoked by path.

**`scanners/trend/`** ← paired consolidation:

- `scripts/trend_scan.py` → `scripts/scanners/trend/cli.py` (expose `main()`)
- `scripts/trend_scan_lib/*` → `scripts/scanners/trend/*` (flat, sibling of `cli.py`)

**`scanners/uw/`** ← paired consolidation:

- `scripts/uw_scan.py` → `scripts/scanners/uw/scan.py` (expose `main()`)
- `scripts/uw_analyze.py` → `scripts/scanners/uw/analyze.py` (expose `main()`)
- `scripts/uw_scan_lib/{signals,context,confluence.py,ranking.py,universe.py}` → `scripts/scanners/uw/{same}`

**Direct moves into `scanners/`:**

| Old                                | New                                         |
| ---------------------------------- | ------------------------------------------- |
| `scripts/cri_scan.py`              | `scripts/scanners/cri.py`                   |
| `scripts/vcg_scan.py`              | `scripts/scanners/vcg.py`                   |
| `scripts/gex_scan.py`              | `scripts/scanners/gex.py`                   |
| `scripts/leap_iv_scanner.py`       | `scripts/scanners/leap_iv.py`               |
| `scripts/leap_scanner_uw.py`       | `scripts/scanners/leap_uw.py`               |
| `scripts/garch_convergence.py`     | `scripts/scanners/garch.py`                 |
| `scripts/scanner.py`               | `scripts/scanners/scanner.py`               |
| `scripts/discover.py`              | `scripts/scanners/discover.py`              |
| `scripts/discover_forex_dom.py`    | `scripts/scanners/discover_forex.py`        |
| `scripts/repair_cri_rvol_cache.py` | `scripts/scanners/repair_cri_rvol_cache.py` |

### `execution/` (9 files)

| Old                            | New                                      |
| ------------------------------ | ---------------------------------------- |
| `scripts/ib_execute.py`        | `scripts/execution/ib_execute.py`        |
| `scripts/ib_place_order.py`    | `scripts/execution/ib_place_order.py`    |
| `scripts/ib_order_manage.py`   | `scripts/execution/ib_order_manage.py`   |
| `scripts/ib_orders.py`         | `scripts/execution/ib_orders.py`         |
| `scripts/ib_option_chain.py`   | `scripts/execution/ib_option_chain.py`   |
| `scripts/ib_reconcile.py`      | `scripts/execution/ib_reconcile.py`      |
| `scripts/ib_sync.py`           | `scripts/execution/ib_sync.py`           |
| `scripts/naked_short_audit.py` | `scripts/execution/naked_short_audit.py` |
| `scripts/futu_sync.py`         | `scripts/execution/futu_sync.py`         |

### `reports/` (12 files)

| Old                                       | New                                               |
| ----------------------------------------- | ------------------------------------------------- |
| `scripts/portfolio_attribution.py`        | `scripts/reports/portfolio_attribution.py`        |
| `scripts/portfolio_performance.py`        | `scripts/reports/portfolio_performance.py`        |
| `scripts/portfolio_report.py`             | `scripts/reports/portfolio_report.py`             |
| `scripts/performance_explainer_report.py` | `scripts/reports/performance_explainer_report.py` |
| `scripts/scenario_analysis.py`            | `scripts/reports/scenario_analysis.py`            |
| `scripts/scenario_report.py`              | `scripts/reports/scenario_report.py`              |
| `scripts/evaluate.py`                     | `scripts/reports/evaluate.py`                     |
| `scripts/kelly.py`                        | `scripts/reports/kelly.py`                        |
| `scripts/risk_reversal.py`                | `scripts/reports/risk_reversal.py`                |
| `scripts/blotter.py`                      | `scripts/reports/blotter.py`                      |
| `scripts/free_trade_analyzer.py`          | `scripts/reports/free_trade_analyzer.py`          |
| `scripts/verify_options_oi.py`            | `scripts/reports/verify_options_oi.py`            |

### `shares/` (4 files)

| Old                                | New                                       |
| ---------------------------------- | ----------------------------------------- |
| `scripts/generate_cta_share.py`    | `scripts/shares/generate_cta_share.py`    |
| `scripts/generate_regime_share.py` | `scripts/shares/generate_regime_share.py` |
| `scripts/generate_vcg_share.py`    | `scripts/shares/generate_vcg_share.py`    |
| `scripts/generate_gex_share.py`    | `scripts/shares/generate_gex_share.py`    |

### `services/` (10 files)

| Old                                     | New                                              |
| --------------------------------------- | ------------------------------------------------ |
| `scripts/exit_order_service.py`         | `scripts/services/exit_order_service.py`         |
| `scripts/cta_sync_service.py`           | `scripts/services/cta_sync_service.py`           |
| `scripts/setup_exit_order_service.sh`   | `scripts/services/setup_exit_order_service.sh`   |
| `scripts/setup_cta_sync_service.sh`     | `scripts/services/setup_cta_sync_service.sh`     |
| `scripts/setup_monitor_daemon.sh`       | `scripts/services/setup_monitor_daemon.sh`       |
| `scripts/setup_cri_service.sh`          | `scripts/services/setup_cri_service.sh`          |
| `scripts/setup_data_refresh_service.sh` | `scripts/services/setup_data_refresh_service.sh` |
| `scripts/run_cta_sync.sh`               | `scripts/services/run_cta_sync.sh`               |
| `scripts/run_cri_scan.sh`               | `scripts/services/run_cri_scan.sh`               |
| `scripts/run_data_refresh.sh`           | `scripts/services/run_data_refresh.sh`           |

### `ta/` (paired collapse)

- `scripts/ta_cli.py` → `scripts/ta/cli.py` (expose `main()`)
- `scripts/ta_premarket_prep.py` → `scripts/ta/premarket_prep.py` (expose `main()`)
- `scripts/ta_reseed_massive.py` → `scripts/ta/reseed_massive.py` (expose `main()`)
- `scripts/ta_lib/{bars,indicators,store,service,__init__}.py` → `scripts/ta/{same}`

### `infra/` (13 files, 3 tiers)

**`infra/` top-level (6 files):**

| Old                             | New                                   |
| ------------------------------- | ------------------------------------- |
| `scripts/cloud.sh`              | `scripts/infra/cloud.sh`              |
| `scripts/local.sh`              | `scripts/infra/local.sh`              |
| `scripts/docker_ib_gateway.sh`  | `scripts/infra/docker_ib_gateway.sh`  |
| `scripts/ibc_remote_control.sh` | `scripts/infra/ibc_remote_control.sh` |
| `scripts/setup_ibc.sh`          | `scripts/infra/setup_ibc.sh`          |
| `scripts/cleanup-dead-code.sh`  | `scripts/infra/cleanup-dead-code.sh`  |

**`infra/ib_realtime/` (Node subsystem — 4 files):**

| Old                               | New                                                 |
| --------------------------------- | --------------------------------------------------- |
| `scripts/ib_realtime_server.js`   | `scripts/infra/ib_realtime/ib_realtime_server.js`   |
| `scripts/ib_connection_status.js` | `scripts/infra/ib_realtime/ib_connection_status.js` |
| `scripts/ib_tick_handler.js`      | `scripts/infra/ib_realtime/ib_tick_handler.js`      |
| `scripts/test_ib_realtime.py`     | `scripts/infra/ib_realtime/test_ib_realtime.py`     |

**`infra/dev/` (Python dev tools — 4 files):**

| Old                              | New                                        |
| -------------------------------- | ------------------------------------------ |
| `scripts/run_pytest_affected.py` | `scripts/infra/dev/run_pytest_affected.py` |
| `scripts/site_seo_audit.py`      | `scripts/infra/dev/site_seo_audit.py`      |
| `scripts/context_constructor.py` | `scripts/infra/dev/context_constructor.py` |
| `scripts/batched_relay.py`       | `scripts/infra/dev/batched_relay.py`       |

---

## Shim Strategy

### Python Shims (for `.py` files invoked by subprocess, shell, or `run_script()`)

**Template (`scripts/<old_name>.py`):**

```python
#!/usr/bin/env python3.13
"""Compatibility shim. Real home: scripts/<bucket>/<new_name>.py.

Phase 1 preserves old invocation paths. Removed in Phase 2."""
from <bucket>.<new_name> import *  # noqa: F401,F403 — re-export for sibling imports
from <bucket>.<new_name> import main  # explicit CLI entry

if __name__ == "__main__":
    main()
```

**Requirements:**

1. **Target module must expose `main()`.** If the old file has inline logic inside `if __name__ == "__main__":`, extract into `def main(): ...` in a **prep commit** before the move commit. (Addresses review ISSUE-4.)
2. **`scripts/` must be on `sys.path[0]`** when shim is invoked. True by default because Python prepends the invoked script's directory (`scripts/`) to `sys.path`. New bucket dirs (`fetchers/`, `scanners/`, etc.) then become importable as top-level packages.
3. **For modules with `_private` names used externally** — add to an explicit re-export list instead of relying on `import *`. Pre-move grep: `rg "from <module> import _" scripts/ web/`.

**Who gets a shim:** every moved `.py` file listed in the External Callsite Audit below. Files with no external callers and no bare-name sibling imports (rare — most files are imported somewhere) can move clean.

### Shell Symlinks (for `.sh` files invoked by launchd/cron/sibling shell scripts)

**Pattern:** `ln -s services/run_cri_scan.sh scripts/run_cri_scan.sh`

Symlinks beat shell-exec wrappers for `.sh` files because (a) zero maintenance surface — no drift between wrapper and real script, (b) `bash` and launchd resolve symlinks transparently, (c) `$(dirname "$0")` inside the target script resolves to the **real** directory on macOS/Linux (`readlink`-safe on both), so path-relative logic inside the real script still works.

**Verification before merge:** test that `$0`, `$BASH_SOURCE[0]`, and `$(cd "$(dirname "$0")" && pwd)` all behave correctly when invoked via symlink on the actual dev machine.

**Files receiving symlinks** (every `.sh` that moves to `services/` or `infra/` and has an external caller):

| Symlink at old path                     | Target                                   |
| --------------------------------------- | ---------------------------------------- |
| `scripts/run_data_refresh.sh`           | `services/run_data_refresh.sh`           |
| `scripts/run_cri_scan.sh`               | `services/run_cri_scan.sh`               |
| `scripts/run_cta_sync.sh`               | `services/run_cta_sync.sh`               |
| `scripts/setup_cri_service.sh`          | `services/setup_cri_service.sh`          |
| `scripts/setup_cta_sync_service.sh`     | `services/setup_cta_sync_service.sh`     |
| `scripts/setup_exit_order_service.sh`   | `services/setup_exit_order_service.sh`   |
| `scripts/setup_monitor_daemon.sh`       | `services/setup_monitor_daemon.sh`       |
| `scripts/setup_data_refresh_service.sh` | `services/setup_data_refresh_service.sh` |
| `scripts/cloud.sh`                      | `infra/cloud.sh`                         |
| `scripts/local.sh`                      | `infra/local.sh`                         |
| `scripts/docker_ib_gateway.sh`          | `infra/docker_ib_gateway.sh`             |
| `scripts/ibc_remote_control.sh`         | `infra/ibc_remote_control.sh`            |
| `scripts/setup_ibc.sh`                  | `infra/setup_ibc.sh`                     |
| `scripts/cleanup-dead-code.sh`          | `infra/cleanup-dead-code.sh`             |

---

## External Callsite Audit (Files Requiring Shims)

Confirmed from repo-wide grep:

**Invoked by `scripts/api/server.py` via `run_script()`** (resolves to `SCRIPTS_DIR/<name>`):

- `ta_premarket_prep.py`, `trend_scan.py`, `scanner.py`, `discover.py`, `cri_scan.py`, `vcg_scan.py`, `gex_scan.py`, `portfolio_attribution.py`, `portfolio_performance.py`, `ib_option_chain.py`, `generate_cta_share.py`, `generate_regime_share.py`, `generate_vcg_share.py`, `generate_gex_share.py`.

**Invoked by `web/` via `subprocess.run(["scripts/<file>", ...])`** (see `web/app/api/pi/route.ts`, `web/app/api/ticker/ratings/route.ts`, `web/app/api/menthorq/[command]/image/route.tsx`, `web/tests/integration.test.ts`):

- `evaluate.py`, `ib_sync.py`, `leap_scanner_uw.py`, `fetch_analyst_ratings.py`, `fetch_ticker.py`, `fetch_flow.py`, `fetch_menthorq_dashboard.py`, `scanner.py`, `discover.py`, `kelly.py`, `ib_order_manage.py`, `test_ib_realtime.py`, `generate_gex_share.py`, `generate_regime_share.py`.

**Invoked by shell scripts:**

- `scripts/run_data_refresh.sh` → `scanner.py`, `discover.py`, `cri_scan.py`, `repair_cri_rvol_cache.py`.
- `scripts/run_cta_sync.sh` → `cta_sync_service.py`.
- `scripts/benchmarks/autoresearch.sh:12` → `scanner.py`.

**Imported by bare name from sibling files / tests** (107 sites across 50 files, grep-audited): every moved `.py` file needs its sibling-import paths rewritten to `from <bucket>.<name>` in the move commit, OR the shim keeps the old bare-name working.

---

## Per-File Pre-Commit Checklist

**Before every move commit**, run this checklist on each file being moved. Addresses review ISSUE-1, ISSUE-2, ISSUE-3, ISSUE-4.

### 1. `sys.path.insert(0, ...)` audit

Files known to self-register their own directory on `sys.path`:

| File                            | Pattern                                                                          | Edit required after move                                        |
| ------------------------------- | -------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| `risk_reversal.py:29`           | `sys.path.insert(0, str(SCRIPT_DIR))` where `SCRIPT_DIR = Path(__file__).parent` | Change `SCRIPT_DIR` to `Path(__file__).resolve().parent.parent` |
| `portfolio_report.py:46,62,778` | 3 separate `sys.path.insert(0, str(SCRIPT_DIR))`                                 | All three: `.parent` → `.parent.parent`                         |
| `portfolio_performance.py:36`   | `sys.path.insert(0, str(Path(__file__).resolve().parent))`                       | Add `.parent`                                                   |
| `evaluate.py:43`                | `sys.path.insert(0, str(_SCRIPT_DIR))`                                           | `_SCRIPT_DIR` → one level up                                    |
| `gex_scan.py:35`                | same                                                                             | same                                                            |
| `garch_convergence.py:47`       | same                                                                             | same                                                            |
| `fetch_news.py:22`              | same                                                                             | same                                                            |
| `ib_order_manage.py:20`         | `sys.path.insert(0, str(Path(__file__).parent))`                                 | `.parent` → `.parent.parent`                                    |
| `naked_short_audit.py:20`       | same                                                                             | same                                                            |
| `ib_reconcile.py:26`            | same                                                                             | same                                                            |
| `verify_options_oi.py:25`       | same                                                                             | same                                                            |
| `ib_place_order.py:25,26`       | two inserts: `PROJECT_ROOT` + `Path(__file__).parent`                            | `Path(__file__).parent` → `.parent.parent`                      |

### 2. `blotter.py` hardcoded sibling path (ISSUE-2)

`scripts/blotter.py:19`:

```python
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trade_blotter'))
```

After move to `scripts/reports/blotter.py`, this path becomes `scripts/reports/trade_blotter/` which doesn't exist.

**Edit required:**

```python
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'trade_blotter'))
```

### 3. `ta_cli.py` / `ta_premarket_prep.py` project-root walk (ISSUE-3)

Both compute `_project_root = Path(__file__).resolve().parent.parent` — today resolves to `/project` from `scripts/ta_cli.py`. After move to `scripts/ta/cli.py` → `.parent.parent` resolves to `scripts/`, which is wrong.

**Edit required:** `.parent.parent` → `.parent.parent.parent` in both files (and `ta_reseed_massive.py` if it has the same pattern — verify at move time).

### 4. `main()` extraction (ISSUE-4)

For every `.py` file being moved with a `if __name__ == "__main__":` block containing **inline logic** (not just `main()` or `asyncio.run(main())`), extract the inline logic into a `def main():` function in a **prep commit** separate from the move. Verify with:

```bash
rg -l '^if __name__ == .__main__.:' scripts/*.py | while read f; do
  if ! rg -q '^def main\(' "$f"; then echo "NEEDS EXTRACTION: $f"; fi
done
```

### 5. Dynamic-import grep sweep (ISSUE-10)

Per bucket move, grep for dynamic references to every moved module name:

```bash
# For a module named foo being moved to bucket/foo:
rg -E "(import_module|__import__|run_module|run_path)\s*\(['\"]foo" scripts/ web/
rg -E "['\"]foo\.(py|)?['\"]" scripts/ web/ | grep -v 'scripts/foo\.py'  # bare references
```

### 6. Private-name sweep (ISSUE-11)

Before relying on `from X import *` in the shim:

```bash
rg "from <moved_module> import _" scripts/ web/
```

If any hits, add `_name` to the shim's explicit re-export list or define `__all__` in the target.

---

## Pre-Phase-1 PR — `pyproject.toml` + `__init__.py` Scaffolding

This is its own small PR landed **before** any bucket move. Two reasons:

- Editor/CI ruff runs in subsequent PRs use the new `known-first-party` list, avoiding diff churn from import re-sorting.
- Pre-creating `__init__.py` files in the new (empty) bucket dirs locks in regular-package semantics from day one — avoids the implicit-namespace-package ambiguity class.

### Step 1 — create empty bucket dirs with `__init__.py`

```bash
for d in fetchers scanners scanners/_shared scanners/trend scanners/uw \
         execution reports shares services ta infra infra/ib_realtime infra/dev; do
  mkdir -p "scripts/$d"
  touch "scripts/$d/__init__.py"
done
```

13 empty files. Pure scaffolding — no code moves yet.

### Step 2 — `pyproject.toml` update (ISSUE-6)

Current content includes:

```toml
[tool.ruff.lint.isort]
known-first-party = ["utils", "trade_blotter"]
```

**Required update** (landed in the first bucket PR — `shares/`):

```toml
[tool.ruff.lint.isort]
known-first-party = [
    "utils",
    "trade_blotter",
    "fetchers",
    "scanners",
    "execution",
    "reports",
    "shares",
    "services",
    "ta",
    "infra",
    "clients",  # also add while we're editing
    "analysis",
    "scanner_lib",     # kept during transition — removed in Phase 2
    "trend_scan_lib",  # kept during transition
    "uw_scan_lib",     # kept during transition
    "ta_lib",          # kept during transition
]
```

Also verify no `[tool.pytest.ini_options]` `testpaths` setting hardcodes paths that need updating.

### Step 3 — pytest discovery exclusion (P1-R-2)

`scripts/infra/ib_realtime/test_ib_realtime.py` is a manual smoke harness, not a unit test, but its filename matches pytest's auto-discovery glob (`test_*.py`). Without exclusion, pytest will try to collect it during the regular suite run.

Add to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "strict"
norecursedirs = [
    "scripts/infra/ib_realtime",   # manual smoke harness, not a unit test
    ".venv", "node_modules", "data", "logs", "tmp",
]
markers = [...]
```

Renaming the file (drop `test_` prefix) is the alternative, but `web/package.json:20` and `web/README.md:291-293` reference `scripts/test_ib_realtime.py` by name — renaming would break them. The pytest-only exclusion is zero-touch externally.

---

## Internal Import Rewrite

For each move, rewrite bare-name imports:

```bash
# Example for fetch_flow move:
rg -l "from fetch_flow import\b|import fetch_flow\b" scripts/ web/  # find call sites
# Use sed or editor to rewrite to: from fetchers.fetch_flow import ...
```

Verify the rewrite is complete:

```bash
rg "from fetch_flow import\b|import fetch_flow\b" scripts/ web/   # should return 0 hits
```

Same pattern for every moved module. Tests, siblings, `api/` code, and `web/` TypeScript stubs (rare, but check) all get rewritten in the move commit.

---

## Verification Plan

### Baseline (green on main before any move)

```bash
python3.13 scripts/run_pytest_affected.py
cd web && npm test && npx playwright test
python3.13 -m pytest scripts/tests/ -x --tb=short
```

Plus the smoke list:

```
python3.13 scripts/fetch_flow.py --help
python3.13 scripts/fetch_ticker.py          # exit 2
python3.13 scripts/fetch_analyst_ratings.py --help
python3.13 scripts/fetch_menthorq_dashboard.py --help
python3.13 scripts/discover.py --help
python3.13 scripts/scanner.py --help
python3.13 scripts/kelly.py --help
python3.13 scripts/evaluate.py --help
python3.13 scripts/ib_order_manage.py --help
python3.13 scripts/ib_sync.py --help
python3.13 scripts/ib_option_chain.py --help
python3.13 scripts/leap_scanner_uw.py --help
python3.13 scripts/trend_scan.py --help
python3.13 scripts/uw_scan.py --help
python3.13 scripts/uw_analyze.py --help
python3.13 scripts/ta_cli.py --help
python3.13 scripts/ta_premarket_prep.py --help
python3.13 scripts/cri_scan.py --help
python3.13 scripts/vcg_scan.py --help
python3.13 scripts/gex_scan.py --help
python3.13 scripts/generate_gex_share.py --help
python3.13 scripts/generate_regime_share.py --help
python3.13 scripts/generate_cta_share.py --help
python3.13 scripts/generate_vcg_share.py --help
python3.13 scripts/test_ib_realtime.py --help
bash scripts/run_cri_scan.sh --help 2>&1 | head -3    # via shim/symlink
bash scripts/run_cta_sync.sh --help 2>&1 | head -3
```

### Per-Bucket Commit Gates

After each bucket commit:

1. `python3.13 -m pytest scripts/tests/ -x` — green.
2. All `--help` smokes above — still green.
3. `rg "from <old_module_name>\b" scripts/ web/` — zero results for every module moved in this bucket.
4. Start FastAPI: `bash scripts/infra/local.sh` (or `scripts/local.sh` via symlink). `curl localhost:8321/health` expects `ib_gateway.port_listening: true`.
5. One full scan through FastAPI: `curl localhost:8321/trend-scan/run`.
6. Integration suite: `cd web && npx playwright test tests/integration.test.ts`.
7. The per-file pre-commit checklist items 1–6 verified for each file in the bucket.

### Soak Requirement

Wait at least one full trading day between bucket PRs. If the 8:30 AM ET trend scan runs successfully, the scheduler path is verified end-to-end.

---

## Sequencing (Lowest-Risk First)

Each step is an atomic PR. Main stays green throughout.

0. **Pre-Phase-1 scaffolding PR** — empty `__init__.py` files in 13 new bucket dirs + `pyproject.toml` `known-first-party` update + `norecursedirs` for `infra/ib_realtime/`. Zero functional change.
1. **`shares/`** — 4 files. Narrow blast radius. Warm-up bucket.
2. **`reports/`** — 12 files. Mostly self-contained. Includes the `blotter.py` hardcoded-sibling fix (ISSUE-2), `risk_reversal.py` / `portfolio_report.py` `sys.path` fixes (ISSUE-1).
3. **`fetchers/`** — 10 files. Web subprocess callers rely on shims.
4. **`execution/`** — 9 files. Broker-critical; run full integration smoke.
5. **`infra/`** — pre-check launchd plists on all deploy hosts (`ssh vps launchctl list | grep xenon`, `ls ~/Library/LaunchAgents/`). If any plist references moving `.sh` paths, hold until coordinated plist re-install. Symlinks at old paths cover runtime callers.
6. **`services/`** — same launchd pre-check. `run_data_refresh.sh:185` calls `scripts/repair_cri_rvol_cache.py` (still at old path via shim until Step 8a — cross-bucket dependency noted, shim covers).
7. **`ta/`** — paired consolidation. `ta_premarket_prep.py` runs at 6 AM ET via scheduler; shim at `scripts/ta_premarket_prep.py` keeps the `run_script("ta_premarket_prep.py")` call working. Apply `_project_root` fix (ISSUE-3).
8. **`scanners/`** — split into two PRs:
   - **8a:** `_shared/` rename + direct moves (cri, vcg, gex, leap\_\*, garch, scanner, discover, discover_forex, repair_cri).
   - **8b:** `trend/` + `uw/` paired consolidations. `trend_scan.py` runs at 8:30 AM ET; shim covers `run_script("trend_scan.py", ...)`.

**Cross-bucket shim dependencies (must remain alive until Phase 2):**

- `scripts/repair_cri_rvol_cache.py` shim → alive through Step 6–8a window (called by `services/run_data_refresh.sh`).
- `scripts/scanner.py`, `scripts/discover.py`, `scripts/cri_scan.py` shims → alive through Step 6–8a window.
- All shims retained through Phase 1; bulk deletion happens in Phase 2.

---

## Launchd / Cron Audit (Pre-Step-5/6 Blocker)

Before Step 5 (`infra/`) and Step 6 (`services/`), run on every deployment host:

```bash
# On local machine:
ls ~/Library/LaunchAgents/ | grep -iE "xenon|cri|cta|exit_order|trend|ta_prem|monitor|data_refresh"
launchctl list | grep -iE "xenon|cri|cta|exit_order|data_refresh"

# On VPS (via ssh):
ssh vps 'launchctl list | grep -iE "xenon|cri|cta|exit_order|data_refresh"'
ssh vps 'systemctl list-units --type=service --all | grep -iE "xenon|cri|cta"'
ssh vps 'crontab -l'

# Anywhere a plist is found, inspect it:
cat ~/Library/LaunchAgents/<name>.plist | grep -E "scripts/(run|setup)_"
```

If any plist hardcodes a moving `.sh` path, the post-merge action is:

1. Re-run the moved `./scripts/services/setup_X_service.sh install` → rewrites the plist at the new path.
2. `launchctl unload ~/Library/LaunchAgents/<old>.plist && launchctl load ~/Library/LaunchAgents/<new>.plist`.
3. Verify with `launchctl list | grep xenon`.

Local dev machine audit at spec-write time: `~/Library/LaunchAgents/` is empty for xenon services. VPS audit pending.

---

## Rollback

Each bucket PR is an atomic commit. If a shim failure surfaces post-merge:

1. **Git revert the bucket commit.** `git revert <sha> -m 1` for the merge commit.
2. **Verify `scripts/<old_name>.py` files are back** at old paths with original content.
3. **If any `setup_*_service.sh install` was run post-merge** (to update a launchd plist to the new path), re-run the OLD installer to restore the plist. Installed plists can diverge from repo state, so this is a manual runbook step, not automatic from `git revert`.
4. **Reload affected launchd jobs** if applicable: `launchctl unload` + `launchctl load`.
5. **Verify next scheduled run succeeds** — e.g., wait for the next 8:30 AM ET window, inspect `data/trend_scan.json` timestamp, and `logs/cri-scan.err.log`.

---

## Success Criteria

- `ls scripts/` shows ~22 entries instead of ~75.
- Every command in `scripts/CLAUDE.md`'s command table still runs from its old path (via shim/symlink).
- `python3.13 -m pytest scripts/tests/` passes.
- `cd web && npm test && npx playwright test` passes.
- `rg "from <old_bucket_file_name> import\b"` returns zero hits for every moved module.
- 8:30 AM ET trend scan runs successfully on first trading day after `scanners/` PR.
- 6 AM ET TA prep runs successfully on first trading day after `ta/` PR.
- No launchd plist errors in Console.app for one week after `services/` PR.
- `pyproject.toml` `known-first-party` list includes all new buckets.
