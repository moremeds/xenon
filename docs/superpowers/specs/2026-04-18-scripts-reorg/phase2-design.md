# Phase 2 — `scripts/` → `src/` with `uv` Packaging

**Date:** 2026-04-18
**Prerequisite:** Phase 1 (`phase1-design.md`) merged and soaked for ≥2 weeks with zero regressions.
**Scope:** Rename the Python package tree from `scripts/` to `src/`, adopt `uv` as the package manager, declare a real installable `xenon` package with `[project.scripts]` entry points. Retire all Phase 1 shims. Shell scripts and thin CLI wrappers stay in a slim `scripts/` dir.

---

## Problem

After Phase 1, `scripts/` is well-organized by verb, but:

1. **The `scripts/` name is semantically wrong.** Half the directory is Python modules imported by other modules, not "scripts" in the executable sense. A fresh reader expects `scripts/` to mean "shell + standalone tools," not "the entire Python codebase."
2. **The import graph depends on `sys.path` tricks.** Every entry point does `sys.path.insert(0, SCRIPTS_DIR)` because `scripts/` is treated as a namespace root by convention, not by package declaration. Bare-name imports (`from fetch_flow import X`) work only when the caller happens to have `scripts/` on `sys.path[0]`.
3. **No dependency lockfile.** `scripts/requirements-api.txt` pins some packages but not transitively, and environment reproduction between the dev laptop and VPS relies on luck.
4. **No canonical command manifest.** The list of CLIs the project exposes is implicit — scattered across `scripts/CLAUDE.md`, `web/lib/commands.ts`, Phase 1's shim files, and `scripts/api/server.py`'s `run_script()` calls.
5. **Phase 1 shim debt.** Every moved file has a shim at the old path. That's ~40 extra files whose only purpose is backward compatibility. Deleting them requires updating every external caller.

## Goals

1. `scripts/` becomes what its name suggests: shell scripts and tiny CLI wrappers only.
2. Python code lives under `src/` as an installable package named `xenon`.
3. `uv` manages dependencies, lockfile, virtualenv, and CLI entry-point generation.
4. CLI tools invoke via generated binaries (`evaluate`, `trend-scan`, `uw-scan`, …), declared once in `pyproject.toml`, resolvable anywhere in the project after `uv sync`.
5. All Phase 1 shims deleted.
6. All external callers (web `subprocess`, shell scripts, launchd plists, CI, docs) updated to the new invocation form in the same PR as the shim removal.

## Non-Goals

- Splitting `xenon` into multiple sub-packages. One package, multiple sub-modules.
- Publishing `xenon` to PyPI. Local editable install only.
- Porting `scripts/api/` internal structure. Module re-homed to `src/xenon/api/` without internal refactoring.
- Touching the `web/` frontend bundling or `bun`/`npm` workflow. Only subprocess call sites update.

---

## System Dependencies (Bootstrap Prerequisites — P2-3)

`uv` manages Python packages, but several depend on **C libraries that must be pre-installed on the host** before `uv sync` succeeds. Building from a clean machine:

| Package                                               | macOS                                 | Linux (Debian/Ubuntu)       | Notes                                             |
| ----------------------------------------------------- | ------------------------------------- | --------------------------- | ------------------------------------------------- |
| TA-Lib (Python)                                       | `brew install ta-lib`                 | `apt install libta-lib-dev` | Compile-time C dep; `uv` builds the wheel locally |
| Python 3.13                                           | `uv python install 3.13` (uv-managed) | same                        | Pinned via `.python-version`                      |
| `playwright` browsers                                 | `uv run playwright install chromium`  | same                        | Post-install step, not at sync time               |
| Node.js (for `web/` and `scripts/infra/ib_realtime/`) | `brew install node`                   | `nvm` / `nodesource`        | Pre-existing dependency; out of Phase 2 scope     |

**Bootstrap order on a fresh machine:**

```bash
brew install ta-lib node                        # macOS — system C libs first
curl -LsSf https://astral.sh/uv/install.sh | sh # uv itself
git clone <repo> && cd xenon
uv python install 3.13                          # ensures matching Python
uv sync                                         # installs xenon editable + deps
uv run playwright install chromium              # browser binaries
```

