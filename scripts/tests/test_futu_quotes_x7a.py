"""futu-quotes Task 1 (X7a): ORDER_BOOK bid/ask — contract D3/D11/G3.

Covers the paired QUOTE+ORDER_BOOK subscribe (ok only when both are held
in the current OpenD session), ORDER_BOOK refusal → rejected/not_supported,
top-of-book → bid/ask/bidSize/askSize, per-symbol book receipt time with
stale (> FUTU_BOOK_STALE_S) and non-RTH nulling, timestamp =
max(QUOTE ts, book receipt ts), the QUOTE-ts high-water mark,
FUTU_UNITS_PER_SYMBOL (default 2) quota accounting, and paired
unsubscribe under the existing 60 s rules.

No network: the service runs against ``FakeOpenD``; the real
``FutuOpenD`` subtype pairing is exercised with a stub context.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from futu import SubType

from scripts.tests.test_futu_quotes import (
    DEFAULT_SNAPSHOTS,
    Clock,
    FakeOpenD,
    FakeWS,
    _raw_row,
    _settled_service,
    _want,
)
from xenon.futu_quotes import opend as opend_mod
from xenon.futu_quotes.quote_map import ET, order_book_top


class Wall:
    def __init__(self, dt: datetime) -> None:
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt


def et(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    """ET wall time → aware UTC (the service's wall clock is UTC)."""
    return datetime(y, mo, d, h, mi, tzinfo=ET).astimezone(timezone.utc)


def _book_row(code: str, bid=101.20, bid_size=300, ask=101.30, ask_size=400) -> dict:
    """ORDER_BOOK push row in the SDK's unpacked shape:
    ``Bid``/``Ask`` = per-level (price, volume, count, detail) tuples."""
    bids = [(bid, bid_size, 2, {})] if bid is not None else []
    asks = [(ask, ask_size, 1, {})] if ask is not None else []
    return {"code": code, "Bid": bids, "Ask": asks}


class _StubCtx:
    """Minimal quote-context stand-in recording (codes, subtypes) pairs."""

    def __init__(self, ret=None, msg: str = "") -> None:
        self.ret = opend_mod.RET_OK if ret is None else ret
        self.msg = msg
        self.calls: list[tuple] = []

    def subscribe(self, codes, subtypes):
        self.calls.append(("subscribe", list(codes), list(subtypes)))
        return self.ret, self.msg

    def unsubscribe(self, codes, subtypes):
        self.calls.append(("unsubscribe", list(codes), list(subtypes)))
        return self.ret, self.msg


# ---------------------------------------------------------------------------
# paired subtype subscribe/unsubscribe at the real adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opend_subscribe_unsubscribe_send_paired_subtypes():
    """FutuOpenD issues ONE call carrying [QUOTE, ORDER_BOOK] so a symbol
    is held only when both subtypes landed, and released together."""
    assert opend_mod._SUBTYPES == [SubType.QUOTE, SubType.ORDER_BOOK]
    od = opend_mod.FutuOpenD()
    od._ctx = _StubCtx()
    assert await od.subscribe(["US.AAPL"]) == {"US.AAPL": None}
    assert await od.unsubscribe(["US.AAPL"]) == {"US.AAPL": None}
    assert od._ctx.calls == [
        ("subscribe", ["US.AAPL"], [SubType.QUOTE, SubType.ORDER_BOOK]),
        ("unsubscribe", ["US.AAPL"], [SubType.QUOTE, SubType.ORDER_BOOK]),
    ]


@pytest.mark.asyncio
async def test_opend_subscribe_refusal_surfaces_per_code_error():
    """An ORDER_BOOK refusal on the paired call comes back as the code's
    error string (→ classified not_supported by the caller)."""
    od = opend_mod.FutuOpenD()
    od._ctx = _StubCtx(ret=opend_mod.RET_ERROR, msg="ORDER_BOOK not supported")
    res = await od.subscribe(["US.AAPL"])
    assert res == {"US.AAPL": "ORDER_BOOK not supported"}
    assert opend_mod.classify_subscribe_error(res["US.AAPL"]) == "not_supported"


def test_order_book_top_mapping():
    assert order_book_top(_book_row("US.AAPL")) == {
        "bid": 101.20,
        "bidSize": 300,
        "ask": 101.30,
        "askSize": 400,
    }
    # One-sided book: the missing side stays null.
    assert order_book_top(_book_row("US.AAPL", ask=None)) == {
        "bid": 101.20,
        "bidSize": 300,
        "ask": None,
        "askSize": None,
    }
    # Malformed levels are tolerated, never fatal.
    assert order_book_top({"code": "US.AAPL", "Bid": [()], "Ask": "x"}) == {
        "bid": None,
        "bidSize": None,
        "ask": None,
        "askSize": None,
    }


# ---------------------------------------------------------------------------
# service level: refusal, readiness, mapping, staleness, quota
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_book_refusal_rejected_not_supported():
    """ORDER_BOOK refusal on the paired subscribe → assign.rejected
    not_supported (the symbol stays on IB, never ok)."""
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    fake.book_refused.add("US.HYG")
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "HYG"])
    await svc.drain()
    assert svc.admission.is_ok("AAPL")
    assert not svc.admission.is_ok("HYG")
    last = ws.last("assign")
    assert last["ok"] == ["AAPL"]
    assert {"symbol": "HYG", "reason": "not_supported"} in last["rejected"]


