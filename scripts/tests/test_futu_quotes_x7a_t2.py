"""futu-quotes Task 2 (X7a): options — contract D4.

Covers the option relay key ``SYM_YYYYMMDD_STRIKE_RIGHT`` → Futu code
mapping (the `futu_code_for` packing, round-tripped through
``futu_to_ib``), local ``not_supported`` refusal for unmappable keys
(class-share underlyings, malformed shapes), OPTION sec_type acceptance
in the verify path, RTH-only sessions (never overnight), factored
greeks/IV from the QUOTE push (``FUTU_GREEK_FACTORS``), and ``undPrice``
from the held underlying's latest Futu last. assign/batch keys are the
relay keys.

No network: the service runs against ``FakeOpenD``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scripts.tests.test_futu_quotes import (
    DEFAULT_SNAPSHOTS,
    Clock,
    FakeOpenD,
    FakeWS,
    _raw_row,
    _settled_service,
    _snapshot_row,
    _want,
)
from scripts.tests.test_futu_quotes_x7a import Wall, _book_row
from xenon.futu_quotes.admission import code_for, option_underlying, ticker_of
from xenon.futu_quotes.quote_map import ET, option_quote_fields
from xenon.utils.symbol_norm import futu_to_ib


def et(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    """ET wall time → aware UTC (the service's wall clock is UTC)."""
    return datetime(y, mo, d, h, mi, tzinfo=ET).astimezone(timezone.utc)

OPT_CODE = "US.AAPL261030C150000"
OPT_KEY = "AAPL_20261030_150_C"

OPT_SNAPSHOTS = {
    **DEFAULT_SNAPSHOTS,
    OPT_CODE: _snapshot_row(OPT_CODE, sec_type="OPTION"),
}


def _opt_row(code: str = OPT_CODE, last: float = 3.40, **kw) -> dict:
    """OPTION QUOTE push row with the ex-data fields the SDK unpacks."""
    row = _raw_row(code, last)
    row.update(
        implied_volatility=28.5,
        delta=0.45,
        gamma=0.02,
        theta=-0.05,
        vega=0.12,
        premium=3.40,
    )
    row.update(kw)
    return row


# ---------------------------------------------------------------------------
# relay key ↔ Futu code
# ---------------------------------------------------------------------------


def test_option_relay_key_maps_to_futu_code_and_back():
    """D4: SYM_YYYYMMDD_STRIKE_RIGHT → US.<UND><YYMMDD><C|P><milli> with
    the `futu_code_for` packing; `futu_to_ib` parses it back identically."""
    assert code_for(OPT_KEY) == OPT_CODE
    back = futu_to_ib(OPT_CODE)
    assert back["kind"] == "OPT"
    assert (back["symbol"], back["expiry"], back["right"], back["strike"]) == (
        "AAPL",
        "20261030",
        "C",
        150.0,
    )
    # Decimal strike stays exact through the milli packing (no zero pad).
    code = code_for("MSFT_20261218_152.5_P")
    assert code == "US.MSFT261218P152500"
    back = futu_to_ib(code)
    assert (back["strike"], back["right"]) == (152.5, "P")
    # Push codes unpack to the exact relay key (assign/batch identity).
    assert ticker_of(OPT_CODE) == OPT_KEY
    assert ticker_of("US.MSFT261218P152500") == "MSFT_20261218_152.5_P"
    assert option_underlying(OPT_KEY) == "AAPL"
    # Stocks are untouched.
    assert code_for("AAPL") == "US.AAPL"
    assert ticker_of("US.AAPL") == "AAPL"


def test_option_key_refusals_have_no_code():
    """Unmappable keys → None (service rejects them not_supported)."""
    assert code_for("BRK.B_20261030_150_C") is None  # class-share underlying
    assert code_for("BRK B_20261030_150_C") is None
    assert code_for("AAPL_2026_150_C") is None  # bad expiry
    assert code_for("AAPL_20261030_150_X") is None  # bad right
    assert code_for("AAPL_20261030_0.1234_C") is None  # 4-decimal strike
    assert code_for("AAPL_20261030_0_C") is None  # zero strike


def test_option_quote_fields_factor_rules():
    """greeks/IV = push × factor; missing/non-finite values stay None."""
    factors = {"iv": 0.01, "delta": 1, "gamma": 1, "theta": 1, "vega": 1}
    assert option_quote_fields(_opt_row(), factors) == pytest.approx(
        {
            "impliedVol": 0.285,
            "delta": 0.45,
            "gamma": 0.02,
            "theta": -0.05,
            "vega": 0.12,
        }
    )
    # A factor key absent from the JSON → that field stays NULL.
    out = option_quote_fields(_opt_row(), {"delta": 1})
    assert out["delta"] == 0.45 and out["impliedVol"] is None
    # A push field absent → NULL even with a factor.
    out = option_quote_fields(_opt_row(implied_volatility=None), factors)
    assert out["impliedVol"] is None


# ---------------------------------------------------------------------------
# service level: assign/verify, refusal, session, greeks, undPrice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_option_want_ok_uses_option_code_reports_relay_key():
    """want <relay key> → OPTION snapshot verify + paired subscribe on the
    Futu option code → assign.ok under the relay key."""
    clock = Clock(1000.0)
    wall = Wall(datetime(2026, 10, 23, 14, 30, 0, tzinfo=timezone.utc))
    fake = FakeOpenD(snapshots=OPT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock, wall=wall)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, [OPT_KEY])
    await svc.drain()

    assert OPT_KEY in ws.last("assign")["ok"]
    # Verify + subscribe hit the Futu option code, not the relay key.
    assert OPT_CODE in fake.subscribed
    assert fake.subscribe_calls == [OPT_CODE]


