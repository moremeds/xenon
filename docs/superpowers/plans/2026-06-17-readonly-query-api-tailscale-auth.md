# Read-Only Query API over Tailscale/LAN — Auth Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let scripts query the prod xenon API's read-only IB/Futu surfaces (portfolio, orders, blotter, journal, futu positions, trades, performance) over Tailscale/LAN via an `X-API-Key`, while closing the current hole where the prod API is reachable with **no authentication at all** — including live order placement.

**Architecture:** The prod macmini Docker API (`100.66.147.98:8321`) is already network-reachable (`0.0.0.0:8321` published) but its auth middleware passes _all_ requests through because `CLERK_JWKS_URL` is unset in the container (`server.py:648`). We replace that "Clerk-unset ⇒ open" default with **safe-by-default**: open only when _no_ auth secret is configured (dev/test); otherwise enforce. The web UI keeps full read+write access via a shared `X-Internal-Token` attached by `xenonFetch` (the single web→api chokepoint). External callers get a read-only `XENON_QUERY_API_KEY` scoped to GET-only query paths; write/sync paths are never in the allowlist, so order placement stops being reachable from the network.

**Tech Stack:** Python 3.13 / FastAPI / `uv` / pytest (Python); Next.js / TypeScript / Vitest (web); Docker Compose + GHCR (prod deploy).

---

## Context an implementer needs (read before starting)

- **Auth today** (`src/xenon/api/auth.py`, `src/xenon/api/server.py:642-671`): one HTTP middleware gates every non-exempt path. It currently short-circuits to "pass" when `CLERK_JWKS_URL` is unset, and otherwise bypasses `127.0.0.1`/`::1`, then tries `verify_api_key` (scoped to `/historical/*`, `/contract/qualify`), then Clerk JWT.
- **Why writes are exposed:** the single middleware gates reads and writes uniformly; with the Clerk-unset bypass, `POST /orders/place|cancel|modify` are as open as `GET /portfolio`. Verified empirically: `curl http://100.66.147.98:8321/portfolio` (no token) returns live positions.
- **Why we don't just enable Clerk:** `xenonFetch` (`web/lib/xenonApi.ts:21-49`) _can_ attach a Clerk JWT but **no prod web route passes one** — the prod UI works only because api-layer auth is off. Turning Clerk on globally would 401 the UI's own `web→api` calls (web container `172.18.0.5` → api `172.18.0.2`, not localhost). The internal-token approach avoids touching every route.
- **Why an internal token, not source-IP trust:** Docker SNATs external callers to the bridge gateway, so `request.client.host` can't reliably separate "my UI" from "external script." A shared secret makes the trust explicit.
- **Test infra:** Python tests run under `uv run pytest`; many hit the app via FastAPI `TestClient` with NO auth secrets set (so they rely on the open path). Phase-2 autouse DB rollback fixtures apply. `XENON_API_TEST_MODE` is set in test/harness contexts and is **false in prod** (confirmed via `/health` → `"test_mode":false`).
- **Existing regression test that MUST stay green unchanged:** `src/xenon/api/tests/test_historical_auth.py` asserts the `MDW_API_KEY` scope (historical-only; `/blotter` and `/orders/place` rejected). Our changes must not alter `MDW_API_KEY` behavior or the `API_KEY_ALLOWED_PATHS` symbol it imports.

## File Structure

- **Modify** `src/xenon/api/auth.py` — add `QUERY_API_KEY_PATHS` (method-aware read-only allowlist), extend `verify_api_key` to also accept `XENON_QUERY_API_KEY`, add `AuthDecision` + `classify_auth(request)` pure decision helper. `auth.py` is the right home: it already owns all auth primitives and is the only auth module.
- **Modify** `src/xenon/api/server.py:642-671` — rewrite `auth_middleware` to delegate to `classify_auth`.
- **Modify** `web/lib/xenonApi.ts` — attach `X-Internal-Token` from `process.env.XENON_INTERNAL_API_TOKEN` at call time.
- **Create** `src/xenon/api/tests/test_query_api_key.py` — `verify_api_key` query-scope tests + `classify_auth` decision-matrix tests.
- **Create** `web/tests/xenonApi.internal-token.test.ts` — Vitest for the header.
- **Modify** `src/xenon/api/CLAUDE.md` (Auth section), `docs/runbooks/remote-deploy.md` (new env vars), `docs/reference/order-path-incident-history.md` (incident row).

