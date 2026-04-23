import time
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from xenon.execution import quote_guard, quote_tokens
from xenon.execution.preflight import ReasonCode

SECRET = "b" * 64
NYC = ZoneInfo("America/New_York")
MIDDAY_RTH = datetime(2026, 4, 22, 13, 0, tzinfo=NYC)


def _mint(con_id: int, ticker: str, bid: str, ask: str, bid_sz: int = 100, ask_sz: int = 100, age_ms: int = 0) -> str:
    payload = quote_tokens.QuotePayload(
        con_id=con_id,
        ticker=ticker,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=bid_sz,
        ask_size=ask_sz,
        ts_server_ms=int(time.time() * 1000) - age_ms,
    )
    return quote_tokens.mint(payload, SECRET)


def _leg(con_id: int, action: str, token: str, right: str = "C"):
    return quote_guard.CheckComboLeg(
        token=token,
        con_id=con_id,
        ticker="SPY",
        action=action,
        right=right,
        ratio=1,
    )


def test_bull_call_spread_buy_envelope_in_band_accepts():
    legs = [
        _leg(1, "BUY", _mint(1, "SPY", "4.50", "4.70")),
        _leg(2, "SELL", _mint(2, "SPY", "2.00", "2.20")),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="BUY",
        limit_price=Decimal("2.80"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is True, v.reason_detail


def test_bull_call_spread_buy_envelope_over_band_rejects():
    legs = [
        _leg(1, "BUY", _mint(1, "SPY", "4.50", "4.70")),
        _leg(2, "SELL", _mint(2, "SPY", "2.00", "2.20")),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="BUY",
        limit_price=Decimal("3.00"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.LIMIT_OUT_OF_BAND


def test_bull_call_spread_sell_envelope_uses_net_bid_floor():
    legs = [
        _leg(1, "BUY", _mint(1, "SPY", "4.50", "4.70")),
        _leg(2, "SELL", _mint(2, "SPY", "2.00", "2.20")),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="SELL",
        limit_price=Decimal("2.20"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is True
    v2 = quote_guard.check_combo(
        legs=legs,
        envelope_action="SELL",
        limit_price=Decimal("2.00"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v2.accept is False
    assert v2.reason_code == ReasonCode.LIMIT_OUT_OF_BAND


def test_risk_reversal_buy_call_sell_put():
    legs = [
        _leg(1, "BUY", _mint(1, "SPY", "4.50", "4.70"), right="C"),
        _leg(2, "SELL", _mint(2, "SPY", "3.00", "3.20"), right="P"),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="BUY",
        limit_price=Decimal("1.75"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is True, v.reason_detail


def test_stale_leg_token_rejects_whole_combo():
    legs = [
        _leg(1, "BUY", _mint(1, "SPY", "4.50", "4.70")),
        _leg(2, "SELL", _mint(2, "SPY", "2.00", "2.20", age_ms=10_000)),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="BUY",
        limit_price=Decimal("2.70"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.STALE_QUOTE


def test_zero_size_leg_rejects():
    legs = [
        _leg(1, "BUY", _mint(1, "SPY", "4.50", "4.70", bid_sz=0)),
        _leg(2, "SELL", _mint(2, "SPY", "2.00", "2.20")),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="BUY",
        limit_price=Decimal("2.70"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.STALE_QUOTE


def test_token_contract_mismatch_rejects():
    tok = _mint(1, "SPY", "4.50", "4.70")
    legs = [
        quote_guard.CheckComboLeg(
            token=tok,
            con_id=99,
            ticker="SPY",
            action="BUY",
            right="C",
            ratio=1,
        ),
        _leg(2, "SELL", _mint(2, "SPY", "2.00", "2.20")),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="BUY",
        limit_price=Decimal("2.70"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.STALE_QUOTE