@pytest.mark.asyncio
async def test_ok_only_after_paired_subscribe_succeeds():
    """While the paired subscribe is parked at OpenD the symbol is pending;
    it becomes ok only after that call confirms it in this session."""
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    fake.defer_all = True
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain(timeout=0.05)
    assert fake.subscribe_calls == ["US.AAPL"]  # one call for the pair
    assert not svc.admission.is_ok("AAPL")
    assert {"symbol": "AAPL", "reason": "subscribing"} in ws.last("assign")["pending"]

    fake.complete_subscribe("US.AAPL")
    await svc.drain()
    assert svc.admission.is_ok("AAPL")
    assert ws.last("assign")["ok"] == ["AAPL"]


@pytest.mark.asyncio
async def test_book_top_overlay_and_timestamp_max():
    """bid/ask/sizes come from ORDER_BOOK; timestamp = max(QUOTE ts, book
    receipt). QUOTE rows before any book keep the four fields null."""
    clock = Clock(1000.0)
    wall = Wall(datetime(2026, 10, 23, 14, 30, 5, tzinfo=timezone.utc))
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock, wall=wall)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()

    # No book yet: the four fields are null on the QUOTE-driven row.
    fake.push([_raw_row("US.AAPL", 101.25)])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"]["AAPL"]
    assert (upd["bid"], upd["ask"], upd["bidSize"], upd["askSize"]) == (
        None,
        None,
        None,
        None,
    )
    assert upd["timestamp"] == "2026-10-23T14:30:05Z"

    # Book push at wall 14:30:07 (> QUOTE ts) re-emits the last quote with
    # the top level and the receipt-time timestamp.
    wall.dt = datetime(2026, 10, 23, 14, 30, 7, tzinfo=timezone.utc)
    fake.push_book([_book_row("US.AAPL")])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"]["AAPL"]
    assert upd["bid"] == 101.20 and upd["bidSize"] == 300
    assert upd["ask"] == 101.30 and upd["askSize"] == 400
    assert upd["last"] == 101.25 and upd["source"] == "futu"
    assert upd["timestamp"] == "2026-10-23T14:30:07Z"

    # A QUOTE row also carries the still-fresh book (ts = max of the two).
    fake.push([_raw_row("US.AAPL", 101.5, time_="10:30:09")])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"]["AAPL"]
    assert upd["bid"] == 101.20 and upd["askSize"] == 400
    assert upd["last"] == 101.5
    assert upd["timestamp"] == "2026-10-23T14:30:09Z"  # max(09s quote, 07s book)


@pytest.mark.asyncio
async def test_book_push_without_prior_quote_emits_nothing():
    """A book row alone cannot fabricate a quote — no emission until a
    QUOTE row arrives; then the fresh book rides along."""
    clock = Clock(1000.0)
    wall = Wall(datetime(2026, 10, 23, 14, 30, 5, tzinfo=timezone.utc))
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock, wall=wall)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()

    fake.push_book([_book_row("US.AAPL")])
    clock.advance(0.2)
    await svc.flush_now()
    assert ws.frames("batch") == []

    fake.push([_raw_row("US.AAPL", 101.25)])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"]["AAPL"]
    assert upd["bid"] == 101.20 and upd["ask"] == 101.30