---

### Task 1: Read-only query scope for `verify_api_key`

**Files:**

- Modify: `src/xenon/api/auth.py:104-126`
- Test: `src/xenon/api/tests/test_query_api_key.py` (create)

- [ ] **Step 1: Write the failing test**

Create `src/xenon/api/tests/test_query_api_key.py`:

```python
"""Tests for the read-only query API key scope and the auth decision matrix."""

import os
from unittest.mock import patch

from xenon.api.auth import (
    QUERY_API_KEY_PATHS,
    verify_api_key,
    classify_auth,
    AuthDecision,
)


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
        # POST /portfolio is not a read path; only GET is allowlisted
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

    def test_all_query_paths_allowed(self):
        with patch.dict(os.environ, {"XENON_QUERY_API_KEY": "qk"}, clear=False):
            for method, path in QUERY_API_KEY_PATHS:
                req = FakeRequest(path, method, {"X-API-Key": "qk"})
                assert verify_api_key(req) is not None, f"{method} {path} should be allowed"

    def test_query_paths_are_all_get(self):
        # Read-only by construction: every allowlisted query path is GET.
        assert all(m == "GET" for m, _ in QUERY_API_KEY_PATHS)
        assert ("GET", "/portfolio") in QUERY_API_KEY_PATHS
        assert ("GET", "/orders") in QUERY_API_KEY_PATHS
        assert ("GET", "/blotter") in QUERY_API_KEY_PATHS
        assert ("GET", "/journal") in QUERY_API_KEY_PATHS
        assert ("GET", "/futu/portfolio") in QUERY_API_KEY_PATHS
        assert ("GET", "/trades/entry-dates") in QUERY_API_KEY_PATHS
        assert ("GET", "/performance") in QUERY_API_KEY_PATHS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest src/xenon/api/tests/test_query_api_key.py::TestQueryApiKey -x`
Expected: FAIL — `ImportError: cannot import name 'QUERY_API_KEY_PATHS'` (and `classify_auth`/`AuthDecision`).

- [ ] **Step 3: Implement the query scope in `auth.py`**

In `src/xenon/api/auth.py`, after the existing `API_KEY_ALLOWED_PATHS` block (line ~108), add:

```python
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

    Returns a service identity dict if the key matches a scope AND the
    (method,)path is in that scope, else None. Never grants write/order paths.
    """
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        return None

    path = request.url.path

    mdw_key = os.environ.get("MDW_API_KEY")
    if mdw_key and hmac.compare_digest(api_key.encode(), mdw_key.encode()):
        if path in API_KEY_ALLOWED_PATHS:
            return {"sub": "mdw-service", "service": True}

    query_key = os.environ.get("XENON_QUERY_API_KEY")
    if query_key and hmac.compare_digest(api_key.encode(), query_key.encode()):
        if (request.method, path) in QUERY_API_KEY_PATHS:
            return {"sub": "query-service", "service": True, "scope": "read-only"}

    return None
```

Note: `request.method` is only read inside the query-key branch, which the legacy
`test_historical_auth.py` (MDW-only, no query key) never reaches — so its
method-less `FakeRequest` keeps working.

- [ ] **Step 4: Run test to verify the query-scope tests pass**

Run: `uv run pytest src/xenon/api/tests/test_query_api_key.py::TestQueryApiKey -x`
Expected: PASS (note `classify_auth`/`AuthDecision` import still missing → collection error). If the import error blocks collection, temporarily comment the `classify_auth, AuthDecision` import line, run, then restore it for Task 2. Cleaner: do Task 2's Step 1 edit to add the stub before running. Prefer running both Task 1 + Task 2 test additions together if executing inline.

- [ ] **Step 5: Verify legacy auth test still green**

Run: `uv run pytest src/xenon/api/tests/test_historical_auth.py -x`
Expected: PASS (unchanged — MDW scope and `API_KEY_ALLOWED_PATHS` untouched).

