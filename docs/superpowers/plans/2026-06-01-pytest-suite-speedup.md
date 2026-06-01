# Pytest Suite Speedup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the `python-tests` CI job from ~14.5 min to under 4 min on a stock GitHub Actions runner, with zero loss of coverage and zero loss of isolation guarantees between tests.

**Architecture:** Three sequenced phases, each independently mergeable and individually verifiable. Phase 1 is a low-risk fixture refactor; Phase 2 is the semantic shift from TRUNCATE-between-tests to per-test transactional rollback; Phase 3 layers parallelization (`pytest-xdist`) on top of the cheaper-per-test fixture.

**Tech Stack:** `pytest`, `pytest-asyncio` (existing), `SQLAlchemy`, `psycopg`, Postgres 16. Adds `pytest-xdist` in Phase 3.

---

## Baseline Measurement (do this first, do not skip)

Without a real baseline, every "improvement" is a guess. Capture it before touching anything.

### Task 0: Capture baseline metrics

**Files:**

- Create: `docs/perf/2026-06-01-pytest-baseline.txt` (artifact, not committed code)

- [ ] **Step 1: Run the full suite locally with per-test durations**

```bash
uv sync --extra test
DATABASE_URL_TEST=postgresql+asyncpg://xenon_app:xenon_dev@192.168.50.47:5432/core_test \
  uv run pytest --durations=50 -q 2>&1 | tee docs/perf/2026-06-01-pytest-baseline.txt
```

Capture: total wall-clock, top 50 slowest tests, total test count, total skip/fail count.

- [ ] **Step 2: Profile fixture overhead specifically**

```bash
DATABASE_URL_TEST=postgresql+asyncpg://xenon_app:xenon_dev@192.168.50.47:5432/core_test \
  uv run pytest --durations=50 --durations-min=0.05 -q scripts/tests/test_account_scope.py 2>&1 | tail -60
```

Pick a small test file (5-10 tests) and compare per-test time vs the fixture overhead. Expected ratio today: ~80% fixture, ~20% test logic. After Phase 2 this should invert.

- [ ] **Step 3: Record CI baseline**

```bash
gh run view <latest-master-CI-id> --json jobs --jq '.jobs[] | {name, duration: (((.completedAt | fromdate) - (.startedAt | fromdate)) | tostring + "s")}' >> docs/perf/2026-06-01-pytest-baseline.txt
```

Known baseline as of 2026-06-01: `python-tests: 872s` on master CI run `26734950694`. Confirm or update.

---

## Phase 1 — Session-scoped engine + smarter truncate (low-risk, 30-50% win expected)

**Files:**

- Modify: `scripts/tests/conftest.py:55-130` (truncate fixture + helpers)
- Modify: `src/xenon/api/tests/conftest.py:14-90` (duplicate truncate fixture)
- Create: `src/xenon/_test_db.py` (shared helper extracted from both conftests)

### Task 1: Extract shared truncate helper

**Why:** The two conftests carry near-identical truncate logic — same table list, same engine-per-test pattern, same offline-tolerance flag. Extracting once means Phase 2 changes happen in one place.

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_db_fixture.py
from xenon._test_db import truncate_all_xenon_tables, get_session_engine

def test_session_engine_is_singleton():
    e1 = get_session_engine()
    e2 = get_session_engine()
    assert e1 is e2, "session engine must be cached, not recreated per call"

def test_truncate_uses_session_engine():
    engine = get_session_engine()
    truncate_all_xenon_tables(engine)
    # If we got here without raising, the helper accepted the engine.
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest scripts/tests/test_db_fixture.py -xvs`
Expected: `ImportError: cannot import name 'truncate_all_xenon_tables'`

- [ ] **Step 3: Implement `src/xenon/_test_db.py`**

```python
"""Shared Postgres test-DB helpers used by scripts/tests/ and src/xenon/api/tests/.

Caches a single engine for the pytest session — previously each truncate cycle
created and disposed a fresh engine, which dominated CI runtime for short tests.
"""
from __future__ import annotations
import os
from functools import lru_cache
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

