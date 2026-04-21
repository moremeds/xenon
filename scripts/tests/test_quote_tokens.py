import time
from decimal import Decimal

import pytest
from xenon.execution.quote_tokens import QuoteTokenExpired, QuoteTokenInvalid

from xenon.execution import quote_tokens

SECRET = "a" * 64


def _payload():
    return quote_tokens.QuotePayload(
        con_id=756733,
        ticker="SPY",
        bid=Decimal("500.10"),
        ask=Decimal("500.20"),
        bid_size=100,
        ask_size=120,
        ts_server_ms=int(time.time() * 1000),
    )


def test_mint_then_verify_roundtrip():
    token = quote_tokens.mint(_payload(), SECRET)
    out = quote_tokens.verify(token, SECRET, max_age_ms=500)
    assert out.ticker == "SPY"
    assert out.bid == Decimal("500.10")


def test_tampered_token_rejected():
    token = quote_tokens.mint(_payload(), SECRET)
    tampered = token[:-4] + ("A" if token[-1] != "A" else "B") * 4
    with pytest.raises(QuoteTokenInvalid):
        quote_tokens.verify(tampered, SECRET, max_age_ms=500)


def test_wrong_secret_rejected():
    token = quote_tokens.mint(_payload(), SECRET)
    with pytest.raises(QuoteTokenInvalid):
        quote_tokens.verify(token, "wrong-secret", max_age_ms=500)


def test_expired_token_rejected():
    stale = _payload().model_copy(update={"ts_server_ms": int(time.time() * 1000) - 10_000})
    token = quote_tokens.mint(stale, SECRET)
    with pytest.raises(QuoteTokenExpired):
        quote_tokens.verify(token, SECRET, max_age_ms=500)
