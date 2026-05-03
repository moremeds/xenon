# PG Migration Clean Cutoff Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every JSON/file artifact whose canonical source is now Postgres is deleted from `data/` in lockstep with reader+writer migration. After this plan lands, the only files remaining in `data/` are file-canonical-by-design (config, caches, ephemeral) or explicitly deferred (futu_portfolio.json, orders.duckdb).

**Architecture:** Clean-cutoff principle. Each task = one PG-canonical artifact. The plan favors the **central loader pattern** wherever possible: instead of migrating 6 individual call sites, migrate the one shared loader function and verify all readers route through it. No JSON fallback. No "dual-write for safety." If PG is canonical, file equivalents do not exist.

**Tech Stack:** Python 3.13 (uv), SQLAlchemy Core (async via `xenon.db.engine.get_engine`, sync via `get_sync_engine`), pytest + pytest-asyncio, existing PG harness (`scripts/tests/conftest.py`, `src/xenon/db/tests/`), AST-based guard tests (`scripts/tests/test_portfolio_json_not_read.py` is the pattern).

---

## v2 changelog (vs. v1)

This plan was reviewed by the codex-review tribunal (Codex + Gemini + Claude). 13 critical/important fixes folded in:

1. **Central loader pattern** — `load_portfolio_payload_sync` from `xenon.utils.portfolio_loader` already exists; migrate it once, not 6 callers (Gemini G7).
2. **Correct function signatures** — `get_latest_portfolio_payload` is `async`; sync code uses `load_portfolio_payload_sync(scope=...)`. AccountScope's method is `resolve_from_env()`, not `from_env()` (Codex C1, Gemini G1+G6).
3. **Correct table/column names** — `xenon.order_fills.filled_at` (not `xenon.fills.fill_date`); `xenon.uw_flow_events` (not `uw_unusual_flow_log`); `uw_analyze_snapshots.report` JSONB (not `payload`) (Codex C4+C8+C10, Gemini G9).
4. **Missed readers added** — `portfolio_attribution.py:25`, `portfolio_report.py:49` both define `TRADE_LOG_PATH` (Codex C11).
5. **DATA_DIR refactor before deleting PORTFOLIO_PATH** — `ib_sync.py:936/955/1387` all use `PORTFOLIO_PATH.parent`; introduce `DATA_DIR` first (Codex C2).
6. **PG write failure must be fatal** — `save_portfolio()` currently catches and warns; must propagate (Codex C3).
7. **Exit-orders rescoped** — `ExitOrdersHandler` reads richer nested data (`target/stop/contract_spec/status`); migration requires either retiring the daemon or adding a new PG table. Plan flags as decision point (Codex C5).
8. **NAV schema extension** — `nav_history` table is missing `cash/stock/options` columns; Alembic migration runs before deleting `nav_history_ib.json` (Codex C7).
9. **uw_analyze SQL fixed** — append-only inserts; read latest via `ORDER BY snapshot_at DESC, id DESC LIMIT 1`; no `ON CONFLICT` (Codex C8).
10. **uw_flow tracker collapsed** — `uw_flow_events` already used by tracker; just remove JSON fallback + add guard (Codex C10).
11. **CI fixture seeding** — `web/tests/setup/seed-fixtures.ts` writes the JSON files we're deleting; must update in same PR (Gemini G3).
12. **AST-based guards** — string regex misses `p = DATA_DIR / "x.json"; p.read_text()` patterns; AST walker catches all reads (Codex C12).
13. **Monkeypatch-based assertions** — `not os.path.exists(...)` is environment-dependent; better to monkeypatch the loader/`open` to raise (Codex C13).

Plus Claude self-review additions: backup before delete, branch/PR workflow, fixture pre-creation, schema verification per task, ib_sync entry_date task ordering (Tasks A+B+C bundled in one PR since they edit overlapping `convert_to_portfolio_format` code).

---

## Pre-flight context

**Why this plan exists:** The W1-W5 PG migration scoped itself to "runtime web/API surfaces" and deferred CLI/audit readers as low priority. The 2026-05-03 audit found 8 PG-canonical artifacts that still have file readers and/or writers. With production being declared fresh on a containerized stack (clean-slate framing), the migration completes before "production starts."

**Why no fallback:** there is no live production to protect. Every migration runs against the PG schema that's already populated; a failure mode of "reader returns empty" can only occur if the PG query is wrong, which the per-task test catches before merge.

**Order of execution:** Setup tasks (-2, -1, 0) first. Then **Tasks A+B+C bundled in one PR** because they edit overlapping code in `ib_sync.py:919-995`. Then independent Tasks D-H. Then fix-up Tasks X, Y, Z.

**Inventory** (full audit results, 2026-05-03):