- [ ] **Step 6: Commit**

```bash
git add src/xenon/api/auth.py src/xenon/api/tests/test_query_api_key.py
git commit -m "feat(auth): add read-only XENON_QUERY_API_KEY scope (GET-only query paths)"
```

---

### Task 2: Safe-by-default auth decision (`classify_auth`) + internal-token trust

**Files:**

- Modify: `src/xenon/api/auth.py` (add `AuthDecision` + `classify_auth`)
- Test: `src/xenon/api/tests/test_query_api_key.py` (append `TestClassifyAuth`)

- [ ] **Step 1: Write the failing test** — append to `src/xenon/api/tests/test_query_api_key.py`:

```python
class TestClassifyAuth:
    # Each test controls the env explicitly and removes XENON_API_TEST_MODE so the
    # test-mode bypass doesn't mask the decision under test.
    BASE = {"XENON_API_TEST_MODE": "", "CLERK_JWKS_URL": "", "MDW_API_KEY": "",
            "XENON_QUERY_API_KEY": "", "XENON_INTERNAL_API_TOKEN": ""}

    def _env(self, **overrides):
        env = dict(self.BASE)
        env.update(overrides)
        return env

    def test_open_when_no_secret_configured(self):
        with patch.dict(os.environ, self._env(), clear=False):
            for k in self.BASE:
                if not self._env()[k]:
                    os.environ.pop(k, None)
            req = FakeRequest("/portfolio", "GET", {}, client_host="100.66.147.98")
            d = classify_auth(req)
            assert d.action == "pass"
            assert d.identity["open"] is True

    def test_localhost_passes_when_secret_configured(self):
        with patch.dict(os.environ, self._env(XENON_QUERY_API_KEY="qk"), clear=False):
            os.environ.pop("XENON_API_TEST_MODE", None)
            req = FakeRequest("/portfolio", "GET", {}, client_host="127.0.0.1")
            d = classify_auth(req)
            assert d.action == "pass"
            assert d.identity["local"] is True

    def test_internal_token_passes_any_path(self):
        with patch.dict(os.environ, self._env(XENON_INTERNAL_API_TOKEN="itok"), clear=False):
            os.environ.pop("XENON_API_TEST_MODE", None)
            req = FakeRequest("/orders/place", "POST",
                              {"X-Internal-Token": "itok"}, client_host="172.18.0.5")
            d = classify_auth(req)
            assert d.action == "pass"
            assert d.identity["internal"] is True

    def test_query_key_passes_read_path(self):
        with patch.dict(os.environ, self._env(XENON_QUERY_API_KEY="qk"), clear=False):
            os.environ.pop("XENON_API_TEST_MODE", None)
            req = FakeRequest("/portfolio", "GET",
                              {"X-API-Key": "qk"}, client_host="100.66.147.98")
            d = classify_auth(req)
            assert d.action == "pass"
            assert d.identity["sub"] == "query-service"

    def test_query_key_denied_on_write_path(self):
        with patch.dict(os.environ, self._env(XENON_QUERY_API_KEY="qk"), clear=False):
            os.environ.pop("XENON_API_TEST_MODE", None)
            req = FakeRequest("/orders/place", "POST",
                              {"X-API-Key": "qk"}, client_host="100.66.147.98")
            d = classify_auth(req)
            assert d.action == "deny"

    def test_no_creds_denied_when_secret_configured(self):
        with patch.dict(os.environ, self._env(XENON_QUERY_API_KEY="qk"), clear=False):
            os.environ.pop("XENON_API_TEST_MODE", None)
            req = FakeRequest("/portfolio", "GET", {}, client_host="100.66.147.98")
            d = classify_auth(req)
            assert d.action == "deny"

    def test_clerk_branch_when_configured_and_no_other_match(self):
        with patch.dict(os.environ, self._env(CLERK_JWKS_URL="https://x/jwks"), clear=False):
            os.environ.pop("XENON_API_TEST_MODE", None)
            req = FakeRequest("/portfolio", "GET", {}, client_host="100.66.147.98")
            d = classify_auth(req)
            assert d.action == "clerk"

    def test_test_mode_always_passes(self):
        with patch.dict(os.environ, self._env(XENON_API_TEST_MODE="1",
                                              XENON_QUERY_API_KEY="qk"), clear=False):
            req = FakeRequest("/orders/place", "POST", {}, client_host="100.66.147.98")
            d = classify_auth(req)
            assert d.action == "pass"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest src/xenon/api/tests/test_query_api_key.py::TestClassifyAuth -x`