XENON_TABLES = (
    "events.outbox",
    "xenon.order_fills",
    "xenon.order_events",
    "xenon.order_submissions",
    "xenon.wizard_protection",
    "xenon.wizard_events",
    "xenon.wizard_combo_attempts",
    "xenon.wizard_sessions",
    "xenon.uw_flow_event_ticks",
    "xenon.uw_flow_events",
    "xenon.uw_api_stats",
    "xenon.uw_analyze_flow_alerts",
    "xenon.uw_analyze_gex_strikes",
    "xenon.uw_analyze_short_volume_trend",
    "xenon.uw_analyze_snapshots",
    "xenon.positions",
    "xenon.account_snapshots",
    "xenon.journal_entries",
    "xenon.trades",
    "xenon.nav_history",
    "xenon.gex_snapshots",
    "xenon.scan_results",
    "xenon.vcg_series",
    "xenon.cri_series",
    "xenon.ticker_cache",
)

def sync_test_db_url() -> str:
    url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg://")

@lru_cache(maxsize=1)
def get_session_engine() -> Engine:
    return create_engine(
        sync_test_db_url(),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
        pool_size=2,
        max_overflow=0,
    )

@lru_cache(maxsize=1)
def is_pg_reachable() -> bool:
    try:
        with get_session_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False

def truncate_all_xenon_tables(engine: Engine | None = None) -> None:
    if not is_pg_reachable():
        return
    eng = engine or get_session_engine()
    with eng.begin() as conn:
        # Single TRUNCATE statement is ~5x faster than 26 individual ones (one
        # parser/planner pass, one lock acquisition, one WAL flush).
        conn.execute(text(f"TRUNCATE {', '.join(XENON_TABLES)} CASCADE"))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest scripts/tests/test_db_fixture.py -xvs`
Expected: both tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/_test_db.py scripts/tests/test_db_fixture.py
git commit -m "test: extract shared session-scoped PG truncate helper"
```

### Task 2: Switch scripts/tests/conftest.py to the helper

- [ ] **Step 1: Replace `_truncate_postgres_tables` and `_pg_reachable` with imports**

In `scripts/tests/conftest.py`, replace lines 27-105 (the helpers) with:

```python
from xenon._test_db import (
    get_session_engine,
    is_pg_reachable,
    sync_test_db_url,
    truncate_all_xenon_tables,
)


@pytest.fixture(autouse=True)
def _postgres_orders_test_db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", sync_test_db_url())
    try:
        import xenon.db.engine as engine_mod
        monkeypatch.setattr(engine_mod, "_sync_engine", None)
    except Exception:
        pass
    truncate_all_xenon_tables()
    yield
    truncate_all_xenon_tables()


@pytest.fixture
def pg_test_engine():
    if not is_pg_reachable():
        pytest.skip(f"PG test DB unreachable at {sync_test_db_url()}")
    return get_session_engine()
```

- [ ] **Step 2: Run the suite to confirm no regressions**

```bash
uv run python scripts/infra/dev/run_pytest_affected.py
```

Expected: all green. Capture new wall-clock vs baseline.

- [ ] **Step 3: Run the full suite and record durations**

```bash
uv run pytest --durations=20 -q 2>&1 | tail -30
```

Expected: 20-30% wall-clock reduction vs Phase 0 baseline (fixture overhead dropped from ~0.5s/test to ~0.05s/test).

- [ ] **Step 4: Commit**

```bash
git add scripts/tests/conftest.py
git commit -m "test: scripts/tests/conftest uses session-scoped engine + single TRUNCATE"
```

### Task 3: Switch src/xenon/api/tests/conftest.py to the helper

Same pattern as Task 2 against the duplicate fixture.

- [ ] **Step 1: Replace lines 14-90 of `src/xenon/api/tests/conftest.py` with imports + thinned fixture**