| Artifact                                                                                                              | Size  | Writer(s)                                                                                                                               | Reader(s) — including the central loader where applicable                                                                                                                                                                              | PG canonical source                                                                                    |
| --------------------------------------------------------------------------------------------------------------------- | ----- | --------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `data/portfolio.json` (+ .bak)                                                                                        | small | `ib_sync.py:39,919-995`                                                                                                                 | **Central:** `xenon.utils.portfolio_loader.load_portfolio_payload_sync` ← `portfolio_adapter.py` (IB branch already migrated). **Direct:** `naked_short_audit.py`, `leap_iv.py:1044`, `portfolio_report.py:845`, `ib_sync.py:992,1387` | `xenon.account_snapshots.payload`                                                                      |
| `data/trade_log.json`                                                                                                 | small | `ib_sync.py:936` (read+write); `monitor_daemon/handlers/fill_monitor.py:26` (writer); `monitor_daemon/handlers/exit_orders.py` (reader) | `ib_sync.py:936`, `portfolio_report.py:49+436`, `portfolio_attribution.py:25`, `monitor_daemon/handlers/exit_orders.py:61-86` (richer data — see Task B note)                                                                          | `xenon.order_fills.filled_at` + `xenon.trades.opened_at` + `xenon.journal_entries`                     |
| `data/blotter.json`                                                                                                   | small | `ib_sync.py:955`                                                                                                                        | `ib_sync.py:955` (only)                                                                                                                                                                                                                | TBD — likely retire entry_date blotter join entirely (trade_log + portfolio prev sufficient)           |
| `data/nav_history.jsonl`                                                                                              | small | none found (legacy read-only data)                                                                                                      | `portfolio_performance.py:309`                                                                                                                                                                                                         | `xenon.nav_history` table                                                                              |
| `data/nav_history_ib.json`                                                                                            | small | `portfolio_performance.py:377` (cache write)                                                                                            | `portfolio_performance.py:388/391`                                                                                                                                                                                                     | `xenon.nav_history` after schema extension (currently missing cash/stock/options)                      |
| `data/uw_analyze_history/`                                                                                            | 450M  | `uw_analyze_cache.py:62` (`_DEFAULT_HISTORY_DIR`)                                                                                       | `uw_analyze_cache.py` history reads                                                                                                                                                                                                    | `xenon.uw_analyze_snapshots` (append-only)                                                             |
| `data/uw_analyze_cache.json`                                                                                          | 5.5M  | `uw_analyze_cache.py:61` (`_DEFAULT_CACHE_PATH`)                                                                                        | `uw_analyze_cache.py` cache reads                                                                                                                                                                                                      | `xenon.uw_analyze_snapshots` latest-by-ticker via `ORDER BY snapshot_at DESC, id DESC LIMIT 1`         |
| `data/uw_api_stats_history.json`                                                                                      | small | `uw_api_stats.py:45`                                                                                                                    | `uw_api_stats.py` history reads                                                                                                                                                                                                        | `xenon.uw_api_stats` (already backfilled)                                                              |
| `data/uw_unusual_flow_log.json`                                                                                       | small | `uw_analyze_flow_tracker.py:44`                                                                                                         | `uw_analyze_flow_tracker.py` (already loads PG fallback per `:382`)                                                                                                                                                                    | `xenon.uw_flow_events` (table EXISTS; tracker already uses it — Task G is just removing JSON fallback) |
| `data/cri.json`, `data/scanner.json`, `data/gex.json`, `data/vcg.json`, `data/discover.json`, `data/performance.json` | small | `repair_cri_rvol_cache.py:387` (cri only); server.py dual-write already removed                                                         | `shares/generate_vcg_share.py:4`, `shares/generate_gex_share.py:5`, `performance_explainer_report.py:889`                                                                                                                              | Existing PG queries via `/portfolio`, `/gex`, `/vcg`, `/discover`, `/performance`, `/cri` route paths  |

**Truly dead (Phase 0 cull):** `data/ta.duckdb` (84M), `data/*.bak`, `data/flow_analysis.json`, `data/ta_premarket_*.json`. Output dirs `data/{analysis,evidence,scanner,universe,uw_scan,cri_scheduled}/` audited per Task 0.

---

## Setup

### Task -2: Create branch and draft PR

**Files:** none (git only)

- [ ] **Step 1: Create branch off latest master**

```bash
git fetch origin master
git checkout -b feat/pg-clean-cutoff origin/master
git push -u origin feat/pg-clean-cutoff
```

- [ ] **Step 2: Open draft PR with plan link**

```bash
gh pr create --draft --title "PG migration clean cutoff" --body "$(cat <<'EOF'
## Summary
Clean-cutoff PG migration per docs/plans/2026-05-03-pg-migration-clean-cutoff.md.
Every PG-canonical JSON artifact in data/ deleted in lockstep with reader+writer migration.
No JSON fallback. Frees ~540MB.

## Status
Draft. Will mark ready when all tasks complete and tests green.
EOF
)"
```

Expected: PR URL printed. Note it for later commits.

### Task -1: Add reusable PG-seeding fixtures

**Files:**

- Modify: `scripts/tests/conftest.py` — add fixtures used across multiple Phase 1 tasks

- [ ] **Step 1: Add fixture for an account snapshot with a naked short**

```python
# scripts/tests/conftest.py — append

@pytest.fixture
def seeded_pg_with_naked_short_position(pg_test_engine, scope_fixture):
    """Inserts an account_snapshot with a naked short call (no covering long calls or shares)."""
    payload = {
        "positions": [
            {"ticker": "AAPL", "right": "C", "action": "SHORT", "qty": -1,
             "expiry": "2026-06-19", "strike": 200, "entry_date": "2026-04-15"},
        ],
        "bankroll": 100000, "cash": 50000,
    }
    from xenon.db.schema import account_snapshots
    with pg_test_engine.begin() as conn:
        conn.execute(account_snapshots.insert().values(
            broker=scope_fixture.broker, account_env=scope_fixture.account_env,
            broker_account=scope_fixture.broker_account, payload=payload,
        ))
    yield SimpleNamespace(scope=scope_fixture, payload=payload)
```

- [ ] **Step 2: Add fixture for order_fills history (used by Task B)**

