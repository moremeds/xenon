# ruff: noqa: F811 -- route fixtures are imported by name and re-bound as test args
"""Gate-4 PR-03b round 5 — monotone watermark (1), send-by fence (2),
terminal-UUID resurrection (3), permId-only keys (4), request-owned body
match (5), fill lag (6), unstamped UUID rows (7)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select, text

from scripts.tests.test_gate4_reserve import (
    SCOPE,
    _insert_fill,
    _insert_submission,
    _reserve,
    _seed_snapshot,
    _snapshot_payload,
    _stock,
    _working_with_legs,
)
from scripts.tests.test_gate4_round3 import _legs, _mirror, _set_perm
from scripts.tests.test_gate4_route import (  # noqa: F401 — fixtures
    _modify_qty,
    _pg,
    _pg_engine,
    _sell_call_body,
    _submission_rows,
    _test_mode_off,
    broker_calls,
    client,
    fresh_quote,
    ib_data,
)
from scripts.tests.test_gate4_route import _seed_snapshot as _route_seed_snapshot
from scripts.tests.test_gate4_route import _stock as _route_stock
from scripts.tests.test_gate4_sync_pass import (  # noqa: F401 — fixtures
    ACCOUNT,
    _fresh_meta_cache,
    _insert_uuid_sell_call,
    _latest_payload,
    _opt,
    _row,
    _run_sync,
    _shares_client,
    _trade,
)
from xenon.api import server as server_mod
from xenon.api.subprocess import ScriptResult
from xenon.db.schema import order_submissions
from xenon.execution import gate4, ib_order_manage, ib_orders, orders_store

# Route fixtures truncate and use the committed engine; every test here is
# committed (the barrier test then runs B on a real second connection).
pytestmark = pytest.mark.committed_db

_REAL_RUN_IB_SCRIPT = server_mod._run_ib_script_with_recovery


def _now():
    return datetime.now(timezone.utc)


def _committed_sql(sql, **params):
    engine = _pg_engine()
    with engine.begin() as conn:
        conn.execute(text(sql), params)
    engine.dispose()


# ---------------------------------------------------------------------------
# item 1 — the watermark never moves backwards
# ---------------------------------------------------------------------------


def test_mirror_that_waited_on_the_lock_cannot_hide_a_newer_amend(monkeypatch):
    """Astra delta-2 P1-1 barrier. Mirror M started (its read began
    earlier) and waits on the account lock. Meanwhile read R begins and
    amend B (2 -> 1) commits first. M then writes. R — which began before
    B — reports qty 1: it must NOT confirm B, and the next short stays
    blocked (IB may still hold 2-3 contracts)."""
    _seed_snapshot(_snapshot_payload([_stock("SPY", 300)]))
    sub_id = _working_with_legs("r5-barrier", qty=2)
    _set_perm(sub_id, "990301")
    real_lock = orders_store._lock_gate4_scope
    box: dict = {}

    def _lock_after_amend(conn, *args):
        if "b" not in box:  # M is now waiting on the lock
            box["r_started"] = _now()  # read R begins ...
            box["b"] = gate4.begin_modify(sub_id, 1, SCOPE)  # ... then B commits
        real_lock(conn, *args)

    m_read = _now() - timedelta(seconds=1)
    monkeypatch.setattr(orders_store, "_lock_gate4_scope", _lock_after_amend)
    assert _mirror("990301", 3, observed_at=m_read)["observed"] is True  # stale raise: writes
    monkeypatch.setattr(orders_store, "_lock_gate4_scope", real_lock)
    opened_at = datetime.fromisoformat(_legs(sub_id)[1]["pending_amend_opened_at"])

    out = _mirror("990301", 1, observed_at=box["r_started"])

    assert "reconciled" not in out
    qty, legs = _legs(sub_id)
    assert qty == 2
    assert legs["pending_amend_id"] == box["b"].amend_id and legs["pending_amend_qty"] == 1
    assert box["r_started"] < opened_at
    assert _reserve("r5-barrier-next").status == "rejected"


def test_confirm_needs_read_after_amend_opened():
    """`pending_amend_opened_at` gates confirm/expire on its own, even when
    the row watermark says the read is fresh."""
    opened = _now()
    payload = {
        "pending_amend_id": "x",
        "pending_amend_qty": 2,
        "pending_amend_opened_at": opened.isoformat(),
        "pending_amend_deadline": (opened - timedelta(seconds=1)).isoformat(),
    }
    before = opened - timedelta(milliseconds=1)
    assert orders_store._reconcile_pending(payload, 2, observed_at=before, fresh=True) == (payload, None)
    assert (
        orders_store._reconcile_pending(payload, 2, observed_at=opened + timedelta(milliseconds=1), fresh=True)[1]
        == "confirmed"
    )


# ---------------------------------------------------------------------------
# item 2 — send-by fence
# ---------------------------------------------------------------------------


def test_delayed_spawn_past_send_by_sends_nothing_and_keeps_max(client, broker_calls, fresh_quote, monkeypatch):
    """The gateway probe delays the modify spawn past send_by: the route
    never starts the CLI, settles uncertain, and demand stays max(1,2)
    until a fresh read after send_by + grace."""
    engine = _pg_engine()
    _route_seed_snapshot(engine, [_route_stock("SPY", 200)])
    engine.dispose()
    assert client.post("/orders/place", json=_sell_call_body("r5-fence-1")).status_code == 200

    spawned = []

    async def _spawn(entry, args, timeout=30.0, cwd=None):
        spawned.append(args)
        return ScriptResult(ok=True, data={"status": "ok"})

    async def _slow_probe():
        await asyncio.sleep(0.3)
        return {"port_listening": True, "upstream_dead": False}

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", _REAL_RUN_IB_SCRIPT)
    monkeypatch.setattr(server_mod, "run_entry_point", _spawn)
    monkeypatch.setattr(server_mod, "_pool_has_any_connection", lambda: False)
    monkeypatch.setattr(server_mod, "check_ib_gateway", _slow_probe)
    monkeypatch.setitem(server_mod._ib_last_failure, "order", 0.0)
    monkeypatch.setattr(gate4, "MODIFY_SEND_WINDOW_S", 0.1)

    resp = _modify_qty(client, 2, 1)

    assert resp.status_code == 503, resp.text
    assert spawned == []
    row = _submission_rows()[0]._mapping
    legs = row["legs"]
    assert legs["pending_amend_qty"] == 2 and row["quantity"] == 1
    nxt = client.post("/orders/place", json=_sell_call_body("r5-fence-next"))
    assert nxt.status_code == 400 and nxt.json()["reason_code"] == "ETF_CALL_UNCOVERED"

    deadline = datetime.fromisoformat(legs["pending_amend_deadline"])
    send_by = datetime.fromisoformat(legs["pending_amend_send_by"])
    assert deadline == send_by + timedelta(seconds=gate4.AMEND_IB_GRACE_S)
    perm = row["perm_id"]
    late = deadline - timedelta(milliseconds=1)
    orders_store.register_from_snapshot(perm, "4242", "SPY", "OPT", "SELL", 1, 5.0, 100, observed_at=late, **SCOPE)
    assert _submission_rows()[0]._mapping["legs"]["pending_amend_qty"] == 2
    out = orders_store.register_from_snapshot(
        perm, "4242", "SPY", "OPT", "SELL", 1, 5.0, 100, observed_at=deadline + timedelta(seconds=1), **SCOPE
    )
    assert out["expired"] is True


def test_route_passes_send_by_to_cli():
    args = ["modify", "--order-id", "1", "--send-by", (_now() + timedelta(seconds=5)).isoformat()]
    assert server_mod._send_window_closed(args) is False
    args[-1] = (_now() - timedelta(seconds=1)).isoformat()
    assert server_mod._send_window_closed(args) is True
    assert server_mod._send_window_closed(["modify", "--order-id", "1"]) is False


def _fence_cli_run(send_by, *, connect_delay=0.0):
    """Run the REAL modify_order against a fake IB whose order is owned by
    clientId 26 (forces the reconnect path). Returns (reply, place calls)."""

    class _Event:
        def __iadd__(self, handler):
            return self

        def __isub__(self, handler):
            return self

    t = MagicMock()
    t.order.orderId, t.order.permId, t.order.clientId = 4242, 777001, 26
    t.order.orderType, t.order.lmtPrice, t.order.totalQuantity = "LMT", 2.0, 1
    t.orderStatus.status = "Submitted"
    t.contract.secType = "OPT"
    c = MagicMock()
    c.ib.client.clientId = 0
    c.ib.errorEvent = _Event()
    c.get_open_orders.return_value = [t]
    c.connect.side_effect = lambda **kw: time.sleep(connect_delay)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        ib_order_manage.modify_order(c, 4242, 777001, None, "127.0.0.1", 4002, new_quantity=2, send_by=send_by)
    return json.loads(buf.getvalue()), c.place_order.call_count


def test_cli_past_send_by_never_calls_place_order():
    reply, placed = _fence_cli_run(_now() - timedelta(seconds=1))
    assert placed == 0
    assert reply["classification"] == "send_window_closed"
    assert reply["amend_outcome"] == "not_sent"


def test_cli_send_by_rechecked_after_reconnect():
    """send_by was in the future at start; the owner reconnect ate it."""
    reply, placed = _fence_cli_run(_now() + timedelta(seconds=0.2), connect_delay=0.3)
    assert placed == 0
    assert reply["classification"] == "send_window_closed"


def test_cli_rejects_naive_send_by():
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        ib_order_manage._parse_send_by("2026-10-17T10:00:00")


# ---------------------------------------------------------------------------
# item 3 — resurrection keeps demand and metadata (real sync pass)
# ---------------------------------------------------------------------------


def test_resurrected_uuid_row_reserves_observed_qty_and_restamps(monkeypatch, client, broker_calls, fresh_quote):
    sub_id = _insert_uuid_sell_call(990310, qty=1, legs=None)
    _committed_sql("UPDATE xenon.order_submissions SET state='CANCELLED' WHERE submission_id=:s", s=sub_id)
    c = _opt(500, con_id=111)
    fake = _shares_client(200)
    fake.add_details(c)
    fake.open_orders.append(_trade(c, order_id=90310, perm_id=990310, qty=2))

    _run_sync(monkeypatch, fake)

    assert _latest_payload()["observation"]["complete"] is True
    row = _row(sub_id)
    assert row["state"] == "WORKING"
    assert row["legs"]["observed_qty"] == 2
    leg = row["legs"]["legs"][0]
    assert (leg["tc"], leg["mult"], leg["und_sec"], leg["con_id"]) == ("SPY", 100, "STK", 111)
    # 2 contracts reserve all 200 shares (qty 1 would leave 100 free).
    resp = client.post("/orders/place", json=_sell_call_body("r5-resurrect-next"))
    assert resp.status_code == 400 and resp.json()["reason_code"] == "ETF_CALL_UNCOVERED", resp.text


# ---------------------------------------------------------------------------
# item 4 — an order without permId is never keyed by orderId
# ---------------------------------------------------------------------------


def test_two_clients_permid_zero_same_order_id_make_pass_incomplete(monkeypatch, client, broker_calls, fresh_quote):
    c = _opt(500, con_id=111)
    fake = _shares_client(300)
    fake.add_details(c)
    for cid in (21, 22):
        t = _trade(c, order_id=7, perm_id=0)
        t.order.clientId = cid
        fake.open_orders.append(t)

    _run_sync(monkeypatch, fake)

    obs = _latest_payload()["observation"]
    assert obs["complete"] is False
    assert [r for r in _submission_rows() if r._mapping["submission_id"] == "snapshot-7"] == []


# ---------------------------------------------------------------------------
# item 5 — identical replay after a mirror tick is still a duplicate
# ---------------------------------------------------------------------------


def test_replay_after_mirror_tick_is_duplicate(client, broker_calls, fresh_quote):
    engine = _pg_engine()
    _route_seed_snapshot(engine, [_route_stock("SPY", 200)])
    engine.dispose()
    body = _sell_call_body("r5-replay-1")
    assert client.post("/orders/place", json=body).status_code == 200
    observed = {
        "orderId": 4242,
        "permId": 777001,
        "clientId": 26,
        "account": ACCOUNT,
        "contract": {
            "secType": "OPT",
            "symbol": "SPY",
            "strike": 500.0,
            "right": "C",
            "expiry": "20261218",
            "conId": 111,
            "tradingClass": "SPY",
            "multiplier": "100",
            "underSecType": "STK",
        },
        "action": "SELL",
        "totalQuantity": 1.0,
        "limitPrice": 5.0,
        "tif": "DAY",
    }
    ib_orders.sync_open_orders_to_postgres([observed], scope=SCOPE, observed_at=_now())
    assert _submission_rows()[0]._mapping["legs"]["account"] == ACCOUNT  # the mirror wrote

    again = client.post("/orders/place", json=body)

    assert again.status_code == 200, again.text
    assert len(broker_calls) == 1
    assert len(_submission_rows()) == 1


# ---------------------------------------------------------------------------
# item 6 — fills just before as_of count as after-snapshot demand
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("age_s", "expect"), [(30, "rejected"), (120, "winner")])
def test_fill_inside_lag_window_counts_as_after_snapshot(age_s, expect):
    """100 shares; a Xenon short call filled `age_s` before as_of. Inside
    FILL_LAG_S the positions read may not show it yet, so it still
    reserves its cover; outside the window the snapshot owns it."""
    as_of = _now()
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100)], as_of=as_of))
    sub_id = _insert_submission("r5-lag", state="FILLED", qty=1)
    _insert_fill("r5-lag-exec", sub_id=sub_id, qty=1, filled_at=as_of - timedelta(seconds=age_s))
    assert _reserve(f"r5-lag-next-{age_s}").status == expect


# ---------------------------------------------------------------------------
# item 7 — Xenon rows whose permId was never stamped
# ---------------------------------------------------------------------------


def _unstamped_uuid_row(perm_marker, *, placing_client_id=26):
    sub_id = _insert_uuid_sell_call(perm_marker, qty=1, legs=None)
    _committed_sql(
        "UPDATE xenon.order_submissions SET perm_id=NULL, placing_client_id=:c WHERE submission_id=:s",
        c=placing_client_id,
        s=sub_id,
    )
    return sub_id


def test_unstamped_uuid_row_is_matched_by_order_and_client_id(monkeypatch, client, broker_calls, fresh_quote):
    sub_id = _unstamped_uuid_row(990320)  # ib_order_id = 90320
    c = _opt(500, con_id=111)
    fake = _shares_client(200)
    fake.add_details(c)
    t = _trade(c, order_id=90320, perm_id=990320)
    t.order.clientId = 26
    fake.open_orders.append(t)

    _run_sync(monkeypatch, fake)

    ids = [r._mapping["submission_id"] for r in _submission_rows()]
    assert ids == [sub_id]  # no parallel snapshot-990320 row
    row = _row(sub_id)
    assert row["perm_id"] == "990320"
    assert row["legs"]["legs"][0]["tc"] == "SPY"  # mirrored onto the UUID row


def test_unstamped_uuid_row_other_client_is_not_guessed(monkeypatch, client, broker_calls, fresh_quote):
    sub_id = _unstamped_uuid_row(990321, placing_client_id=27)
    c = _opt(500, con_id=111)
    fake = _shares_client(200)
    fake.add_details(c)
    t = _trade(c, order_id=90321, perm_id=990321)
    t.order.clientId = 26
    fake.open_orders.append(t)

    _run_sync(monkeypatch, fake)

    assert _row(sub_id)["perm_id"] is None
    engine = _pg_engine()
    with engine.connect() as conn:
        snap = conn.execute(
            select(order_submissions.c.submission_id).where(order_submissions.c.submission_id == "snapshot-990321")
        ).first()
    engine.dispose()
    assert snap is not None
