# Read-Only Query API over Tailscale/LAN — Auth Hardening Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let scripts query the prod xenon API's read-only IB/Futu surfaces (portfolio, orders, blotter, journal, futu positions, trades, performance) over Tailscale/LAN via an `X-API-Key`, while closing the current hole where the prod API is reachable with **no authentication at all** — including live order placement.

**Architecture:** The prod macmini Docker API (`100.66.147.98:8321`) is already network-reachable (`0.0.0.0:8321` published) but its auth middleware passes _all_ requests through because `CLERK_JWKS_URL` is unset in the container (`server.py:648`). We make auth **fail-closed**: every non-exempt request is denied unless it proves identity (localhost, the web UI's internal token, a scoped API key, or Clerk JWT). A misconfigured/unloaded prod env therefore denies rather than exposes. Dev/test open access requires an explicit `XENON_AUTH_ALLOW_DEV_OPEN=1` opt-in set only by dev launchers and the test harness. The web UI keeps full read+write access via a shared `X-Internal-Token` attached on every web→api call. External callers get a read-only `XENON_QUERY_API_KEY` scoped to GET-only query paths; write/sync paths are never in the allowlist.

**Tech Stack:** Python 3.13 / FastAPI / `uv` / pytest (Python); Next.js / TypeScript / Vitest (web); Docker Compose + GHCR (prod deploy).

---

## Context an implementer needs (read before starting)

- **Auth today** (`src/xenon/api/auth.py`, `src/xenon/api/server.py:642-671`): one HTTP middleware gates every non-exempt path. It short-circuits to "pass" when `CLERK_JWKS_URL` is unset, then bypasses `127.0.0.1`/`::1`, then tries `verify_api_key` (scoped to `/historical/*`, `/contract/qualify`), then Clerk JWT.
- **Why writes are exposed today:** the single middleware gates reads and writes uniformly; with the Clerk-unset bypass, `POST /orders/place|cancel|modify` are as open as `GET /portfolio`. Verified empirically: `curl http://100.66.147.98:8321/portfolio` (no token) returns live positions.
- **Why we don't just enable Clerk:** `xenonFetch` (`web/lib/xenonApi.ts:21-49`) _can_ attach a Clerk JWT but **no prod web route passes one** — the prod UI works only because api-layer auth is off. The internal-token approach avoids touching every route.
- **web→api is NOT a single chokepoint.** Most of ~70 web files go through `xenonFetch`, but **two server-side routes fetch FastAPI directly**: `web/app/api/wizard/stream/route.ts` (SSE proxy) and `web/app/api/previous-close/route.ts` (calls `POST /ws-ticket`). Both must also carry the internal token or they 401 in prod. (Verified: `rg -rln "XENON_API_URL" web/app web/lib` → exactly `xenonApi.ts`, `wizard/stream/route.ts`, `previous-close/route.ts`.)
- **`/ws-ticket` has a route-level dependency:** `server.py:1296` is `async def get_ws_ticket(payload: dict = Depends(verify_clerk_jwt))`. Under the new model the middleware authorizes and Clerk is unset in prod, so this dependency must read the middleware-set identity instead (otherwise it raises `RuntimeError: CLERK_JWKS_URL not set` for non-localhost callers).
- **Fail-closed, not open-by-default.** For a live-trading system, "env failed to load → API open" is unacceptable. The only non-authenticated pass is `XENON_AUTH_ALLOW_DEV_OPEN=1`, which prod never sets.
- **Test/dev access:** The local dev stack (`dev.sh`) and the web test harness call FastAPI from `127.0.0.1` → localhost bypass. The Python test suite drives the app via `TestClient` whose `client.host` is `"testclient"` (non-localhost), so a process-wide `XENON_AUTH_ALLOW_DEV_OPEN=1` (set by a root `conftest.py`) keeps the existing suite green. Auth-specific tests override it to `""` to observe gating.
- **Strict truthy parsing:** mirror `server.py::_is_test_mode` — `os.environ.get(...).lower() in {"1","true","yes","on"}`. A bare `if os.environ.get(...)` treats `"0"`/`"false"` as truthy (a real bug).
- **No DB migration** in this plan → no `core_dev` migration risk; the prod `migrator` step stays a no-op.
- **Verified facts (planning):** `server.py:39` is `from xenon.api.auth import verify_api_key, verify_clerk_jwt`; `JSONResponse`/`HTTPException`/`Request` already imported (`server.py:25-27`). All four auth env vars are absent from `.env`, so the test process is unaffected. No route uses `Depends(verify_clerk_jwt)` except `/ws-ticket`. `test_historical_auth.py` imports `API_KEY_ALLOWED_PATHS` and asserts the MDW scope — must stay green unchanged.

## File Structure

- **Modify** `src/xenon/api/auth.py` — add `_truthy_env`, `QUERY_API_KEY_PATHS`, extend `verify_api_key` (query scope + explicit deny-after-match), add `AuthDecision` + `classify_auth` (fail-closed), add `validate_auth_config`.
- **Modify** `src/xenon/api/server.py` — rewrite `auth_middleware` to delegate to `classify_auth`; trim `AUTH_EXEMPT_PATHS`; refactor `get_ws_ticket`; call `validate_auth_config` + log posture in `lifespan`.
- **Modify** `web/lib/xenonApi.ts` — add exported `internalApiHeaders()`; `xenonFetch` uses it.
- **Modify** `web/app/api/wizard/stream/route.ts`, `web/app/api/previous-close/route.ts` — attach internal token.
- **Create** `conftest.py` (repo root) — set `XENON_AUTH_ALLOW_DEV_OPEN=1` for the test session.
- **Modify** `scripts/infra/dev.sh` — export `XENON_AUTH_ALLOW_DEV_OPEN=1`.
- **Modify** `web/tests/fastapiHarness.ts` — set `XENON_AUTH_ALLOW_DEV_OPEN=1` in the spawned uvicorn env.
- **Modify** `docker-compose.yml` — inject `XENON_INTERNAL_API_TOKEN` into the `web` service from root `.env` (single source).
- **Create** `src/xenon/api/tests/test_query_api_key.py` — unit + integration tests.
- **Create** `web/tests/xenonApi.internal-token.test.ts` — Vitest for the header + the two direct routes.
- **Modify** `src/xenon/api/CLAUDE.md`, `docs/runbooks/remote-deploy.md`, `docs/reference/order-path-incident-history.md`.

---

### Task 1: Read-only query scope for `verify_api_key`

**Files:**

- Modify: `src/xenon/api/auth.py:104-126`
- Test: `src/xenon/api/tests/test_query_api_key.py` (create)

- [ ] **Step 1: Write the failing test.** Create `src/xenon/api/tests/test_query_api_key.py`:

```python
"""Tests for the read-only query API key scope and the auth decision matrix."""

import os
from unittest.mock import patch

# Task 2 appends `classify_auth, validate_auth_config` to this import.
from xenon.api.auth import QUERY_API_KEY_PATHS, verify_api_key


class FakeRequest:
    def __init__(self, path, method="GET", headers=None, client_host=None):
        self.url = type("URL", (), {"path": path})()
        self.method = method
        self.headers = headers or {}
        self.client = type("Client", (), {"host": client_host})() if client_host else None


class TestQueryApiKey:
    def test_query_key_allows_get_portfolio(self):
        with patch.dict(os.environ, {"XENON_QUERY_API_KEY": "qk"}, clear=False):
            req = FakeRequest("/portfolio", "GET", {"X-API-Key": "qk"})
            result = verify_api_key(req)
            assert result is not None
            assert result["sub"] == "query-service"
            assert result["scope"] == "read-only"

    def test_query_key_rejects_post_on_read_path(self):
        with patch.dict(os.environ, {"XENON_QUERY_API_KEY": "qk"}, clear=False):
            req = FakeRequest("/portfolio", "POST", {"X-API-Key": "qk"})
            assert verify_api_key(req) is None

    def test_query_key_rejects_order_place(self):
        with patch.dict(os.environ, {"XENON_QUERY_API_KEY": "qk"}, clear=False):
            req = FakeRequest("/orders/place", "POST", {"X-API-Key": "qk"})
            assert verify_api_key(req) is None

    def test_query_key_rejects_get_order_place(self):
        with patch.dict(os.environ, {"XENON_QUERY_API_KEY": "qk"}, clear=False):
            req = FakeRequest("/orders/place", "GET", {"X-API-Key": "qk"})
            assert verify_api_key(req) is None

    def test_wrong_query_key(self):
        with patch.dict(os.environ, {"XENON_QUERY_API_KEY": "qk"}, clear=False):
            req = FakeRequest("/portfolio", "GET", {"X-API-Key": "nope"})
            assert verify_api_key(req) is None

    def test_mdw_key_does_not_grant_query_paths(self):
        # MDW key matches but /portfolio is not in its scope → explicit deny,
        # even if a query key is also set (must not fall through to query scope).
        with patch.dict(os.environ, {"MDW_API_KEY": "mk", "XENON_QUERY_API_KEY": "qk"}, clear=False):
            req = FakeRequest("/portfolio", "GET", {"X-API-Key": "mk"})
            assert verify_api_key(req) is None

    def test_all_query_paths_allowed(self):
        with patch.dict(os.environ, {"XENON_QUERY_API_KEY": "qk"}, clear=False):
            for method, path in QUERY_API_KEY_PATHS:
                req = FakeRequest(path, method, {"X-API-Key": "qk"})
                assert verify_api_key(req) is not None, f"{method} {path} should be allowed"

    def test_query_paths_are_all_get_and_complete(self):
        assert all(m == "GET" for m, _ in QUERY_API_KEY_PATHS)
        expected = {
            ("GET", "/portfolio"), ("GET", "/orders"), ("GET", "/blotter"),
            ("GET", "/journal"), ("GET", "/futu/portfolio"),
            ("GET", "/trades/entry-dates"), ("GET", "/performance"),
        }
        assert set(QUERY_API_KEY_PATHS) == expected

    def test_write_and_sync_paths_never_granted_by_query_key(self):
        # Codex ISSUE-6: assert the query key is rejected for every mutating/sync path.
        write_paths = [
            ("POST", "/orders/place"), ("POST", "/orders/cancel"), ("POST", "/orders/modify"),
            ("POST", "/orders/refresh"), ("POST", "/portfolio/sync"),
            ("POST", "/portfolio/background-sync"), ("POST", "/blotter"),
            ("POST", "/performance"), ("POST", "/performance/background"),
            ("POST", "/futu/sync"), ("POST", "/ib/restart"), ("POST", "/journal"),
            ("POST", "/journal/sync"), ("POST", "/watchlist"), ("DELETE", "/watchlist/AAPL"),
            ("POST", "/wizard/sessions/abc/submit"), ("POST", "/wizard/sessions/abc/reprice"),
            ("POST", "/wizard/sessions/abc/protect"), ("POST", "/wizard/sessions/abc/abort"),
        ]
        with patch.dict(os.environ, {"XENON_QUERY_API_KEY": "qk"}, clear=False):
            for method, path in write_paths:
                req = FakeRequest(path, method, {"X-API-Key": "qk"})
                assert verify_api_key(req) is None, f"{method} {path} must be denied"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest src/xenon/api/tests/test_query_api_key.py::TestQueryApiKey -x`
Expected: FAIL — `ImportError: cannot import name 'QUERY_API_KEY_PATHS'`.

- [ ] **Step 3: Implement the query scope + helper in `auth.py`.** After the existing `API_KEY_ALLOWED_PATHS` block (line ~108), add:

```python
def _truthy_env(name: str) -> bool:
    """Strict truthy parse mirroring server._is_test_mode — '0'/'false'/'' are False."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# Read-only query key — (METHOD, PATH) tuples. GET-only by construction so an
# external key can never trigger a write/sync. /blotter and /performance also
# have POST siblings (sync/rebuild) which are deliberately excluded.
QUERY_API_KEY_PATHS = frozenset({
    ("GET", "/portfolio"),
    ("GET", "/orders"),
    ("GET", "/blotter"),
    ("GET", "/journal"),
    ("GET", "/futu/portfolio"),
    ("GET", "/trades/entry-dates"),
    ("GET", "/performance"),
})
```

Then replace the body of `verify_api_key` (lines 111-126) with:

```python
def verify_api_key(request: Request) -> dict | None:
    """Check X-API-Key against the two configured key scopes.

    - MDW_API_KEY        → historical/contract endpoints (API_KEY_ALLOWED_PATHS), unchanged.
    - XENON_QUERY_API_KEY → read-only query endpoints (QUERY_API_KEY_PATHS), GET-only.

    A matched key makes a definitive grant-or-deny decision (returns immediately),
    so a key can never be evaluated against another key's scope. Never grants
    write/order paths.
    """
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        return None

    path = request.url.path

    mdw_key = os.environ.get("MDW_API_KEY")
    if mdw_key and hmac.compare_digest(api_key.encode(), mdw_key.encode()):
        if path in API_KEY_ALLOWED_PATHS:
            return {"sub": "mdw-service", "service": True}
        return None  # key matched, path out of scope → deny (no fall-through)

    query_key = os.environ.get("XENON_QUERY_API_KEY")
    if query_key and hmac.compare_digest(api_key.encode(), query_key.encode()):
        if (request.method, path) in QUERY_API_KEY_PATHS:
            return {"sub": "query-service", "service": True, "scope": "read-only"}
        return None  # key matched, method/path out of scope → deny

    return None
```

Note: `request.method` is read only inside the query-key branch; the legacy `test_historical_auth.py` (MDW-only, method-less `FakeRequest`) never reaches it.

- [ ] **Step 4: Run the query-scope tests**

Run: `uv run pytest src/xenon/api/tests/test_query_api_key.py::TestQueryApiKey -x`
Expected: PASS. (File imports only `QUERY_API_KEY_PATHS, verify_api_key`; Task 2 adds the rest.)

- [ ] **Step 5: Verify legacy auth test still green**

Run: `uv run pytest src/xenon/api/tests/test_historical_auth.py -x`
Expected: PASS (MDW scope + `API_KEY_ALLOWED_PATHS` untouched).

- [ ] **Step 6: Commit**

```bash
git add src/xenon/api/auth.py src/xenon/api/tests/test_query_api_key.py
git commit -m "feat(auth): read-only XENON_QUERY_API_KEY scope (GET-only) with explicit deny"
```

---

### Task 2: Fail-closed `classify_auth` + config validation

**Files:**

- Modify: `src/xenon/api/auth.py` (add `AuthDecision`, `classify_auth`, `validate_auth_config`)
- Test: `src/xenon/api/tests/test_query_api_key.py` (append `TestClassifyAuth`, `TestValidateAuthConfig`)

- [ ] **Step 1: Write the failing test.** First extend the import at the top of `src/xenon/api/tests/test_query_api_key.py` to:

```python
from xenon.api.auth import (
    QUERY_API_KEY_PATHS,
    verify_api_key,
    classify_auth,
    validate_auth_config,
)
```

Then append to the same file:

```python
class TestClassifyAuth:
    # BASE patches every auth env var to "" (falsy) via patch.dict(clear=False),
    # overriding the root conftest's XENON_AUTH_ALLOW_DEV_OPEN=1 and anything from
    # .env. So the decision under test is never masked by the dev-open pass or a
    # leaked secret. Each test overrides only the var(s) it exercises.
    BASE = {"CLERK_JWKS_URL": "", "MDW_API_KEY": "", "XENON_QUERY_API_KEY": "",
            "XENON_INTERNAL_API_TOKEN": "", "XENON_AUTH_ALLOW_DEV_OPEN": ""}

    def _env(self, **overrides):
        env = dict(self.BASE)
        env.update(overrides)
        return env

    def test_fail_closed_when_nothing_configured(self):
        with patch.dict(os.environ, self._env(), clear=False):
            req = FakeRequest("/portfolio", "GET", {}, client_host="100.66.147.98")
            assert classify_auth(req).action == "deny"

    def test_dev_open_flag_passes(self):
        with patch.dict(os.environ, self._env(XENON_AUTH_ALLOW_DEV_OPEN="1"), clear=False):
            req = FakeRequest("/portfolio", "GET", {}, client_host="100.66.147.98")
            d = classify_auth(req)
            assert d.action == "pass"
            assert d.identity["dev_open"] is True

    def test_dev_open_flag_false_string_does_not_pass(self):
        # Strict truthy: "0" must NOT enable dev-open.
        with patch.dict(os.environ, self._env(XENON_AUTH_ALLOW_DEV_OPEN="0"), clear=False):
            req = FakeRequest("/portfolio", "GET", {}, client_host="100.66.147.98")
            assert classify_auth(req).action == "deny"

    def test_localhost_passes(self):
        with patch.dict(os.environ, self._env(), clear=False):
            for host in ("127.0.0.1", "::1"):
                req = FakeRequest("/portfolio", "GET", {}, client_host=host)
                d = classify_auth(req)
                assert d.action == "pass", host
                assert d.identity["local"] is True

    def test_internal_token_passes_any_path(self):
        with patch.dict(os.environ, self._env(XENON_INTERNAL_API_TOKEN="itok"), clear=False):
            req = FakeRequest("/orders/place", "POST",
                              {"X-Internal-Token": "itok"}, client_host="172.18.0.5")
            d = classify_auth(req)
            assert d.action == "pass"
            assert d.identity["internal"] is True

    def test_internal_token_wrong_value_denied(self):
        with patch.dict(os.environ, self._env(XENON_INTERNAL_API_TOKEN="itok"), clear=False):
            req = FakeRequest("/orders/place", "POST",
                              {"X-Internal-Token": "wrong"}, client_host="172.18.0.5")
            assert classify_auth(req).action == "deny"

    def test_query_key_passes_read_path(self):
        with patch.dict(os.environ, self._env(XENON_QUERY_API_KEY="qk"), clear=False):
            req = FakeRequest("/portfolio", "GET",
                              {"X-API-Key": "qk"}, client_host="100.66.147.98")
            d = classify_auth(req)
            assert d.action == "pass"
            assert d.identity["sub"] == "query-service"

    def test_query_key_denied_on_write_path(self):
        with patch.dict(os.environ, self._env(XENON_QUERY_API_KEY="qk"), clear=False):
            req = FakeRequest("/orders/place", "POST",
                              {"X-API-Key": "qk"}, client_host="100.66.147.98")
            assert classify_auth(req).action == "deny"

    def test_no_creds_denied_when_secret_configured(self):
        with patch.dict(os.environ, self._env(XENON_QUERY_API_KEY="qk"), clear=False):
            req = FakeRequest("/portfolio", "GET", {}, client_host="100.66.147.98")
            assert classify_auth(req).action == "deny"

    def test_clerk_branch_when_configured_and_no_other_match(self):
        with patch.dict(os.environ, self._env(CLERK_JWKS_URL="https://x/jwks"), clear=False):
            req = FakeRequest("/portfolio", "GET", {}, client_host="100.66.147.98")
            assert classify_auth(req).action == "clerk"


class TestValidateAuthConfig:
    def test_raises_when_internal_token_equals_query_key(self):
        with patch.dict(os.environ, {"XENON_INTERNAL_API_TOKEN": "same",
                                     "XENON_QUERY_API_KEY": "same"}, clear=False):
            import pytest
            with pytest.raises(RuntimeError):
                validate_auth_config()

    def test_ok_when_distinct(self):
        with patch.dict(os.environ, {"XENON_INTERNAL_API_TOKEN": "a",
                                     "XENON_QUERY_API_KEY": "b"}, clear=False):
            # returns a posture string, does not raise
            assert isinstance(validate_auth_config(), str)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest src/xenon/api/tests/test_query_api_key.py::TestClassifyAuth -x`
Expected: FAIL — `cannot import name 'classify_auth'`.

- [ ] **Step 3: Implement `AuthDecision`, `classify_auth`, `validate_auth_config` in `auth.py`.**

Add near the top of `auth.py` (after imports):

```python
from dataclasses import dataclass

_LOCALHOST_HOSTS = frozenset({"127.0.0.1", "::1"})


@dataclass(frozen=True)
class AuthDecision:
    """Outcome of the synchronous auth pre-check.

    action: "pass"  → authorized (use identity); call next.
            "clerk" → defer to async Clerk JWT validation.
            "deny"  → 401.
    """
    action: str
    identity: dict | None = None
```

Add at the end of `auth.py`:

```python
def classify_auth(request: Request) -> AuthDecision:
    """Fail-closed auth decision for the HTTP middleware (exempt paths handled upstream).

    A non-exempt request is DENIED unless it proves identity. The only
    non-authenticated pass is the explicit dev opt-in XENON_AUTH_ALLOW_DEV_OPEN=1,
    which production never sets — so an unloaded/misconfigured prod env denies
    rather than exposes the trading API.
    """
    client_host = request.client.host if request.client else None
    if client_host in _LOCALHOST_HOSTS:
        return AuthDecision("pass", {"sub": "localhost", "local": True})

    internal_token = os.environ.get("XENON_INTERNAL_API_TOKEN")
    if internal_token:
        hdr = request.headers.get("X-Internal-Token")
        if hdr and hmac.compare_digest(hdr.encode(), internal_token.encode()):
            return AuthDecision("pass", {"sub": "internal-ui", "internal": True})

    identity = verify_api_key(request)
    if identity is not None:
        return AuthDecision("pass", identity)

    if os.environ.get("CLERK_JWKS_URL"):
        return AuthDecision("clerk")

    if _truthy_env("XENON_AUTH_ALLOW_DEV_OPEN"):
        return AuthDecision("pass", {"sub": "dev-open", "dev_open": True})

    return AuthDecision("deny")


def validate_auth_config() -> str:
    """Validate auth env at startup; return a one-line posture string for logging.

    Raises RuntimeError if the internal token and query key are identical
    (a leaked read-only key could then be replayed as X-Internal-Token for full
    write access).
    """
    internal_token = os.environ.get("XENON_INTERNAL_API_TOKEN")
    query_key = os.environ.get("XENON_QUERY_API_KEY")
    if internal_token and query_key and hmac.compare_digest(
        internal_token.encode(), query_key.encode()
    ):
        raise RuntimeError(
            "XENON_INTERNAL_API_TOKEN must differ from XENON_QUERY_API_KEY "
            "(equal values let a leaked read-only key gain write access)"
        )
    configured = bool(
        internal_token or query_key
        or os.environ.get("MDW_API_KEY") or os.environ.get("CLERK_JWKS_URL")
    )
    if configured:
        return "auth: ENFORCED (authenticated access required for non-localhost)"
    if _truthy_env("XENON_AUTH_ALLOW_DEV_OPEN"):
        return "auth: DEV-OPEN (XENON_AUTH_ALLOW_DEV_OPEN=1; non-localhost requests pass)"
    return "auth: FAIL-CLOSED (no secrets, no dev-open; non-localhost requests denied)"
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest src/xenon/api/tests/test_query_api_key.py -x`
Expected: PASS (`TestQueryApiKey`, `TestClassifyAuth`, `TestValidateAuthConfig`).

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/auth.py src/xenon/api/tests/test_query_api_key.py
git commit -m "feat(auth): fail-closed classify_auth + dev-open opt-in + config validation"
```

---

### Task 3: Middleware wiring + `/ws-ticket` + exempt-path trim

**Files:**

- Modify: `src/xenon/api/server.py` (import, `AUTH_EXEMPT_PATHS`, `auth_middleware`, `get_ws_ticket`, `lifespan`)
- Test: `src/xenon/api/tests/test_query_api_key.py` (append `TestMiddlewareIntegration`)

- [ ] **Step 1: Write the failing test** — append to `src/xenon/api/tests/test_query_api_key.py`:

```python
class TestMiddlewareIntegration:
    """Drive the real app middleware via TestClient (client.host == 'testclient',
    non-localhost). GATED disables dev-open so gating is observable."""

    GATED = {"XENON_QUERY_API_KEY": "qk", "XENON_AUTH_ALLOW_DEV_OPEN": "",
             "XENON_INTERNAL_API_TOKEN": "", "CLERK_JWKS_URL": "", "MDW_API_KEY": ""}

    def _client(self):
        from fastapi.testclient import TestClient
        from xenon.api.server import app
        return TestClient(app)

    def test_health_open_without_auth(self):
        with patch.dict(os.environ, self.GATED, clear=False):
            assert self._client().get("/health").status_code == 200

    def test_openapi_now_requires_auth(self):
        # Codex ISSUE-11: /openapi.json is no longer exempt.
        with patch.dict(os.environ, self.GATED, clear=False):
            assert self._client().get("/openapi.json").status_code == 401

    def test_query_path_denied_without_key(self):
        with patch.dict(os.environ, self.GATED, clear=False):
            assert self._client().get("/trades/entry-dates").status_code == 401

    def test_write_path_denied_with_query_key(self):
        with patch.dict(os.environ, self.GATED, clear=False):
            r = self._client().post("/orders/place", headers={"X-API-Key": "qk"}, json={})
            assert r.status_code == 401  # gate denies before any order logic runs

    def test_query_path_allowed_with_key(self):
        with patch.dict(os.environ, self.GATED, clear=False):
            r = self._client().get("/portfolio", headers={"X-API-Key": "qk"})
            # Authorized: middleware passed it through. 200 (snapshot) or 404
            # (empty test DB) are both valid authorized responses; 401/403/500 are not.
            assert r.status_code in (200, 404)

    def test_internal_token_reaches_handler(self):
        with patch.dict(os.environ, {**self.GATED, "XENON_INTERNAL_API_TOKEN": "itok"}, clear=False):
            r = self._client().get("/portfolio", headers={"X-Internal-Token": "itok"})
            assert r.status_code in (200, 404)

    def test_ws_ticket_with_internal_token_no_clerk(self):
        # Codex ISSUE-4: /ws-ticket must work via the middleware identity (no Clerk
        # in prod). create_ticket is in-memory, so this is fully deterministic.
        with patch.dict(os.environ, {**self.GATED, "XENON_INTERNAL_API_TOKEN": "itok"}, clear=False):
            r = self._client().post("/ws-ticket", headers={"X-Internal-Token": "itok"})
            assert r.status_code == 200
            assert "ticket" in r.json()

    def test_ws_ticket_denied_without_creds(self):
        with patch.dict(os.environ, self.GATED, clear=False):
            assert self._client().post("/ws-ticket").status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest src/xenon/api/tests/test_query_api_key.py::TestMiddlewareIntegration -x`
Expected: FAIL — `test_query_path_denied_without_key` returns 200 (old middleware passes through).

- [ ] **Step 3a: Update the auth import** at `src/xenon/api/server.py:39`. Change:

```python
from xenon.api.auth import verify_api_key, verify_clerk_jwt
```

to:

```python
from xenon.api.auth import verify_api_key, verify_clerk_jwt, classify_auth, validate_auth_config
```

- [ ] **Step 3b: Trim `AUTH_EXEMPT_PATHS`** (`server.py:630`). Change the set to drop `/docs` and `/openapi.json` (they exposed the full live-trading API schema to every tailnet/LAN caller — Codex ISSUE-11). Keep:

```python
AUTH_EXEMPT_PATHS = {
    "/health",
    "/ws-ticket/validate",
}
```

(Local dev / `npm run gen:types` fetch `/openapi.json` from `127.0.0.1` → still allowed via the localhost pass.)

- [ ] **Step 3c: Rewrite `auth_middleware`** (`server.py:642-671`):

```python
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Authorize via classify_auth (fail-closed); defer Clerk JWT to async validation."""
    if request.url.path in AUTH_EXEMPT_PATHS:
        return await call_next(request)

    decision = classify_auth(request)

    if decision.action == "pass":
        request.state.user = decision.identity
        return await call_next(request)

    if decision.action == "clerk":
        try:
            payload = await verify_clerk_jwt(request)
            request.state.user = payload
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)

    return JSONResponse(status_code=401, content={"detail": "Authentication required"})
```

- [ ] **Step 3d: Refactor `get_ws_ticket`** (`server.py:1295-1299`) to read the middleware-set identity instead of `Depends(verify_clerk_jwt)`:

```python
@app.post("/ws-ticket")
async def get_ws_ticket(request: Request):
    """Issue a short-lived ticket for WebSocket authentication.

    The middleware already authorized this request (localhost / internal token /
    API key / Clerk) and set request.state.user. Reading it here avoids a second
    Clerk validation that would raise when CLERK_JWKS_URL is unset (prod).
    """
    user = getattr(request.state, "user", None) or {}
    ticket = create_ticket(user.get("sub", "unknown"))
    return {"ticket": ticket}
```

- [ ] **Step 3e: Validate + log auth posture at startup.** In the FastAPI `lifespan` startup section of `server.py` (early, before the server serves — near the existing startup logging), add:

```python
    from xenon.api.auth import validate_auth_config
    logger.info(validate_auth_config())  # raises if internal token == query key
```

(Import is already added in Step 3a; the inline import here is belt-and-suspenders and may be omitted if the top-level import is in scope.)

- [ ] **Step 4: Run the middleware tests + broader API suite**

Run: `uv run pytest src/xenon/api/tests/test_query_api_key.py -x`
Then: `uv run pytest src/xenon/api/tests -q`
Expected: PASS. The full suite stays green because the root `conftest.py` (Task 5) sets `XENON_AUTH_ALLOW_DEV_OPEN=1` process-wide and `TestClient` requests therefore hit the dev-open pass; the auth tests above override it to `""`.

> If you run Task 3 before Task 5, the broader suite will 401. Implement Task 5's root `conftest.py` first if executing out of order, or run only `test_query_api_key.py` until Task 5 lands.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/server.py src/xenon/api/tests/test_query_api_key.py
git commit -m "feat(auth): middleware delegates to fail-closed classify_auth; gate /ws-ticket + /openapi.json"
```

---

### Task 4: Internal token on every web→api call

**Files:**

- Modify: `web/lib/xenonApi.ts`, `web/app/api/wizard/stream/route.ts`, `web/app/api/previous-close/route.ts`
- Test: `web/tests/xenonApi.internal-token.test.ts` (create)

- [ ] **Step 1: Write the failing test.** Create `web/tests/xenonApi.internal-token.test.ts`:

```typescript
import { describe, it, expect, vi, afterEach } from "vitest";
import { xenonFetch, internalApiHeaders } from "../lib/xenonApi";

function jsonResponse() {
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

describe("internalApiHeaders", () => {
  afterEach(() => {
    delete process.env.XENON_INTERNAL_API_TOKEN;
  });

  it("sets X-Internal-Token when env present", () => {
    process.env.XENON_INTERNAL_API_TOKEN = "s3cret";
    const h = new Headers();
    internalApiHeaders(h);
    expect(h.get("X-Internal-Token")).toBe("s3cret");
  });

  it("no-op when env absent", () => {
    delete process.env.XENON_INTERNAL_API_TOKEN;
    const h = new Headers();
    internalApiHeaders(h);
    expect(h.get("X-Internal-Token")).toBeNull();
  });
});

describe("xenonFetch internal token", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    delete process.env.XENON_INTERNAL_API_TOKEN;
  });

  it("attaches X-Internal-Token when env set", async () => {
    process.env.XENON_INTERNAL_API_TOKEN = "s3cret";
    const fetchMock = vi.fn(async () => jsonResponse());
    vi.stubGlobal("fetch", fetchMock);
    await xenonFetch("/portfolio");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(init.headers).get("X-Internal-Token")).toBe("s3cret");
  });

  it("omits X-Internal-Token when env unset", async () => {
    delete process.env.XENON_INTERNAL_API_TOKEN;
    const fetchMock = vi.fn(async () => jsonResponse());
    vi.stubGlobal("fetch", fetchMock);
    await xenonFetch("/portfolio");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(init.headers).get("X-Internal-Token")).toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run tests/xenonApi.internal-token.test.ts`
Expected: FAIL — `internalApiHeaders` is not exported.

- [ ] **Step 3a: Add `internalApiHeaders` + use it in `xenonFetch`** (`web/lib/xenonApi.ts`). After the `XenonApiError` class, add the exported helper:

```typescript
/**
 * Attach the server-to-server trust header so the api can keep order-write
 * endpoints closed to external callers. Read at call time (not module load) so
 * per-request/test env changes take effect. MUST be a server-only env var —
 * never NEXT_PUBLIC_ (would leak the secret to the browser).
 */
export function internalApiHeaders(headers: Headers): Headers {
  const internalToken = process.env.XENON_INTERNAL_API_TOKEN;
  if (internalToken) {
    headers.set("X-Internal-Token", internalToken);
  }
  return headers;
}
```

Then in `xenonFetch`, change the header block (lines 25-29) to call it:

```typescript
const { timeout = 30_000, token, ...fetchOpts } = opts ?? {};
const headers = new Headers(fetchOpts.headers);
if (token) {
  headers.set("Authorization", `Bearer ${token}`);
}
internalApiHeaders(headers);
```

- [ ] **Step 3b: Attach the token in the SSE proxy** (`web/app/api/wizard/stream/route.ts`). Change the `fetch` headers (line 16-19) from:

```typescript
const upstream = await fetch(upstreamUrl.toString(), {
  headers: { Accept: "text/event-stream" },
  cache: "no-store",
  signal: request.signal,
});
```

to:

```typescript
const upstream = await fetch(upstreamUrl.toString(), {
  headers: internalApiHeaders(new Headers({ Accept: "text/event-stream" })),
  cache: "no-store",
  signal: request.signal,
});
```

Add the import at the top: `import { internalApiHeaders } from "@/lib/xenonApi";` (the `@/` alias maps to the web root per `web/tsconfig.json` `"@/*": ["./*"]`, and sibling routes like `web/app/api/orders/route.ts` import `from "@/lib/xenonApi"`).

- [ ] **Step 3c: Attach the token in the previous-close `/ws-ticket` call** (`web/app/api/previous-close/route.ts:29-32`). Change:

```typescript
    const res = await fetch(`${XENON_API}/ws-ticket`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
```

to add the internal token alongside the existing Authorization header:

```typescript
    const wsHeaders = new Headers({ "Content-Type": "application/json" });
    if (token) wsHeaders.set("Authorization", `Bearer ${token}`);
    internalApiHeaders(wsHeaders);
    const res = await fetch(`${XENON_API}/ws-ticket`, {
      method: "POST",
      headers: wsHeaders,
```

Add the same `internalApiHeaders` import to this file.

> Do NOT touch this file's `fetchFromUW`/`fetchFromYahoo` fallbacks — they are pre-existing and out of scope. (Note for the backlog: the Yahoo fallback violates the repo "Never use Yahoo Finance" rule; flag separately, do not fix here.)

- [ ] **Step 4: Run tests**

Run: `cd web && npx vitest run tests/xenonApi.internal-token.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/lib/xenonApi.ts web/app/api/wizard/stream/route.ts web/app/api/previous-close/route.ts web/tests/xenonApi.internal-token.test.ts
git commit -m "feat(web): attach X-Internal-Token on all web->api calls (xenonFetch + stream + ws-ticket)"
```

---

### Task 5: Dev/test opt-in plumbing for `XENON_AUTH_ALLOW_DEV_OPEN`

Fail-closed means dev and tests must explicitly opt into open access. Localhost callers (dev.sh, the web harness) already pass via the localhost rule, but the Python `TestClient` suite (non-localhost `testclient` host) needs the flag.

**Files:**

- Create: `conftest.py` (repo root)
- Modify: `scripts/infra/dev.sh`, `web/tests/fastapiHarness.ts`

- [ ] **Step 1: Create the repo-root `conftest.py`** so the flag is set process-wide for every pytest worker (xdist forks re-import it). Content:

```python
"""Repo-root pytest config.

Set XENON_AUTH_ALLOW_DEV_OPEN=1 for the whole test session so the fail-closed
auth middleware allows TestClient requests (whose client.host is 'testclient',
not localhost). Auth-specific tests override this via patch.dict to observe
gating. Never set in production.
"""
import os

os.environ.setdefault("XENON_AUTH_ALLOW_DEV_OPEN", "1")
```

- [ ] **Step 2: Verify the broader suite is green with the flag.**

Run: `uv run pytest src/xenon/api/tests -q`
Then a couple of TestClient-driven suites elsewhere, e.g.: `uv run pytest src/xenon/api/tests/test_watchlist_routes.py src/xenon/api/tests/test_operator_endpoint.py -q`
Expected: PASS. (Per the Phase-3 xdist note in CLAUDE.md, if you later add per-worker DB infra, re-confirm the root conftest is still imported by every worker — it is, being at rootdir.)

- [ ] **Step 3: Export the flag in `dev.sh`.** In `scripts/infra/dev.sh`, near the other exports (~line 181), add:

```bash
# Dev stack is fail-closed-safe: localhost calls already pass, but export the
# dev-open flag so any non-localhost dev tooling (curl from another device on
# the dev box's network) also works. Production never sets this.
export XENON_AUTH_ALLOW_DEV_OPEN=1
```

- [ ] **Step 4: Set the flag in the web test harness.** In `web/tests/fastapiHarness.ts`, where the spawned uvicorn env is built (the object containing `XENON_API_TEST_MODE: "1"`, ~line 136), add:

```typescript
        XENON_AUTH_ALLOW_DEV_OPEN: "1",
```

- [ ] **Step 5: Run the web order-route integration tests** (they spin up the harnessed FastAPI):

Run: `cd web && npm test`
Expected: PASS (harness FastAPI now allows its non-localhost calls if any; localhost calls already passed).

- [ ] **Step 6: Commit**

```bash
git add conftest.py scripts/infra/dev.sh web/tests/fastapiHarness.ts
git commit -m "test(auth): XENON_AUTH_ALLOW_DEV_OPEN opt-in for dev.sh + pytest + web harness"
```

---

### Task 6: Single-source the internal token in compose

**Files:**

- Modify: `docker-compose.yml`

- [ ] **Step 1: Inject the token into the `web` service from root `.env`** (Gemini ISSUE-4 — avoid maintaining it in two env files). In `docker-compose.yml`, the `web` service `environment:` block (line ~105-109) currently has `XENON_API_URL`. Add:

```yaml
environment:
  XENON_API_URL: http://api:8321
  # Server-to-server trust secret, sourced once from the root .env (compose
  # interpolates ${VAR} from the compose-dir .env). Same value reaches the
  # api service via its env_file: ./.env. Single source of truth.
  XENON_INTERNAL_API_TOKEN: ${XENON_INTERNAL_API_TOKEN:-}
```

(The `api` service already loads `./.env` via `env_file`, so it picks up `XENON_INTERNAL_API_TOKEN` and `XENON_QUERY_API_KEY` from the same file. No web `env_file` duplication needed.)

- [ ] **Step 2: Validate compose syntax**

Run: `docker compose -f docker-compose.yml config >/dev/null && echo OK`
Expected: `OK` (no schema error). If Docker is unavailable locally, skip with a note; CI/operator validates on the mini.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "chore(compose): inject XENON_INTERNAL_API_TOKEN into web from root .env (single source)"
```

---

### Task 7: Documentation

**Files:** `src/xenon/api/CLAUDE.md`, `docs/runbooks/remote-deploy.md`, `docs/reference/order-path-incident-history.md`

- [ ] **Step 1: Replace the `## Auth — Security-Relevant Behavior` body in `src/xenon/api/CLAUDE.md`:**

```markdown
## Auth — Security-Relevant Behavior

**Auth-exempt paths:** `/health`, `/ws-ticket/validate`. (`/docs` and `/openapi.json`
are NOT exempt — they expose the full trading API schema and now require auth.)

**Fail-closed.** `auth.classify_auth(request)` (sync) is called by
`server.py::auth_middleware`. A non-exempt request is DENIED (401) unless it proves
identity, in this order:

1. Source `127.0.0.1`/`::1` → pass (on-box / dev stack / web harness).
2. Valid `X-Internal-Token` == `XENON_INTERNAL_API_TOKEN` → pass, full access.
   `xenonFetch` AND the two direct web→api callers (`wizard/stream`,
   `previous-close`) attach it via `internalApiHeaders()`.
3. Valid `X-API-Key`: `XENON_QUERY_API_KEY` → GET-only read paths
   (`QUERY_API_KEY_PATHS`); `MDW_API_KEY` → `/historical/*` + `/contract/qualify`
   (`API_KEY_ALLOWED_PATHS`). A matched key grants-or-denies definitively; neither
   grants write/sync paths.
4. `CLERK_JWKS_URL` set → defer to async Clerk JWT.
5. `XENON_AUTH_ALLOW_DEV_OPEN=1` (strict truthy) → pass. **Dev/test only** — set by
   `dev.sh`, the root `conftest.py`, and the web harness. Production never sets it,
   so a prod env that fails to load denies rather than exposes the API.
6. Else → 401.

`validate_auth_config()` runs at lifespan startup: it refuses to boot if
`XENON_INTERNAL_API_TOKEN == XENON_QUERY_API_KEY` (a leaked read-only key could
otherwise be replayed for write access) and logs the posture (ENFORCED / DEV-OPEN /
FAIL-CLOSED).

Order-write endpoints (`/orders/place|cancel|modify`, `/portfolio/sync`,
`POST /blotter`, `POST /futu/sync`, etc.) are in no API-key allowlist, so an external
key can never reach them — only the internal UI token, localhost, or Clerk.
```

- [ ] **Step 2: Add an env subsection to `docs/runbooks/remote-deploy.md`** (near the `/opt/xenon/.env` docs):

```markdown
### Query-API auth env (added 2026-06-17)

The api is fail-closed; set these in `/opt/xenon/.env` (single source — compose
injects the token into the web container, and `env_file: ./.env` gives it to the
api/realtime/migrator services):

| Var                        | Purpose                                                                                                                                                                      |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `XENON_INTERNAL_API_TOKEN` | Shared secret proving `web→api` is the trusted UI (full access). `compose.yml` web service must have `environment: XENON_INTERNAL_API_TOKEN: ${XENON_INTERNAL_API_TOKEN:-}`. |
| `XENON_QUERY_API_KEY`      | Read-only key for external scripts. Send as `X-API-Key`. GET-only query paths. MUST differ from the internal token (the api refuses to boot if equal).                       |

Generate with `openssl rand -hex 32` (two distinct values). `XENON_AUTH_ALLOW_DEV_OPEN`
is NEVER set in prod. After editing, `docker-compose up -d` to recreate containers
(env_file changes need a recreate, not a restart).
```

- [ ] **Step 3: Append a row to `docs/reference/order-path-incident-history.md`** (mirror its existing column format; read the header row first):

> Date `2026-06-17`; Symptom: prod API reachable over Tailscale/LAN with **no auth**, incl. `/orders/place|cancel|modify` (open order placement); Root cause: `auth_middleware` passed all requests through when `CLERK_JWKS_URL` unset in the container + port published `0.0.0.0:8321`; Fix: fail-closed `classify_auth` (deny unless localhost/internal-token/api-key/clerk/dev-open), read-only `XENON_QUERY_API_KEY`, internal-token on all web→api calls, startup config validation; Regression test: `src/xenon/api/tests/test_query_api_key.py`.

- [ ] **Step 4: Commit**

```bash
git add src/xenon/api/CLAUDE.md docs/runbooks/remote-deploy.md docs/reference/order-path-incident-history.md
git commit -m "docs(auth): document fail-closed model, query-API key, prod env"
```

---

### Task 8: Full local verification gate

**Files:** none (verification only)

- [ ] **Step 1: Python — affected + auth suites**

Run: `uv run python scripts/infra/dev/run_pytest_affected.py`
Then: `uv run pytest src/xenon/api/tests/test_query_api_key.py src/xenon/api/tests/test_historical_auth.py -q`
Expected: PASS, 0 failures.

- [ ] **Step 2: Web — vitest + typecheck + lint**

Run: `cd web && npm test && npm run typecheck && npm run lint`
Expected: PASS (includes `xenonApi.internal-token.test.ts`).

- [ ] **Step 3: Sanity — confirm no NEW direct web→api callers slipped in**

Run: `rg -rln "XENON_API_URL" web/app web/lib --glob '!*.test.*'`
Expected: exactly `web/lib/xenonApi.ts`, `web/app/api/wizard/stream/route.ts`, `web/app/api/previous-close/route.ts` — all now carrying the internal token. If a fourth appears, it needs `internalApiHeaders()` too.

- [ ] **Step 4: Record evidence** — paste the green summary lines into the PR (Task 9). No success claims without the output.

---

### Task 9: PR + CI

- [ ] **Step 1: Push branch + open PR**

```bash
git push -u origin feat/readonly-query-api-auth
gh pr create --title "feat(auth): fail-closed API auth + read-only query key (close open prod hole)" \
  --body "$(cat <<'EOF'
## Summary
Prod API was reachable over Tailscale/LAN with NO auth (incl. order placement) because the middleware passed everything through when CLERK_JWKS_URL was unset. This makes auth fail-closed, adds a read-only X-API-Key (XENON_QUERY_API_KEY) for external query scripts, and keeps the web UI working via an internal shared-secret token attached on every web->api call.

## Changes
- Fail-closed classify_auth: deny unless localhost / X-Internal-Token / scoped X-API-Key / Clerk / explicit XENON_AUTH_ALLOW_DEV_OPEN (dev/test only).
- XENON_QUERY_API_KEY → GET-only query paths; write/sync never granted.
- internalApiHeaders() on xenonFetch + wizard/stream + previous-close; /ws-ticket reads middleware identity.
- /docs + /openapi.json now require auth. Startup validation refuses boot if internal token == query key.
- Compose single-sources the internal token; docs + incident history updated.

## Tests
- src/xenon/api/tests/test_query_api_key.py (key scope, fail-closed matrix, write-path denials, config validation, middleware integration).
- web/tests/xenonApi.internal-token.test.ts.
- (paste local green output here)

## Deploy (operator, fail-closed-aware)
Set XENON_INTERNAL_API_TOKEN + XENON_QUERY_API_KEY (distinct) in /opt/xenon/.env and add the web `environment:` injection to /opt/xenon/compose.yml before deploy. See remote-deploy runbook.
EOF
)"
```

- [ ] **Step 2: Watch CI to a real conclusion** (don't trust `--watch` exit code — see `gh_run_watch_exit_code` memory):

```bash
gh pr checks --watch || true
gh pr view --json statusCheckRollup -q '.statusCheckRollup[] | "\(.name): \(.conclusion)"'
```

Expected: every check `SUCCESS`. Fix before proceeding.

---

### Task 10: Deploy to prod + live E2E validation (OPERATOR-CONFIRMED)

> **STOP GATE:** ships code to the live trading macmini and changes prod auth. The
> executing agent MUST pause for explicit human confirmation before any step here.

**Files:** none (deploy + verification). Uses the canonical `docs/runbooks/remote-deploy.md` flow.

- [ ] **Step 1: Generate two distinct secrets**

```bash
echo "XENON_INTERNAL_API_TOKEN=$(openssl rand -hex 32)"
echo "XENON_QUERY_API_KEY=$(openssl rand -hex 32)"
```

Record both; you need the query key for the curl in Step 6. They MUST differ (the api refuses to boot otherwise).

- [ ] **Step 2: Set prod env + compose injection** (`ssh macmini` prefixes `export PATH=/opt/homebrew/bin:$PATH`)
- Add `XENON_INTERNAL_API_TOKEN=<tok>` and `XENON_QUERY_API_KEY=<key>` to `/opt/xenon/.env`.
- In `/opt/xenon/compose.yml`, add to the `web` service `environment:` block: `XENON_INTERNAL_API_TOKEN: ${XENON_INTERNAL_API_TOKEN:-}`.
- Do NOT set `XENON_AUTH_ALLOW_DEV_OPEN` anywhere in prod.

- [ ] **Step 3: Ship the code via GHCR** — merge the PR, then cut a release (`release.yml::ghcr-push` builds `api`/`web` images):

```bash
# on this dev Mac, on master after merge:
./scripts/release/cut.sh                 # bumps VERSION/package.json/CHANGELOG, tags vX.Y.Z
git push origin master --follow-tags     # fires release.yml::ghcr-push
# wait for ghcr.io/moremeds/xenon-{api,web}:X.Y.Z (no v prefix) to publish
```

- [ ] **Step 4: Pull + recreate on the mini** (canonical runbook command — hyphenated `docker-compose`, profile-migrate pull so a bad tag fails at pull):

```bash
ssh macmini 'export PATH=/opt/homebrew/bin:$PATH; cd /opt/xenon && \
  cp compose.yml compose.yml.bak && \
  docker-compose --profile migrate pull && \
  docker-compose --profile migrate run --rm migrator && \
  docker-compose up -d'
ssh macmini 'export PATH=/opt/homebrew/bin:$PATH; cd /opt/xenon && docker-compose ps'
```

- [ ] **Step 5: Deploy precheck — secrets actually loaded into the containers** (redacted; lengths only — Codex ISSUE-8):

```bash
ssh macmini 'export PATH=/opt/homebrew/bin:$PATH; \
  echo "api INTERNAL len:"; docker exec xenon-api-1 sh -c "printf %s \"\$XENON_INTERNAL_API_TOKEN\" | wc -c"; \
  echo "api QUERY len:";    docker exec xenon-api-1 sh -c "printf %s \"\$XENON_QUERY_API_KEY\" | wc -c"; \
  echo "web INTERNAL len:"; docker exec xenon-web-1 sh -c "printf %s \"\$XENON_INTERNAL_API_TOKEN\" | wc -c"'
ssh macmini 'export PATH=/opt/homebrew/bin:$PATH; docker logs --tail 30 xenon-api-1 2>&1 | grep -i "auth:"'
```

Expected: api INTERNAL/QUERY both 64, web INTERNAL 64; startup log shows `auth: ENFORCED`. If the log shows `FAIL-CLOSED` or the container crash-loops with the "must differ" RuntimeError, fix the env before validating.

- [ ] **Step 6: Live E2E — real REST queries over Tailscale**

```bash
KEY=<XENON_QUERY_API_KEY>

# (a) No key → 401 (was 200 before the fix). If this is NOT 401, STOP — auth is not enforced; do NOT run (e).
curl -s -o /dev/null -w "no-key /portfolio -> %{http_code}\n" http://100.66.147.98:8321/portfolio

# (b) With key → 200 + REAL live portfolio JSON
curl -s -w "\nkey /portfolio -> %{http_code}\n" -H "X-API-Key: $KEY" \
  http://100.66.147.98:8321/portfolio | head -c 800

# (c) With key → real open orders + today's fills (IB)
curl -s -w "\nkey /orders?broker=IB -> %{http_code}\n" -H "X-API-Key: $KEY" \
  "http://100.66.147.98:8321/orders?broker=IB" | head -c 800

# (d) With key → real Futu positions
curl -s -w "\nkey /futu/portfolio -> %{http_code}\n" -H "X-API-Key: $KEY" \
  http://100.66.147.98:8321/futu/portfolio | head -c 600

# (e) Write path with key → MUST be 401 (gate denies before order logic). Run ONLY after (a) confirmed 401.
curl -s -o /dev/null -w "key POST /orders/place -> %{http_code}\n" -X POST \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{}' \
  http://100.66.147.98:8321/orders/place

# (f) openapi now gated, health still open
curl -s -o /dev/null -w "no-key /openapi.json -> %{http_code}\n" http://100.66.147.98:8321/openapi.json
curl -s -o /dev/null -w "/health -> %{http_code}\n" http://100.66.147.98:8321/health
```

Expected: (a) 401, (b) 200 real bankroll/positions, (c) 200 real orders, (d) 200 real Futu, (e) **401**, (f) 401 then 200.

- [ ] **Step 7: Confirm the prod UI still works** — browser (chrome-cdp) to the prod web URL; verify portfolio + orders render and the combo-wizard stream + quotes work (internal-token path intact). No 401s in the network tab.

- [ ] **Step 8: Record validation evidence** in a PR comment: the curl status lines + a redacted real-JSON snippet + the UI confirmation.

---

## Self-Review (completed during planning, post-tribunal)

- **Spec coverage:** read-only key (T1), fail-closed + dev-open + config validation (T2, T5), middleware + /ws-ticket + /docs gating (T3), internal token on ALL web→api calls incl. the two direct routes (T4), single-source compose (T6), docs (T7), local gate (T8), PR/CI (T9), deploy + live write-path-401 validation (T10). ✔
- **Tribunal findings folded in:** Codex ISSUE-1 (strict truthy / removed test-mode bypass), ISSUE-2 (fail-closed + dev-open), ISSUE-3/5/12 (two direct routes + helper + rg checklist), ISSUE-4 (/ws-ticket), ISSUE-6 (write-path test list), ISSUE-7 (token≠key validation), ISSUE-8 (deploy precheck), ISSUE-9 (runbook deploy flow), ISSUE-10 (200/404 not 500), ISSUE-11 (/docs+/openapi gated); Gemini ISSUE-1 (explicit deny), 2/5 (no god-mode), 3 (/ws-ticket), 4 (single source), 6 (::1 test), 7 (startup posture log). ✔
- **Placeholder scan:** none; every code step shows full code, every doc step full content. ✔
- **Type consistency:** `_truthy_env`, `QUERY_API_KEY_PATHS` (frozenset of `(method, path)`), `AuthDecision(action, identity)`, `classify_auth(request) -> AuthDecision`, `validate_auth_config() -> str`, `internalApiHeaders(headers) -> Headers`; identity keys (`local`/`internal`/`dev_open`/`sub`/`scope`) consistent across T1–T4 + docs. ✔
- **Regression safety:** `API_KEY_ALLOWED_PATHS`/`MDW_API_KEY` untouched (T1 S5); root `conftest.py` dev-open keeps the existing TestClient suite green (T5); auth tests override the flag to observe gating. ✔

## Adversarial edge cases considered (Pass 3)

- **CORS preflight (`OPTIONS`) → 401?** Only matters for cross-origin _browser_ calls to `:8321`, which exist solely from `localhost:3000` in dev (→ localhost pass). External scripts/curl with `X-API-Key` don't preflight. Prod browsers talk to Next.js, not the api directly. Non-issue.
- **`/ws-ticket` now binds `sub="internal-ui"`** (when called by `previous-close` with the internal token) instead of the Clerk sub. Acceptable for this single-operator system — the relay only needs a valid ticket. Residual: if per-user ticket identity ever matters, pass a real sub.
- **`XENON_AUTH_ALLOW_DEV_OPEN` leaking into prod** would re-open the API to non-localhost. Mitigations: never set in prod `.env`/compose; the startup posture log prints `auth: DEV-OPEN` loudly. Residual (no hard block) — acceptable given the log + that prod sets real secrets (which make the posture `ENFORCED`).
- **`/openapi.json` gating** does not break CI/build: no `gen:types`/openapi fetch exists in `web/package.json` or `ci.yml`; local type-gen runs from localhost. Verified.
- **Empty `X-Internal-Token`/`X-API-Key` headers** → falsy/`compare_digest` mismatch → no match → correct deny. `request.client is None` → host `None` → not localhost → proceeds to other checks. Verified by inspection.