```python
from xenon._test_db import (
    get_session_engine,
    is_pg_reachable,
    sync_test_db_url,
    truncate_all_xenon_tables,
)


@pytest.fixture(autouse=True)
def _postgres_orders_test_db(monkeypatch):
    url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )
    monkeypatch.setenv("DATABASE_URL", url)
    try:
        import xenon.db.engine as engine_mod
        monkeypatch.setattr(engine_mod, "_sync_engine", None)
        engine_mod._engine = None
        engine_mod.init_engine(url)
    except Exception:
        pass
    truncate_all_xenon_tables()
    yield
    truncate_all_xenon_tables()
```

- [ ] **Step 2: Run the FastAPI suite specifically**

```bash
uv run pytest src/xenon/api/tests/ -q
```

Expected: all green, faster.

- [ ] **Step 3: Commit**

```bash
git add src/xenon/api/tests/conftest.py
git commit -m "test: api/tests/conftest uses shared session-scoped engine"
```

### Phase 1 stopping point

Open PR. Measure CI delta on a real master push. Target: 872s → ~550s.

---

## Phase 2 — Transactional rollback (replaces TRUNCATE entirely)

**Files:**

- Modify: `src/xenon/_test_db.py` (add txn fixture + connection injection)
- Modify: `scripts/tests/conftest.py` (swap autouse from TRUNCATE-pre/post to txn savepoint)
- Modify: `src/xenon/api/tests/conftest.py` (same swap)
- Modify: `src/xenon/db/engine.py` (only if app engine can't be redirected via monkeypatch — verify in Task 4)

**Why:** Even with Phase 1's faster single-TRUNCATE, every test still writes-then-truncates 26 tables. A test that touches one row in `xenon.trades` pays the cost of erasing 25 unrelated tables. Transactional rollback eliminates all of that: every test runs inside a transaction, every write goes into the WAL but never commits, and at test end a single `ROLLBACK` discards everything in O(1). Pattern is canonical in SQLAlchemy testing (see SQLAlchemy docs: "Joining a Session into an External Transaction").

### Task 4: Verify the app engine can be redirected to the test connection

- [ ] **Step 1: Write a failing test that proves the requirement**

```python
# scripts/tests/test_txn_rollback_isolation.py
import pytest
from sqlalchemy import text
from xenon.db import engine as engine_mod

def test_test_writes_are_invisible_after_rollback(pg_session):
    """A row inserted inside a test must NOT be visible after the test ends."""
    pg_session.execute(text(
        "INSERT INTO xenon.ticker_cache (ticker, payload) VALUES ('TEST', '{}'::jsonb)"
    ))
    # After this test ends, the next test must see ticker_cache empty.

def test_no_leak_from_previous_test(pg_session):
    row = pg_session.execute(
        text("SELECT count(*) FROM xenon.ticker_cache WHERE ticker='TEST'")
    ).scalar()
    assert row == 0, "previous test's INSERT leaked across the rollback boundary"

def test_app_engine_writes_use_test_connection(pg_session):
    """If the app routes through engine_mod._sync_engine to a different connection,
    rollback won't see the writes — this test guards that wiring."""
    from xenon.db.engine import get_sync_engine
    app_engine = get_sync_engine()
    with app_engine.connect() as conn:
        conn.execute(text(
            "INSERT INTO xenon.ticker_cache (ticker, payload) VALUES ('FROM_APP', '{}'::jsonb)"
        ))
        conn.commit()  # the app commits internally
    # Verify visible inside the test's txn
    seen = pg_session.execute(
        text("SELECT count(*) FROM xenon.ticker_cache WHERE ticker='FROM_APP'")
    ).scalar()
    assert seen == 1, "app's commit was not visible to test session — connection injection failed"
```

- [ ] **Step 2: Run to confirm `pg_session` doesn't exist yet**

Expected: `fixture 'pg_session' not found`.

- [ ] **Step 3: Implement the txn fixture in `_test_db.py`**

```python
# Append to src/xenon/_test_db.py

import pytest
from sqlalchemy.engine import Connection

@pytest.fixture
def pg_session():
    """Per-test connection inside a transaction that always rolls back.

    Replaces the TRUNCATE-pre/post pattern. ~10x cheaper per test:
    no schema-touching DDL, no WAL flushes, no lock acquisitions on 26 tables.
    """
    if not is_pg_reachable():
        pytest.skip(f"PG test DB unreachable at {sync_test_db_url()}")
    engine = get_session_engine()
    connection = engine.connect()
    txn = connection.begin()
    try:
        yield connection
    finally:
        txn.rollback()
        connection.close()
```

- [ ] **Step 4: Implement app-engine connection injection**

In `_test_db.py`, add a fixture that monkey-patches `xenon.db.engine.get_sync_engine` to return an engine bound to the test connection. SQLAlchemy supports `Engine.create()` from an existing connection via `Connection.execution_options(bind_arguments=...)`, or simpler: replace `engine_mod._sync_engine` with a thin wrapper whose `.connect()` returns the test connection.

```python
@pytest.fixture
def app_engine_bound_to_test(pg_session, monkeypatch):
    """Make the app's engine return the test's connection.

    Required for any test that exercises a route or CLI that opens its own
    SQLAlchemy session — without this, the route writes to a different
    connection and rollback misses them.
    """
    class _BoundEngine:
        def __init__(self, conn): self._conn = conn
        def connect(self): return self._conn  # NOTE: no .close()
        def begin(self): return self._conn.begin_nested()  # SAVEPOINT
        def dispose(self): pass

    import xenon.db.engine as engine_mod
    monkeypatch.setattr(engine_mod, "_sync_engine", _BoundEngine(pg_session))
    return pg_session
```

- [ ] **Step 5: Run to verify all three tests pass**

Expected: all green, including the cross-engine visibility test.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/_test_db.py scripts/tests/test_txn_rollback_isolation.py
git commit -m "test: add per-test transactional rollback fixture (pg_session, app_engine_bound_to_test)"
```

### Task 5: Migrate scripts/tests/conftest.py autouse from TRUNCATE to txn

- [ ] **Step 1: Replace the autouse `_postgres_orders_test_db` body**

```python
@pytest.fixture(autouse=True)
def _postgres_orders_test_db(monkeypatch, pg_session):
    """Autouse: every test now runs inside a transaction that rolls back.

    The `pg_session` fixture handles the BEGIN/ROLLBACK. We still need to
    redirect the app engine here so CLI subprocess tests can write via the
    same connection.
    """
    monkeypatch.setenv("DATABASE_URL", sync_test_db_url())
    try:
        import xenon.db.engine as engine_mod
        from xenon._test_db import _BoundEngine  # exported in Task 4
        monkeypatch.setattr(engine_mod, "_sync_engine", _BoundEngine(pg_session))
    except Exception:
        pass
    yield
```

- [ ] **Step 2: Run the affected-tests scope**

```bash
uv run python scripts/infra/dev/run_pytest_affected.py
```

If anything red: the test likely depended on cross-test committed state (it shouldn't, but check). Either fix the test or opt out via a `@pytest.mark.committed_db` marker that swaps in the old TRUNCATE fixture.

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/conftest.py
git commit -m "test: scripts/tests autouse switches from TRUNCATE to txn rollback"
```

### Task 6: Migrate src/xenon/api/tests/conftest.py the same way

- [ ] **Step 1: Same change applied to the duplicate fixture**
- [ ] **Step 2: Run `uv run pytest src/xenon/api/tests/ -q`**
- [ ] **Step 3: Commit**

### Task 7: Handle subprocess-CLI tests that fork a fresh interpreter

**Why this is its own task:** A handful of tests spawn `uv run xenon-foo` subprocesses (e.g. `test_xenon_blotter_history_subprocess`). A forked Python sees a fresh `engine_mod._sync_engine` and gets its OWN connection — outside the test's transaction. Writes from those subprocesses won't roll back.

- [ ] **Step 1: Inventory the subprocess tests**

```bash
grep -rn "subprocess\." scripts/tests/ | grep -v ".pyc" | cut -d: -f1 | sort -u
```

For each, decide:

- **Read-only subprocess** → fine, leave alone.
- **Writes via the same DB the test reads** → mark with `@pytest.mark.committed_db` and keep TRUNCATE for those.

- [ ] **Step 2: Implement the marker-based opt-out**

```python
# In scripts/tests/conftest.py
@pytest.fixture(autouse=True)
def _postgres_orders_test_db(request, monkeypatch, pg_session):
    if request.node.get_closest_marker("committed_db"):
        # Legacy path: TRUNCATE pre+post, no txn isolation.
        truncate_all_xenon_tables()
        monkeypatch.setenv("DATABASE_URL", sync_test_db_url())
        yield
        truncate_all_xenon_tables()
        return
    # New path: txn rollback (default).
    monkeypatch.setenv("DATABASE_URL", sync_test_db_url())
    # ...as in Task 5
    yield
```

Register the marker in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
markers = [
    "e2e: end-to-end tests requiring live external services",
    "committed_db: needs row-level commits visible to subprocess CLIs; uses TRUNCATE",
]
```

- [ ] **Step 3: Mark the inventoried tests, run them, commit**

### Phase 2 stopping point

Open PR. Measure CI delta. Target: ~550s → ~250s.

---

## Phase 3 — pytest-xdist parallelization (multiplier on Phase 2)

**Files:**

- Modify: `pyproject.toml` (add `pytest-xdist` to `[project.optional-dependencies].test`)
- Modify: `src/xenon/_test_db.py` (per-worker DB selection)
- Modify: `.github/workflows/ci.yml` (`python-tests:` job — pre-create per-worker DBs)
- Modify: `scripts/infra/dev/run_pytest_affected.py` (pass `-n auto` for local runs)

### Task 8: Add pytest-xdist + per-worker DB convention

- [ ] **Step 1: Add the dep**

```bash
uv add --dev pytest-xdist
git add pyproject.toml uv.lock
git commit -m "test: add pytest-xdist for parallel test execution"
```

- [ ] **Step 2: Teach `_test_db.py` about `worker_id`**

```python
# src/xenon/_test_db.py
def sync_test_db_url(worker_id: str | None = None) -> str:
    base = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )
    if worker_id and worker_id != "master":
        # pytest-xdist worker → its own database to avoid txn-level contention
        # (multiple txns hammering the same tables = lock contention even with
        # SAVEPOINTs).
        base = base.replace("/xenon_test", f"/xenon_test_{worker_id}")
    return base.replace("postgresql+asyncpg://", "postgresql+psycopg://")

