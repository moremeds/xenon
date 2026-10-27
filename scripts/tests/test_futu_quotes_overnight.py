"""futu-quotes US overnight session (Blue Ocean, Sun-Thu 20:00 ET → 03:50 ET).

Covers the session rule, the overnight QUOTE mapping, the receipt-time
baseline (no stale replay as fresh) and admission/transition behaviour of the
service. No network: the service runs against ``FakeOpenD``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts.tests.test_futu_quotes import (
    DEFAULT_SNAPSHOTS,
    Clock,
    FakeOpenD,
    FakeWS,
    _settled_service,
    _want,
)
from xenon.futu_quotes.admission import us_session
from xenon.futu_quotes.quote_map import (
    ET,
    ExtendedBaseline,
    extended_row_to_pricedata,
    row_to_pricedata,
)


def et(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    """ET wall time → aware UTC (the service's wall clock is UTC)."""
    return datetime(y, mo, d, h, mi, tzinfo=ET).astimezone(timezone.utc)


class Wall:
    def __init__(self, dt: datetime) -> None:
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt


# ---------------------------------------------------------------------------
# session rule
# ---------------------------------------------------------------------------

AHE = "AFTER_HOURS_END"


@pytest.mark.parametrize(
    ("label", "now", "expected"),
    [
        ("Sun 19:59", et(2026, 10, 25, 19, 59), None),
        ("Sun 20:00", et(2026, 10, 25, 20, 0), "overnight"),
        ("Mon 03:49", et(2026, 10, 26, 3, 49), "overnight"),
        ("Mon 03:50", et(2026, 10, 26, 3, 50), None),
        ("Thu 23:00", et(2026, 10, 29, 23, 0), "overnight"),
        ("Fri 03:00 (Thu night)", et(2026, 10, 30, 3, 0), "overnight"),
        ("Fri 20:30", et(2026, 10, 30, 20, 30), None),
        ("Sat 01:00 (Fri night)", et(2026, 10, 31, 1, 0), None),
        ("Sat 22:00", et(2026, 10, 31, 22, 0), None),
        ("Mon 12:00", et(2026, 10, 26, 12, 0), None),
    ],
)
def test_session_rule_after_hours_end_clock(label, now, expected):
    assert us_session(AHE, now) == expected, label


def test_session_rule_dst_fall_back():
    """2026-11-01 02:00 EDT → 01:00 EST: Sun 20:00 ET is 00:00 UTC before
    the change (Oct 25) and 01:00 UTC after it (Nov 1)."""
    assert us_session(AHE, datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)) == "overnight"
    assert us_session(AHE, datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)) is None  # 19:00 EST
    assert us_session(AHE, datetime(2026, 11, 2, 0, 59, tzinfo=timezone.utc)) is None  # 19:59 EST
    assert us_session(AHE, datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)) == "overnight"
    # Mon 03:49 / 03:50 EST = 08:49 / 08:50 UTC.
    assert us_session(AHE, datetime(2026, 11, 2, 8, 49, tzinfo=timezone.utc)) == "overnight"
    assert us_session(AHE, datetime(2026, 11, 2, 8, 50, tzinfo=timezone.utc)) is None


@pytest.mark.parametrize(
    "now",
    [et(2026, 10, 31, 22, 0), et(2026, 10, 25, 21, 0), et(2026, 10, 26, 11, 0)],
)
def test_session_rule_rth_states_any_clock(now):
    assert us_session("MORNING", now) == "rth"
    assert us_session("AFTERNOON", now) == "rth"


def test_session_rule_overnight_state_and_unknown():
    sat_noon = et(2026, 10, 31, 12, 0)
    in_window = et(2026, 10, 25, 21, 0)
    assert us_session("OVERNIGHT", sat_noon) == "overnight"
    assert us_session("overnight", in_window) == "overnight"
    for state in ("BOGUS", None, "", "CLOSED", "PRE_MARKET_END"):
        assert us_session(state, in_window) is None, state


