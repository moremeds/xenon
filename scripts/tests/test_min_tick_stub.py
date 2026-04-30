"""Lock-in regression for the front-of-house minTick stub.

By design the stub returns 0.01 for every contract — IB enforces the real
tick rule server-side and rejects with code 110 when the limit price is
off-tick. See ``_lookup_min_tick_via_pool`` docstring for the rationale.

If you ever change this, also update the matching docstring + the
``ib_place_order`` structured logger that flags code-110 rejections.
"""

from __future__ import annotations

from decimal import Decimal

from xenon.api.server import _lookup_min_tick_via_pool


def test_min_tick_stub_returns_one_cent_for_every_contract():
    assert _lookup_min_tick_via_pool(con_id=320227571) == Decimal("0.01")
    assert _lookup_min_tick_via_pool(con_id=987654321) == Decimal("0.01")
    # Edge case con_ids: huge int, zero, negative — all approximate to 0.01.
    assert _lookup_min_tick_via_pool(con_id=0) == Decimal("0.01")
    assert _lookup_min_tick_via_pool(con_id=-1) == Decimal("0.01")
