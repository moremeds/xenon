"""Authentication middleware for FastAPI.

Two credentials only — no peer-address trust (a tailscale-serve / Docker proxy
makes every remote peer look local, so client IP is never an identity):

1. X-Internal-Token — Next.js server → API. Full access.
2. X-API-Key — machine-to-machine, path-scoped to read-only data endpoints:
   MDW_API_KEY → API_KEY_ALLOWED_PATHS; XENON_QUERY_API_KEY → QUERY_API_KEY_PATHS.

/health and /ws-ticket/validate are exempt upstream (server.py AUTH_EXEMPT_PATHS).
XENON_AUTH_ALLOW_DEV_OPEN=1 is a dev/test-only escape hatch production never sets.
"""

import hmac
import logging
import os
from dataclasses import dataclass

from fastapi import Request

logger = logging.getLogger("xenon.auth")


@dataclass(frozen=True)
class AuthDecision:
    """Outcome of the synchronous auth pre-check.

    action: "pass" → authorized (use identity); call next.
            "deny" → 401.
    """

    action: str
    identity: dict | None = None


# ---------------------------------------------------------------------------
# API key auth — scoped to read-only historical/contract endpoints
# ---------------------------------------------------------------------------

API_KEY_ALLOWED_PATHS = frozenset(
    {
        "/contract/qualify",
        "/historical/head-timestamp",
        "/historical/bars",
    }
)


def _truthy_env(name: str) -> bool:
    """Strict truthy parse mirroring server._is_test_mode — '0'/'false'/'' are False."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# Read-only query key — (METHOD, PATH) tuples. GET-only by construction so an
# external key can never trigger a write/sync. /blotter and /performance also
# have POST siblings (sync/rebuild) which are deliberately excluded.
QUERY_API_KEY_PATHS = frozenset(
    {
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
        # Market data (read-only IB fetches — POST only because they take a body)
        ("GET", "/options/chain"),
        ("GET", "/options/expirations"),
        ("GET", "/options/greeks"),
        ("GET", "/market-depth"),
        ("POST", "/historical/bars"),
        ("POST", "/historical/head-timestamp"),
        ("POST", "/contract/qualify"),
        # Watchlist
        ("GET", "/watchlist"),
        # WebSocket ticket — allows external clients to open the realtime feed
        ("POST", "/ws-ticket"),
    }
)


def verify_api_key(request: Request) -> dict | None:
    """Check X-API-Key against the two configured key scopes.

    - MDW_API_KEY         → historical/contract endpoints (API_KEY_ALLOWED_PATHS), unchanged.
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


def classify_auth(request: Request) -> AuthDecision:
    """Fail-closed auth decision for the HTTP middleware (exempt paths handled upstream).

    A non-exempt request is DENIED unless it proves identity. The only
    non-authenticated pass is the explicit dev opt-in XENON_AUTH_ALLOW_DEV_OPEN=1,
    which production never sets — so an unloaded/misconfigured prod env denies
    rather than exposes the trading API. Client address is never consulted:
    reachability is not authentication.
    """
    internal_token = os.environ.get("XENON_INTERNAL_API_TOKEN")
    if internal_token:
        hdr = request.headers.get("X-Internal-Token")
        if hdr and hmac.compare_digest(hdr.encode(), internal_token.encode()):
            return AuthDecision("pass", {"sub": "internal-ui", "internal": True})

    identity = verify_api_key(request)
    if identity is not None:
        return AuthDecision("pass", identity)

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
    if internal_token and query_key and hmac.compare_digest(internal_token.encode(), query_key.encode()):
        raise RuntimeError(
            "XENON_INTERNAL_API_TOKEN must differ from XENON_QUERY_API_KEY "
            "(equal values let a leaked read-only key gain write access)"
        )
    configured = bool(internal_token or query_key or os.environ.get("MDW_API_KEY"))
    if configured:
        return "auth: ENFORCED (credential required for all non-exempt paths)"
    if _truthy_env("XENON_AUTH_ALLOW_DEV_OPEN"):
        return "auth: DEV-OPEN (XENON_AUTH_ALLOW_DEV_OPEN=1; requests pass unauthenticated)"
    return "auth: FAIL-CLOSED (no secrets, no dev-open; all non-exempt requests denied)"