@pytest.mark.asyncio
async def test_option_unmappable_key_rejected_not_supported():
    """Class-share underlying: no OpenD call at all — rejected
    not_supported, it stays on IB."""
    clock = Clock(1000.0)
    wall = Wall(datetime(2026, 10, 23, 14, 30, 0, tzinfo=timezone.utc))
    fake = FakeOpenD(snapshots=OPT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock, wall=wall)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["BRK.B_20261030_150_C", "AAPL_2026_150_C"])
    await svc.drain()

    rejected = ws.last("assign")["rejected"]
    assert {"symbol": "BRK.B_20261030_150_C", "reason": "not_supported"} in rejected
    assert {"symbol": "AAPL_2026_150_C", "reason": "not_supported"} in rejected
    assert fake.subscribed == set()
    assert fake.snapshot_calls == 0


@pytest.mark.asyncio
async def test_option_quote_row_emits_relay_key_greeks_null_by_default():
    """Without FUTU_GREEK_FACTORS the five fields stay NULL; undPrice is
    NULL while the underlying is not held."""
    clock = Clock(1000.0)
    wall = Wall(datetime(2026, 10, 23, 14, 30, 0, tzinfo=timezone.utc))
    fake = FakeOpenD(snapshots=OPT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock, wall=wall)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, [OPT_KEY])
    await svc.drain()

    fake.push([_opt_row()])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"][OPT_KEY]
    assert upd["last"] == 3.40
    for f in ("delta", "gamma", "theta", "vega", "impliedVol", "undPrice"):
        assert upd[f] is None, f


