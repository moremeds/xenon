"""futu-quotes Task 3 (X7a): held/view tiers — contract D9.

Covers ``want.tiers`` parsing (absent or non-"view" = held), admission
order ``held`` before ``view``, and quota-pressure eviction of ``view``
before ``held`` — all through the existing 60 s min-subscribed + grace
unsubscribe rules.

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
    _settled_service,
    _want,
)
from scripts.tests.test_futu_quotes_x7a import Wall


def _svc_kw() -> dict:
    return {
        "clock": Clock(1000.0),
        "wall": Wall(datetime(2026, 10, 23, 14, 30, 0, tzinfo=timezone.utc)),
    }


# ---------------------------------------------------------------------------
# tier parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_absent_tier_defaults_held():
    """No ``tiers`` key, a missing symbol, and a non-"view" value all
    mean held."""
    kw = _svc_kw()
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, **kw)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL", "MSFT", "HYG"])  # no tiers key
    await svc.drain()
    assert all(svc.admission.state(s).tier == "held" for s in ("AAPL", "MSFT", "HYG"))

    await _want(svc, ws, 2, ["AAPL", "MSFT", "HYG"], {"MSFT": "view", "HYG": "x"})
    await svc.drain()
    assert svc.admission.state("AAPL").tier == "held"  # absent = held
    assert svc.admission.state("MSFT").tier == "view"
    assert svc.admission.state("HYG").tier == "held"  # non-"view" = held


@pytest.mark.asyncio
async def test_malformed_tiers_value_defaults_held():
    kw = _svc_kw()
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS)
    svc = await _settled_service(fake, **kw)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    import json

    await svc.on_ws_message(
        ws,
        json.dumps(
            {"action": "want", "gen": 1, "symbols": ["MSFT"], "tiers": "view"}
        ),
    )
    await svc.drain()
    assert svc.admission.state("MSFT").tier == "held"


# ---------------------------------------------------------------------------
# admit held before view
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_held_admitted_before_view_under_quota():
    """Ceiling fits one symbol (2 units): the held candidate takes the
    slot even though the view symbol was listed first."""
    kw = _svc_kw()
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=2, own_used=0, total_used=0)
    svc = await _settled_service(fake, **kw)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["MSFT", "AAPL"], {"MSFT": "view"})
    await svc.drain()
    last = ws.last("assign")
    assert "AAPL" in last["ok"]
    assert {"symbol": "MSFT", "reason": "quota"} in last["rejected"]
    assert "MSFT" not in last["ok"]


# ---------------------------------------------------------------------------
# eviction: view before held under pressure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_view_evicted_so_blocked_held_admits():
    """V (view) + H2 (held) fill the ceiling. A new held candidate is
    quota-blocked → V is marked for eviction and released through the
    normal 60 s rules; the held candidate admits, H2 is untouched."""
    kw = _svc_kw()
    clock = kw["clock"]
    # remain=4 → ceiling 4 units = two symbols.
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=4, own_used=0, total_used=0)
    svc = await _settled_service(fake, **kw)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["MSFT", "AAPL"], {"MSFT": "view"})
    await svc.drain()
    assert sorted(ws.last("assign")["ok"]) == ["AAPL", "MSFT"]

    # HYG (held) wants in; ceiling is full → MSFT (view) marked.
    await _want(svc, ws, 2, ["MSFT", "AAPL", "HYG"], {"MSFT": "view"})
    await svc.drain()
    assert svc.admission.state("MSFT").evicted is True
    assert {"symbol": "HYG", "reason": "quota"} in ws.last("assign")["rejected"]

    # Next evaluation reports the eviction truth and keeps H2 ok.
    await svc.tick_unsubscribes_once()
    await svc.drain()
    last = ws.last("assign")
    assert "AAPL" in last["ok"]
    assert {"symbol": "MSFT", "reason": "quota"} in last["rejected"]
    assert svc.admission.state("AAPL").evicted is False

    # 60 s min-lifetime + grace are respected: nothing leaves early.
    assert fake.unsubscribe_calls == []
    clock.advance(121)
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert fake.unsubscribe_calls == ["US.MSFT"]

    # The freed slot goes to the held candidate — MSFT (view, still
    # wanted) may not steal it back while HYG is blocked. HYG's quota
    # backoff is bounded; once it elapses it subscribes.
    clock.advance(15)
    await svc.tick_unsubscribes_once()
    await svc.drain()
    last = ws.last("assign")
    assert "HYG" in last["ok"]
    assert "AAPL" in last["ok"]
    assert {"symbol": "MSFT", "reason": "quota"} in last["rejected"]
    assert not svc.admission.state("MSFT").subscribed
    assert svc.admission.state("MSFT").evicted is False  # release ends it


@pytest.mark.asyncio
async def test_over_capacity_evicts_view_before_held():
    """Provider quota shrinks below believed usage: the view symbol is
    released first; the held symbol is only evicted once views are gone."""
    kw = _svc_kw()
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=4, own_used=0, total_used=0)
    svc = await _settled_service(fake, **kw)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["MSFT", "AAPL"], {"MSFT": "view"})
    await svc.drain()
    assert sorted(ws.last("assign")["ok"]) == ["AAPL", "MSFT"]

    # Provider now reports remain=0 but own_used stays 4 → ceiling = own
    # (4) — no over-capacity yet. Force over: drop provider own_used to 2
    # so ceiling = own(2)+remain(0) = 2 < controlled 4.
    fake.remain = 0
    fake.own_used = 2
    await svc.refresh_quota_once()
    await svc.drain()
    assert svc.admission.state("MSFT").evicted is True
    assert svc.admission.state("AAPL").evicted is False


@pytest.mark.asyncio
async def test_eviction_retracts_when_pressure_resolves():
    """If the pressure goes away before the 60 s mark, the evicted mark
    retracts and nothing is unsubscribed."""
    kw = _svc_kw()
    clock = kw["clock"]
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=4, own_used=0, total_used=0)
    svc = await _settled_service(fake, **kw)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["MSFT", "AAPL"], {"MSFT": "view"})
    await svc.drain()

    await _want(svc, ws, 2, ["MSFT", "AAPL", "HYG"], {"MSFT": "view"})
    await svc.drain()
    assert svc.admission.state("MSFT").evicted is True

    # Pressure resolves: HYG drops out of the want set before the mark
    # can fire — MSFT keeps its slot and stays ok.
    await _want(svc, ws, 3, ["MSFT", "AAPL"], {"MSFT": "view"})
    clock.advance(200)
    await svc.tick_unsubscribes_once()
    await svc.drain()
    assert svc.admission.state("MSFT").evicted is False
    assert fake.unsubscribe_calls == []
    assert "MSFT" in ws.last("assign")["ok"]


@pytest.mark.asyncio
async def test_tier_promotion_to_held_clears_eviction():
    """A want that promotes an evicted symbol to held clears the mark."""
    kw = _svc_kw()
    fake = FakeOpenD(snapshots=DEFAULT_SNAPSHOTS, remain=4, own_used=0, total_used=0)
    svc = await _settled_service(fake, **kw)
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["MSFT", "AAPL"], {"MSFT": "view"})
    await svc.drain()
    await _want(svc, ws, 2, ["MSFT", "AAPL", "HYG"], {"MSFT": "view"})
    await svc.drain()
    assert svc.admission.state("MSFT").evicted is True

    await _want(svc, ws, 3, ["MSFT", "AAPL", "HYG"], {})
    await svc.drain()
    assert svc.admission.state("MSFT").tier == "held"
    assert svc.admission.state("MSFT").evicted is False