```python
@pytest.fixture
def seeded_pg_with_order_fills(pg_test_engine, scope_fixture):
    """Inserts order_fills rows for entry_date derivation tests."""
    from xenon.db.schema import order_fills
    rows = [
        {"ticker": "AAPL", "filled_at": dt.datetime(2026,4,15, tzinfo=dt.timezone.utc),
         "structure": "Long Call", "broker": scope_fixture.broker, ...},
    ]
    with pg_test_engine.begin() as conn:
        for row in rows:
            conn.execute(order_fills.insert().values(**row))
    yield SimpleNamespace(scope=scope_fixture, rows=rows)
```

- [ ] **Step 3: Add fixture for uw_analyze_snapshots (used by Task E)**

```python
@pytest.fixture
def seeded_pg_with_uw_snapshot(pg_test_engine):
    """Inserts a uw_analyze_snapshot row with full report JSONB."""
    from xenon.db.schema import uw_analyze_snapshots
    report = {"price": 200.0, "scores": {"composite": 75, "flow": 80}}
    with pg_test_engine.begin() as conn:
        result = conn.execute(uw_analyze_snapshots.insert().values(
            ticker="AAPL", report=report, display={}, derived={},
        ).returning(uw_analyze_snapshots.c.id))
        snap_id = result.scalar()
    yield SimpleNamespace(snap_id=snap_id, ticker="AAPL", report=report)
```

- [ ] **Step 4: Run conftest to verify fixtures import cleanly**

Run: `uv run pytest scripts/tests/conftest.py --collect-only`
Expected: PASS, no import errors.

- [ ] **Step 5: Commit**

```bash
git add scripts/tests/conftest.py
git commit -m "test(pg): add reusable PG-seeding fixtures for clean-cutoff migration"
```

### Task 0: Backup data/ and cull truly dead artifacts

**Files:**

- Backup: `data/` → `data.backup-2026-05-03/` (rsync, host-local only — gitignored)
- Delete: `data/ta.duckdb`, `data/*.bak`, `data/flow_analysis.json`, `data/ta_premarket_*.json`

- [ ] **Step 1: Backup data/ before any deletion**

```bash
rsync -a --exclude='locks/' --exclude='service_health/' data/ data.backup-2026-05-03/
du -sh data.backup-2026-05-03/
```

Expected: backup ~546M. Add a sticky reminder to delete after 7-day soak (post-PR-merge).

- [ ] **Step 2: Verify each truly-dead candidate has zero source references**

```bash
for item in ta.duckdb portfolio.json.bak orders.json.bak flow_analysis.json ta_premarket_status.json ta_premarket_universe.json; do
  echo "=== $item ==="
  /opt/homebrew/bin/rg -l "$item" -g '*.py' -g '*.ts' -g '*.tsx' -g '*.js' src/ scripts/ web/ 2>&1 | grep -v test | grep -v migrations | head -5
done
```

Expected: each section returns either zero hits or only test/migration references.

- [ ] **Step 3: Verify output directories are stale**

```bash
for dir in analysis evidence scanner universe uw_scan cri_scheduled; do
  echo "=== data/$dir/ ==="
  ls -la data/$dir/ 2>&1 | head -3
  /opt/homebrew/bin/rg -l "data/$dir/" -g '*.py' -g '*.ts' src/ scripts/ web/ 2>&1 | head -3
done
```