If TA-Lib C library is missing, `uv sync` fails with a Python compile error, not a clear "missing system dep" message. Bootstrap docs in `README.md` must spell this out.

---

## Target Layout

```
xenon/                         # repo root
  ├── src/
  │   └── xenon/
  │       ├── __init__.py
  │       ├── fetchers/        # was scripts/fetchers/
  │       ├── scanners/        # was scripts/scanners/
  │       │   ├── _shared/
  │       │   ├── trend/
  │       │   └── uw/
  │       ├── execution/       # was scripts/execution/
  │       ├── reports/         # was scripts/reports/
  │       ├── shares/          # was scripts/shares/
  │       ├── services/        # was scripts/services/ (python files only; .sh stay below)
  │       ├── api/             # was scripts/api/
  │       ├── clients/         # was scripts/clients/
  │       ├── utils/           # was scripts/utils/
  │       ├── analysis/        # was scripts/analysis/
  │       ├── monitor_daemon/  # was scripts/monitor_daemon/
  │       ├── trade_blotter/   # was scripts/trade_blotter/
  │       ├── config/          # was scripts/config/
  │       └── lib/             # was scripts/lib/
  │
  ├── scripts/                 # SLIM — shell + dev tools only
  │   ├── cloud.sh
  │   ├── local.sh
  │   ├── docker_ib_gateway.sh
  │   ├── ibc_remote_control.sh
  │   ├── setup_ibc.sh
  │   ├── cleanup-dead-code.sh
  │   ├── ib_realtime/
  │   │   ├── ib_realtime_server.js
  │   │   ├── ib_connection_status.js
  │   │   ├── ib_tick_handler.js
  │   │   └── test_ib_realtime.py
  │   ├── services/             # recurring service wrappers + installers
  │   │   ├── setup_cri_service.sh
  │   │   ├── setup_cta_sync_service.sh
  │   │   ├── setup_exit_order_service.sh
  │   │   ├── setup_monitor_daemon.sh
  │   │   ├── setup_data_refresh_service.sh
  │   │   ├── run_cri_scan.sh
  │   │   ├── run_cta_sync.sh
  │   │   └── run_data_refresh.sh
  │   └── dev/                  # one-off dev tools (bash, not python)
  │       └── (nothing yet — Phase 1 dev/*.py moved into src/xenon/infra/dev/ or deleted)
  │
  ├── tests/                   # ROOT-LEVEL tests (was scripts/tests/)
  │   └── (pytest discovers from root, sees xenon package via editable install)
  │
  ├── pyproject.toml           # package metadata, dependencies, entry points
  ├── uv.lock                  # generated by `uv lock`, committed
  ├── .python-version          # 3.13
  ├── docker/ …
  ├── web/ …
  ├── docs/ …
  ├── data/ …
  └── README.md
```

**Key shape:**

- `src/xenon/` is the Python package. Editable install (`uv pip install -e .` or `uv sync`) puts it on `sys.path` as `xenon`.
- `scripts/` drops from ~75 entries to ~15. Bash + Node only, except for `ib_realtime/test_ib_realtime.py` (which is genuinely a dev tool, not production path — could also move to `tests/` or `src/xenon/infra/dev/` at your preference).
- `tests/` moves to repo root. pytest discovers from root; tests import `from xenon.fetchers.fetch_flow import ...` like any consumer.

---

## `pyproject.toml` (Target State)

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "xenon"
version = "0.2.0"
requires-python = ">=3.13"
description = "Market structure reconstruction system"
readme = "README.md"

dependencies = [
    # (migrated from scripts/requirements-api.txt, with version pins)
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "python-dotenv>=1.0",
    "httpx>=0.27",
    "duckdb>=1.1",
    "pandas>=2.2",
    "numpy>=2.0",
    "ib-insync>=0.9.86",
    "futu-api>=9.0",
    "ta-lib>=0.5",
    "playwright>=1.48",
    "pillow>=11.0",
    "anthropic>=0.39",
    "requests>=2.32",
    "beautifulsoup4>=4.12",
    # ... (full list from requirements-api.txt, pinned)
]

[project.optional-dependencies]
test = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "responses>=0.25",
    "freezegun>=1.5",
]
dev = [
    "ruff>=0.7",
    "mypy>=1.13",
]

