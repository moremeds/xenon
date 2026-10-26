"""futu-quotes PR #251 review round 1 (X7a): Grok FAIL-4 findings.

F1 — a market-session change (rth↔overnight↔closed) must clear the stored
     ORDER_BOOK top and the RTH quote template (Amendment A1: "session
     change already clear it"), and a book push outside RTH must not be
     stored at all.
F2 — a book-driven rebuild must never emit a row whose QUOTE time is
     behind the flushed high-water mark (stale template after the mark
     advanced, e.g. an overnight row's receipt time).
F3 — sub-$1 strikes must round-trip: Futu packs strike×1000 with no width
     padding, so ``US.F261120C500`` is the $0.50 call and must unpack back
     to the relay key ``F_20261120_0.5_C`` (JS-number formatting).
F4 — inside one coalesce window an older QUOTE row must not replace the
     stored template the book path rebuilds from.

No network: the service runs against ``FakeOpenD``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.tests.test_futu_quotes import (
    DEFAULT_SNAPSHOTS,
    Clock,
    FakeOpenD,
    FakeWS,
    _raw_row,
    _settled_service,
    _want,
)
from scripts.tests.test_futu_quotes_x7a import Wall, _book_row
from xenon.futu_quotes.admission import code_for, ticker_of


def _svc_kw() -> dict:
    return {
        "clock": Clock(1000.0),
        "wall": Wall(datetime(2026, 10, 23, 19, 30, 0, tzinfo=timezone.utc)),
    }


def _overnight_row(code: str, price: float, volume: float) -> dict:
    return _raw_row(
        code,
        101.25,  # last_price = RTH close carried on overnight rows
        overnight_price=price,
        overnight_volume=volume,
        overnight_high_price=price,
        overnight_low_price=price,
    )


# ---------------------------------------------------------------------------
# F1 — session change clears book + template; no book store outside RTH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_change_clears_book_and_template():
    """Friday RTH book/template must not survive into the next RTH: A1 says
    a session change clears the book whether or not the age cutoff is on,
    and the template is session-bound the same way."""
    kw = _svc_kw()
    clock = kw["clock"]
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, **kw)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()

    fake.push([_raw_row("US.AAPL", 101.25)])
    fake.push_book([_book_row("US.AAPL")])
    clock.advance(0.2)
    await svc.flush_now()
    assert ws.last("batch")["updates"]["AAPL"]["bid"] == 101.20
    assert svc._book and svc._last_quote  # stored during RTH

    # rth → overnight: book and template are dropped with the transition.
    fake.market_us = "OVERNIGHT"
    await svc.poll_session_once()
    await svc.drain()
    assert svc._book == {}
    assert svc._last_quote == {}

    # A book push while not in RTH is not stored for later use.
    fake.push_book([_book_row("US.AAPL", bid=90.0, ask=90.1)])
    await svc.drain()
    assert svc._book == {}

    # Back to RTH: the first quote emits NULL book fields — Friday's and
    # the overnight tops are gone — until a fresh RTH book arrives.
    fake.market_us = "AFTERNOON"
    await svc.poll_session_once()
    await svc.drain()
    fake.push([_raw_row("US.AAPL", 102.0, time_="10:31:00")])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"]["AAPL"]
    assert upd["last"] == 102.0
    assert (upd["bid"], upd["ask"], upd["bidSize"], upd["askSize"]) == (
        None,
        None,
        None,
        None,
    )

    fake.push_book([_book_row("US.AAPL")])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"]["AAPL"]
    assert upd["bid"] == 101.20 and upd["last"] == 102.0


# ---------------------------------------------------------------------------
# F2 — no rebuild from a template behind the high-water mark
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_template_not_rebuilt_after_overnight_mark():
    """Review scenario: Friday 15:59 quote stored + flushed; an overnight
    trade publishes Sunday and moves the mark to its receipt time; a book
    push in Monday's RTH must not re-emit Friday's ``last``."""
    clock = Clock(1000.0)
    wall = Wall(datetime(2026, 10, 23, 19, 59, 0, tzinfo=timezone.utc))
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, clock=clock, wall=wall)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()

    # Friday RTH: last 101.25 stored and flushed (mark = 15:59 ET quote ts).
    fake.push([_raw_row("US.AAPL", 101.25, time_="15:59:00")])
    clock.advance(0.2)
    await svc.flush_now()
    assert ws.last("batch")["updates"]["AAPL"]["last"] == 101.25

    # Sunday overnight (receipt wall = Sunday 20:30 ET): first row is the
    # baseline, the changed row publishes and moves the mark to Sunday.
    wall.dt = datetime(2026, 10, 26, 0, 30, 0, tzinfo=timezone.utc)
    fake.market_us = "OVERNIGHT"
    await svc.poll_session_once()
    await svc.drain()
    fake.push([_overnight_row("US.AAPL", 103.05, 17485)])
    clock.advance(0.2)
    await svc.flush_now()
    fake.push([_overnight_row("US.AAPL", 103.10, 18132)])
    clock.advance(0.2)
    await svc.flush_now()
    assert ws.last("batch")["updates"]["AAPL"]["last"] == 103.10

    # Monday RTH: a book push arrives before any regular quote. The Friday
    # template is gone — the rebuild must not happen, so no batch at all.
    wall.dt = datetime(2026, 10, 26, 13, 30, 0, tzinfo=timezone.utc)
    fake.market_us = "MORNING"
    await svc.poll_session_once()
    await svc.drain()
    sent = len(ws.frames("batch"))
    fake.push_book([_book_row("US.AAPL", bid=103.0, ask=103.2)])
    clock.advance(0.2)
    await svc.flush_now()
    assert len(ws.frames("batch")) == sent  # no stale-last rebuild

    # The stored book rides the next real quote instead.
    fake.push(
        [_raw_row("US.AAPL", 103.4, date="2026-10-26", time_="09:31:00")]
    )
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"]["AAPL"]
    assert upd["last"] == 103.4 and upd["bid"] == 103.0


@pytest.mark.asyncio
async def test_book_rebuild_skipped_when_template_behind_mark():
    """The emit-side guard: whatever puts the template behind the flushed
    mark, a book push must not re-emit that older ``last``."""
    kw = _svc_kw()
    clock = kw["clock"]
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, **kw)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()

    fake.push([_raw_row("US.AAPL", 101.25)])
    clock.advance(0.2)
    await svc.flush_now()
    assert ws.last("batch")["updates"]["AAPL"]["last"] == 101.25

    # Backdate the stored template behind the flushed mark (the stale-
    # template state finding 2 describes).
    pd, ts = svc._last_quote["AAPL"]
    svc._last_quote["AAPL"] = (pd, ts - timedelta(seconds=30))

    sent = len(ws.frames("batch"))
    fake.push_book([_book_row("US.AAPL")])
    clock.advance(0.2)
    await svc.flush_now()
    assert len(ws.frames("batch")) == sent


# ---------------------------------------------------------------------------
# F3 — sub-$1 strikes round-trip to the relay key
# ---------------------------------------------------------------------------


def test_ticker_of_sub_dollar_strikes_round_trip():
    """``futu_to_ib`` only classifies 4–9-digit strike suffixes as options;
    Futu packs $0.50 as ``500`` — three digits. The unpack must cover every
    code ``code_for`` can emit, with JS-number strike formatting."""
    cases = {
        "F_20261120_0.5_C": "US.F261120C500",
        "NIO_20261120_0.05_C": "US.NIO261120C50",
        "SOFI_20261120_1_C": "US.SOFI261120C1000",
        "X_20261120_2.5_C": "US.X261120C2500",
        "MSFT_20261218_152.5_P": "US.MSFT261218P152500",
        "AAPL_20261030_150_C": "US.AAPL261030C150000",
    }
    for key, code in cases.items():
        assert code_for(key) == code
        assert ticker_of(code) == key, code


# ---------------------------------------------------------------------------
# F4 — an older quote inside the window does not replace the template
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_older_quote_inside_window_does_not_replace_template():
    """A cached quote arriving after a newer one — but before the flush —
    loses the batcher tie yet still overwrote ``_last_quote``; the next
    book push would then re-emit the older ``last``."""
    kw = _svc_kw()
    clock = kw["clock"]
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, **kw)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()

    # Newer quote first, then an older cached row inside the same window.
    fake.push([_raw_row("US.AAPL", 101.5, time_="10:30:05")])
    fake.push([_raw_row("US.AAPL", 99.9, time_="10:30:02")])
    clock.advance(0.2)
    await svc.flush_now()
    assert ws.last("batch")["updates"]["AAPL"]["last"] == 101.5

    # The template must still be the 10:30:05 row — checked directly: the
    # stale-template guard alone would hide a poisoned template by just
    # skipping the rebuild.
    assert svc._last_quote["AAPL"][0]["last"] == 101.5

    # And the book rebuild emits that last, not 99.9.
    fake.push_book([_book_row("US.AAPL")])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"]["AAPL"]
    assert upd["last"] == 101.5