Expected: FAIL — `cannot import name 'classify_auth'`.

- [ ] **Step 3: Implement `AuthDecision` + `classify_auth` in `auth.py`**

Add near the top of `src/xenon/api/auth.py` (after imports, before `_get_jwks_client`):

```python
from dataclasses import dataclass

_LOCALHOST_HOSTS = frozenset({"127.0.0.1", "::1"})


@dataclass(frozen=True)
class AuthDecision:
    """Outcome of the synchronous auth pre-check.

    action: "pass"  → request authorized (use identity); call next.
            "clerk" → fall through to async Clerk JWT validation.
            "deny"  → 401.
    """
    action: str
    identity: dict | None = None
```

Add at the end of `auth.py`:

```python
def classify_auth(request: Request) -> AuthDecision:
    """Synchronous auth decision for the HTTP middleware (exempt paths handled upstream).

    Safe-by-default: the request is only treated as open when NO auth secret is
    configured at all (a dev/test box). Once any secret is set (prod), every
    caller must prove identity: localhost (on-box), the internal UI token, or a
    scoped X-API-Key. Clerk JWT validation is deferred to the async caller.
    """
    # Test/harness contexts bypass auth entirely. XENON_API_TEST_MODE is never
    # set in prod (verified via /health -> test_mode:false), so this cannot
    # weaken production.
    if os.environ.get("XENON_API_TEST_MODE"):
        return AuthDecision("pass", {"sub": "test", "local": True})

    internal_token = os.environ.get("XENON_INTERNAL_API_TOKEN")
    query_key = os.environ.get("XENON_QUERY_API_KEY")
    mdw_key = os.environ.get("MDW_API_KEY")
    clerk = os.environ.get("CLERK_JWKS_URL")

    if not (internal_token or query_key or mdw_key or clerk):
        # No auth configured → dev box, preserve open behavior.
        return AuthDecision("pass", {"sub": "open", "open": True})

    client_host = request.client.host if request.client else None
    if client_host in _LOCALHOST_HOSTS:
        return AuthDecision("pass", {"sub": "localhost", "local": True})

    # Trusted internal UI (web container → api) via shared secret.
    if internal_token:
        hdr = request.headers.get("X-Internal-Token")
        if hdr and hmac.compare_digest(hdr.encode(), internal_token.encode()):
            return AuthDecision("pass", {"sub": "internal-ui", "internal": True})

    # External read-only API key (and MDW historical key).
    identity = verify_api_key(request)
    if identity is not None:
        return AuthDecision("pass", identity)

    if clerk:
        return AuthDecision("clerk")

    return AuthDecision("deny")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest src/xenon/api/tests/test_query_api_key.py -x`
Expected: PASS (both `TestQueryApiKey` and `TestClassifyAuth`).

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/auth.py src/xenon/api/tests/test_query_api_key.py
git commit -m "feat(auth): safe-by-default classify_auth + internal-token trust"
```

---

### Task 3: Wire the middleware to `classify_auth`

**Files:**

- Modify: `src/xenon/api/server.py:642-671`
- Test: `src/xenon/api/tests/test_query_api_key.py` (append `TestMiddlewareIntegration`)

- [ ] **Step 1: Write the failing test** — append to `src/xenon/api/tests/test_query_api_key.py`:

```python
class TestMiddlewareIntegration:
    """Drive the real app middleware via TestClient. TestClient's client.host is
    'testclient' (not localhost), so these exercise the non-localhost path. We
    force-disable test mode for these cases so gating is observable."""

    def _client(self):
        from fastapi.testclient import TestClient
        from xenon.api.server import app
        return TestClient(app)

    def test_health_open_without_auth(self):
        # /health is auth-exempt regardless of config.
        with patch.dict(os.environ, {"XENON_QUERY_API_KEY": "qk",
                                     "XENON_API_TEST_MODE": ""}, clear=False):
            os.environ.pop("XENON_API_TEST_MODE", None)
            r = self._client().get("/health")
            assert r.status_code == 200

    def test_query_path_denied_without_key(self):
        with patch.dict(os.environ, {"XENON_QUERY_API_KEY": "qk",
                                     "XENON_API_TEST_MODE": ""}, clear=False):
            os.environ.pop("XENON_API_TEST_MODE", None)
            r = self._client().get("/trades/entry-dates")
            assert r.status_code == 401

    def test_query_path_allowed_with_key(self):
        with patch.dict(os.environ, {"XENON_QUERY_API_KEY": "qk",
                                     "XENON_API_TEST_MODE": ""}, clear=False):
            os.environ.pop("XENON_API_TEST_MODE", None)
            r = self._client().get("/trades/entry-dates", headers={"X-API-Key": "qk"})
            # 401 must NOT happen; any non-401 (200/404/500 from downstream) means
            # the auth gate passed. Assert the gate, not the handler.
            assert r.status_code != 401
