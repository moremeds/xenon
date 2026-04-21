"""Server-signed quote snapshot tokens.

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §7.

Format: base64url(json_payload) + "." + base64url(hmac_sha256(json_payload, secret)).
Client never decodes — it only passes the opaque token back on submit.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import time
from decimal import Decimal
from hashlib import sha256

from pydantic import BaseModel


class QuoteTokenInvalid(Exception):
    pass


class QuoteTokenExpired(Exception):
    pass


class QuotePayload(BaseModel):
    con_id: int
    ticker: str
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    ts_server_ms: int


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def _payload_bytes(p: QuotePayload) -> bytes:
    d = {
        "con_id": p.con_id,
        "ticker": p.ticker,
        "bid": str(p.bid),
        "ask": str(p.ask),
        "bid_size": p.bid_size,
        "ask_size": p.ask_size,
        "ts_server_ms": p.ts_server_ms,
    }
    return json.dumps(d, separators=(",", ":"), sort_keys=True).encode("utf-8")


def mint(payload: QuotePayload, secret: str) -> str:
    body = _payload_bytes(payload)
    sig = hmac.new(secret.encode("utf-8"), body, sha256).digest()
    return f"{_b64e(body)}.{_b64e(sig)}"


def verify(token: str, secret: str, max_age_ms: int) -> QuotePayload:
    try:
        body_b64, sig_b64 = token.split(".", 1)
        body = _b64d(body_b64)
        sig = _b64d(sig_b64)
    except (ValueError, binascii.Error) as exc:
        raise QuoteTokenInvalid("malformed token") from exc

    expected = hmac.new(secret.encode("utf-8"), body, sha256).digest()
    if not hmac.compare_digest(expected, sig):
        raise QuoteTokenInvalid("signature mismatch")

    data = json.loads(body.decode("utf-8"))
    payload = QuotePayload(
        con_id=data["con_id"],
        ticker=data["ticker"],
        bid=Decimal(data["bid"]),
        ask=Decimal(data["ask"]),
        bid_size=data["bid_size"],
        ask_size=data["ask_size"],
        ts_server_ms=data["ts_server_ms"],
    )
    age_ms = int(time.time() * 1000) - payload.ts_server_ms
    if age_ms < 0 or age_ms > max_age_ms:
        raise QuoteTokenExpired(f"quote age {age_ms} ms > {max_age_ms}")
    return payload