@pytest.fixture(scope="session")
def _worker_db_url(worker_id):
    return sync_test_db_url(worker_id)
```

- [ ] **Step 3: Pre-create per-worker DBs in CI**

In `.github/workflows/ci.yml`'s `python-tests:` step:

```yaml
- name: Create per-worker test databases
  run: |
    for i in gw0 gw1 gw2 gw3; do
      PGPASSWORD=xenon_dev psql -h localhost -U xenon_app -d xenon_test \
        -c "CREATE DATABASE xenon_test_$i WITH TEMPLATE xenon_test"
    done
  env:
    PGPASSWORD: xenon_dev
```

Then update the test command:

```yaml
- run: uv run pytest -n 4 --dist loadgroup
```

`--dist loadgroup` keeps tests in the same `xdist_group` together — useful for the autouse fixture that mutates `xenon.api.trading_mode` via `importlib.reload`.

- [ ] **Step 4: Verify local parallel run**

```bash
uv run pytest -n 4 -q
```

Expected: ~4x speedup on Phase 2 numbers. Watch for failures that only surface under parallelism (test ordering assumptions, fixture state leaks).

- [ ] **Step 5: Commit**

### Phase 3 stopping point

Open PR. Measure final CI delta. Target: ~250s → ~80s (with 4-way parallelism on the 4-core GH runner; the test setup overhead now amortizes over 4 workers).

### Phase 3 implementation notes (2026-06-02)

Two things diverged from the original plan:

1. **Per-worker DB creation is in `src/xenon/_test_db.py`, not the CI workflow.**
   A session-scoped autouse fixture (`_ensure_worker_db`) DROPs and CREATEs
   `xenon_test_<wid>` from `TEMPLATE xenon_test` on every worker startup.
   Putting it in the fixture (vs `psql` in CI yaml) gives one code path for
   both CI and local — and the autouse handles cleanup if a prior session
   crashed (`WITH (FORCE)` on Postgres 14+).
2. **Local dev needs `xenon_app` to have CREATEDB. If it doesn't, the
   fixture catches `InsufficientPrivilege` and flips `_WORKER_DB_DISABLED`
   so all xdist workers fall back to the master `xenon_test`.** This keeps
   the suite runnable locally but loses isolation (workers contend on
   committed-state and async-path writes — saw ~127 races out of ~1700
   tests on the dev box). CI is unaffected because the postgres-image
   `xenon_app` is superuser.

   Local users who want full parallelism: `psql ... -c "ALTER ROLE
