"""Tests for the read-only query API key scope and the auth decision matrix."""

import os
from unittest.mock import patch

from xenon.api.auth import (
    QUERY_API_KEY_PATHS,
    classify_auth,
    validate_auth_config,
    verify_api_key,
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

    def test_query_paths_are_complete(self):
        # Market-data endpoints use POST because they take a request body but
        # are still read-only (no state changes). Only state-mutating POSTs are
        # excluded — see test_write_and_sync_paths_never_granted_by_query_key.
        expected = {
            # Portfolio / account
            ("GET", "/portfolio"),
            ("GET", "/futu/portfolio"),
            ("GET", "/attribution"),
            # Orders / fills / journal / live quote
            ("GET", "/orders"),
            ("GET", "/orders/quote"),
            ("GET", "/blotter"),
            ("GET", "/journal"),
            ("GET", "/trades/entry-dates"),
            # Performance / NAV
            ("GET", "/performance"),
            # Market data
            ("GET", "/options/chain"),
            ("GET", "/options/expirations"),
            ("GET", "/options/greeks"),
            ("GET", "/market-depth"),
            ("POST", "/historical/bars"),
            ("POST", "/historical/head-timestamp"),
            ("POST", "/contract/qualify"),
            # Watchlist
            ("GET", "/watchlist"),
            # WebSocket ticket (allows external clients to open the realtime feed)
            ("POST", "/ws-ticket"),
        }
        assert set(QUERY_API_KEY_PATHS) == expected

    def test_market_depth_get_granted_post_denied(self):
        # GET /market-depth is the read-only L2 snapshot; POST is not in scope.
        with patch.dict(os.environ, {"XENON_QUERY_API_KEY": "qk"}, clear=False):
            allowed = FakeRequest("/market-depth", "GET", {"X-API-Key": "qk"})
            assert verify_api_key(allowed) is not None
            denied = FakeRequest("/market-depth", "POST", {"X-API-Key": "qk"})
            assert verify_api_key(denied) is None

    def test_options_greeks_get_granted_post_denied(self):
        # GET /options/greeks is the read-only broker-greeks snapshot; POST is not in scope.
        with patch.dict(os.environ, {"XENON_QUERY_API_KEY": "qk"}, clear=False):
            allowed = FakeRequest("/options/greeks", "GET", {"X-API-Key": "qk"})
            assert verify_api_key(allowed) is not None
            denied = FakeRequest("/options/greeks", "POST", {"X-API-Key": "qk"})
            assert verify_api_key(denied) is None

    def test_write_and_sync_paths_never_granted_by_query_key(self):
        # Assert the query key is rejected for every mutating/sync path.
        write_paths = [
            ("POST", "/orders/place"),
            ("POST", "/orders/cancel"),
            ("POST", "/orders/modify"),
            ("POST", "/orders/refresh"),
            ("POST", "/portfolio/sync"),
            ("POST", "/portfolio/background-sync"),
            ("POST", "/blotter"),
            ("POST", "/performance"),
            ("POST", "/performance/background"),
            ("POST", "/futu/sync"),
            ("POST", "/ib/restart"),
            ("POST", "/journal"),
            ("POST", "/journal/sync"),
            ("POST", "/watchlist"),
            ("DELETE", "/watchlist/AAPL"),
            ("POST", "/wizard/sessions/abc/submit"),
            ("POST", "/wizard/sessions/abc/reprice"),
            ("POST", "/wizard/sessions/abc/protect"),
            ("POST", "/wizard/sessions/abc/abort"),
        ]
        with patch.dict(os.environ, {"XENON_QUERY_API_KEY": "qk"}, clear=False):
            for method, path in write_paths:
                req = FakeRequest(path, method, {"X-API-Key": "qk"})
                assert verify_api_key(req) is None, f"{method} {path} must be denied"


class TestClassifyAuth:
    # BASE patches every auth env var to "" (falsy) via patch.dict(clear=False),
    # overriding the root conftest's XENON_AUTH_ALLOW_DEV_OPEN=1 and anything from
    # .env. So the decision under test is never masked by the dev-open pass or a
    # leaked secret. Each test overrides only the var(s) it exercises.
    BASE = {
        "CLERK_JWKS_URL": "",
        "MDW_API_KEY": "",
        "XENON_QUERY_API_KEY": "",
        "XENON_INTERNAL_API_TOKEN": "",
        "XENON_AUTH_ALLOW_DEV_OPEN": "",
    }

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
            req = FakeRequest("/orders/place", "POST", {"X-Internal-Token": "itok"}, client_host="172.18.0.5")
            d = classify_auth(req)
            assert d.action == "pass"
            assert d.identity["internal"] is True

    def test_internal_token_wrong_value_denied(self):
        with patch.dict(os.environ, self._env(XENON_INTERNAL_API_TOKEN="itok"), clear=False):
            req = FakeRequest("/orders/place", "POST", {"X-Internal-Token": "wrong"}, client_host="172.18.0.5")
            assert classify_auth(req).action == "deny"

    def test_query_key_passes_read_path(self):
        with patch.dict(os.environ, self._env(XENON_QUERY_API_KEY="qk"), clear=False):
            req = FakeRequest("/portfolio", "GET", {"X-API-Key": "qk"}, client_host="100.66.147.98")
            d = classify_auth(req)
            assert d.action == "pass"
            assert d.identity["sub"] == "query-service"

    def test_query_key_denied_on_write_path(self):
        with patch.dict(os.environ, self._env(XENON_QUERY_API_KEY="qk"), clear=False):
            req = FakeRequest("/orders/place", "POST", {"X-API-Key": "qk"}, client_host="100.66.147.98")
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
        import pytest

        with patch.dict(os.environ, {"XENON_INTERNAL_API_TOKEN": "same", "XENON_QUERY_API_KEY": "same"}, clear=False):
            with pytest.raises(RuntimeError):
                validate_auth_config()

    def test_ok_when_distinct(self):
        with patch.dict(os.environ, {"XENON_INTERNAL_API_TOKEN": "a", "XENON_QUERY_API_KEY": "b"}, clear=False):
            assert isinstance(validate_auth_config(), str)


class TestMiddlewareIntegration:
    """Drive the real app middleware via TestClient (client.host == 'testclient',
    non-localhost). GATED disables dev-open so gating is observable."""

    GATED = {
        "XENON_QUERY_API_KEY": "qk",
        "XENON_AUTH_ALLOW_DEV_OPEN": "",
        "XENON_INTERNAL_API_TOKEN": "",
        "CLERK_JWKS_URL": "",
        "MDW_API_KEY": "",
    }

    def _client(self):
        from fastapi.testclient import TestClient

        from xenon.api.server import app

        return TestClient(app)

    def test_health_open_without_auth(self):
        with patch.dict(os.environ, self.GATED, clear=False):
            assert self._client().get("/health").status_code == 200

    def test_openapi_now_requires_auth(self):
        # /openapi.json is no longer exempt.
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
        # /ws-ticket must work via the middleware identity (no Clerk in prod).
        # create_ticket is in-memory, so this is fully deterministic.
        with patch.dict(os.environ, {**self.GATED, "XENON_INTERNAL_API_TOKEN": "itok"}, clear=False):
            r = self._client().post("/ws-ticket", headers={"X-Internal-Token": "itok"})
            assert r.status_code == 200
            assert "ticket" in r.json()

    def test_ws_ticket_denied_without_creds(self):
        with patch.dict(os.environ, self.GATED, clear=False):
            assert self._client().post("/ws-ticket").status_code == 401
