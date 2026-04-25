# Paper / Live Mode Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single `XENON_TRADING_MODE` env var that drives IB Gateway port selection and account-prefix verification. Order-mutating routes refuse to serve when the connected account doesn't match the declared mode.

**Architecture:** New `src/xenon/api/trading_mode.py` owns mode parsing + port mapping + prefix verification. `DEFAULT_GATEWAY_PORT` derivation in `src/xenon/clients/ib_client.py` and `src/xenon/utils/ib_connection.py` switches from raw `IB_GATEWAY_PORT` env var to `trading_mode.EXPECTED_PORT`. `server.py` lifespan calls `verify_account_prefix()` once after pool connect and stores the result on `app.state`. A small dependency `require_mode_verified` is applied to the four `/orders/*` POST routes. `/health` surfaces `trading_mode`, `account`, `mode_verified`. Old `cloud.sh` / `local.sh` are moved under `scripts/infra/docker/`; new `scripts/infra/dev.sh` becomes the primary launcher.

**Tech Stack:** Python 3.13, FastAPI, ib_insync (via `IBClient`), pytest + pytest-asyncio, `uv` for everything, bash for runner.

**Spec:** `docs/superpowers/specs/2026-04-25-paper-live-mode-switch-design.md`

---

## File Structure

| File                                              | Action                                            | Responsibility                                                                                                                                                          |
| ------------------------------------------------- | ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/xenon/api/trading_mode.py`                   | CREATE                                            | Sole owner of `MODE`, `EXPECTED_PORT`, `EXPECTED_PREFIX`, `parse_mode()`, `verify_account()`                                                                            |
| `src/xenon/clients/ib_client.py`                  | MODIFY (line 110)                                 | `DEFAULT_GATEWAY_PORT` derives from `trading_mode.EXPECTED_PORT`                                                                                                        |
| `src/xenon/utils/ib_connection.py`                | MODIFY (line 12)                                  | Same change as `ib_client.py`                                                                                                                                           |
| `src/xenon/api/server.py`                         | MODIFY (lines 240-260, 1053-1063, 1553/1785/1899) | Lifespan runs prefix guard; `/health` exposes mode fields; `require_mode_verified` dependency on `/orders/place`, `/orders/cancel`, `/orders/modify`, `/orders/refresh` |
| `src/xenon/api/tests/test_trading_mode.py`        | CREATE                                            | Unit: parse/port/prefix                                                                                                                                                 |
| `src/xenon/api/tests/test_health_trading_mode.py` | CREATE                                            | Integration: `/health` surfaces fields                                                                                                                                  |
| `src/xenon/api/tests/test_orders_mode_guard.py`   | CREATE                                            | Integration: 503 on mismatch, 200 on match                                                                                                                              |
| `scripts/infra/dev.sh`                            | CREATE                                            | Primary launcher: read mode → derive port → probe → start FastAPI + Next                                                                                                |
| `scripts/infra/docker/cloud.sh`                   | MOVE from `scripts/infra/cloud.sh`                | Archived Docker/VPS runner                                                                                                                                              |
| `scripts/infra/docker/local.sh`                   | MOVE from `scripts/infra/local.sh`                | Archived Docker runner                                                                                                                                                  |
| `scripts/infra/docker/README.md`                  | CREATE                                            | One-paragraph archival note                                                                                                                                             |
| `.env.example`                                    | MODIFY                                            | Add `XENON_TRADING_MODE=paper` block; mark `IB_GATEWAY_PORT` as ignored                                                                                                 |

---

## Task 1: `trading_mode` module — port + prefix mapping (TDD)

**Files:**

- Create: `src/xenon/api/trading_mode.py`
- Test: `src/xenon/api/tests/test_trading_mode.py`

- [ ] **Step 1: Write the failing test**

Create `src/xenon/api/tests/test_trading_mode.py`:

```python
"""Unit tests for trading_mode: parse, port mapping, prefix guard.

Each test reloads the module so module-level constants pick up the patched env.
"""
from __future__ import annotations

import importlib
import pytest