@pytest.mark.asyncio
async def test_stale_book_nulls_fields_and_keeps_quote_ts():
    """With a positive FUTU_BOOK_STALE_S (A1 opt-in), a book older than the
    cutoff → the four fields go back to null and timestamp is the QUOTE
    ts again."""
    clock = Clock(1000.0)
    wall = Wall(datetime(2026, 10, 23, 14, 30, 0, tzinfo=timezone.utc))
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock, wall=wall, book_stale_s=10.0)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()

    fake.push_book([_book_row("US.AAPL")])
    fake.push([_raw_row("US.AAPL", 101.25)])
    clock.advance(0.2)
    await svc.flush_now()
    assert ws.last("batch")["updates"]["AAPL"]["bid"] == 101.20

    # 11 s later the book is stale: the next QUOTE row emits nulls and is
    # stamped with its own ts, not the book receipt.
    wall.dt = datetime(2026, 10, 23, 14, 30, 11, tzinfo=timezone.utc)
    fake.push([_raw_row("US.AAPL", 101.4, time_="10:30:06")])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"]["AAPL"]
    assert (upd["bid"], upd["ask"], upd["bidSize"], upd["askSize"]) == (
        None,
        None,
        None,
        None,
    )
    assert upd["timestamp"] == "2026-10-23T14:30:06Z"


@pytest.mark.asyncio
async def test_book_age_rule_off_by_default():
    """Amendment A1: FUTU_BOOK_STALE_S defaults to 0 = age rule off. An old
    book on a still-held pair keeps overlaying in RTH (Futu only pushes
    ORDER_BOOK on change; quiet ≠ invalid)."""
    clock = Clock(1000.0)
    wall = Wall(datetime(2026, 10, 23, 14, 30, 0, tzinfo=timezone.utc))
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock, wall=wall)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()

    fake.push_book([_book_row("US.AAPL")])
    fake.push([_raw_row("US.AAPL", 101.25)])
    clock.advance(0.2)
    await svc.flush_now()
    assert ws.last("batch")["updates"]["AAPL"]["bid"] == 101.20

    # 60 s with no book push: the pair is still held in this OpenD session,
    # so the book top still overlays and timestamp lifts to the receipt.
    wall.dt = datetime(2026, 10, 23, 14, 31, 0, tzinfo=timezone.utc)
    fake.push([_raw_row("US.AAPL", 101.4, time_="10:30:55")])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"]["AAPL"]
    assert (upd["bid"], upd["ask"], upd["bidSize"], upd["askSize"]) == (
        101.20,
        101.30,
        300,
        400,
    )
    assert upd["timestamp"] == "2026-10-23T14:30:55Z"


@pytest.mark.asyncio
async def test_book_receipt_never_advances_highwater():
    """The high-water mark stays on the QUOTE source ts: a quote newer
    than the mark but older than the book receipt still forwards."""
    clock = Clock(1000.0)
    wall = Wall(datetime(2026, 10, 23, 14, 30, 5, tzinfo=timezone.utc))
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock, wall=wall)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()

    fake.push([_raw_row("US.AAPL", 101.25)])  # QUOTE ts 14:30:05Z → mark
    clock.advance(0.2)
    await svc.flush_now()

    wall.dt = datetime(2026, 10, 23, 14, 30, 20, tzinfo=timezone.utc)
    fake.push_book([_book_row("US.AAPL")])  # emitted ts 14:30:20Z
    clock.advance(0.2)
    await svc.flush_now()
    assert ws.last("batch")["updates"]["AAPL"]["timestamp"] == "2026-10-23T14:30:20Z"

    # QUOTE ts 14:30:10: newer than the mark (14:30:05) though older than
    # the book receipt. Dropped only if the mark had wrongly moved to the
    # book time.
    fake.push([_raw_row("US.AAPL", 101.9, time_="10:30:10")])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"]["AAPL"]
    assert upd["last"] == 101.9
    assert upd["timestamp"] == "2026-10-23T14:30:20Z"  # max(quote 10s, book 20s)


