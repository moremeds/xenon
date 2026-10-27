"""futu-quotes runtime-settings ``config`` frame (D5/D6).

The relay forwards the DB overrides of FUTU_QUOTE_QUOTA,
FUTU_UNITS_PER_SYMBOL, FUTU_BOOK_STALE_S and FUTU_GREEK_FACTORS; null,
missing or invalid values fall back to the service's env values. Quota and
units apply on the next admission evaluation, greeks / book staleness on
the next emit. No network: the service runs against ``FakeOpenD``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts.tests.test_futu_quotes import Clock, FakeOpenD, FakeWS, _settled_service, _want
from scripts.tests.test_futu_quotes_x7a import Wall
from scripts.tests.test_futu_quotes_x7a_t2 import OPT_KEY, OPT_SNAPSHOTS, _opt_row

ENV = {"FUTU_QUOTE_QUOTA": 800, "FUTU_UNITS_PER_SYMBOL": 2, "FUTU_BOOK_STALE_S": 0.0, "FUTU_GREEK_FACTORS": None}


async def _config(svc, ws, values):
    await svc.on_ws_message(ws, json.dumps({"action": "config", "values": values}))


@pytest.mark.asyncio
async def test_config_applies_valid_values_and_falls_back_to_env():
    svc = await _settled_service(FakeOpenD())
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    assert svc.effective_config() == ENV
    await _config(
        svc,
        ws,
        {"FUTU_QUOTE_QUOTA": 400, "FUTU_UNITS_PER_SYMBOL": 0, "FUTU_BOOK_STALE_S": 3, "FUTU_GREEK_FACTORS": {"x": 1}},
    )
    assert svc.effective_config() == {**ENV, "FUTU_QUOTE_QUOTA": 400, "FUTU_BOOK_STALE_S": 3}
    assert svc.admission.ceiling() <= 400
    await _config(svc, ws, {})  # every key null/missing → env values
    assert svc.effective_config() == ENV


@pytest.mark.asyncio
async def test_config_frame_is_not_a_want():
    svc = await _settled_service(FakeOpenD())
    ws = FakeWS()
    await svc.on_ws_connect(ws)
    await _want(svc, ws, 1, ["AAPL"])
    await svc.drain()
    await _config(svc, ws, {"FUTU_UNITS_PER_SYMBOL": 3})
    await svc.drain()
    assert svc.admission.desired == ["AAPL"]
    assert svc.admission.units_per_symbol == 3
    await _config(svc, ws, "bad")  # not an object: ignored
    assert svc.admission.units_per_symbol == 3


@pytest.mark.asyncio
async def test_config_from_replaced_connection_ignored():
    svc = await _settled_service(FakeOpenD())
    old, new = FakeWS(), FakeWS()
    await svc.on_ws_connect(old)
    await svc.on_ws_connect(new)
    await _config(svc, old, {"FUTU_QUOTE_QUOTA": 10})
    assert svc.admission.quota_cap == 800


@pytest.mark.asyncio
async def test_greek_factors_apply_on_next_emit():
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
    assert ws.last("batch")["updates"][OPT_KEY]["impliedVol"] is None

    await _config(svc, ws, {"FUTU_GREEK_FACTORS": {"iv": 0.01}})
    fake.push([_opt_row(last=3.5)])
    clock.advance(0.2)
    await svc.flush_now()
    upd = ws.last("batch")["updates"][OPT_KEY]
    assert upd["impliedVol"] == pytest.approx(0.285)
    assert upd["delta"] is None