[project.scripts]
# CLI entry points — installed as binaries on $PATH via `uv sync`.
# All entries are prefixed with `xenon-` to prevent collision with system PATH
# binaries and other tools that might install short generic names like `scan`,
# `evaluate`, or `kelly` (P2-5). Each maps to a `main()` function in target module.
xenon-api                  = "xenon.api.server:main"

xenon-evaluate             = "xenon.reports.evaluate:main"
xenon-kelly                = "xenon.reports.kelly:main"
xenon-blotter              = "xenon.reports.blotter:main"
xenon-portfolio-report     = "xenon.reports.portfolio_report:main"
xenon-portfolio-perf       = "xenon.reports.portfolio_performance:main"
xenon-portfolio-attr       = "xenon.reports.portfolio_attribution:main"
xenon-scenario-analysis    = "xenon.reports.scenario_analysis:main"
xenon-scenario-report      = "xenon.reports.scenario_report:main"
xenon-risk-reversal        = "xenon.reports.risk_reversal:main"
xenon-free-trade-analyzer  = "xenon.reports.free_trade_analyzer:main"
xenon-verify-options-oi    = "xenon.reports.verify_options_oi:main"

xenon-fetch-ticker         = "xenon.fetchers.fetch_ticker:main"
xenon-fetch-flow           = "xenon.fetchers.fetch_flow:main"
xenon-fetch-options        = "xenon.fetchers.fetch_options:main"
xenon-fetch-oi-changes     = "xenon.fetchers.fetch_oi_changes:main"
xenon-fetch-analyst        = "xenon.fetchers.fetch_analyst_ratings:main"
xenon-fetch-news           = "xenon.fetchers.fetch_news:main"
xenon-fetch-menthorq-cta   = "xenon.fetchers.fetch_menthorq_cta:main"
xenon-fetch-menthorq-dash  = "xenon.fetchers.fetch_menthorq_dashboard:main"
xenon-fetch-x-watchlist    = "xenon.fetchers.fetch_x_watchlist:main"
xenon-fetch-x-xai          = "xenon.fetchers.fetch_x_xai:main"

xenon-scan                 = "xenon.scanners.scanner:main"
xenon-discover             = "xenon.scanners.discover:main"
xenon-discover-forex       = "xenon.scanners.discover_forex:main"
xenon-trend-scan           = "xenon.scanners.trend.cli:main"
xenon-uw-scan              = "xenon.scanners.uw.scan:main"
xenon-uw-analyze           = "xenon.scanners.uw.analyze:main"
xenon-cri-scan             = "xenon.scanners.cri:main"
xenon-vcg-scan             = "xenon.scanners.vcg:main"
xenon-gex-scan             = "xenon.scanners.gex:main"
xenon-leap-iv              = "xenon.scanners.leap_iv:main"
xenon-leap-uw              = "xenon.scanners.leap_uw:main"
xenon-garch-convergence    = "xenon.scanners.garch:main"
xenon-repair-cri-rvol      = "xenon.scanners.repair_cri_rvol_cache:main"

xenon-ib-sync              = "xenon.execution.ib_sync:main"
xenon-ib-execute           = "xenon.execution.ib_execute:main"
xenon-ib-place-order       = "xenon.execution.ib_place_order:main"
xenon-ib-order-manage      = "xenon.execution.ib_order_manage:main"
xenon-ib-orders            = "xenon.execution.ib_orders:main"
xenon-ib-option-chain      = "xenon.execution.ib_option_chain:main"
xenon-ib-reconcile         = "xenon.execution.ib_reconcile:main"
xenon-naked-short-audit    = "xenon.execution.naked_short_audit:main"
xenon-futu-sync            = "xenon.execution.futu_sync:main"

xenon-generate-cta-share    = "xenon.shares.generate_cta_share:main"
xenon-generate-regime-share = "xenon.shares.generate_regime_share:main"
xenon-generate-vcg-share    = "xenon.shares.generate_vcg_share:main"
xenon-generate-gex-share    = "xenon.shares.generate_gex_share:main"

xenon-exit-order-service   = "xenon.services.exit_order_service:main"
xenon-cta-sync-service     = "xenon.services.cta_sync_service:main"

[tool.hatch.build.targets.wheel]
packages = ["src/xenon"]