@pytest.mark.asyncio
async def test_overnight_rows_never_carry_book_fields():
    """Not-RTH session: overnight rows keep last/overnight data but the
    four book fields stay null; a book push emits nothing by itself."""
    clock = Clock(1000.0)
    wall = Wall(et(2026, 10, 25, 20, 30))  # Sun 20:30 ET → overnight
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, market_us="AFTER_HOURS_END")
    svc = await _settled_service(fake, clock=clock, wall=wall)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()
    assert svc.session == "overnight"

    fake.push_book([_book_row("US.AAPL")])
    fake.push([
        _raw_row("US.AAPL", 101.0, overnight_price=101.0, overnight_volume=500)
    ])  # baseline: swallowed
    clock.advance(0.2)
    await svc.flush_now()
    assert ws.frames("batch") == []

    fake.push([
        _raw_row("US.AAPL", 101.1, overnight_price=101.1, overnight_volume=700)
    ])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"]["AAPL"]
    assert upd["last"] == 101.1 and upd["session"] == "overnight"
    assert (upd["bid"], upd["ask"], upd["bidSize"], upd["askSize"]) == (
        None,
        None,
        None,
        None,
    )

    batches = ws.frames("batch")
    fake.push_book([_book_row("US.AAPL", bid=101.05)])
    clock.advance(0.2)
    await svc.flush_now()
    # A book push alone emits nothing overnight (session is not RTH).
    assert ws.frames("batch") == batches


@pytest.mark.asyncio
async def test_book_push_during_overnight_not_emitted():
    """Session flips RTH → overnight: the last RTH quote template must NOT
    be re-emitted by an overnight book push (fields stay null off-RTH)."""
    clock = Clock(1000.0)
    wall = Wall(datetime(2026, 10, 23, 14, 30, 5, tzinfo=timezone.utc))
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)  # AFTERNOON → rth
    svc = await _settled_service(fake, clock=clock, wall=wall)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()
    fake.push([_raw_row("US.AAPL", 101.25)])  # _last_quote template stored
    clock.advance(0.2)
    await svc.flush_now()
    assert len(ws.frames("batch")) == 1

    # Flip to overnight: Sun 20:30 ET is inside the Blue Ocean window.
    fake.market_us = "AFTER_HOURS_END"
    wall.dt = et(2026, 10, 25, 20, 30)
    await svc.poll_session_once()
    await svc.drain()
    assert svc.session == "overnight"

    # Overnight baseline: first row swallowed, then the symbol is ok.
    fake.push([
        _raw_row("US.AAPL", 101.0, overnight_price=101.0, overnight_volume=500)
    ])
    await svc.drain()
    assert svc.admission.is_ok("AAPL")

    fake.push_book([_book_row("US.AAPL")])
    clock.advance(0.2)
    await svc.flush_now()
    # Only the RTH quote batch ever went out; the book push emitted nothing.
    assert len(ws.frames("batch")) == 1


@pytest.mark.asyncio
async def test_two_units_per_symbol_quota():
    """FUTU_UNITS_PER_SYMBOL (default 2): remain=4 units admits exactly
    two symbols; the third is quota-rejected."""
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=4, own_used=0, total_used=0)
    svc = await _settled_service(fake)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "MSFT", "HYG"])
    await svc.drain()
    last = ws.last("assign")
    assert sorted(last["ok"]) == ["AAPL", "MSFT"]
    assert {"symbol": "HYG", "reason": "quota"} in last["rejected"]
    assert svc.admission.own_used() == 2  # symbols; provider units = 4
    assert fake.own_used == 4  # provider truth in units


@pytest.mark.asyncio
async def test_units_per_symbol_configurable():
    """The unit cost is a constructor/env parameter (FUTU_UNITS_PER_SYMBOL)."""
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=6, own_used=0, total_used=0)
    svc = await _settled_service(fake, units_per_symbol=3)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "MSFT", "HYG"])
    await svc.drain()
    last = ws.last("assign")
    assert sorted(last["ok"]) == ["AAPL", "MSFT"]
    assert {"symbol": "HYG", "reason": "quota"} in last["rejected"]


@pytest.mark.asyncio
async def test_unsubscribe_releases_pair_and_book_state():
    """The 60 s rules are unchanged; the release also drops the book and
    the quote template so a resubscribe starts clean."""
    clock = Clock(1000.0)
    wall = Wall(datetime(2026, 10, 23, 14, 30, 5, tzinfo=timezone.utc))
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock, wall=wall)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()
    fake.push_book([_book_row("US.AAPL")])
    fake.push([_raw_row("US.AAPL", 101.25)])
    assert "AAPL" in svc._book and "AAPL" in svc._last_quote

    clock.advance(200)
    await _want(svc, ws, 2, [])
    clock.advance(61)  # past min-lifetime + grace
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert fake.unsubscribe_calls == ["US.AAPL"]  # one call, both subtypes
    assert not svc.admission.state("AAPL").subscribed
    assert "AAPL" not in svc._book and "AAPL" not in svc._last_quote