# ---------------------------------------------------------------------------
# overnight mapping
# ---------------------------------------------------------------------------

NOW = et(2026, 10, 25, 20, 25)  # Sun 20:25 EDT = 00:25 UTC Mon


def _on_row(code: str = "US.SPY", **kw) -> dict:
    """Shape of a live overnight QUOTE push (prices synthetic)."""
    row = {
        "code": code,
        "data_date": "2026-10-23",
        "data_time": "16:00:00.012",
        "last_price": 760.5,
        "open_price": 755.0,
        "high_price": 762.0,
        "low_price": 754.0,
        "prev_close_price": 757.0,
        "volume": 50_000_000,
        "overnight_price": 761.08,
        "overnight_volume": 17485,
        "overnight_high_price": 761.4,
        "overnight_low_price": 760.9,
    }
    row.update(kw)
    return row


def test_overnight_row_maps_overnight_fields():
    pd, ts = extended_row_to_pricedata("SPY", _on_row(), NOW, "overnight")  # type: ignore[misc]
    assert ts == NOW
    assert pd["last"] == 761.08
    assert pd["volume"] == 17485
    assert pd["high"] == 761.4
    assert pd["low"] == 760.9
    assert pd["open"] is None
    assert pd["close"] == 760.5  # RTH last_price, not prev_close_price
    assert pd["timestamp"] == "2026-10-26T00:25:00Z"
    assert pd["session"] == "overnight"
    assert pd["source"] == "futu"
    assert pd["bid"] is None and pd["ask"] is None and pd["impliedVol"] is None
    json.dumps(pd, allow_nan=False)


@pytest.mark.parametrize("bad", [None, "N/A", "NaN", float("nan"), float("inf"), 0, -1.0])
def test_overnight_row_without_price_dropped(bad):
    row = _on_row(overnight_price=bad)
    assert extended_row_to_pricedata("SPY", row, NOW, "overnight") is None
    row.pop("overnight_price")
    assert extended_row_to_pricedata("SPY", row, NOW, "overnight") is None


@pytest.mark.parametrize("bad", [None, "N/A", float("nan"), 0, -5])
def test_overnight_row_without_volume_dropped(bad):
    """A rolled or empty row (volume missing or ≤ 0) is not a trade."""
    row = _on_row(overnight_volume=bad)
    assert extended_row_to_pricedata("SPY", row, NOW, "overnight") is None
    row.pop("overnight_volume")
    assert extended_row_to_pricedata("SPY", row, NOW, "overnight") is None


def test_rth_mapping_ignores_overnight_fields():
    """RTH mapping of a row that carries overnight fields is unchanged."""
    row = _on_row()
    plain = {k: v for k, v in row.items() if not k.startswith("overnight_")}
    pd, _ = row_to_pricedata("SPY", row)  # type: ignore[misc]
    pd_plain, _ = row_to_pricedata("SPY", plain)  # type: ignore[misc]
    assert pd == pd_plain
    assert "session" not in pd
    assert pd["last"] == 760.5


# ---------------------------------------------------------------------------
# baseline (no stale replay as fresh)
# ---------------------------------------------------------------------------


def test_baseline_first_row_unchanged_changed_reset():
    b = ExtendedBaseline()
    assert not b.changed("SPY", _on_row(), "overnight")  # first row: baseline
    assert not b.changed("SPY", _on_row(), "overnight")  # unchanged
    assert b.changed("SPY", _on_row(overnight_volume=18132), "overnight")  # new trade
    assert not b.changed("SPY", _on_row(overnight_volume=18132), "overnight")
    assert b.changed("SPY", _on_row(overnight_volume=18132, overnight_price=761.25), "overnight")
    b.reset("SPY")
    assert not b.changed("SPY", _on_row(overnight_volume=1), "overnight")  # baseline again
    b.reset_all()
    assert not b.changed("SPY", _on_row(overnight_volume=2), "overnight")


