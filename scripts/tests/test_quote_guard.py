import time
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from xenon.execution import quote_guard, quote_tokens
from xenon.execution.preflight import ReasonCode

SECRET = "b" * 64
NYC = ZoneInfo("America/New_York")
MIDDAY_RTH = datetime(2026, 4, 22, 13, 0, tzinfo=NYC)
AFTER_HOURS = datetime(2026, 4, 22, 17, 0, tzinfo=NYC)


def _payload(**over):
    p = dict(
        con_id=756733,
        ticker="SPY",
        bid=Decimal("500.10"),
        ask=Decimal("500.20"),
        bid_size=100,
        ask_size=120,
        ts_server_ms=int(time.time() * 1000),
    )
    p.update(over)
    return quote_tokens.QuotePayload(**p)


def _tick_rule(con_id: int):
    if con_id == 777:
        return Decimal("0.05")
    return Decimal("0.01")


def test_fresh_token_on_tick_and_in_band_accepts():
    token = quote_tokens.mint(_payload(), SECRET)
    v = quote_guard.check(
        token=token,
        token_secret=SECRET,
        con_id=756733,
        ticker="SPY",
        security_type="STK",
        action="BUY",
        limit_price=Decimal("500.20"),
        now=MIDDAY_RTH,
        tick_rule_lookup=_tick_rule,
    )
    assert v.accept is True


def test_stale_token_rejects_with_STALE_QUOTE():
    stale = _payload(ts_server_ms=int(time.time() * 1000) - 10_000)
    token = quote_tokens.mint(stale, SECRET)
    v = quote_guard.check(
        token=token,
        token_secret=SECRET,
        con_id=756733,
        ticker="SPY",
        security_type="STK",
        action="BUY",
        limit_price=Decimal("500.20"),
        now=MIDDAY_RTH,
        tick_rule_lookup=_tick_rule,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.STALE_QUOTE


def test_off_tick_cheap_option_rejects_with_LIMIT_OFF_TICK():
    token = quote_tokens.mint(_payload(con_id=777, bid=Decimal("0.05"), ask=Decimal("0.10")), SECRET)
    v = quote_guard.check(
        token=token,
        token_secret=SECRET,
        con_id=777,
        ticker="SPY",
        security_type="OPT",
        action="BUY",
        limit_price=Decimal("0.052"),
        now=MIDDAY_RTH,
        tick_rule_lookup=_tick_rule,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.LIMIT_OFF_TICK


def test_on_tick_cheap_option_two_tick_band_accepts():
    token = quote_tokens.mint(_payload(con_id=777, bid=Decimal("0.05"), ask=Decimal("0.10")), SECRET)
    v = quote_guard.check(
        token=token,
        token_secret=SECRET,
        con_id=777,
        ticker="SPY",
        security_type="OPT",
        action="BUY",
        limit_price=Decimal("0.10"),
        now=MIDDAY_RTH,
        tick_rule_lookup=_tick_rule,
    )
    assert v.accept is True


def test_expensive_option_out_of_band_rejects():
    token = quote_tokens.mint(_payload(bid=Decimal("9.50"), ask=Decimal("10.00")), SECRET)
    v = quote_guard.check(
        token=token,
        token_secret=SECRET,
        con_id=756733,
        ticker="SPY",
        security_type="OPT",
        action="BUY",
        limit_price=Decimal("12.00"),
        now=MIDDAY_RTH,
        tick_rule_lookup=_tick_rule,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.LIMIT_OUT_OF_BAND


def test_opt_outside_rth_blocks_with_OPTION_MARKET_CLOSED():
    token = quote_tokens.mint(_payload(), SECRET)
    v = quote_guard.check(
        token=token,
        token_secret=SECRET,
        con_id=756733,
        ticker="SPY",
        security_type="OPT",
        action="BUY",
        limit_price=Decimal("500.20"),
        now=AFTER_HOURS,
        tick_rule_lookup=_tick_rule,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.OPTION_MARKET_CLOSED
    assert "market" in (v.reason_detail or "").lower()


def test_crossed_or_zero_size_rejects_as_QUOTE_UNAVAILABLE():
    token = quote_tokens.mint(_payload(bid=Decimal("500.25"), ask=Decimal("500.20")), SECRET)
    v = quote_guard.check(
        token=token,
        token_secret=SECRET,
        con_id=756733,
        ticker="SPY",
        security_type="STK",
        action="BUY",
        limit_price=Decimal("500.20"),
        now=MIDDAY_RTH,
        tick_rule_lookup=_tick_rule,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.QUOTE_UNAVAILABLE


def test_tick_rule_cache_hits_second_call():
    calls = {"n": 0}

    def source(con_id: int) -> Decimal:
        calls["n"] += 1
        return Decimal("0.01")

    cache = quote_guard.TickRuleCache(source=source, ttl_seconds=3600)
    assert cache.get(756733) == Decimal("0.01")
    assert cache.get(756733) == Decimal("0.01")
    assert calls["n"] == 1


def test_tick_rule_cache_refreshes_after_ttl(monkeypatch):
    calls = {"n": 0}

    def source(con_id: int) -> Decimal:
        calls["n"] += 1
        return Decimal("0.01")

    t = {"now": 1000.0}
    monkeypatch.setattr(quote_guard.time, "monotonic", lambda: t["now"])
    cache = quote_guard.TickRuleCache(source=source, ttl_seconds=60)
    cache.get(1)
    t["now"] += 61
    cache.get(1)
    assert calls["n"] == 2


def test_invalid_token_signature_rejects_as_STALE_QUOTE():
    token = quote_tokens.mint(_payload(), SECRET)
    bad = token[:-4] + "AAAA"
    v = quote_guard.check(
        token=bad, token_secret=SECRET,
        con_id=756733, ticker="SPY", security_type="STK",
        action="BUY", limit_price=Decimal("500.20"),
        now=MIDDAY_RTH, tick_rule_lookup=_tick_rule,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.STALE_QUOTE
    assert "invalid" in (v.reason_detail or "")


def test_token_contract_mismatch_rejects():
    token = quote_tokens.mint(_payload(ticker="SPY", con_id=756733), SECRET)
    v = quote_guard.check(
        token=token, token_secret=SECRET,
        con_id=999999, ticker="QQQ", security_type="STK",
        action="BUY", limit_price=Decimal("500.20"),
        now=MIDDAY_RTH, tick_rule_lookup=_tick_rule,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.QUOTE_CONTRACT_MISMATCH
    assert "mismatch" in (v.reason_detail or "")


def test_sell_below_floor_rejects_with_LIMIT_OUT_OF_BAND():
    token = quote_tokens.mint(_payload(bid=Decimal("500.10"), ask=Decimal("500.20")), SECRET)
    v = quote_guard.check(
        token=token, token_secret=SECRET,
        con_id=756733, ticker="SPY", security_type="STK",
        action="SELL", limit_price=Decimal("400.00"),
        now=MIDDAY_RTH, tick_rule_lookup=_tick_rule,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.LIMIT_OUT_OF_BAND