```

Note for the implementer: `/trades/entry-dates` is chosen because it is a thin
DB read with no IB dependency; under the autouse rollback DB fixture it returns
200 with an empty list. If your DB fixture isn't active, it may 500 — that still
proves the auth gate passed (not 401). Keep the assertion as `!= 401`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest src/xenon/api/tests/test_query_api_key.py::TestMiddlewareIntegration -x`
Expected: FAIL — `test_query_path_denied_without_key` returns 200 (old middleware passes through because CLERK unset).

- [ ] **Step 3: Rewrite `auth_middleware` in `server.py`**

Replace the function body at `src/xenon/api/server.py:642-671` with:

```python
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Authorize via classify_auth; defer Clerk JWT to async validation."""
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

Update the import near the top of `server.py` (it currently imports
`verify_clerk_jwt`, `verify_api_key`). Find the existing
`from xenon.api.auth import ...` line and add `classify_auth`:

```python
from xenon.api.auth import verify_clerk_jwt, verify_api_key, classify_auth
```

(If the existing import lists different names, just add `classify_auth` to it.)

- [ ] **Step 4: Run the middleware tests**

Run: `uv run pytest src/xenon/api/tests/test_query_api_key.py -x`
Expected: PASS (all three classes).

- [ ] **Step 5: Run the broader API test suite to catch regressions**

Run: `uv run pytest src/xenon/api/tests -q`
Expected: PASS. If a TestClient-based test that sets a secret in env now 401s, confirm `XENON_API_TEST_MODE` is set in that test/conftest (it should be) — the test-mode bypass covers it. Do NOT widen the open path to fix; investigate the specific test.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/api/server.py src/xenon/api/tests/test_query_api_key.py
git commit -m "feat(auth): middleware delegates to classify_auth (safe-by-default)"
```

---

### Task 4: `xenonFetch` attaches the internal token

**Files:**

- Modify: `web/lib/xenonApi.ts:21-35`
- Test: `web/tests/xenonApi.internal-token.test.ts` (create)

- [ ] **Step 1: Write the failing test**

Create `web/tests/xenonApi.internal-token.test.ts`:

```typescript
import { describe, it, expect, vi, afterEach } from "vitest";
import { xenonFetch } from "../lib/xenonApi";

function jsonResponse() {
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

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
    const headers = new Headers(init.headers);
    expect(headers.get("X-Internal-Token")).toBe("s3cret");
  });

  it("omits X-Internal-Token when env unset", async () => {
    delete process.env.XENON_INTERNAL_API_TOKEN;
    const fetchMock = vi.fn(async () => jsonResponse());
    vi.stubGlobal("fetch", fetchMock);

    await xenonFetch("/portfolio");

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(headers.get("X-Internal-Token")).toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run tests/xenonApi.internal-token.test.ts`
Expected: FAIL — first test: expected `"s3cret"`, got `null`.

- [ ] **Step 3: Implement the header in `xenonFetch`**

In `web/lib/xenonApi.ts`, change the header-building block (lines 25-29) from:

