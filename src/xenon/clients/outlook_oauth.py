"""MSAL device-code OAuth helper for consumer Outlook IMAP XOAUTH2.

Microsoft retired basic-auth IMAP for most consumer outlook.com accounts.
Even with app passwords enabled and "Let devices use IMAP" toggled on,
the server returns AUTHENTICATE failed. The supported path is XOAUTH2:
exchange an OAuth access token (via MSAL device-code flow against an app
the user registered) for IMAP authentication.

One-time setup the operator does:
  1. https://entra.microsoft.com → App registrations → New registration
  2. Supported account types: "Personal Microsoft accounts only"
  3. Authentication → Allow public client flows: Yes
  4. API permissions → Microsoft Graph → Delegated:
       - IMAP.AccessAsUser.All
       - offline_access
  5. Copy the Application (client) ID → set OUTLOOK_OAUTH_CLIENT_ID in .env

Runtime:
  - First call prints a device code + URL; the operator signs in once
    in a browser; the refresh token persists in TOKEN_CACHE_PATH.
  - Subsequent calls use cached refresh token silently.

The token cache is operator-bound (not committed, not part of any backup
flow). Treat the cache file like a credential.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import msal

logger = logging.getLogger(__name__)

# Personal-only authority; this is the public Microsoft endpoint for MSA
# accounts (outlook.com, hotmail.com, live.com).
AUTHORITY = "https://login.microsoftonline.com/consumers"

# Outlook IMAP scope. `offline_access` is implicit on v2.0 endpoint but
# we list it for clarity. MSAL drops scopes it doesn't recognise.
SCOPES = ["https://outlook.office.com/IMAP.AccessAsUser.All"]

# Default cache lives outside the repo. The cache stores a refresh token,
# treat it like a credential — chmod 0600 by the OS umask when created.
DEFAULT_CACHE_PATH = Path.home() / ".cache" / "xenon" / "outlook-msal-cache.json"

CLIENT_ID_ENV = "OUTLOOK_OAUTH_CLIENT_ID"


class OAuthSetupError(RuntimeError):
    """OUTLOOK_OAUTH_CLIENT_ID not set, or device-code flow refused."""


@dataclass(frozen=True)
class OAuthToken:
    access_token: str
    account_username: str  # the MSA login that signed in; can differ from OUTLOOK_USER


def _load_cache(path: Path) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if path.exists():
        try:
            cache.deserialize(path.read_text())
        except (OSError, ValueError) as exc:
            logger.warning("token cache at %s unreadable, starting fresh: %s", path, exc)
    return cache


def _save_cache(cache: msal.SerializableTokenCache, path: Path) -> None:
    if not cache.has_state_changed:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write with restrictive perms: open with O_CREAT|O_WRONLY|O_TRUNC at 0600.
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, cache.serialize().encode("utf-8"))
    finally:
        os.close(fd)


def acquire_token(
    client_id: Optional[str] = None,
    cache_path: Path = DEFAULT_CACHE_PATH,
    prompt_callback=None,
) -> OAuthToken:
    """Return a fresh access token, prompting via device-code flow only if needed.

    `prompt_callback`, if provided, receives the dict that MSAL emits with
    user_code / verification_uri / message. Defaults to printing to stderr.
    """
    cid = client_id or os.environ.get(CLIENT_ID_ENV)
    if not cid:
        raise OAuthSetupError(
            f"{CLIENT_ID_ENV} not set; register an app on entra.microsoft.com "
            "with personal accounts + IMAP.AccessAsUser.All + offline_access, "
            "then set OUTLOOK_OAUTH_CLIENT_ID in .env"
        )

    cache = _load_cache(cache_path)
    app = msal.PublicClientApplication(cid, authority=AUTHORITY, token_cache=cache)

    accounts = app.get_accounts()
    result: Optional[dict] = None
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise OAuthSetupError(f"device flow initiation failed: {flow.get('error_description') or flow}")
        if prompt_callback is not None:
            prompt_callback(flow)
        else:
            import sys

            print(flow["message"], file=sys.stderr, flush=True)
        result = app.acquire_token_by_device_flow(flow)

    _save_cache(cache, cache_path)

    if "access_token" not in result:
        raise OAuthSetupError(f"token acquisition failed: {result.get('error_description') or result}")

    # Best-effort username from MSAL claim payload.
    username = ""
    try:
        username = result.get("id_token_claims", {}).get("preferred_username") or ""
    except AttributeError:
        pass
    if not username and accounts:
        username = accounts[0].get("username", "")

    return OAuthToken(access_token=result["access_token"], account_username=username)


def build_xoauth2_sasl(user: str, access_token: str) -> bytes:
    """Build the SASL XOAUTH2 initial response per Google/Microsoft spec.

    Format:
        user=<email>\\x01auth=Bearer <token>\\x01\\x01

    IMAP imaplib.IMAP4.authenticate expects raw bytes (no base64 — imaplib
    does the encoding itself when AUTHENTICATE is used with a callable).
    """
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")