# ---------------------------------------------------------------------------
# service level
# ---------------------------------------------------------------------------


async def _overnight_service(clock: Clock, wall: Wall, fake: FakeOpenD):
    svc = await _settled_service(fake, clock=clock, wall=wall)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["SPY"])
    await svc.drain()
    return svc, ws


async def _push_flush(svc, fake: FakeOpenD, clock: Clock, row: dict) -> None:
    fake.push([row])
    await svc.drain()  # a first trade row re-evaluates readiness
    clock.advance(0.11)
    await svc.flush_now()


def _pending(ws: FakeWS) -> list:
    return ws.last("assign")["pending"]


SESSION_PENDING = [{"symbol": "SPY", "reason": "session"}]


SNAPS = {**DEFAULT_SNAPSHOTS, "US.SPY": {"code": "US.SPY", "sec_type": "ETF", "last_price": 1.0}}


@pytest.mark.asyncio
async def test_overnight_ok_only_after_first_trade_row():
    """Overnight: a subscribed symbol stays pending/session (relay keeps it
    on IB) until a row with overnight price and volume > 0 is seen; that
    baseline row makes it ok but is not published."""
    clock, wall = Clock(1000.0), Wall(NOW)
    fake = FakeOpenD(snapshots=SNAPS, market_us=AHE)
    svc, ws = await _overnight_service(clock, wall, fake)
    assert svc.session == "overnight"
    assert "US.SPY" in fake.subscribed
    assert _pending(ws) == SESSION_PENDING
    assert not svc.admission.is_ok("SPY")

    # No overnight trade yet: price missing, then volume 0.
    await _push_flush(svc, fake, clock, _on_row(overnight_price=None))
    await _push_flush(svc, fake, clock, _on_row(overnight_volume=0))
    assert _pending(ws) == SESSION_PENDING
    assert ws.frames("batch") == []

    await _push_flush(svc, fake, clock, _on_row())  # first trade row
    last = ws.last("assign")
    assert last["ok"] == ["SPY"] and last["pending"] == []
    assert ws.frames("batch") == []  # baseline: not published


@pytest.mark.asyncio
async def test_overnight_volume_zero_row_not_published():
    clock, wall = Clock(1000.0), Wall(NOW)
    fake = FakeOpenD(snapshots=SNAPS, market_us=AHE)
    svc, ws = await _overnight_service(clock, wall, fake)
    await _push_flush(svc, fake, clock, _on_row())  # baseline, now ok
    await _push_flush(svc, fake, clock, _on_row(overnight_volume=0, overnight_price=759.0))
    assert ws.frames("batch") == []
    await _push_flush(svc, fake, clock, _on_row(overnight_volume=17500))
    assert len(ws.frames("batch")) == 1


@pytest.mark.asyncio
async def test_rth_has_no_overnight_readiness_gate():
    clock, wall = Clock(1000.0), Wall(et(2026, 10, 26, 11, 0))
    fake = FakeOpenD(snapshots=SNAPS, market_us="MORNING")
    svc, ws = await _overnight_service(clock, wall, fake)
    assert ws.last("assign")["ok"] == ["SPY"]


@pytest.mark.asyncio
async def test_overnight_baseline_then_changed_published():
    clock, wall = Clock(1000.0), Wall(NOW)
    fake = FakeOpenD(snapshots=SNAPS, market_us=AHE)
    svc, ws = await _overnight_service(clock, wall, fake)

    await _push_flush(svc, fake, clock, _on_row())  # cached first push
    assert ws.frames("batch") == []

    await _push_flush(svc, fake, clock, _on_row())  # unchanged
    assert ws.frames("batch") == []

    wall.dt = et(2026, 10, 25, 20, 26)
    await _push_flush(svc, fake, clock, _on_row(overnight_volume=18132, overnight_price=761.25))
    frames = ws.frames("batch")
    assert len(frames) == 1
    pd = frames[0]["updates"]["SPY"]
    assert pd["last"] == 761.25
    assert pd["volume"] == 18132
    assert pd["session"] == "overnight"
    assert pd["timestamp"] == "2026-10-26T00:26:00Z"

    await _push_flush(svc, fake, clock, _on_row(overnight_volume=18132, overnight_price=761.25))
    assert len(ws.frames("batch")) == 1  # unchanged again: nothing