[tool.ruff]
target-version = "py313"
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "W", "B", "I"]
ignore = ["E501"]

[tool.ruff.lint.isort]
known-first-party = ["xenon"]

[tool.pytest.ini_options]
asyncio_mode = "strict"
testpaths = ["tests"]
markers = [
    "integration: live tests hitting real MenthorQ (requires credentials)",
    "e2e: live tests hitting real Massive API (requires MASSIVE_API_KEY)",
]
```

---

## `uv` Workflow

### Bootstrap

One-time on any machine:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # install uv
cd /path/to/xenon
uv sync                                            # creates .venv, installs xenon editable, installs deps
```

After `uv sync`, all `[project.scripts]` entry points are on `$PATH` inside the uv-managed venv. Activate via `uv run <cmd>` or `source .venv/bin/activate`.

### Daily usage

```bash
uv run trend-scan --top 25                  # run a CLI
uv run python -m xenon.api.server           # run a module
uv run pytest tests/                        # run tests
uv add <package>                            # add + lock + install
uv lock --upgrade                           # refresh lockfile
uv pip install -e .                         # fallback, if needed
```

### CI / Docker

- `uv sync --frozen` in CI → fails if `uv.lock` is out of date (forces explicit lock updates).
- Docker: copy `pyproject.toml` + `uv.lock`, run `uv sync --frozen --no-dev`, COPY `src/`, set `CMD [".venv/bin/xenon-api"]` (direct binary, see Production Invocation below).

---

## Production Invocation — `.venv/bin/<entry>`, NOT `uv run` (P2-1, P2-2)

**Critical:** every production-path call site invokes the entry-point binary **directly** at `.venv/bin/<entry>`, bypassing `uv run`.

**Why direct invocation, not `uv run`:**

1. **Latency.** `uv run` re-checks venv/lock state every invocation (50–600ms). Calling `.venv/bin/xenon-cri-scan` is a normal `exec()` — zero `uv` overhead. With FastAPI's `run_script()` having timeouts as tight as 15 seconds (e.g., `ib_option_chain.py`, `portfolio_attribution.py`), the cumulative `uv run` overhead per request matters.
2. **launchd PATH.** Launchd processes start with a minimal `PATH=/usr/bin:/bin:/usr/sbin:/sbin`. `uv` (installed at `~/.cargo/bin/uv` or `~/.local/bin/uv`) is NOT on launchd's PATH. Any `run_*.sh` invoking `uv run` from a launchd plist fails with "uv: command not found." `.venv/bin/xenon-X` works regardless of PATH because we use absolute or repo-relative paths.
3. **Determinism.** `.venv/bin/<entry>` invokes a fixed, materialized binary. Calling `uv run <entry>` may trigger silent re-resolution if another `uv` operation modified the lockfile.

**`uv run` IS appropriate for:**

- Interactive developer commands (`uv run pytest`, `uv run python -i`).
- One-off ops invocations from a TTY where you want lock-state validation.
- CI bootstrap (`uv sync --frozen` first, then direct calls).

**Specific call-site updates:**

### `scripts/api/subprocess.py` — replace `run_script` semantics

```python
# Before (Phase 1):
async def run_script(script: str, args=None, timeout=30.0, cwd=None) -> ScriptResult:
    script_path = SCRIPTS_DIR / script
    cmd = [sys.executable, str(script_path)] + (args or [])
    # ...

# After (Phase 2):
VENV_BIN = PROJECT_ROOT / ".venv" / "bin"

async def run_entry_point(entry: str, args=None, timeout=30.0) -> ScriptResult:
    """Invoke a `[project.scripts]` entry point binary directly. No uv overhead."""
    binary = VENV_BIN / entry
    if not binary.exists():
        return ScriptResult(ok=False, error=f"Entry point not found: {entry} (did you run `uv sync`?)")
    cmd = [str(binary)] + (args or [])
    # ... same subprocess plumbing
```

`run_script()` is kept as a thin wrapper that resolves the legacy filename → entry-point name during the migration window, then deleted.

### `scripts/services/run_*.sh` and `setup_*_service.sh`

```bash
# Before:
"$PYTHON_BIN" scripts/cri_scan.py --json

# After:
"$PROJECT_DIR/.venv/bin/xenon-cri-scan" --json
```