```typescript
const { timeout = 30_000, token, ...fetchOpts } = opts ?? {};
const headers = new Headers(fetchOpts.headers);
if (token) {
  headers.set("Authorization", `Bearer ${token}`);
}
```

to:

```typescript
const { timeout = 30_000, token, ...fetchOpts } = opts ?? {};
const headers = new Headers(fetchOpts.headers);
if (token) {
  headers.set("Authorization", `Bearer ${token}`);
}
// Server-to-server trust: the web container proves it is the trusted UI so
// the api can keep order-write endpoints closed to external callers. Read at
// call time (not module load) so per-request/test env changes take effect.
// MUST be a server-only env var — never NEXT_PUBLIC_ (would leak to browser).
const internalToken = process.env.XENON_INTERNAL_API_TOKEN;
if (internalToken) {
  headers.set("X-Internal-Token", internalToken);
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run tests/xenonApi.internal-token.test.ts`
Expected: PASS (both cases).

- [ ] **Step 5: Commit**

```bash
git add web/lib/xenonApi.ts web/tests/xenonApi.internal-token.test.ts
git commit -m "feat(web): xenonFetch attaches X-Internal-Token for trusted web->api"
```

---

### Task 5: Documentation

**Files:**

- Modify: `src/xenon/api/CLAUDE.md` (Auth section)
- Modify: `docs/runbooks/remote-deploy.md` (env vars)
- Modify: `docs/reference/order-path-incident-history.md` (incident row)

- [ ] **Step 1: Update `src/xenon/api/CLAUDE.md` — replace the `## Auth — Security-Relevant Behavior` section body with:**

