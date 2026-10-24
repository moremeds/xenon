"""PR #236 review round 3 follow-ups (Astra R3-F1, Fable R3-2): a refused
connect closes its context; held-row rechecks are capped and rotate.

Fake Futu SDK contexts / trade contexts; made-up ids, not market data.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scripts.tests.test_futu_observation import (  # noqa: F401 — autouse fixture
    ACC,
    SCOPE,
    SPY_META,
    _env,
    _order,
    _pos,
    _subs,
    resolver,
)
from scripts.tests.test_futu_observation_r2 import ByIdCtx
from scripts.tests.test_futu_observation_r4 import _acc_frame
from xenon.api.services import futu_observation as fo
from xenon.execution import orders_store


def test_refused_account_closes_each_context_and_keeps_no_stale_account():
    from futu import RET_OK

    from xenon.clients.futu_client import FutuClient
    from xenon.clients.futu_exceptions import FutuConnectionError

    contexts = []

    def _new_ctx(**_kw):
        ctx = MagicMock()
        rows = [(100, "SIMULATE")] if len(contexts) < 2 else [(100, "SIMULATE"), (200, "SIMULATE")]
        ctx.get_acc_list.return_value = (RET_OK, _acc_frame(rows))
        contexts.append(ctx)
        return ctx

    with (
        patch("xenon.clients.futu_client._probe_opend"),
        patch("xenon.clients.futu_client.OpenSecTradeContext", side_effect=_new_ctx),
    ):
        client = FutuClient(trd_env="SIMULATE", acc_id=200)
        for _ in range(2):
            with pytest.raises(FutuConnectionError):
                client.connect()
            assert (client._trd_ctx, client._acc_id, client.trd_env_of_matched_account()) == (None, None, None)
        assert [c.close.call_count for c in contexts] == [1, 1]

        client.connect()
        assert client.is_connected() and client._acc_id == 200
        assert contexts[2].close.call_count == 0
        client.disconnect()
        assert [c.close.call_count for c in contexts] == [1, 1, 1]


@pytest.mark.committed_db
def test_held_row_rechecks_are_capped_and_rotate(monkeypatch):
    monkeypatch.setenv("FUTU_TRADE_ACC_ID", ACC)
    monkeypatch.setenv("FUTU_TRADE_ENV", "SIMULATE")
    monkeypatch.setattr(fo, "_FULL_PASSES", {}, raising=False)
    monkeypatch.setattr(fo, "NOT_OBSERVED_RECHECK_PASSES", 1, raising=False)  # every full pass rechecks
    monkeypatch.setattr(fo, "NOT_OBSERVED_RECHECK_MAX", 3, raising=False)
    ids = [f"79{i:02d}" for i in range(10, 15)]
    ctx = ByIdCtx(positions=[_pos("US.SPY", 500.0)], open_orders=[_order(oid) for oid in ids])

    def run():
        start = len(ctx.calls)
        assert fo.run_pass(SCOPE, ctx, resolve_meta=resolver((SPY_META,)))["complete"] is True
        return [kw["order_id"] for name, kw in ctx.calls[start:] if name == "order_by_id"]

    run()
    ctx.open_orders = []
    run()
    run()  # second miss: all five held
    assert {_subs()[oid].reason_code for oid in ids} == {orders_store.FUTU_NOT_OBSERVED}

    first, second = run(), run()

    assert len(first) == 3 and len(second) == 3
    assert set(first) | set(second) == set(ids)  # rotation reaches every held row