@pytest.mark.asyncio
async def test_overnight_baseline_reset_on_unsubscribe():
    clock, wall = Clock(1000.0), Wall(NOW)
    fake = FakeOpenD(snapshots=SNAPS, market_us=AHE)
    svc, ws = await _overnight_service(clock, wall, fake)
    await _push_flush(svc, fake, clock, _on_row())  # baseline

    clock.advance(121)
    await _want(svc, ws, 2, [])
    clock.advance(61)
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert "US.SPY" in fake.unsubscribe_calls

    await _want(svc, ws, 3, ["SPY"])
    await svc.drain()
    assert not svc.admission.is_ok("SPY")  # readiness reset with the baseline
    assert _pending(ws) == SESSION_PENDING
    # Differs from the pre-unsubscribe value, but it is the first row of the
    # new subscription: baseline only.
    await _push_flush(svc, fake, clock, _on_row(overnight_volume=99999))
    assert svc.admission.is_ok("SPY")
    assert ws.frames("batch") == []


@pytest.mark.asyncio
async def test_overnight_baseline_reset_on_opend_reconnect():
    clock, wall = Clock(1000.0), Wall(NOW)
    fake = FakeOpenD(snapshots=SNAPS, market_us=AHE)
    svc, ws = await _overnight_service(clock, wall, fake)
    await _push_flush(svc, fake, clock, _on_row())  # baseline

    fake.drop()
    await fake.connect()
    await svc.opend_recovered()
    await svc.drain()
    assert not svc.admission.is_ok("SPY")  # readiness reset with the baseline
    await _push_flush(svc, fake, clock, _on_row(overnight_volume=99999))
    assert svc.admission.is_ok("SPY")
    assert ws.frames("batch") == []  # first row of the new OpenD session
    await _push_flush(svc, fake, clock, _on_row(overnight_volume=100000))
    assert len(ws.frames("batch")) == 1


@pytest.mark.asyncio
async def test_overnight_baseline_reset_on_relay_reconnect():
    clock, wall = Clock(1000.0), Wall(NOW)
    fake = FakeOpenD(snapshots=SNAPS, market_us=AHE)
    svc, ws = await _overnight_service(clock, wall, fake)
    await _push_flush(svc, fake, clock, _on_row())  # baseline

    ws2 = FakeWS()
    await svc.on_ws_connect(ws2)
    await _want(svc, ws2, 1, ["SPY"])
    await svc.drain()
    await _push_flush(svc, fake, clock, _on_row(overnight_volume=99999))
    assert ws2.frames("batch") == []