```markdown
## Auth — Security-Relevant Behavior

**Auth-exempt paths:** `/health`, `/ws-ticket/validate`, `/docs`, `/openapi.json`.

**Decision is centralized in `auth.classify_auth(request)`** (synchronous), called
by `server.py::auth_middleware`. Safe-by-default:

1. `XENON_API_TEST_MODE` set → pass (test/harness only; never set in prod).
2. **No auth secret configured at all** (`XENON_INTERNAL_API_TOKEN` /
   `XENON_QUERY_API_KEY` / `MDW_API_KEY` / `CLERK_JWKS_URL` all unset) → pass.
   This is the _only_ open path and exists for dev boxes. **Prod sets secrets, so
   prod is never open.** (This replaced the old "CLERK unset ⇒ open to all"
   default, which left the prod API publicly writable over Tailscale/LAN.)
3. Source `127.0.0.1`/`::1` → pass (on-box / dev stack).
4. Valid `X-Internal-Token` == `XENON_INTERNAL_API_TOKEN` → pass with full access.
   This is how the web container (`web→api`, a non-localhost compose call) stays
   trusted without forwarding Clerk JWTs. `xenonFetch` attaches it automatically.
5. Valid `X-API-Key`: `XENON_QUERY_API_KEY` grants **GET-only** read paths
   (`QUERY_API_KEY_PATHS`: portfolio, orders, blotter, journal, futu/portfolio,
   trades/entry-dates, performance); `MDW_API_KEY` grants `/historical/*` +
   `/contract/qualify` (`API_KEY_ALLOWED_PATHS`). Neither grants write/sync paths.
6. Clerk configured and nothing above matched → async JWT validation.
7. Else → 401.

**Order-write endpoints** (`/orders/place|cancel|modify`, `/portfolio/sync`,
`POST /blotter`, `POST /futu/sync`) are never in any API-key allowlist, so an
external key cannot reach them — only the internal UI token or localhost can.

Component map, files, ticket flow: `docs/architecture/api-infrastructure.md`.
```

- [ ] **Step 2: Update `docs/runbooks/remote-deploy.md` — add to the `/opt/xenon/.env` and `web.env` documentation:**

Add a subsection near the env documentation (search for `/opt/xenon/.env`):

```markdown
### Query-API auth env (added 2026-06-17)

The api authorizes external read-only callers and the internal UI via secrets.
Set in `/opt/xenon/.env` (api/realtime/migrator) and mirror the internal token
into `/opt/xenon/web.env` (web container):

| Var                        | File(s)                  | Purpose                                                                                                          |
| -------------------------- | ------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| `XENON_INTERNAL_API_TOKEN` | `.env` **and** `web.env` | Shared secret proving `web→api` is the trusted UI (full access). Same value in both files. Never `NEXT_PUBLIC_`. |
| `XENON_QUERY_API_KEY`      | `.env`                   | Read-only key for external scripts. Send as `X-API-Key`. GET-only query paths.                                   |

Generate strong values, e.g. `openssl rand -hex 32`. After editing, recreate
containers so env_file changes load: `docker compose up -d`. Without these set,
the api falls back to the dev-only open path — do not run prod without them.
```

- [ ] **Step 3: Append a row to `docs/reference/order-path-incident-history.md`** (match the existing table format; read the file's header row first and mirror its columns). Content of the row:

> Date `2026-06-17`; Symptom: prod API reachable over Tailscale/LAN with **no auth**, incl. `/orders/place|cancel|modify` (open order placement); Root cause: `auth_middleware` passed all requests through when `CLERK_JWKS_URL` unset in the container, and the port is published `0.0.0.0:8321`; Fix: `classify_auth` safe-by-default (open only when _no_ secret configured), internal-token trust for `web→api`, read-only `XENON_QUERY_API_KEY` scope; Regression test: `src/xenon/api/tests/test_query_api_key.py`.

- [ ] **Step 4: Commit**

```bash
git add src/xenon/api/CLAUDE.md docs/runbooks/remote-deploy.md docs/reference/order-path-incident-history.md
git commit -m "docs(auth): document query-API key + internal-token model and prod env"
```

---

### Task 6: Full local verification gate

**Files:** none (verification only)

- [ ] **Step 1: Python — affected + auth suites**

Run: `uv run python scripts/infra/dev/run_pytest_affected.py`
Then: `uv run pytest src/xenon/api/tests/test_query_api_key.py src/xenon/api/tests/test_historical_auth.py -q`
Expected: PASS, 0 failures.

- [ ] **Step 2: Web — vitest + typecheck + lint**

Run:

```bash
cd web && npm test && npm run typecheck && npm run lint
```

Expected: PASS. `xenonApi.internal-token.test.ts` included in vitest run.

- [ ] **Step 3: Record evidence**

Paste the final summary lines (test counts, 0 failures) into the PR description in Task 7. Do not claim green without the output.

---

### Task 7: PR + CI

**Files:** none (git/PR ops)

- [ ] **Step 1: Push branch + open PR**

```bash
git push -u origin feat/readonly-query-api-auth
gh pr create --title "feat(auth): read-only query API key + close open prod auth hole" \
  --body "$(cat <<'EOF'
## Summary
Prod API was reachable over Tailscale/LAN with NO auth (incl. order placement) because the auth middleware passed everything through when CLERK_JWKS_URL was unset. This makes auth safe-by-default and adds a read-only X-API-Key (XENON_QUERY_API_KEY) for external query scripts, while keeping the web UI fully working via an internal shared-secret token.

## Changes
- `classify_auth` safe-by-default decision (open only when NO secret configured).
- `XENON_QUERY_API_KEY` → GET-only query paths (portfolio/orders/blotter/journal/futu/trades/performance).
- `XENON_INTERNAL_API_TOKEN` → trusted web→api; `xenonFetch` attaches it.
- Order-write endpoints unreachable by external key.

## Tests
- `src/xenon/api/tests/test_query_api_key.py` (key scope, decision matrix, middleware).
- `web/tests/xenonApi.internal-token.test.ts`.
- (paste local green output here)

## Deploy (operator)
Requires `XENON_INTERNAL_API_TOKEN` (in `/opt/xenon/.env` + `web.env`) and
`XENON_QUERY_API_KEY` (in `/opt/xenon/.env`) before `up -d`. See remote-deploy runbook.
EOF
)"
```

- [ ] **Step 2: Watch CI to a real conclusion (do not trust `--watch` exit code)**

```bash
gh pr checks --watch || true
gh pr view --json statusCheckRollup -q '.statusCheckRollup[] | "\(.name): \(.conclusion)"'
```

Expected: every check `SUCCESS`. If any is not, fix before proceeding.

---

### Task 8: Deploy to prod + live E2E validation (OPERATOR-CONFIRMED)

> **STOP GATE:** This task ships code to the live trading macmini and changes
> prod auth. Before running it, the executing agent MUST pause and get explicit
> human confirmation. Deploy steps are outward-facing/irreversible-ish.

**Files:** none (deploy + verification)

- [ ] **Step 1: Generate secrets (operator)**

```bash
echo "XENON_INTERNAL_API_TOKEN=$(openssl rand -hex 32)"
echo "XENON_QUERY_API_KEY=$(openssl rand -hex 32)"
```

Record both securely (you'll need the query key for the curl in Step 5).

- [ ] **Step 2: Set env on the mini** (per remote-deploy runbook; `ssh macmini` prefixes `PATH=/opt/homebrew/bin:$PATH`)

- Add `XENON_INTERNAL_API_TOKEN=<tok>` and `XENON_QUERY_API_KEY=<key>` to `/opt/xenon/.env`.
- Add `XENON_INTERNAL_API_TOKEN=<tok>` (same value) to `/opt/xenon/web.env`.

- [ ] **Step 3: Ship the code via GHCR** — merge the PR, then cut a release so `release.yml::ghcr-push` builds new `api`/`web` images:

```bash
# from this dev Mac, on master after merge:
./scripts/release/cut.sh           # interactive version bump
git push origin master --follow-tags
# wait for release.yml ghcr-push to publish ghcr.io/moremeds/xenon-{api,web}:<ver>
```

- [ ] **Step 4: Pull + recreate on the mini** (bump tags in `/opt/xenon/compose.yml` to `<ver>`):

```bash
ssh macmini 'export PATH=/opt/homebrew/bin:$PATH; cd /opt/xenon && \
  docker compose pull api web && docker compose up -d api web'
ssh macmini 'export PATH=/opt/homebrew/bin:$PATH; cd /opt/xenon && docker compose ps'
```

- [ ] **Step 5: Live E2E — real REST queries over Tailscale**

```bash
KEY=<XENON_QUERY_API_KEY>

# (a) No key → now 401 (was 200 before the fix)
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

# (e) Write path with key → MUST be 401 (order placement no longer exposed)
curl -s -o /dev/null -w "key POST /orders/place -> %{http_code}\n" -X POST \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{}' \
  http://100.66.147.98:8321/orders/place

# (f) /health still open (auth-exempt)
curl -s -o /dev/null -w "/health -> %{http_code}\n" http://100.66.147.98:8321/health
```

Expected: (a) 401, (b) 200 with real bankroll/positions, (c) 200 with real orders,
(d) 200 with real Futu data, (e) **401**, (f) 200.

- [ ] **Step 6: Confirm the prod UI still works** — browser (chrome-cdp) to the prod web URL; verify the portfolio + orders pages render (the internal-token path keeps `web→api` authorized). No auth errors in the console/network tab.

- [ ] **Step 7: Record validation evidence** in the PR / a comment: the curl status lines + a redacted snippet of real JSON, and the UI screenshot/confirmation.

---

## Self-Review (completed during planning)

- **Spec coverage:** Approach A (read-only key + lock writes + keep UI via internal token + close open default) — Tasks 1 (query key scope), 2 (safe-by-default + internal token), 3 (middleware), 4 (xenonFetch), 5 (docs), 8 (deploy + live validation incl. write-path 401). ✔ All design points covered.
- **Placeholder scan:** No TBD/TODO; every code step shows full code; doc steps show full content. ✔
- **Type consistency:** `QUERY_API_KEY_PATHS` (frozenset of `(method, path)`), `AuthDecision(action, identity)`, `classify_auth(request) -> AuthDecision`, identity dict keys (`open`/`local`/`internal`/`sub`/`scope`) used consistently across Tasks 1–3 and the middleware. `verify_api_key` keeps returning `dict | None`. ✔
- **Backward-compat:** `API_KEY_ALLOWED_PATHS` + `MDW_API_KEY` behavior untouched → `test_historical_auth.py` unchanged-green (Task 1 Step 5). No-secret + test-mode paths preserve existing TestClient suites (Task 3 Step 5). ✔

```

```
