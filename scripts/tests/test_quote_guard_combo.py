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


# ---------- credit-structure coverage (ISSUE-1 regression) ----------

# For a held SHORT credit vertical (sold 500C + bought 510C hedge):
#   Structural legs: [500C action=SELL, 510C action=BUY] (LONG->BUY, SHORT->SELL).
#   CLOSING a short structure requires envelope=SELL - IB reverses the legs,
#   so the actual execution is: buy 500C (pay ask 4.70) + sell 510C (receive
#   bid 2.00) -> net debit 2.70 paid by the user.
# With envelope=BUY on the same structural legs, IB would *open* a new short
# credit spread (as-labeled) - tests use envelope=SELL to exercise the close.


def test_short_call_vertical_close_debit_in_band_accepts():
    """Closing a short credit spread at market debit must pass.

    Close via envelope=SELL so IB reverses to BUY 500C / SELL 510C.
    Market debit-to-close = 4.70 - 2.00 = 2.70.
    """
    legs = [
        _leg(1, "SELL", _mint(1, "SPY", "4.50", "4.70")),
        _leg(2, "BUY", _mint(2, "SPY", "2.00", "2.20")),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="SELL",
        limit_price=Decimal("2.80"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is True, v.reason_detail


def test_short_call_vertical_close_fat_finger_high_debit_rejects():
    """Close of short credit spread is a DEBIT; fat-finger +27 must reject.

    envelope=SELL + [SELL, BUY] → IB reverses → user pays ~2.70 debit.
    The band must be a CAP (sign(exec_net)>0), not a floor keyed on
    envelope=SELL. Gemini re-review ISSUE-1 flagged that the previous
    envelope-keyed logic let this through by applying the SELL-side floor
    (e.g. limit 27 ≥ 2.185 → accept) even though the trade was a debit.
    """
    legs = [
        _leg(1, "SELL", _mint(1, "SPY", "4.50", "4.70")),
        _leg(2, "BUY", _mint(2, "SPY", "2.00", "2.20")),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="SELL",
        limit_price=Decimal("27.00"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.LIMIT_OUT_OF_BAND


def test_iron_condor_close_debit_in_band_accepts():
    """Close of short iron condor (envelope=SELL -> IB reverses)."""
    legs = [
        _leg(1, "SELL", _mint(1, "SPY", "2.50", "2.60"), right="P"),
        _leg(2, "BUY", _mint(2, "SPY", "1.20", "1.30"), right="P"),
        _leg(3, "SELL", _mint(3, "SPY", "2.40", "2.50"), right="C"),
        _leg(4, "BUY", _mint(4, "SPY", "1.10", "1.20"), right="C"),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="SELL",
        limit_price=Decimal("2.90"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is True, v.reason_detail


def test_short_call_vertical_open_credit_in_band_accepts():
    """Open short credit vertical: envelope=BUY, user accepts up to 2.40 credit.

    Structural legs [SELL 500C, BUY 510C]; envelope=BUY executes as-labeled:
    receive 4.50 bid, pay 2.20 ask -> net credit 2.30. exec_net=-2.30,
    |exec_net|=2.30. User limit 2.40 is within band.
    """
    legs = [
        _leg(1, "SELL", _mint(1, "SPY", "4.50", "4.70")),
        _leg(2, "BUY", _mint(2, "SPY", "2.00", "2.20")),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="BUY",
        limit_price=Decimal("2.40"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is True, v.reason_detail


def test_credit_spread_open_fat_finger_low_credit_rejects():
    """Opening a short credit spread is a CREDIT; fat-finger 0.01 rejects.

    envelope=BUY + [SELL, BUY] → as-labeled execution: receive 4.50 bid,
    pay 2.20 ask → credit ~2.30 (exec_net=-2.30). User accepting only 0.01
    credit on a 2.30-credit market is a fat-finger down → floor rejects.
    Gemini re-review ISSUE-1: previous envelope-keyed logic would apply
    the BUY-side cap (limit 0.01 ≤ 2.415 → accept) even though the trade
    was a credit receive — this case catches that.
    """
    legs = [
        _leg(1, "SELL", _mint(1, "SPY", "4.50", "4.70")),
        _leg(2, "BUY", _mint(2, "SPY", "2.00", "2.20")),
    ]
    v = quote_guard.check_combo(
        legs=legs,
        envelope_action="BUY",
        limit_price=Decimal("0.01"),
        token_secret=SECRET,
        now=MIDDAY_RTH,
    )
    assert v.accept is False
    assert v.reason_code == ReasonCode.LIMIT_OUT_OF_BAND