@pytest.mark.asyncio
async def test_option_greeks_stamped_with_factors():
    """FUTU_GREEK_FACTORS set → push value × factor on each field."""
    clock = Clock(1000.0)
    wall = Wall(datetime(2026, 10, 23, 14, 30, 0, tzinfo=timezone.utc))
    fake = FakeOpenD(snapshots=OPT_SNAPSHOTS)
    svc = await _settled_service(
        fake,
        clock=clock,
        wall=wall,
        greek_factors={"iv": 0.01, "delta": 1, "gamma": 1, "theta": 1, "vega": 1},
    )
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, [OPT_KEY])
    await svc.drain()

    fake.push([_opt_row()])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"][OPT_KEY]
    assert upd["impliedVol"] == pytest.approx(0.285)
    assert upd["delta"] == pytest.approx(0.45)
    assert upd["gamma"] == pytest.approx(0.02)
    assert upd["theta"] == pytest.approx(-0.05)
    assert upd["vega"] == pytest.approx(0.12)


@pytest.mark.asyncio
async def test_option_undprice_is_held_underlyings_last():
    """undPrice = latest Futu last of the held underlying; a later book
    push on the option re-emits with the newest underlying last."""
    clock = Clock(1000.0)
    wall = Wall(datetime(2026, 10, 23, 14, 30, 0, tzinfo=timezone.utc))
    fake = FakeOpenD(snapshots=OPT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock, wall=wall)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, [OPT_KEY, "AAPL"])
    await svc.drain()

    fake.push([_raw_row("US.AAPL", 101.25), _opt_row()])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"][OPT_KEY]
    assert upd["undPrice"] == 101.25

    # Underlying moves; an option ORDER_BOOK push re-emits the option
    # carrying the newest underlying last.
    fake.push([_raw_row("US.AAPL", 101.50, time_="10:30:06")])
    fake.push_book([_book_row(OPT_CODE)])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"][OPT_KEY]
    assert upd["undPrice"] == 101.50
    assert upd["bid"] == 101.20 and upd["ask"] == 101.30


@pytest.mark.asyncio
async def test_option_book_push_overlays_relay_key():
    """ORDER_BOOK for the option code overlays bid/ask on the relay key."""
    clock = Clock(1000.0)
    wall = Wall(datetime(2026, 10, 23, 14, 30, 0, tzinfo=timezone.utc))
    fake = FakeOpenD(snapshots=OPT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock, wall=wall)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, [OPT_KEY])
    await svc.drain()

    fake.push([_opt_row()])
    fake.push_book([_book_row(OPT_CODE, bid=3.35, bid_size=10, ask=3.45, ask_size=8)])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"][OPT_KEY]
    assert (upd["bid"], upd["bidSize"], upd["ask"], upd["askSize"]) == (
        3.35,
        10,
        3.45,
        8,
    )


@pytest.mark.asyncio
async def test_option_never_overnight():
    """Options are RTH only: overnight they stay pending session, and an
    overnight QUOTE push for the option code is dropped entirely — even
    one carrying overnight fields."""
    clock = Clock(1000.0)
    wall = Wall(et(2026, 10, 26, 14, 30))
    fake = FakeOpenD(snapshots=OPT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock, wall=wall)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, [OPT_KEY, "AAPL"])
    await svc.drain()
    assert OPT_KEY in ws.last("assign")["ok"]

    # Mon 20:05 ET: overnight opens; the option goes pending session.
    fake.market_us = "AFTER_HOURS_END"
    wall.dt = et(2026, 10, 26, 20, 5)
    await svc.poll_session_once()
    await svc.drain()
    assert svc.session == "overnight"
    assert {"symbol": OPT_KEY, "reason": "session"} in ws.last("assign")["pending"]

    fake.push(
        [
            _opt_row(overnight_price=3.40, overnight_volume=5),
            _raw_row("US.AAPL", 100.9, overnight_price=100.9, overnight_volume=10),
        ]
    )
    clock.advance(0.2)
    await svc.flush_now()
    assert not svc.admission.is_ok(OPT_KEY)

    # AAPL's second overnight row publishes; the option never appears.
    fake.push([_raw_row("US.AAPL", 100.95, overnight_price=100.95, overnight_volume=18)])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"].get("AAPL")
    assert upd is not None and upd["session"] == "overnight"
    for b in ws.frames("batch"):
        assert OPT_KEY not in b["updates"]