def _reload(monkeypatch, mode_value: str | None):
    if mode_value is None:
        monkeypatch.delenv("XENON_TRADING_MODE", raising=False)
    else:
        monkeypatch.setenv("XENON_TRADING_MODE", mode_value)
    import xenon.api.trading_mode as tm
    return importlib.reload(tm)


def test_parse_paper(monkeypatch):
    tm = _reload(monkeypatch, "paper")
    assert tm.MODE == "paper"
    assert tm.EXPECTED_PORT == 4002
    assert tm.EXPECTED_PREFIX == "DU"


def test_parse_live(monkeypatch):
    tm = _reload(monkeypatch, "live")
    assert tm.MODE == "live"
    assert tm.EXPECTED_PORT == 4001
    assert tm.EXPECTED_PREFIX == "U"


def test_default_is_paper_when_unset(monkeypatch):
    tm = _reload(monkeypatch, None)
    assert tm.MODE == "paper"
    assert tm.EXPECTED_PORT == 4002


def test_case_insensitive_and_trimmed(monkeypatch):
    tm = _reload(monkeypatch, "  LIVE  ")
    assert tm.MODE == "live"


def test_invalid_value_raises_at_import(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", "demo")
    import xenon.api.trading_mode as tm
    with pytest.raises(ValueError, match="XENON_TRADING_MODE"):
        importlib.reload(tm)


def test_verify_account_paper_matches(monkeypatch):
    tm = _reload(monkeypatch, "paper")
    assert tm.verify_account("DU1234567") is True


def test_verify_account_live_matches(monkeypatch):
    tm = _reload(monkeypatch, "live")
    assert tm.verify_account("U1234567") is True


def test_verify_account_paper_rejects_live(monkeypatch):
    tm = _reload(monkeypatch, "paper")
    assert tm.verify_account("U1234567") is False


def test_verify_account_live_rejects_paper(monkeypatch):
    tm = _reload(monkeypatch, "live")
    # "DU…" must NOT match live's "U" prefix — the live check must reject DU explicitly
    assert tm.verify_account("DU1234567") is False


def test_verify_account_empty_is_false(monkeypatch):
    tm = _reload(monkeypatch, "live")
    assert tm.verify_account("") is False
    assert tm.verify_account(None) is False  # type: ignore[arg-type]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest src/xenon/api/tests/test_trading_mode.py -xvs
```

Expected: FAIL with `ModuleNotFoundError: No module named 'xenon.api.trading_mode'`.

- [ ] **Step 3: Write the minimal implementation**

Create `src/xenon/api/trading_mode.py`:

```python
"""Single source of truth for paper-vs-live trading mode.

Driven by the `XENON_TRADING_MODE` env var. Owned constants:
- MODE              — "paper" | "live"
- EXPECTED_PORT     — 4002 (paper) | 4001 (live)
- EXPECTED_PREFIX   — "DU" (paper) | "U" (live, but not "DU")

`verify_account(account)` returns True iff the account string matches the
declared mode's prefix. Used by the startup guard (server.py lifespan) to
catch ".env says live but Gateway is logged in as paper" and vice versa.

Spec: docs/superpowers/specs/2026-04-25-paper-live-mode-switch-design.md
"""
from __future__ import annotations

import os
from typing import Literal

Mode = Literal["paper", "live"]

_DEFAULT_MODE: Mode = "paper"
_PORT_BY_MODE: dict[Mode, int] = {"paper": 4002, "live": 4001}
_PREFIX_BY_MODE: dict[Mode, str] = {"paper": "DU", "live": "U"}


def parse_mode(raw: str | None) -> Mode:
    """Parse and validate the mode env var. Defaults to 'paper' when unset/blank."""
    if raw is None:
        return _DEFAULT_MODE
    value = raw.strip().lower()
    if not value:
        return _DEFAULT_MODE
    if value not in _PORT_BY_MODE:
        raise ValueError(
            f"XENON_TRADING_MODE must be 'paper' or 'live', got {raw!r}"
        )
    return value  # type: ignore[return-value]


MODE: Mode = parse_mode(os.environ.get("XENON_TRADING_MODE"))
EXPECTED_PORT: int = _PORT_BY_MODE[MODE]
EXPECTED_PREFIX: str = _PREFIX_BY_MODE[MODE]


def verify_account(account: str | None) -> bool:
    """True iff `account` matches the declared mode's prefix.

    Live mode rejects 'DU…' explicitly — a bare `startswith("U")` would
    accept paper accounts since they also start with U after the D.
    """
    if not account:
        return False
    if MODE == "paper":
        return account.startswith("DU")
    # live: starts with U but NOT DU
    return account.startswith("U") and not account.startswith("DU")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest src/xenon/api/tests/test_trading_mode.py -xvs
```

Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/trading_mode.py src/xenon/api/tests/test_trading_mode.py
git commit -m "feat(api): add trading_mode module — XENON_TRADING_MODE → port + prefix"
```

---

## Task 2: Wire `DEFAULT_GATEWAY_PORT` to derive from trading_mode

**Files:**

- Modify: `src/xenon/clients/ib_client.py:109-110`
- Modify: `src/xenon/utils/ib_connection.py:11-12`
- Test: `src/xenon/api/tests/test_trading_mode.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `src/xenon/api/tests/test_trading_mode.py`:

```python
def test_default_gateway_port_follows_mode_paper(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.delenv("IB_GATEWAY_PORT", raising=False)
    import xenon.api.trading_mode as tm
    importlib.reload(tm)
    import xenon.clients.ib_client as ibc
    importlib.reload(ibc)
    assert ibc.DEFAULT_GATEWAY_PORT == 4002


def test_default_gateway_port_follows_mode_live(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.delenv("IB_GATEWAY_PORT", raising=False)
    import xenon.api.trading_mode as tm
    importlib.reload(tm)
    import xenon.clients.ib_client as ibc
    importlib.reload(ibc)
    assert ibc.DEFAULT_GATEWAY_PORT == 4001


def test_ib_gateway_port_env_var_is_ignored(monkeypatch):
    """Spec: IB_GATEWAY_PORT is no longer consulted; mode wins."""
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("IB_GATEWAY_PORT", "9999")
    import xenon.api.trading_mode as tm
    importlib.reload(tm)
    import xenon.clients.ib_client as ibc
    importlib.reload(ibc)
    assert ibc.DEFAULT_GATEWAY_PORT == 4002  # mode wins, env var ignored
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest src/xenon/api/tests/test_trading_mode.py -xvs -k default_gateway_port
```

Expected: FAIL — `ibc.DEFAULT_GATEWAY_PORT == 4001` (still reading `IB_GATEWAY_PORT` env var with default 4001).

- [ ] **Step 3: Patch `ib_client.py`**

Edit `src/xenon/clients/ib_client.py` lines 109-110:

```python
# OLD:
DEFAULT_HOST = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
DEFAULT_GATEWAY_PORT = int(os.environ.get("IB_GATEWAY_PORT", "4001"))

# NEW:
from xenon.api.trading_mode import EXPECTED_PORT as _EXPECTED_PORT

DEFAULT_HOST = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
DEFAULT_GATEWAY_PORT = _EXPECTED_PORT
```

- [ ] **Step 4: Patch `utils/ib_connection.py`**

Edit `src/xenon/utils/ib_connection.py` lines 11-12 the same way:

```python
# OLD:
DEFAULT_GATEWAY_PORT = int(os.environ.get("IB_GATEWAY_PORT", "4001"))

# NEW:
from xenon.api.trading_mode import EXPECTED_PORT as _EXPECTED_PORT
DEFAULT_GATEWAY_PORT = _EXPECTED_PORT
```

(Keep the surrounding `os` import if other code in the file uses it.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest src/xenon/api/tests/test_trading_mode.py -xvs
```

Expected: PASS (13 tests).

- [ ] **Step 6: Run the affected-tests sweep to catch import-cycle regressions**

```bash
uv run python scripts/infra/dev/run_pytest_affected.py
```

Expected: PASS. If anything fails, investigate before proceeding — the import order between `xenon.api.trading_mode` and `xenon.clients.ib_client` matters.

- [ ] **Step 7: Commit**

```bash
git add src/xenon/clients/ib_client.py src/xenon/utils/ib_connection.py src/xenon/api/tests/test_trading_mode.py
git commit -m "feat(ib): derive DEFAULT_GATEWAY_PORT from XENON_TRADING_MODE"
```

---

## Task 3: Account-prefix guard in lifespan + `/health` surface (TDD)

**Files:**

- Modify: `src/xenon/api/server.py` (lifespan body around line 260; `/health` at line 1053)
- Test: `src/xenon/api/tests/test_health_trading_mode.py`

- [ ] **Step 1: Write the failing test**

Create `src/xenon/api/tests/test_health_trading_mode.py`:

```python
"""/health surfaces trading_mode, account, mode_verified."""
from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_in_test_mode(monkeypatch):
    """Boot the FastAPI app in test mode with mode=paper and a fake managed account."""
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    # Force re-import so module-level constants pick up the env
    import xenon.api.trading_mode as tm
    importlib.reload(tm)
    import xenon.api.server as server
    importlib.reload(server)
    # Stub the managed-account lookup the lifespan would call
    server._FAKE_MANAGED_ACCOUNT = "DU9999999"  # set by patch below
    monkeypatch.setattr(
        server, "_get_managed_account_for_health", lambda: "DU9999999"
    )
    with TestClient(server.app) as c:
        yield c


def test_health_includes_trading_mode_fields(client_in_test_mode):
    r = client_in_test_mode.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["trading_mode"] == "paper"
    assert body["account"] == "DU9999999"
    assert body["mode_verified"] is True


def test_health_mode_verified_false_on_mismatch(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    import xenon.api.trading_mode as tm
    importlib.reload(tm)
    import xenon.api.server as server
    importlib.reload(server)
    monkeypatch.setattr(
        server, "_get_managed_account_for_health", lambda: "DU9999999"
    )
    with TestClient(server.app) as c:
        r = c.get("/health")
        body = r.json()
        assert body["trading_mode"] == "live"
        assert body["account"] == "DU9999999"
        assert body["mode_verified"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest src/xenon/api/tests/test_health_trading_mode.py -xvs
```

Expected: FAIL — `KeyError: 'trading_mode'` or `AttributeError: module ... has no attribute '_get_managed_account_for_health'`.

- [ ] **Step 3: Add the helper + lifespan guard in `server.py`**

In `src/xenon/api/server.py`, near the top of the module add (just below the existing `from xenon.api.ib_pool import IBPool` import block):

```python
from xenon.api import trading_mode
```

Add a module-level helper (place it just above `lifespan`, near line 238):

```python
def _get_managed_account_for_health() -> str:
    """Return the first managedAccount from the IB pool's sync client.

    Returns "" when the pool isn't connected (test mode, Gateway down, etc.).
    Pulled out as a module-level function so tests can monkeypatch it
    without booting a real IB connection.
    """
    if ib_pool is None:
        return ""
    client = ib_pool.get("sync")
    if client is None:
        return ""
    try:
        accounts = client.ib.managedAccounts()
    except Exception:  # noqa: BLE001
        return ""
    return accounts[0] if accounts else ""
```

Inside `lifespan` (after `pool_status = await ib_pool.connect_all()` around line 260), add:

```python
    # Trading-mode prefix guard — verify Gateway login matches XENON_TRADING_MODE.
    # Failure does not abort startup; it sets app.state.mode_verified=False
    # and the order routes refuse to serve until .env + Gateway are aligned.
    account = await asyncio.to_thread(_get_managed_account_for_health)
    verified = trading_mode.verify_account(account)
    app.state.trading_mode = trading_mode.MODE
    app.state.account = account
    app.state.mode_verified = verified
    if not verified:
        logger.error(
            "TRADING MODE MISMATCH — declared=%s, account=%r (expected prefix %r). "
            "Order routes will return 503 until .env XENON_TRADING_MODE matches "
            "the IB Gateway login.",
            trading_mode.MODE,
            account,
            trading_mode.EXPECTED_PREFIX,
        )
    else:
        logger.info("Trading mode verified: %s account=%s", trading_mode.MODE, account)
```

Inside the `_is_test_mode()` branch of `lifespan` (just before `yield`, around line 251), set the same `app.state` fields so `/health` works in tests:

```python
        app.state.trading_mode = trading_mode.MODE
        # Tests monkeypatch _get_managed_account_for_health; honor that.
        account = _get_managed_account_for_health()
        app.state.account = account
        app.state.mode_verified = trading_mode.verify_account(account)
```

Update `/health` (around line 1053):

```python
@app.get("/health")
async def health():
    gw = await check_ib_gateway()
    return {
        "status": "ok",
        "test_mode": _is_test_mode(),
        "ib_gateway": gw,
        "ib_pool": ib_pool.status() if ib_pool else {},
        "uw": uw_available,
        "futu": _compute_futu_health(),
        "trading_mode": getattr(app.state, "trading_mode", trading_mode.MODE),
        "account": getattr(app.state, "account", ""),
        "mode_verified": getattr(app.state, "mode_verified", False),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest src/xenon/api/tests/test_health_trading_mode.py -xvs
```

Expected: PASS (2 tests).

- [ ] **Step 5: Confirm no regressions in existing health/lifespan tests**

```bash
uv run pytest src/xenon/api/tests/ -xvs -k "health or lifespan or rehydrate or server"
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/api/server.py src/xenon/api/tests/test_health_trading_mode.py
git commit -m "feat(api): account-prefix guard in lifespan; /health exposes trading_mode"
```

---

## Task 4: Block `/orders/*` POST routes when `mode_verified=False` (TDD)

**Files:**

- Modify: `src/xenon/api/server.py` (add `require_mode_verified` dependency; apply to `/orders/refresh`, `/orders/place`, `/orders/cancel`, `/orders/modify` at lines 1349, 1553, 1785, 1899)
- Test: `src/xenon/api/tests/test_orders_mode_guard.py`

- [ ] **Step 1: Write the failing test**

Create `src/xenon/api/tests/test_orders_mode_guard.py`:

```python
"""Order routes return 503 when trading mode is unverified."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_with_mismatch(monkeypatch):
    """Boot in test mode with mode=live but a paper-prefixed account."""
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    import xenon.api.trading_mode as tm
    importlib.reload(tm)
    import xenon.api.server as server
    importlib.reload(server)
    monkeypatch.setattr(
        server, "_get_managed_account_for_health", lambda: "DU1111111"
    )
    with TestClient(server.app) as c:
        yield c


@pytest.fixture
def client_with_match(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    import xenon.api.trading_mode as tm
    importlib.reload(tm)
    import xenon.api.server as server
    importlib.reload(server)
    monkeypatch.setattr(
        server, "_get_managed_account_for_health", lambda: "DU1111111"
    )
    with TestClient(server.app) as c:
        yield c


@pytest.mark.parametrize("path", [
    "/orders/refresh",
    "/orders/place",
    "/orders/cancel",
    "/orders/modify",
])
def test_orders_routes_blocked_on_mismatch(client_with_mismatch, path):
    r = client_with_mismatch.post(path, json={})
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert "trading mode" in detail.lower()
    assert "live" in detail.lower()  # declared mode named
    assert "DU1111111" in detail  # observed account named


def test_orders_refresh_passes_when_verified(client_with_match):
    """Sanity: when mode matches, the guard does not block.

    /orders/refresh in test_mode short-circuits to {"status": "ok", "orders": []}
    so we get past the guard without needing real IB.
    """
    r = client_with_match.post("/orders/refresh")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "orders": []}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest src/xenon/api/tests/test_orders_mode_guard.py -xvs
```

Expected: FAIL — order routes don't currently return 503 on mismatch.

- [ ] **Step 3: Add the dependency in `server.py`**

In `src/xenon/api/server.py`, add the dependency function near the top of the route section (just above `@app.post("/orders/refresh")` at line 1349):

```python
def require_mode_verified(request: Request) -> None:
    """Reject order-mutating requests when trading mode is unverified.

    The check reads app.state populated by the lifespan guard. Returns 503
    with a body that names both the declared mode and the observed account
    so the operator can fix the mismatch (edit .env or relog Gateway).
    """
    state = request.app.state
    verified = getattr(state, "mode_verified", False)
    if verified:
        return
    declared = getattr(state, "trading_mode", trading_mode.MODE)
    observed = getattr(state, "account", "")
    raise HTTPException(
        status_code=503,
        detail=(
            f"Trading mode mismatch: .env declares XENON_TRADING_MODE={declared!r} "
            f"but IB Gateway is logged in as account={observed!r} "
            f"(expected prefix {trading_mode.EXPECTED_PREFIX!r}). "
            f"Fix: align .env with the Gateway login and restart."
        ),
    )
```

Make sure `Request` is imported from `fastapi` at the top of the file (it already imports `Depends`, `FastAPI`, `HTTPException`, etc. — add `Request` to the import line if not present).

Apply the dependency to all four order-mutating routes by editing the decorator lines:

```python
# Line ~1349
@app.post("/orders/refresh", dependencies=[Depends(require_mode_verified)])

# Line ~1553
@app.post("/orders/place", dependencies=[Depends(require_mode_verified)])

# Line ~1785
@app.post("/orders/cancel", dependencies=[Depends(require_mode_verified)])

# Line ~1899
@app.post("/orders/modify", dependencies=[Depends(require_mode_verified)])
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest src/xenon/api/tests/test_orders_mode_guard.py -xvs
```

Expected: PASS (5 tests).

- [ ] **Step 5: Confirm no regressions in the existing orders-routes tests**

```bash
uv run pytest src/xenon/api/tests/test_orders_routes_failures.py -xvs
```

Expected: PASS. If this fixture boots `TestClient(app)` without the new mode env, the existing tests will hit the guard. If they fail, add `XENON_TRADING_MODE=paper` + a managed-account monkeypatch via the autouse `_isolate_orders_db` fixture in `conftest.py` — extend that fixture to also stub `_get_managed_account_for_health` returning `"DU0000000"`.

If you need to extend `conftest.py`:

```python
@pytest.fixture(autouse=True)
def _trading_mode_paper_default(monkeypatch):
    """Default every test to paper + a paper-prefixed account so the lifespan
    guard verifies. Tests that care about mismatch override these."""
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    # Stub managed-account lookup *before* server import; tests that reload
    # server.py will see the patched env, and the lifespan helper will be
    # patched by per-test fixtures or by the line below if server is already
    # imported.
    try:
        import xenon.api.server as server
        monkeypatch.setattr(
            server, "_get_managed_account_for_health", lambda: "DU0000000",
            raising=False,
        )
    except Exception:
        pass
    yield
```

- [ ] **Step 6: Run the full api test suite**

```bash
uv run pytest src/xenon/api/tests/ -xvs
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/xenon/api/server.py src/xenon/api/tests/test_orders_mode_guard.py src/xenon/api/tests/conftest.py
git commit -m "feat(api): block /orders/* routes with 503 when trading mode unverified"
```

---

## Task 5: Move `cloud.sh` + `local.sh` under `scripts/infra/docker/`

**Files:**

- Move: `scripts/infra/cloud.sh` → `scripts/infra/docker/cloud.sh`
- Move: `scripts/infra/local.sh` → `scripts/infra/docker/local.sh`
- Create: `scripts/infra/docker/README.md`

- [ ] **Step 1: Make the directory and move with `git mv` (preserves history)**

```bash
mkdir -p scripts/infra/docker
git mv scripts/infra/cloud.sh scripts/infra/docker/cloud.sh
git mv scripts/infra/local.sh scripts/infra/docker/local.sh
```

- [ ] **Step 2: Create the README**

Create `scripts/infra/docker/README.md`:

```markdown
# Archived Docker / VPS runners

These scripts are the legacy launchers for two paths that are no longer the active dev story:

- `cloud.sh` — runs local services against the Hetzner VPS IB Gateway over Tailscale.
- `local.sh` — stops the VPS gateway and starts a local Docker IB Gateway.

The active dev launcher is `scripts/infra/dev.sh`, which talks to a native IB Gateway
(launchd) on the same machine and derives the port from `XENON_TRADING_MODE`.

Use the scripts in this directory only when you need the Docker or VPS fallback —
e.g. CI on a host without a native Gateway, or a temporary VPS-backed shift. They have
not been updated for the `XENON_TRADING_MODE` switch; if you use them, set
`XENON_TRADING_MODE` in `.env` to match the Gateway's `TRADING_MODE`.
```

- [ ] **Step 3: Grep for any references to the old paths and update**

```bash
grep -rn "scripts/infra/cloud.sh\|scripts/infra/local.sh" --include="*.md" --include="*.sh" --include="*.py" --include="*.ts" --include="*.tsx" --include="*.yml" --include="*.yaml" .
```

Update each hit (most likely candidates: `CLAUDE.md`, the docker startup checklist, runbooks). If a hit references the script as a startup step, change the path; if it describes when to use it, mention it's the archived path.

- [ ] **Step 4: Commit the move**

```bash
git add scripts/infra/docker/
git commit -m "chore(infra): move cloud.sh + local.sh under scripts/infra/docker/ (archived)"
```

---

## Task 6: New `scripts/infra/dev.sh` — mode-aware launcher

**Files:**

- Create: `scripts/infra/dev.sh`

- [ ] **Step 1: Write the script**

Create `scripts/infra/dev.sh` with executable bit:

```bash
#!/usr/bin/env bash
# dev.sh — primary dev launcher. Reads XENON_TRADING_MODE, derives the IB
# Gateway port (4001 live, 4002 paper), probes that the native Gateway is
# logged in for that mode, then starts FastAPI + Next dev.
#
# Usage:
#   ./scripts/infra/dev.sh              # mode from .env (default: paper)
#   ./scripts/infra/dev.sh paper        # override per-invocation
#   ./scripts/infra/dev.sh live
#
# Does NOT edit .env. Does NOT start IB Gateway — that is manual for v1.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

log_info() { printf '\033[32m[dev.sh]\033[0m %s\n' "$*"; }
log_warn() { printf '\033[33m[dev.sh]\033[0m %s\n' "$*" >&2; }
log_err()  { printf '\033[31m[dev.sh]\033[0m %s\n' "$*" >&2; }

# 1. Resolve mode: arg > .env > default paper
MODE="${1:-}"
if [[ -z "$MODE" && -f "$ENV_FILE" ]]; then
  MODE="$(grep -E '^XENON_TRADING_MODE=' "$ENV_FILE" | tail -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs || true)"
fi
MODE="${MODE:-paper}"
MODE="$(echo "$MODE" | tr '[:upper:]' '[:lower:]' | xargs)"

case "$MODE" in
  paper) PORT=4002 ;;
  live)  PORT=4001 ;;
  *)
    log_err "Invalid mode '$MODE' — must be 'paper' or 'live'."
    exit 2
    ;;
esac

log_info "Trading mode: $MODE  →  IB Gateway port $PORT"

# 2. Probe the Gateway port. Bail out with a clear message if not listening.
if ! (exec 3<>/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null; then
  log_err "IB Gateway is NOT listening on 127.0.0.1:$PORT."
  log_err "Launch IB Gateway in '$MODE' mode (Login → ${MODE^^}) and re-run."
  exit 3
fi
exec 3<&- 2>/dev/null || true
exec 3>&- 2>/dev/null || true
log_info "IB Gateway port $PORT is listening."

# 3. Start FastAPI + Next dev. Replicate whatever cloud.sh / local.sh did at
# the end. The simplest concrete commands match the existing dev workflow:
#   - FastAPI:  uv run uvicorn xenon.api.server:app --host 127.0.0.1 --port 8321 --reload
#   - Next.js:  cd web && npm run dev
#
# The Python service is run in the foreground so Ctrl-C tears it down; Next
# is started in the background and killed on exit via trap.

cleanup() {
  if [[ -n "${NEXT_PID:-}" ]] && kill -0 "$NEXT_PID" 2>/dev/null; then
    log_info "Stopping Next dev (pid $NEXT_PID)…"
    kill "$NEXT_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

log_info "Starting Next dev (background)…"
( cd "$REPO_ROOT/web" && npm run dev ) &
NEXT_PID=$!

log_info "Starting FastAPI on 127.0.0.1:8321 (foreground)…"
cd "$REPO_ROOT"
exec uv run uvicorn xenon.api.server:app --host 127.0.0.1 --port 8321 --reload
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x scripts/infra/dev.sh
```

- [ ] **Step 3: Smoke test argument parsing without launching the stack**

Run a deliberate fail-fast invocation that exits before starting services. With no Gateway running:

```bash
./scripts/infra/dev.sh paper
```

Expected: exit code 3 with `IB Gateway is NOT listening on 127.0.0.1:4002.`

```bash
./scripts/infra/dev.sh frob
```

Expected: exit code 2 with `Invalid mode 'frob'`.

- [ ] **Step 4: Commit**

```bash
git add scripts/infra/dev.sh
git commit -m "feat(infra): scripts/infra/dev.sh — mode-aware native-Gateway launcher"
```

---

## Task 7: Update `.env.example`

**Files:**

- Modify: `.env.example`

- [ ] **Step 1: Edit the file**

Replace the existing IB Gateway block at the top of `.env.example` (lines 1-5):

```
# Trading mode — paper (port 4002) or live (port 4001). Default: paper.
# Drives IB Gateway port selection AND the startup account-prefix guard.
# IB_GATEWAY_PORT is no longer consulted; this var is the only switch.
XENON_TRADING_MODE=paper

# IB Gateway connection
# IB_GATEWAY_HOST=127.0.0.1            # Default; override only for cloud/VPS fallback
# IB_GATEWAY_MODE=launchd              # "launchd" (default), or "docker"/"cloud" via scripts/infra/docker/
```

Leave the rest of the file unchanged.

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "docs(env): document XENON_TRADING_MODE in .env.example"
```

---

## Task 8: Final verification

- [ ] **Step 1: Run the full Python test suite**

```bash
uv run pytest
```

Expected: PASS. If anything new breaks, the most likely cause is a test that boots `TestClient(app)` without the env stubs added in Task 4 Step 5 — extend `conftest.py` per that step.

- [ ] **Step 2: Type check the web side (no changes expected, but the API contract changed)**

```bash
cd web && npm run typecheck
```

Expected: PASS.

- [ ] **Step 3: Manual /health check (skip if no IB Gateway running locally)**

Walk through the operator manual end-to-end against a real Gateway: see the **Manual — switching between paper and live** section in `docs/superpowers/specs/2026-04-25-paper-live-mode-switch-design.md`. At minimum:

1. Start in the matching mode and confirm `/health` returns `mode_verified: true`.
2. Edit `.env` to the _other_ mode without re-logging Gateway, restart `dev.sh`, confirm `/health` returns `mode_verified: false` and `POST /orders/refresh` returns 503 with the mismatch detail.
3. Re-log Gateway to align, restart, confirm green again.

- [ ] **Step 4: Spec coverage check**

Skim `docs/superpowers/specs/2026-04-25-paper-live-mode-switch-design.md` and confirm every numbered item is shipped:

- Single source of truth env var → Task 1
- Mode→port mapping → Task 2
- Account-prefix guard at startup → Task 3
- `/health` surface → Task 3
- Order-route 503 on mismatch → Task 4
- Runner: archive old, add new → Tasks 5 + 6
- `.env.example` documentation → Task 7
- Tests cover all of the above → Tasks 1, 3, 4

If any item is missing, add a follow-up task. If everything's there, you're done.

---

## Plan Self-Review Notes

- **Spec coverage:** every requirement in the design doc has a task. Out-of-scope items (UI badge, removing cloud/docker mode branches, auto-launching Gateway) are not implemented and not tasked — correct per spec.
- **Type consistency:** `MODE`, `EXPECTED_PORT`, `EXPECTED_PREFIX`, `verify_account()`, `parse_mode()`, `_get_managed_account_for_health()`, `require_mode_verified()` — names used identically across all tasks.
- **Placeholder scan:** no TBD/TODO/"add appropriate handling" — every code step shows code; every command shows expected output.
- **Soft spot:** Task 4 Step 5 anticipates that the existing `test_orders_routes_failures.py` may now hit the guard. The conftest extension is provided inline; if the existing fixtures already isolate this differently, adapt rather than duplicate.