If any has live writers (dynamic-path scanners can't be caught by literal grep — verify by running a paper scan first), EXCLUDE that dir from this task.

- [ ] **Step 4: Delete verified-dead artifacts**

```bash
rm -f data/ta.duckdb data/portfolio.json.bak data/orders.json.bak data/flow_analysis.json data/ta_premarket_status.json data/ta_premarket_universe.json
# Output dirs: only those confirmed stale in Step 3:
rm -rf data/analysis data/evidence data/scanner data/universe data/uw_scan data/cri_scheduled
du -sh data/
```

Expected: down by ~85MB+.

- [ ] **Step 5: Run affected tests**

Run: `uv run python scripts/infra/dev/run_pytest_affected.py`
Expected: all green. Any failure means a "dead" candidate wasn't actually dead — restore via `cp data.backup-2026-05-03/<file> data/<file>` and remove from this task.

- [ ] **Step 6: Commit**

```bash
git add -A data/
git commit -m "chore: cull truly orphaned data/ artifacts (Phase 0)"
```

---

## Bundled Task A+B+C — portfolio.json + trade_log.json + blotter.json (one PR)

**Why bundled:** All three are read+written by `ib_sync.py:919-995` (`convert_to_portfolio_format`'s entry_date join). Doing them as separate PRs would require leaving stub code that re-breaks each time.

**Files:**

- Modify: `src/xenon/utils/portfolio_loader.py` — verify `load_portfolio_payload_sync` reads PG (not file)
- Modify: `src/xenon/execution/ib_sync.py` — multiple edits (DATA_DIR refactor, delete write blocks, replace entry_date join)
- Modify: `src/xenon/execution/naked_short_audit.py` — route through central loader
- Modify: `src/xenon/scanners/leap_iv.py:1044` — route through central loader
- Modify: `src/xenon/reports/portfolio_report.py:49,436,587,690,822` — route through central loader; replace `load_trade_log()` with PG query
- Modify: `src/xenon/reports/portfolio_attribution.py:25` — replace `TRADE_LOG_PATH`
- Modify: `src/xenon/monitor_daemon/handlers/fill_monitor.py:26` — remove `DEFAULT_TRADE_LOG` write site
- **Decision required:** `src/xenon/monitor_daemon/handlers/exit_orders.py:61-86` — reads richer nested `target/stop/contract_spec/status`; cannot be replaced by `order_fills` alone. **Two options:**
  - (a) Retire `ExitOrdersHandler` daemon explicitly (delete handler + its tests)
  - (b) Add `xenon.exit_orders` table with target/stop/contract_spec/status columns (Alembic migration + new query)
- Add: `src/xenon/db/queries/order_fills.py::get_entry_date_lookup` — new query for entry_date derivation
- Add: `web/tests/setup/seed-fixtures.ts` updates — seed PG via fastapiHarness, not JSON

- [ ] **Step 1: Verify central loader and AccountScope shape**

```bash
/opt/homebrew/bin/rg -n "def load_portfolio_payload_sync" src/xenon/utils/portfolio_loader.py
/opt/homebrew/bin/rg -n "def resolve_from_env" src/xenon/execution/account_scope.py
psql -h 192.168.50.47 -U xenon_app core_dev -c "SELECT count(*) FROM xenon.account_snapshots; SELECT count(*) FROM xenon.order_fills;"
```

Expected: both functions exist; both tables have rows. If counts are 0, run backfills first (`uv run python scripts/migrations/_2026_04_28_backfill_fills_from_trade_log.py` etc.).

- [ ] **Step 2: Add `get_entry_date_lookup` query**

Create `src/xenon/db/queries/order_fills.py` (or append to existing if file exists):

```python
from sqlalchemy import text
from xenon.execution.account_scope import AccountScope

def get_entry_date_lookup(engine, scope: AccountScope) -> dict[str, str]:
    """Returns {ticker: earliest_filled_at_date, "ticker|structure": date} for entry_date derivation.

    Replaces the data/trade_log.json scan in ib_sync.convert_to_portfolio_format.
    Bug-fix vs v1: ticker-level fallback uses MIN(filled_at), not whatever sorts last.
    """
    with engine.connect() as conn:
        # Per-ticker earliest (separate query — Codex C6 fix)
        per_ticker = conn.execute(text("""
            SELECT ticker, MIN(filled_at)::date::text AS earliest
            FROM xenon.order_fills
            WHERE broker = :broker AND account_env = :env AND broker_account = :acct
            GROUP BY ticker
        """), {"broker": scope.broker, "env": scope.account_env, "acct": scope.broker_account})
        out: dict[str, str] = {ticker: earliest for ticker, earliest in per_ticker}

        # Per (ticker, structure) earliest
        per_struct = conn.execute(text("""
            SELECT DISTINCT ON (ticker, structure)
                ticker, structure, filled_at::date::text AS earliest
            FROM xenon.order_fills
            WHERE broker = :broker AND account_env = :env AND broker_account = :acct
              AND structure IS NOT NULL
            ORDER BY ticker, structure, filled_at ASC
        """), {"broker": scope.broker, "env": scope.account_env, "acct": scope.broker_account})
        for ticker, structure, earliest in per_struct:
            out[f"{ticker}|{structure}"] = earliest
        return out
```

- [ ] **Step 3: Write failing tests for the migrated readers**

Add to `scripts/tests/test_naked_short_audit.py`:

```python
def test_naked_short_audit_reads_pg_via_central_loader(seeded_pg_with_naked_short_position, monkeypatch):
    """Audit pulls portfolio from PG via load_portfolio_payload_sync."""
    # Monkeypatch the file path to an unreadable location — proves no JSON read (Codex C13)
    bad_path = Path("/dev/null/should-not-exist/portfolio.json")
    monkeypatch.setattr("xenon.execution.naked_short_audit.PORTFOLIO_DEFAULT", bad_path, raising=False)

    from xenon.execution.naked_short_audit import audit_open_orders
    violations = audit_open_orders(scope=seeded_pg_with_naked_short_position.scope)
    assert any(v.reason_code == "NAKED_SHORT_CALL" for v in violations)
```

Add to `scripts/tests/test_combo_entry_date.py`:

```python
def test_entry_date_derivation_uses_pg_order_fills(seeded_pg_with_order_fills, monkeypatch):
    """ib_sync.convert_to_portfolio_format reads from order_fills, not data/trade_log.json."""
    monkeypatch.setattr("builtins.open", _raise_on_trade_log)  # Codex C13 — better than os.path.exists
    from xenon.execution.ib_sync import convert_to_portfolio_format
    result = convert_to_portfolio_format(account={...}, collapsed_positions=[{"ticker": "AAPL", ...}], scope=seeded_pg_with_order_fills.scope)
    aapl = next(p for p in result["positions"] if p["ticker"] == "AAPL")
    assert aapl["entry_date"] == "2026-04-15"
```

Where `_raise_on_trade_log` is:

```python
def _raise_on_trade_log(path, *args, **kwargs):
    if "trade_log.json" in str(path):
        raise AssertionError(f"Unexpected read of trade_log.json: {path}")
    return _real_open(path, *args, **kwargs)
```

- [ ] **Step 4: Run tests to verify they fail**

```bash
uv run pytest scripts/tests/test_naked_short_audit.py::test_naked_short_audit_reads_pg_via_central_loader scripts/tests/test_combo_entry_date.py::test_entry_date_derivation_uses_pg_order_fills -xvs
```

Expected: FAIL.

- [ ] **Step 5: DATA_DIR refactor in ib_sync.py — BEFORE deleting PORTFOLIO_PATH (Codex C2)**

In `src/xenon/execution/ib_sync.py`, near top:

```python
# OLD:
PORTFOLIO_PATH = Path(__file__).resolve().parents[3] / "data" / "portfolio.json"

# NEW:
DATA_DIR = Path(__file__).resolve().parents[3] / "data"
# (delete PORTFOLIO_PATH entirely)
```

Then update the 3 `PORTFOLIO_PATH.parent` references:

- Line 936: `trade_log_path = PORTFOLIO_PATH.parent / "trade_log.json"` → REMOVE entirely (replaced by PG query in Step 6)
- Line 955: `blotter_path = PORTFOLIO_PATH.parent / "blotter.json"` → REMOVE entirely (entry_date can derive from trade_log+prev portfolio without blotter)
- Line 1387: `data_dir = str(PORTFOLIO_PATH.parent)` → `data_dir = str(DATA_DIR)`

- [ ] **Step 6: Replace entry_date join with PG query**

In `convert_to_portfolio_format` (lines 919-995), replace the entire trade_log + blotter + portfolio.json join with:

```python
from xenon.db.queries.order_fills import get_entry_date_lookup
from xenon.db.engine import get_sync_engine

trade_log_dates = get_entry_date_lookup(get_sync_engine(), scope)
# blotter_dates removed entirely — trade_log is sufficient with order_fills as source
# prev_dates: replace with get_account_snapshots_history(scope, limit=2) — see Task A note
```

- [ ] **Step 7: Migrate naked_short_audit.py to central loader**

In `src/xenon/execution/naked_short_audit.py`:

```python
# OLD:
PORTFOLIO_DEFAULT = Path("data/portfolio.json")
def main():
    with open(args.portfolio) as f:
        portfolio = json.load(f)

# NEW:
from xenon.utils.portfolio_loader import load_portfolio_payload_sync
from xenon.execution.account_scope import resolve_from_env

def main():
    scope = resolve_from_env()
    portfolio = load_portfolio_payload_sync(scope=scope)
    if portfolio is None:
        raise SystemExit(f"No portfolio snapshot in xenon.account_snapshots for {scope}")
```

- [ ] **Step 8: Migrate the other portfolio.json readers (all route through central loader)**

Apply the same pattern to:

- `src/xenon/scanners/leap_iv.py:1044` (CLI `--portfolio` flag → if provided, error; otherwise call `load_portfolio_payload_sync(scope=resolve_from_env())`)
- `src/xenon/reports/portfolio_report.py:845`

- [ ] **Step 9: Migrate trade_log.json readers**

For `portfolio_report.py`: replace `load_trade_log()` with a new `load_trade_log_from_pg(scope)` that queries `xenon.order_fills` + `xenon.journal_entries` and returns the same dict shape. Same for `portfolio_attribution.py`. Remove `TRADE_LOG_PATH` constants.

For `monitor_daemon/handlers/fill_monitor.py:26`: this is a WRITER, not reader. Remove the `DEFAULT_TRADE_LOG` constant and the file-write logic; PG insert via `xenon.order_fills` already happens in the same handler.

- [ ] **Step 10: Decision point — exit_orders.py**

`ExitOrdersHandler` reads nested `exit_orders.target/stop/contract_spec/status` from `data/portfolio.json`. This data is NOT in `xenon.account_snapshots.payload` today. **Decide:**

```bash
# Option A: retire the daemon (simpler)
rm src/xenon/monitor_daemon/handlers/exit_orders.py
rm scripts/tests/test_monitor_daemon/test_exit_orders.py
# Update src/xenon/monitor_daemon/__init__.py to drop the import

# Option B: add exit_orders table (more work)
# Add to schema.py:
exit_orders = Table("exit_orders", xenon_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ticker", Text, nullable=False),
    Column("order_type", Text),  # 'target' or 'stop'
    Column("target_price", Numeric(12, 4)),
    Column("contracts", Integer),
    Column("contract_spec", JSONB),
    Column("status", Text),
    *scope_columns(),
)
# Then alembic migration + populate from current ib_sync writer + new query module
```

Default to Option A unless live exit-order monitoring is a known requirement. Document the decision in the PR body.

- [ ] **Step 11: Remove the JSON writers in ib_sync.py**

Search and delete in `src/xenon/execution/ib_sync.py`:

- The `PORTFOLIO_PATH.write_text(...)` block in `convert_to_portfolio_format`
- The trade_log.json append in `save_portfolio` (search for `trade_log_path.write_text` or similar)
- The blotter.json write
- The line 992 prev-portfolio fallback (now sourced from `get_account_snapshots_history`)

- [ ] **Step 12: Make PG write failure fatal (Codex C3)**

In `ib_sync.py:1200-1203`:

```python
# OLD:
def save_portfolio(portfolio: dict):
    try:
        _save_portfolio_to_postgres(portfolio)
    except Exception as e:
        print(f"Warning: PG write failed: {e}")

# NEW:
def save_portfolio(portfolio: dict):
    _save_portfolio_to_postgres(portfolio)  # Propagate — clean cutoff means PG is the only sink
```

Add test asserting `save_portfolio` raises (and `xenon-ib-sync` exits nonzero) when PG insert fails.

- [ ] **Step 13: Update web fixture seeding (Gemini G3)**

In `web/tests/setup/seed-fixtures.ts`:

- Remove the `data/portfolio.json`, `data/trade_log.json`, `data/blotter.json` writes (lines 7-25)
- Add equivalent PG seeding via the existing `fastapiHarness.ts` helpers

- [ ] **Step 14: Delete the files**

```bash
rm -f data/portfolio.json data/trade_log.json data/blotter.json
```

- [ ] **Step 15: Run tests to verify migration**

```bash
uv run pytest scripts/tests/test_naked_short_audit.py scripts/tests/test_combo_entry_date.py scripts/tests/test_ib_reconcile.py scripts/tests/test_portfolio_payload_query.py scripts/tests/test_portfolio_attribution.py -xvs
cd web && npm test -- portfolio
```

Expected: all green. If any test still references JSON, fix the test before continuing.

- [ ] **Step 16: Audit events.outbox emissions for the removed writes**

```bash
/opt/homebrew/bin/rg -n "outbox|emit_event|insert_event" src/xenon/execution/ib_sync.py src/xenon/monitor_daemon/handlers/ 2>&1 | head -10
```

For each outbox emission near the removed file writes, ensure the PG insert path emits an equivalent event. Add a regression test if necessary.

- [ ] **Step 17: Tighten guards (AST-based — Codex C12)**

Replace the regex in `scripts/tests/test_portfolio_json_not_read.py` with an AST walker:

```python
import ast
from pathlib import Path

class _RetiredPathReader(ast.NodeVisitor):
    """Detects file reads of retired JSON paths via Path/open/json.load patterns."""
    RETIRED_BASENAMES = {"portfolio.json", "trade_log.json", "blotter.json"}
    READ_FUNCS = {"open", "read_text", "load", "loads"}
    def __init__(self): self.offenses = []
    def visit_Call(self, node):
        # Detect: open("...portfolio.json"), Path(...).read_text(), json.load(open("...portfolio.json"))
        # plus: x = DATA_DIR / "portfolio.json"; x.read_text()
        ... # full implementation in repo
        self.generic_visit(node)
```

Apply to ALL retired basenames in one test (`test_no_source_reads_retired_json`).

- [ ] **Step 18: Run guards**

```bash
uv run pytest scripts/tests/test_portfolio_json_not_read.py scripts/tests/test_dual_write_removal.py -xvs
```

- [ ] **Step 19: Paper sync end-to-end smoke**

```bash
scripts/infra/dev.sh paper &
sleep 30
uv run xenon-ib-sync --sync
curl -s http://localhost:8321/portfolio | jq '.positions | length'
ls data/portfolio.json data/trade_log.json data/blotter.json 2>&1  # Expected: No such file
```

- [ ] **Step 20: Append to order-path-incident-history.md**

```markdown
| 2026-05-03 | A+B+C bundled | portfolio.json + trade_log.json + blotter.json migrated to PG (account_snapshots + order_fills + journal_entries); central loader pattern; DATA_DIR refactor; exit_orders daemon retired/migrated; PG write failure made fatal | Clean-cutoff PG migration | docs/plans/2026-05-03-pg-migration-clean-cutoff.md A+B+C |
```

- [ ] **Step 21: Commit**

```bash
git add -A
git commit -m "feat(pg): migrate portfolio.json + trade_log.json + blotter.json to PG (clean cutoff, bundled)"
```

---

## Task D — nav_history.jsonl + nav_history_ib.json (with schema extension)

**Files:**

- Add: `src/xenon/db/migrations/versions/<new>_add_nav_history_breakdown.py` — Alembic migration adding `cash`, `stock`, `options` columns to `xenon.nav_history`
- Modify: `src/xenon/db/schema.py` — declare new columns
- Modify: `src/xenon/reports/portfolio_performance.py:309,312,377,388,391` — replace JSONL/JSON cache reads with PG queries
- Modify: `src/xenon/execution/ib_sync.py:1085-1115` — extend NAV upsert to include cash/stock/options breakdown

- [ ] **Step 1: Add Alembic migration for breakdown columns**

```bash
uv run alembic revision -m "add cash/stock/options breakdown to nav_history"
```

In the new revision file:

```python
def upgrade():
    op.add_column("nav_history", sa.Column("cash", sa.Numeric(14, 2), schema="xenon"), schema="xenon")
    op.add_column("nav_history", sa.Column("stock", sa.Numeric(14, 2), schema="xenon"), schema="xenon")
    op.add_column("nav_history", sa.Column("options", sa.Numeric(14, 2), schema="xenon"), schema="xenon")

def downgrade():
    op.drop_column("xenon.nav_history", "cash")
    op.drop_column("xenon.nav_history", "stock")
    op.drop_column("xenon.nav_history", "options")
```

Update `schema.py::nav_history` Table definition to include new columns.

- [ ] **Step 2: Run migration against core_dev**

```bash
uv run alembic upgrade head
psql -h 192.168.50.47 -U xenon_app core_dev -c "\d xenon.nav_history"
```

Expected: shows new columns.

- [ ] **Step 3: Backfill historical breakdown from nav_history_ib.json**

One-shot script:

```python
# scripts/migrations/_2026_05_03_backfill_nav_breakdown.py
import json
from pathlib import Path
from xenon.db.engine import get_sync_engine
from xenon.db.schema import nav_history

cache = json.loads(Path("data/nav_history_ib.json").read_text())
with get_sync_engine().begin() as conn:
    for row in cache:
        conn.execute(nav_history.update()
            .where(nav_history.c.date == row["date"])
            .values(cash=row.get("cash"), stock=row.get("stock"), options=row.get("options")))
```

Run: `uv run python scripts/migrations/_2026_05_03_backfill_nav_breakdown.py`

- [ ] **Step 4-N: Standard pattern — write failing test, migrate readers, extend ib_sync writer to include breakdown, delete files, add guard, commit**

```bash
rm -f data/nav_history.jsonl data/nav_history_ib.json
git add -A
git commit -m "feat(pg): migrate nav_history files to xenon.nav_history (with cash/stock/options schema extension)"
```

---

## Task E — uw_analyze_history/ + uw_analyze_cache.json

**Files:**

- Modify: `src/xenon/api/services/uw_analyze_cache.py:61-62` (delete `_DEFAULT_CACHE_PATH` and `_DEFAULT_HISTORY_DIR`)
- Modify: throughout `uw_analyze_cache.py` — replace file I/O with PG operations against `xenon.uw_analyze_snapshots`

- [ ] **Step 1: Map every file operation in uw_analyze_cache.py**

```bash
/opt/homebrew/bin/rg -n "_DEFAULT_CACHE_PATH|_DEFAULT_HISTORY_DIR|read_text|write_text|mkdir|glob" src/xenon/api/services/uw_analyze_cache.py
```

For each, identify the PG equivalent:

- Cache lookup → `SELECT report, display, derived FROM xenon.uw_analyze_snapshots WHERE ticker=:t ORDER BY snapshot_at DESC, id DESC LIMIT 1`
- Cache write → `INSERT INTO xenon.uw_analyze_snapshots(ticker, report, display, derived, ...) VALUES (...)` (append-only — no ON CONFLICT, see Codex C8)
- History append → same insert
- History read → `SELECT ... FROM xenon.uw_analyze_snapshots WHERE ticker=:t ORDER BY snapshot_at DESC LIMIT :n`

- [ ] **Step 2: Write failing test using REAL get_or_run signature (Codex C9)**

```python
def test_uw_analyze_cache_reads_pg_not_file(seeded_pg_with_uw_snapshot, monkeypatch):
    monkeypatch.setattr("builtins.open", _raise_on_uw_files)  # Codex C13 monkeypatch pattern
    from xenon.api.services.uw_analyze_cache import UwAnalyzeCache
    cache = UwAnalyzeCache()
    # Real signature: get_or_run requires `runner` kwarg, no `scope` param
    not_called_runner = lambda *a, **kw: pytest.fail("Runner should not be called for fresh PG cache")
    snapshot = await cache.get_or_run(ticker="AAPL", runner=not_called_runner)
    assert snapshot["report"]["price"] == 200.0
```

- [ ] **Step 3: Migrate cache lookup to PG (append-only, latest by snapshot_at)**

Replace JSON cache reads with the SQL above. Use `report` column (NOT `payload` — Codex C8). Reconstruct the cache dict from `(report, display, derived, dark_pool_summary, options_flow_summary, flow_alerts, materialized_changes)` columns as needed.

- [ ] **Step 4: Migrate cache writes to PG inserts**

Append-only — every snapshot becomes a new row. Latest-wins semantics handled by `ORDER BY snapshot_at DESC, id DESC LIMIT 1` in reads.

- [ ] **Step 5: Migrate history reads/writes**

Same table, just different SELECT (with ORDER BY + LIMIT) vs INSERT.

- [ ] **Step 6: Run all uw_analyze tests**

```bash
uv run pytest scripts/tests/test_uw_analyze_routes.py scripts/tests/test_uw_analyze_loop.py -xvs
```

- [ ] **Step 7: Delete files (frees ~456MB)**

```bash
rm -f data/uw_analyze_cache.json
rm -rf data/uw_analyze_history/
du -sh data/
```

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(pg): migrate uw_analyze cache + history to xenon.uw_analyze_snapshots (clean cutoff, frees 456M)"
```

---

## Task F — uw_api_stats_history.json

**Files:**

- Modify: `src/xenon/utils/uw_api_stats.py:45` (delete `_DEFAULT_HISTORY_PATH`)
- Modify: throughout `uw_api_stats.py` — replace file I/O with `xenon.uw_api_stats` queries (table already populated by backfill)

- [ ] **Step 1: Verify PG table is populated**

```bash
psql -h 192.168.50.47 -U xenon_app core_dev -c "SELECT count(*), max(stat_at) FROM xenon.uw_api_stats"
```

Expected: count > 0 from `_2026_04_26_backfill_uw_api_stats.py`. If empty, re-run backfill.

- [ ] **Step 2: Standard pattern — failing test (with monkeypatch), migrate, delete, guard, commit**

```bash
rm -f data/uw_api_stats_history.json
git add -A
git commit -m "feat(pg): migrate uw_api_stats_history.json to xenon.uw_api_stats (clean cutoff)"
```

---

## Task G — uw_unusual_flow_log.json (collapsed — JSON fallback removal only)

**Files:**

- Modify: `src/xenon/api/services/uw_analyze_flow_tracker.py:44` — delete `_DEFAULT_PATH`
- Modify: `uw_analyze_flow_tracker.py` (~lines 382, 486 per Codex C10) — remove JSON fallback branches; tracker already uses `xenon.uw_flow_events`

**No new table needed** — `uw_flow_events` exists at `schema.py:755` and is already used by the tracker.

- [ ] **Step 1: Verify tracker uses PG**

```bash
/opt/homebrew/bin/rg -n "uw_flow_events|_DEFAULT_PATH|json\." src/xenon/api/services/uw_analyze_flow_tracker.py | head -20
```

Expected: PG operations dominate; JSON fallback is the secondary code path.

- [ ] **Step 2: Remove JSON fallback branches and `_DEFAULT_PATH`**

In `uw_analyze_flow_tracker.py`, find every `if self._path.exists(): ... json.loads(...)` and `self._path.write_text(...)` block. Remove all of them. Tracker becomes PG-only.

- [ ] **Step 3: Add failing test, monkeypatch, delete file, guard, commit**

```bash
rm -f data/uw_unusual_flow_log.json
git add -A
git commit -m "feat(pg): remove uw_unusual_flow_log.json JSON fallback (PG-only via xenon.uw_flow_events)"
```

---

## Task H — Scanner JSON share-readers (cri/scanner/gex/vcg/discover/performance)

**Files:**

- Modify: `src/xenon/scanners/repair_cri_rvol_cache.py:387` — delete `data/cri.json` write
- Modify: `src/xenon/scanners/trend/cli.py:420` — verify dead code (trend deprecated) and delete read entirely
- Modify: `src/xenon/shares/generate_vcg_share.py:4` — read from `xenon.vcg_series` via existing query
- Modify: `src/xenon/shares/generate_gex_share.py:5` — read from `xenon.gex_snapshots` via existing query
- Modify: `src/xenon/reports/performance_explainer_report.py:889` — read from PG via `/api/performance` source query

- [ ] **Step 1: Verify each scanner JSON has zero remaining writers (after removing the cri.json write)**

```bash
for f in cri scanner gex vcg discover performance; do
  echo "=== data/$f.json writers ==="
  /opt/homebrew/bin/rg -n "data/$f\.json|[\"']$f\.json[\"']" -g '*.py' src/ | grep -E "write|dump|save|open.*'w'" | head -5
done
```

Expected: zero (after removing repair_cri_rvol_cache write).

- [ ] **Step 2: Migrate share generators + explainer to PG queries**

Each is a small swap. Use existing query modules (`src/xenon/db/queries/scans.py`, `vcg.py`, `gex.py` if present — verify with `ls src/xenon/db/queries/`).

- [ ] **Step 3: Delete the files**

```bash
rm -f data/cri.json data/scanner.json data/gex.json data/vcg.json data/discover.json data/performance.json
```

- [ ] **Step 4: Run guards**

```bash
uv run pytest scripts/tests/test_dual_write_removal.py scripts/tests/test_vcg_json_not_read.py -xvs
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(pg): retire scanner JSON cache files (cri/scanner/gex/vcg/discover/performance — clean cutoff)"
```

---

## Task Z — Final verification + memory cleanup + PR ready

- [ ] **Step 1: List remaining data/ contents**

```bash
ls -la data/ && du -sh data/
```

Expected: only file-canonical entries remain:

- Config: watchlist.json, strategies.json, presets/, flex_token_config.json
- Caches: analyst_ratings_cache.json, option_close_cache.json, company_info_cache/, menthorq_cache/, price_history_cache/, seasonality_cache/
- Runtime ephemeral: locks/, service_health/
- Deferred: futu_portfolio.json, orders.duckdb

- [ ] **Step 2: Run full test suite**

```bash
uv run pytest && cd web && npm test
```

Expected: all green.

- [ ] **Step 3: Paper sync end-to-end smoke**

```bash
scripts/infra/dev.sh paper &
sleep 30
uv run xenon-ib-sync --sync
curl -s http://localhost:8321/portfolio
curl -s http://localhost:8321/uw-analyze?ticker=AAPL
curl -s http://localhost:8321/vcg
ls data/portfolio.json data/trade_log.json data/blotter.json data/uw_analyze_history 2>&1 | grep -i "no such file"  # All four should report missing
```

- [ ] **Step 4: Update CLAUDE.md (CLI contract change — Claude #11)**

Add to `src/xenon/CLAUDE.md` Commands section: "All CLI tools now require `XENON_TRADING_MODE` + `XENON_BROKER_ACCOUNT` env vars (or invoke via `dev.sh paper|live`). They no longer fall back to per-Mac data/ JSON."

- [ ] **Step 5: Delete the obsolete memory entry**

```bash
rm /Users/chenxi/.claude/projects/-Users-chenxi-projects-xenon/memory/project_postgres_migration_read_side_gap.md
# Then edit MEMORY.md to remove the line referencing it
```

- [ ] **Step 6: Mark PR ready + final commit**

```bash
git add -A
git commit -m "chore: verify PG migration clean cutoff (all guards, smoke tests passing)"
git push
gh pr ready
gh pr edit --body "$(cat <<'EOF'
## Summary
Clean-cutoff PG migration completing the W1-W5 work that scoped itself to runtime web/API surfaces.
- 8 PG-canonical artifacts retired in lockstep with reader+writer migration
- ~540M freed from data/ (uw_analyze_history alone is 450M)
- Central loader pattern (load_portfolio_payload_sync) used wherever multiple readers existed
- AST-based guards lock out future regression (no more Path() construction false negatives)
- exit_orders daemon: [Option A — retired / Option B — migrated to xenon.exit_orders]
- nav_history schema extended with cash/stock/options breakdown columns
- ib_sync save_portfolio: PG failure now fatal (no silent stale state)
- Memory entry project_postgres_migration_read_side_gap.md retired

## Test plan
- [x] All affected pytest passes
- [x] All Vitest passes (web fixture seeding migrated from JSON to PG via fastapiHarness)
- [x] Paper sync end-to-end smoke test passes
- [x] events.outbox emissions audited per migrated path
- [x] AST guards reject all retired JSON paths (including Path() construction patterns)
- [x] Backup at data.backup-2026-05-03/ retained for 7-day soak (delete after merge + 7 days)

## Follow-ups (out of scope)
- Combo wizard DuckDB → PG (separate plan)
- Futu PG migration (separate plan)
- Plan 2: container build artifact
- Plan 3: production cutover

🤖 Plan: docs/plans/2026-05-03-pg-migration-clean-cutoff.md (v2)
EOF
)"
```