Inside the wrapper, also export PATH defensively:

```bash
export PATH="$PROJECT_DIR/.venv/bin:$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:$PATH"
```

This belt-and-suspenders approach lets the wrapper invoke other tooling (`uv`, `git`, etc.) under launchd if needed.

### `web/` TypeScript subprocess calls

```ts
// Before (Phase 1):
subprocess.run(["scripts/evaluate.py", ticker]);

// After (Phase 2):
subprocess.run([".venv/bin/xenon-evaluate", ticker]);
```

The `runScript` helper resolves the entry-name to `path.join(PROJECT_ROOT, ".venv", "bin", "xenon-" + name)`.

---

## Import Rewrite

Every Phase 1 bare-name import becomes qualified:

| Phase 1                                     | Phase 2                                                                   |
| ------------------------------------------- | ------------------------------------------------------------------------- |
| `from fetchers.fetch_flow import X`         | `from xenon.fetchers.fetch_flow import X`                                 |
| `from scanners.trend.cli import main`       | `from xenon.scanners.trend.cli import main`                               |
| `from scanner_lib.cache import X`           | `from xenon.scanners._shared.cache import X` (already renamed in Phase 1) |
| `from utils.ib_connection import X`         | `from xenon.utils.ib_connection import X`                                 |
| `from clients.ib_client import IBClient`    | `from xenon.clients.ib_client import IBClient`                            |
| `import trade_blotter.flex_query` (dynamic) | `import xenon.trade_blotter.flex_query`                                   |

**Automation:** one-shot rewrite per top-level bucket:

```bash
# Dry run with rg + sed:
for pkg in fetchers scanners execution reports shares services ta infra api clients utils analysis monitor_daemon trade_blotter config lib scanner_lib trend_scan_lib uw_scan_lib ta_lib; do
  rg -l "^(from|import)\s+${pkg}(\.|\s)" src/ tests/ | \
    xargs sed -i '' -E "s/^(from|import)[[:space:]]+${pkg}([.[:space:]])/\1 xenon.${pkg}\2/g"
done
```

Verify: `rg '^(from|import) (fetchers|scanners|execution|reports|shares|services|ta|infra)\b'` → zero results.

**Remove `sys.path.insert(0, ...)` patterns.** Every file that did `sys.path.insert(0, SCRIPT_DIR)` can drop that block entirely — editable install handles `sys.path`. Same for Phase 1 fixes (`.parent.parent`, `.parent.parent.parent`). The editable install renders those unnecessary. Per-file edit, grep-verifiable:

```bash
rg -l "sys\.path\.insert" src/xenon/  # find
# remove the pattern manually, verify tests still pass
```

---

## External Caller Updates

Every Phase 1 shim is replaced by a direct new-form invocation. These all happen in one sweep PR at the end of Phase 2.

### `web/` TypeScript

| Before                                               | After                                                                                       |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `subprocess.run(["scripts/evaluate.py", ticker])`    | `subprocess.run([".venv/bin/xenon-evaluate", ticker])`                                      |
| `["scripts/ib_sync.py"]`                             | `[".venv/bin/xenon-ib-sync"]`                                                               |
| `["scripts/leap_scanner_uw.py"]`                     | `[".venv/bin/xenon-leap-uw"]`                                                               |
| `runScript("scripts/fetch_analyst_ratings.py", ...)` | `runEntry("xenon-fetch-analyst", ...)` (helper resolves to `.venv/bin/xenon-fetch-analyst`) |
| `["scripts/kelly.py"]`                               | `[".venv/bin/xenon-kelly"]`                                                                 |

Files touched: `web/app/api/pi/route.ts`, `web/app/api/ticker/ratings/route.ts`, `web/app/api/menthorq/[command]/image/route.tsx`, `web/lib/runner.ts` (if it exists), `web/tests/integration.test.ts`, `web/tests/*.test.ts` assertions that check `scripts/generate_X_share.py` existence → update to check the entry binary exists at `.venv/bin/xenon-generate-X-share` (or just drop the assertion — invocation either works or fails).

### `scripts/api/subprocess.py` `run_script()`

See "Production Invocation" above. `run_entry_point()` invokes `.venv/bin/<entry>` directly, never `uv run`. Every call site in `scripts/api/server.py` updates:

