"""Authentication middleware for FastAPI.

Supports two auth methods:
1. Clerk JWT — browser-based user auth via JWKS
2. API key — headless machine-to-machine auth (scoped to read-only data endpoints)
"""

import hmac
import logging
import os
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

logger = logging.getLogger("xenon.auth")

_jwks_client = None
_algorithms = ["RS256"]

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


def _get_jwks_client():
    """Lazy-initialize JWKS client with key caching."""
    global _jwks_client
    if _jwks_client is None:
        import jwt as pyjwt

        jwks_url = os.environ.get("CLERK_JWKS_URL", "")
        if not jwks_url:
            raise RuntimeError("CLERK_JWKS_URL not set")
        _jwks_client = pyjwt.PyJWKClient(jwks_url, cache_keys=True)
    return _jwks_client


def _get_allowed_users() -> set[str]:
    """Parse comma-separated ALLOWED_USER_IDS env var."""
    raw = os.environ.get("ALLOWED_USER_IDS", "")
    return {uid.strip() for uid in raw.split(",") if uid.strip()}


def _get_issuer() -> str:
    """Get Clerk issuer URL from env."""
    return os.environ.get("CLERK_ISSUER", "")


async def verify_clerk_jwt(request: Request) -> dict:
    """FastAPI dependency: extract and validate Clerk JWT from Authorization header.

    Returns the decoded payload on success.
    Raises HTTPException(401) for missing/invalid tokens.
    Raises HTTPException(403) for non-allowlisted users.
    Bypasses validation for localhost requests (server-to-server).
    """
    # Skip auth for server-to-server calls from localhost (Next.js → FastAPI)
    client_host = request.client.host if request.client else None
    if client_host in ("127.0.0.1", "::1"):
        return {"sub": "localhost", "local": True}

    import jwt as pyjwt

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth_header.removeprefix("Bearer ")

    try:
        jwks_client = _get_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token)

        issuer = _get_issuer()
        decode_options = {"verify_aud": False}

        payload = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=_algorithms,
            issuer=issuer if issuer else None,
            options=decode_options,
        )
    except pyjwt.exceptions.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except pyjwt.exceptions.PyJWTError as e:
        logger.warning("JWT validation failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid token")

    allowed = _get_allowed_users()
    if allowed and payload.get("sub") not in allowed:
        logger.warning("Access denied for user %s", payload.get("sub"))
        raise HTTPException(status_code=403, detail="Not authorized")

    return payload


def auth_required():
    """Return the verify_clerk_jwt dependency for use in route decorators.

    Usage: @app.get("/protected", dependencies=[Depends(auth_required())])
    """
    return Depends(verify_clerk_jwt)


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
        # Orders / fills / journal
        ("GET", "/orders"),
        ("GET", "/blotter"),
        ("GET", "/journal"),
        ("GET", "/trades/entry-dates"),
        # Performance / NAV
        ("GET", "/performance"),
        # Market data (read-only IB fetches — POST only because they take a body)
        ("GET", "/options/chain"),
        ("GET", "/options/expirations"),
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
    if internal_token and query_key and hmac.compare_digest(internal_token.encode(), query_key.encode()):
        raise RuntimeError(
            "XENON_INTERNAL_API_TOKEN must differ from XENON_QUERY_API_KEY "
            "(equal values let a leaked read-only key gain write access)"
        )
    configured = bool(internal_token or query_key or os.environ.get("MDW_API_KEY") or os.environ.get("CLERK_JWKS_URL"))
    if configured:
        return "auth: ENFORCED (authenticated access required for non-localhost)"
    if _truthy_env("XENON_AUTH_ALLOW_DEV_OPEN"):
        return "auth: DEV-OPEN (XENON_AUTH_ALLOW_DEV_OPEN=1; non-localhost requests pass)"
    return "auth: FAIL-CLOSED (no secrets, no dev-open; non-localhost requests denied)"