xenon_app CREATEDB"` against the dev PG. Otherwise stay on serial.

3. **`scripts/infra/dev/run_pytest_affected.py` is NOT changed to `-n auto`.**
   With the fallback in effect, default-parallel local would regress vs
   Phase 2 serial. Users opt in explicitly via `uv run pytest -n auto` (or
   `... -- -n auto` through the affected runner).

4. **`--dist loadgroup` deferred.** The original plan suggested it to keep
   `importlib.reload`-based tests on the same worker, but each xdist worker
   is its own Python process — `xenon.api.trading_mode` reloads don't leak
   across workers. Default `--dist load` is fine.

---

## Verification gates (run after every phase)

1. **Test count unchanged**: `uv run pytest --collect-only -q | wc -l` matches baseline.
2. **No new skips**: compare `pytest -q` skip counts vs baseline. Phase 2 in particular can mask isolation bugs as silent passes if writes silently roll back where the test expected to persist.
3. **CI green on master push** for that phase's PR.
4. **Nightly Playwright run** still passes (catches user-flow regressions tests miss).

## Risks and mitigations

| Risk                                                                            | Mitigation                                                                                                      |
| ------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Phase 2 hides bugs where a test relied on committed cross-test state            | Marker `committed_db` opts back into legacy TRUNCATE; Task 7 inventory catches the suspects                     |
| App-engine connection injection fails for async routes                          | Defer Phase 2 for `src/xenon/api/tests/` until async-engine binding is verified — keep Phase 1 there as the win |
| Phase 3 surfaces fixture-state leaks (autouse reloads `xenon.api.trading_mode`) | `--dist loadgroup` + explicit `@pytest.mark.xdist_group` on the affected files                                  |
| Per-worker DB creation slows CI startup                                         | Use `WITH TEMPLATE xenon_test` (instant clone) + parallel `psql` calls                                          |

## Out of scope (deferred)

- **`pytest-testmon`** (selective re-run based on code-coverage cache): adds CI cache management complexity; revisit if Phase 3 still feels slow.
- **Splitting `python-tests` into `python-unit` + `python-integration` jobs**: cleaner reporting, no net wall-clock win without parallelism inside each.
- **Async-engine binding for FastAPI route tests**: the autouse fixture already monkeypatches `engine_mod._engine = None; init_engine(url)`, which gives correct results; making it use the test transaction is a Phase 2.1 follow-up.

## Self-review notes

- Phase 1 is purely a refactor — same semantics, just one engine instead of N+1. If Phase 2 hits unexpected blockers, Phase 1 alone should land and ship ~30% of the win.
- Phase 2 changes the SEMANTIC contract: tests can no longer assume committed state from previous tests. The marker provides escape valves but I expect <5 tests to need it (they shouldn't have been relying on cross-test state anyway).
- Phase 3 only makes sense after Phase 2: parallel workers with TRUNCATE-everything fixtures fight each other for table locks. With per-test txns there's nothing to fight over.
