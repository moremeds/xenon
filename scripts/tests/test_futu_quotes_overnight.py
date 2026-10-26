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
    OvernightBaseline,
    overnight_row_to_pricedata,
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
    for state in ("BOGUS", None, "", "CLOSED", "PRE_MARKET_BEGIN", "AFTER_HOURS_BEGIN"):
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
    pd, ts = overnight_row_to_pricedata("SPY", _on_row(), NOW)  # type: ignore[misc]
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
    assert overnight_row_to_pricedata("SPY", row, NOW) is None
    row.pop("overnight_price")
    assert overnight_row_to_pricedata("SPY", row, NOW) is None


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
    b = OvernightBaseline()
    assert not b.changed("SPY", _on_row())  # first row: baseline
    assert not b.changed("SPY", _on_row())  # unchanged
    assert b.changed("SPY", _on_row(overnight_volume=18132))  # new trade
    assert not b.changed("SPY", _on_row(overnight_volume=18132))
    assert b.changed("SPY", _on_row(overnight_volume=18132, overnight_price=761.25))
    b.reset("SPY")
    assert not b.changed("SPY", _on_row(overnight_volume=1))  # baseline again
    b.reset_all()
    assert not b.changed("SPY", _on_row(overnight_volume=2))


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
    clock.advance(0.11)
    await svc.flush_now()


SNAPS = {**DEFAULT_SNAPSHOTS, "US.SPY": {"code": "US.SPY", "sec_type": "ETF", "last_price": 1.0}}


@pytest.mark.asyncio
async def test_overnight_state_admits_symbols_ok():
    clock, wall = Clock(1000.0), Wall(NOW)
    fake = FakeOpenD(snapshots=SNAPS, market_us=AHE)
    svc, ws = await _overnight_service(clock, wall, fake)
    assert svc.session == "overnight"
    last = ws.last("assign")
    assert last is not None and last["ok"] == ["SPY"]
    assert last["pending"] == []


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
    assert svc.admission.is_ok("SPY")
    # Differs from the pre-unsubscribe value, but it is the first row of the
    # new subscription: baseline only.
    await _push_flush(svc, fake, clock, _on_row(overnight_volume=99999))
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
    assert svc.admission.is_ok("SPY")
    await _push_flush(svc, fake, clock, _on_row(overnight_volume=99999))
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
    assert svc.admission.is_ok("SPY")
    await _push_flush(svc, fake, clock, _on_row())  # baseline after the switch
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