| Before                                         | After                                                  |
| ---------------------------------------------- | ------------------------------------------------------ |
| `run_script("trend_scan.py", ["--top", "25"])` | `run_entry_point("xenon-trend-scan", ["--top", "25"])` |
| `run_script("cri_scan.py", ["--json"])`        | `run_entry_point("xenon-cri-scan", ["--json"])`        |
| …and ~12 more                                  | …same pattern                                          |

### Shell scripts

Remaining `.sh` scripts update their Python invocations to use the venv binary directly (launchd-safe — see Production Invocation):

| Before                                           | After                                             |
| ------------------------------------------------ | ------------------------------------------------- |
| `"$PYTHON_BIN" scripts/scanner.py --top 25`      | `"$PROJECT_DIR/.venv/bin/xenon-scan" --top 25`    |
| `"$PYTHON_BIN" scripts/cri_scan.py --json`       | `"$PROJECT_DIR/.venv/bin/xenon-cri-scan" --json`  |
| `"$PYTHON_BIN" scripts/repair_cri_rvol_cache.py` | `"$PROJECT_DIR/.venv/bin/xenon-repair-cri-rvol"`  |
| `"$PYTHON_BIN" scripts/cta_sync_service.py`      | `"$PROJECT_DIR/.venv/bin/xenon-cta-sync-service"` |

Add at the top of each wrapper: `export PATH="$PROJECT_DIR/.venv/bin:$PATH"`.

Files: `scripts/services/run_data_refresh.sh`, `scripts/services/run_cri_scan.sh`, `scripts/services/run_cta_sync.sh`, `scripts/benchmarks/autoresearch.sh`.

### Launchd plists

All installed launchd plists that invoke `scripts/services/run_X.sh` keep working (path-stable after Phase 1; only their internal commands change to `.venv/bin/xenon-X`). Plists pointing to absolute `run_*.sh` paths continue to fire — only the wrapper contents update.