@pytest.mark.asyncio
async def test_session_transitions_rth_overnight_closed():
    """The 30 s session poll re-reads the wall clock: the 03:50 edge closes
    the session with no market_us change; MORNING maps RTH again."""
    clock, wall = Clock(1000.0), Wall(et(2026, 10, 26, 15, 0))
    fake = FakeOpenD(snapshots=SNAPS, market_us="AFTERNOON")
    svc, ws = await _overnight_service(clock, wall, fake)
    assert svc.session == "rth"

    # Mon 20:00: overnight opens; symbol stays ok (no assign change needed).
    fake.market_us = AHE
    wall.dt = et(
        2026,
        10,
        26,
        20,
        0,
    )
    await svc.poll_session_once()
    await svc.drain()
    assert svc.session == "overnight"
    assert not svc.admission.is_ok("SPY")  # until the first trade row
    await _push_flush(svc, fake, clock, _on_row())  # baseline after the switch
    assert svc.admission.is_ok("SPY")
    await _push_flush(svc, fake, clock, _on_row(overnight_volume=18000))
    assert ws.frames("batch")[-1]["updates"]["SPY"]["session"] == "overnight"
    n = len(ws.frames("batch"))

    # Tue 03:50: same AFTER_HOURS_END state, clock edge closes the session.
    wall.dt = et(2026, 10, 27, 3, 50)
    await svc.poll_session_once()
    await svc.drain()
    assert svc.session is None
    assert ws.last("assign")["pending"] == [{"symbol": "SPY", "reason": "session"}]
    await _push_flush(svc, fake, clock, _on_row(overnight_volume=19000))
    assert len(ws.frames("batch")) == n

    # Tue 09:30: RTH again, RTH mapping (no session field).
    fake.market_us = "MORNING"
    wall.dt = et(2026, 10, 27, 9, 30)
    await svc.poll_session_once()
    await svc.drain()
    assert ws.last("assign")["ok"] == ["SPY"]
    await _push_flush(svc, fake, clock, _on_row(data_date="2026-10-27", data_time="09:30:05", last_price=762.0))
    pd = ws.frames("batch")[-1]["updates"]["SPY"]
    assert pd["last"] == 762.0
    assert "session" not in pd
    assert pd["timestamp"] == "2026-10-27T13:30:05Z"


@pytest.mark.asyncio
async def test_night1_closed_night2_first_push_not_published():
    """Nightly rollover: night 2's first push differs from night 1's last
    value but is a baseline (the session change resets it), and the symbol
    is pending until it is seen."""
    clock, wall = Clock(1000.0), Wall(et(2026, 10, 26, 20, 5))  # Mon night
    fake = FakeOpenD(snapshots=SNAPS, market_us=AHE)
    svc, ws = await _overnight_service(clock, wall, fake)
    await _push_flush(svc, fake, clock, _on_row())  # night 1 baseline
    await _push_flush(svc, fake, clock, _on_row(overnight_volume=18000))
    assert len(ws.frames("batch")) == 1

    wall.dt = et(2026, 10, 27, 3, 50)  # Tue 03:50: closed
    await svc.poll_session_once()
    await svc.drain()
    assert svc.session is None

    wall.dt = et(2026, 10, 27, 20, 0)  # Tue 20:00: night 2
    await svc.poll_session_once()
    await svc.drain()
    assert svc.session == "overnight"
    assert _pending(ws) == SESSION_PENDING
    await _push_flush(svc, fake, clock, _on_row(overnight_volume=250, overnight_price=763.0))
    assert len(ws.frames("batch")) == 1  # night 2 first push: baseline only
    assert svc.admission.is_ok("SPY")


@pytest.mark.asyncio
async def test_session_kind_flip_drops_buffered_row():
    """A row buffered under one session kind is dropped when the poll flips
    the kind (overnight → rth here), even though the symbol stays ok."""
    clock, wall = Clock(1000.0), Wall(NOW)
    fake = FakeOpenD(snapshots=SNAPS, market_us=AHE)
    svc, ws = await _overnight_service(clock, wall, fake)
    await _push_flush(svc, fake, clock, _on_row())  # baseline, now ok
    fake.push([_on_row(overnight_volume=18000)])  # buffered, not flushed
    assert "SPY" in svc.batcher.pending

    fake.market_us = "MORNING"
    await svc.poll_session_once()
    await svc.drain()
    assert svc.session == "rth"
    assert svc.admission.is_ok("SPY")
    clock.advance(0.11)
    await svc.flush_now()
    assert ws.frames("batch") == []


# ---------------------------------------------------------------------------
# pre-market / after-hours (same receipt-time rules, own fields and close)
# ---------------------------------------------------------------------------


def test_session_rule_pre_and_after_states():
    assert us_session("PRE_MARKET_BEGIN", et(2026, 10, 27, 5, 0)) == "pre"
    assert us_session("AFTER_HOURS_BEGIN", et(2026, 10, 27, 17, 0)) == "after"


def _ext_row(**kw) -> dict:
    """Day D after-hours shape: last_price = D close, prev_close = D-1 close (prices synthetic)."""
    row = _on_row(
        overnight_price=None,
        overnight_volume=None,
        pre_price=758.2,
        pre_volume=900,
        pre_high_price=758.9,
        pre_low_price=757.1,
        after_price=760.9,
        after_volume=4200,
        after_high_price=761.2,
        after_low_price=760.1,
    )
    row.update(kw)
    return row


def test_pre_row_maps_pre_fields_close_is_last_price():
    pd, _ = extended_row_to_pricedata("SPY", _ext_row(), NOW, "pre")  # type: ignore[misc]
    assert (pd["last"], pd["volume"], pd["high"], pd["low"]) == (758.2, 900, 758.9, 757.1)
    assert pd["close"] == 760.5  # previous day's close = last_price before the open
    assert pd["session"] == "pre"


def test_after_row_maps_after_fields_close_is_prev_close():
    pd, _ = extended_row_to_pricedata("SPY", _ext_row(), NOW, "after")  # type: ignore[misc]
    assert (pd["last"], pd["volume"], pd["high"], pd["low"]) == (760.9, 4200, 761.2, 760.1)
    assert pd["close"] == 757.0  # last_price is already today's close
    assert pd["session"] == "after"
    assert pd["bid"] is None and pd["ask"] is None


def test_extended_row_without_session_trade_dropped():
    assert extended_row_to_pricedata("SPY", _ext_row(after_volume=0), NOW, "after") is None
    assert extended_row_to_pricedata("SPY", _ext_row(pre_price="N/A"), NOW, "pre") is None


@pytest.mark.asyncio
async def test_after_hours_baseline_then_published_then_overnight_rebaselines():
    """RTH → after-hours: SPY waits for an after-hours trade row (baseline,
    not published), then a changed row publishes with session "after";
    at 20:00 the overnight session starts from a fresh baseline."""
    clock, wall = Clock(1000.0), Wall(et(2026, 10, 27, 16, 5))
    fake = FakeOpenD(snapshots=SNAPS, market_us="AFTER_HOURS_BEGIN")
    svc, ws = await _overnight_service(clock, wall, fake)
    assert svc.session == "after"
    assert _pending(ws) == SESSION_PENDING

    await _push_flush(svc, fake, clock, _ext_row())  # baseline: ok, not published
    assert ws.last("assign")["ok"] == ["SPY"]
    assert ws.frames("batch") == []

    await _push_flush(svc, fake, clock, _ext_row(after_volume=4300, after_price=761.0))
    pd = ws.frames("batch")[-1]["updates"]["SPY"]
    assert (pd["last"], pd["session"], pd["close"]) == (761.0, "after", 757.0)

    wall.dt = et(2026, 10, 27, 20, 1)
    fake.market_us = AHE
    await svc.poll_session_once()
    await svc.drain()
    assert svc.session == "overnight"
    assert _pending(ws) == SESSION_PENDING  # new session: needs its own trade row


@pytest.mark.asyncio
async def test_pre_market_option_rows_not_published():
    clock, wall = Clock(1000.0), Wall(et(2026, 10, 27, 8, 0))
    fake = FakeOpenD(snapshots=SNAPS, market_us="PRE_MARKET_BEGIN")
    svc, ws = await _overnight_service(clock, wall, fake)
    await _push_flush(svc, fake, clock, _ext_row(code="US.SPY261030C770000"))
    await _push_flush(svc, fake, clock, _ext_row(code="US.SPY261030C770000", pre_volume=950))
    assert ws.frames("batch") == []