Plists that invoke a Python `.py` directly (shouldn't exist per audit, but verify): update to absolute path `.../xenon/.venv/bin/xenon-<entry>`. **Never** use bare `uv run <entry>` from a plist — `uv` is not on launchd's PATH.

Setup-installer scripts (`setup_X_service.sh`) regenerate plists; re-run them once on each host:

```bash
for host in localhost vps; do
  ssh "$host" 'cd /path/to/xenon && for s in services/setup_*_service.sh; do bash "scripts/$s" install; done'
done
```

### Docs

Bulk sed over `docs/`, `CHANGELOG.md`, root-level `*.md`:

```bash
# Map old CLI names to new entry points in docs:
rg "python3\.13?\s+scripts/([a-z_]+)\.py" docs/ -l | while read f; do
  # interactive review + sed per file; don't blindly substitute
done
```

`scripts/CLAUDE.md` and `CLAUDE.md` commands tables get a full rewrite with the new entry-point names.

---

## Shim Deletion

After all external callers update, delete every Phase 1 shim in one commit:

```bash
# Delete Python shims left at old root paths:
for f in scripts/fetch_*.py scripts/ib_*.py scripts/portfolio_*.py scripts/scenario_*.py \
         scripts/generate_*_share.py scripts/trend_scan.py scripts/uw_scan.py scripts/uw_analyze.py \
         scripts/evaluate.py scripts/kelly.py scripts/blotter.py scripts/risk_reversal.py \
         scripts/free_trade_analyzer.py scripts/verify_options_oi.py scripts/scanner.py \
         scripts/discover.py scripts/discover_forex_dom.py scripts/cri_scan.py scripts/vcg_scan.py \
         scripts/gex_scan.py scripts/leap_iv_scanner.py scripts/leap_scanner_uw.py \
         scripts/garch_convergence.py scripts/repair_cri_rvol_cache.py scripts/naked_short_audit.py \
         scripts/futu_sync.py scripts/exit_order_service.py scripts/cta_sync_service.py; do
  [ -f "$f" ] && git rm "$f"
done

# Delete shell symlinks:
for f in scripts/run_*.sh scripts/setup_*.sh scripts/cloud.sh scripts/local.sh \
         scripts/docker_ib_gateway.sh scripts/ibc_remote_control.sh \
         scripts/cleanup-dead-code.sh; do
  [ -L "$f" ] && git rm "$f"
done
```

Then move the real `.sh` files up one level into the new slim `scripts/`:

```bash
git mv scripts/infra/cloud.sh scripts/cloud.sh
git mv scripts/infra/local.sh scripts/local.sh
# ... etc
```

Or keep `scripts/services/`, `scripts/infra/`, etc. as Phase 1 had them — the slim layout shown above is the target.

---

## Migration Order (Risk-Sorted)

Phase 2 is one long-running PR chain. Each step is atomic; main stays green.

1. **Add `pyproject.toml` with `[build-system]` + `[project]` metadata, package at `src/xenon/`**. Start with a _stub_ `src/xenon/__init__.py` and nothing else. `uv sync` succeeds. No behavior change yet.

2. **Move leaf buckets first; `api/` last (P2-7).** Order by import dependency depth — the bucket with the fewest internal-graph dependents moves first, `api/` (which imports nearly everything) moves last:

   **Move order:**
   1. `shares/` (4 files, isolated)
   2. `infra/` (mostly Node, minimal Python imports)
   3. `fetchers/` (consumed by scanners + reports + api)
   4. `scanners/` (consumed by api)
   5. `execution/` (consumed by api)
   6. `reports/` (consumed by api + web)
   7. `services/` (`exit_order_service`, `cta_sync_service` — daemons)
   8. `clients/`, `utils/`, `analysis/`, `lib/`, `config/`, `monitor_daemon/`, `trade_blotter/` (foundational, also consumed by api)
   9. **`api/` last** — most imports of all the above. Single largest move.

   For each bucket move:
   - `git mv scripts/<bucket> src/xenon/<bucket>`.
   - Rewrite imports in the moved files (`from fetchers.foo import` → `from xenon.fetchers.foo import`).
   - **Verify imports across all already-migrated buckets**: `rg "^(from|import) <old_bucket_name>\b" src/xenon/` — must be zero.
   - Update Phase 1 shims at `scripts/<old>.py` to forward to the new location: `from xenon.<bucket>.<name> import *` (or `runpy.run_module("xenon.<bucket>.<name>", run_name="__main__")` for cleaner CLI semantics).
   - Verify: `.venv/bin/pytest tests/` and the smoke CLI list pass.

3. **Add entry points incrementally.** After each bucket lands in `src/xenon/`, add its `[project.scripts]` entries. `uv sync` → `.venv/bin/xenon-*` binaries appear.

4. **`api/` move (Step 2.10) — extra verification.** Before merging:

   ```bash
   # Check no internal api/ files import from a still-unmigrated module:
   rg "^(from|import) (api|clients|utils|analysis|lib|config|trade_blotter|monitor_daemon|fetchers|scanners|execution|reports|shares|services|infra)\b" src/xenon/api/
   # Should return zero — all imports must be `xenon.*` qualified.
   ```

5. **Update `scripts/api/subprocess.py`** with `run_entry_point()` (see Production Invocation section).

6. **Update `web/` TypeScript callers** one file per PR. Each caller switches to `.venv/bin/xenon-X` form. Test on the `web/` side after each change.

7. **Update shell scripts and `scripts/api/server.py`** internal callers, one at a time.

8. **Update launchd plists on all deployment hosts** (re-run `services/setup_*_service.sh install` on each — installer rewrites the plist with the new wrapper contents).

9. **Wait 1 full trading day** with both old (shim) and new (`.venv/bin/xenon-*`) paths working. Verify cron/scheduled runs (8:30 AM trend scan).

10. **Pre-delete audit for `requirements-api.txt` consumers (P2-4).** Before deleting:

    ```bash
    rg -l "requirements-api\.txt" .
    # Inspect every hit:
    # - docker/*Dockerfile? (FastAPI container?)
    # - .github/workflows/*.yml?
    # - any deploy script?
    # - README/docs install instructions?
    ```

    Each consumer must migrate to `uv sync --frozen --no-dev` (or be removed) in the same PR as the deletion. Specifically check:
    - `docker/ib-gateway/` — IB gateway image doesn't run Python (verified at spec time), so no impact. But verify a FastAPI container doesn't exist.
    - CI workflows — none currently exist per audit, but `.github/workflows/` may grow before Phase 2.
    - `web/README.md` and root `README.md` — bootstrap instructions reference `pip install -r requirements-api.txt`.

11. **Delete all shims** in one commit. Verify `rg "scripts/\w+\.py"` in `web/`, `scripts/api/server.py`, `scripts/services/*.sh`, `docs/` returns only current (`.venv/bin/xenon-*`) references.

12. **Move `scripts/tests/` to root `tests/`**. pytest discovers from root. Update `testpaths = ["tests"]` in `pyproject.toml`. Audit `conftest.py` for `sys.path` assumptions before moving.

13. **Flatten `scripts/services/*.sh` and `scripts/infra/*.sh` to `scripts/`** if desired (optional cleanup — keeping the nested dirs matches Phase 1's co-location principle and is fine).

14. **Delete `requirements-api.txt`** — only after Step 10 audit is clean.

**Tag a recovery point before Step 11 (P2-11):** `git tag phase2-pre-shim-deletion <sha>`. Rollback recipe for shim deletion uses this tag, not `HEAD~N` (which drifts as later PRs land).

---

## Verification

### Post-migration smoke (must all pass)

```bash
uv sync --frozen
.venv/bin/pytest tests/
.venv/bin/xenon-evaluate AAPL
.venv/bin/xenon-trend-scan --top 5
.venv/bin/xenon-uw-scan --help
.venv/bin/xenon-cri-scan --json >/dev/null
.venv/bin/xenon-api &              # starts FastAPI
sleep 3
curl http://localhost:8321/health
kill %1
cd web && npm test && npx playwright test
```

(Interactive `uv run xenon-X` works too — the direct binary path is for production/scripted contexts where `uv` overhead matters or PATH is constrained.)

### External-reference audit (must return zero results for retired paths)

```bash
rg "scripts/(fetch_|ib_|portfolio_|scenario_|generate_|trend_scan|uw_scan|uw_analyze|ta_cli|ta_premarket|ta_reseed|evaluate|kelly|blotter|risk_reversal|free_trade_analyzer|verify_options_oi|scanner|discover|cri_scan|vcg_scan|gex_scan|leap_|garch|repair_cri|naked_short_audit|futu_sync|exit_order_service|cta_sync_service)\.py" web/ docs/ scripts/api/ scripts/benchmarks/
```

### Soak

- 8:30 AM ET trend scan runs successfully for 3 consecutive weekdays after migration.
- No `ScriptResult(ok=False, error="Script not found...")` in FastAPI logs for a week.
- No launchd plist errors in Console.app for a week.

---

## Rollback

Phase 2 is chunked across many PRs. Rollback per step:

- **Steps 1–3 (package scaffolding):** `git revert` the step. `uv sync` continues to work from the previous state.
- **Steps 4–7 (caller updates):** Each caller update is its own commit. Revert the individual caller to fall back to shim (shim still exists until Step 9).
- **Step 11 (shim deletion):** If a miss is found post-deletion, restore the missing shim using the recovery tag: `git show phase2-pre-shim-deletion:scripts/<old>.py > scripts/<old>.py`. The tag is stable across later commits, unlike `HEAD~N`. Investigate which caller we missed. The shim just forwards to `xenon.<bucket>.<name>` → trivial to rebuild.
- **Step 10 (test dir move):** `git revert` the move. pytest config updates in the same commit.

The guiding principle: shims stay alive until **every** external caller has been updated. Deletion is the last step, not the first.

---

## Success Criteria

- `uv sync` produces a working environment from a fresh clone in <30 seconds.
- `uv.lock` committed; CI `uv sync --frozen` green.
- All CLI commands run via `.venv/bin/xenon-<name>` (production) or `uv run xenon-<name>` (interactive); `scripts/` directory contains only `.sh` files and one Node subsystem.
- Zero references to retired `scripts/<file>.py` paths in `web/`, `docs/`, `scripts/api/server.py`, or shell scripts.
- `pyproject.toml` is the single source of truth for dependencies and CLI commands.
- `from xenon.X.Y import Z` works everywhere; no `sys.path.insert` required anywhere.
- 8:30 AM ET trend scan runs successfully post-migration.
- `web/` test suite and Playwright E2E suite green.
