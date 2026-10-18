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
    _QUALIFIED,
    ACCOUNT,
    _fresh_meta_cache,
    _insert_uuid_sell_call,
    _latest_payload,
    _opt,
    _row,
    _run_sync,
    _shares_client,
    _stk,
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


def test_reservation_write_never_moves_watermark_back():
    """GREATEST clause alone. An out-of-lock writer (or a clock step back)
    left updated_at later than the next reservation writer's `now`. The
    amend write must not lower it, and a read that began before that later
    stamp (but after the amend opened) must not count as fresh."""
    _seed_snapshot(_snapshot_payload([_stock("SPY", 300)]))
    sub_id = _working_with_legs("r5-greatest", qty=1)
    _set_perm(sub_id, "990302")
    later = _now() + timedelta(seconds=5)
    _committed_sql("UPDATE xenon.order_submissions SET updated_at=:t WHERE submission_id=:s", t=later, s=sub_id)

    b = gate4.begin_modify(sub_id, 2, SCOPE)

    engine = _pg_engine()
    with engine.connect() as conn:
        stamp = conn.execute(
            select(order_submissions.c.updated_at).where(order_submissions.c.submission_id == sub_id)
        ).scalar_one()
    engine.dispose()
    assert stamp > later
    opened_at = datetime.fromisoformat(_legs(sub_id)[1]["pending_amend_opened_at"])
    read_started = later - timedelta(seconds=1)
    assert opened_at < read_started  # the opened_at gate alone would not stop it

    out = _mirror("990302", 2, observed_at=read_started)

    assert "reconciled" not in out
    qty, legs = _legs(sub_id)
    assert qty == 1 and legs["pending_amend_id"] == b.amend_id


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


def _refuse_cooldown(monkeypatch):
    monkeypatch.setitem(server_mod._ib_last_failure, "order", time.monotonic())


def _refuse_gateway_down(monkeypatch):
    async def _down():
        return {"port_listening": False, "upstream_dead": False}

    monkeypatch.setattr(server_mod, "_pool_has_any_connection", lambda: False)
    monkeypatch.setattr(server_mod, "check_ib_gateway", _down)


def _refuse_send_window(monkeypatch):
    async def _slow_probe():
        await asyncio.sleep(0.3)  # spawn delayed past send_by
        return {"port_listening": True, "upstream_dead": False}

    monkeypatch.setattr(server_mod, "_pool_has_any_connection", lambda: False)
    monkeypatch.setattr(server_mod, "check_ib_gateway", _slow_probe)
    monkeypatch.setattr(gate4, "MODIFY_SEND_WINDOW_S", 0.1)


@pytest.mark.parametrize("refuse", [_refuse_cooldown, _refuse_gateway_down, _refuse_send_window])
def test_pre_spawn_refusal_sends_nothing_and_releases_token(refuse, client, broker_calls, fresh_quote, monkeypatch):
    """Round 6 item 3 (Fable P3): the route refuses before any spawn —
    cooldown, gateway down, or (round 5 item 2) send window closed. Nothing
    reached IB, so the amend settles `rejected`: the token is released and a
    retry is not blocked by MODIFY_PENDING."""
    engine = _pg_engine()
    _route_seed_snapshot(engine, [_route_stock("SPY", 200)])
    engine.dispose()
    assert client.post("/orders/place", json=_sell_call_body("r6-refuse-1")).status_code == 200

    spawned = []

    async def _spawn(entry, args, timeout=30.0, cwd=None):
        spawned.append(args)
        return ScriptResult(ok=True, data={"status": "ok"})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", _REAL_RUN_IB_SCRIPT)
    monkeypatch.setattr(server_mod, "run_entry_point", _spawn)
    monkeypatch.setitem(server_mod._ib_last_failure, "order", 0.0)
    refuse(monkeypatch)

    resp = _modify_qty(client, 2, 1)

    assert resp.status_code == 503, resp.text
    assert spawned == []
    row = _submission_rows()[0]._mapping
    assert row["quantity"] == 1
    assert row["legs"]["pending_amend_id"] is None and row["legs"]["pending_amend_qty"] is None
    retry = _modify_qty(client, 2, 2)
    assert retry.json().get("reason_code") != "MODIFY_PENDING", retry.text
    assert spawned == []


def test_open_amend_never_expires_before_send_by_plus_grace():
    """No settle at all: a fresh read just before send_by + grace keeps the
    max reservation; the first read after it expires the token."""
    _seed_snapshot(_snapshot_payload([_stock("SPY", 300)]))
    sub_id = _working_with_legs("r5-noexpiry", qty=1)
    _set_perm(sub_id, "990303")
    gate4.begin_modify(sub_id, 2, SCOPE)
    legs = _legs(sub_id)[1]
    deadline = datetime.fromisoformat(legs["pending_amend_deadline"])
    send_by = datetime.fromisoformat(legs["pending_amend_send_by"])
    assert deadline == send_by + timedelta(seconds=gate4.AMEND_IB_GRACE_S)

    _mirror("990303", 1, observed_at=deadline - timedelta(milliseconds=1))
    assert _legs(sub_id)[1]["pending_amend_qty"] == 2
    assert _mirror("990303", 1, observed_at=deadline + timedelta(seconds=1))["expired"] is True


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


def _unstamped_uuid_row(perm_marker, *, placing_client_id=26, state="WORKING", action="SELL", legs=_QUALIFIED):
    """Xenon row placed during the permId=0 race: perm_id NULL, ib_order_id
    = perm_marker - 900000."""
    sub_id = _insert_uuid_sell_call(perm_marker, qty=1, legs=legs)
    _committed_sql(
        "UPDATE xenon.order_submissions SET perm_id=NULL, placing_client_id=:c, state=:st, action=:a"
        " WHERE submission_id=:s",
        c=placing_client_id,
        st=state,
        a=action,
        s=sub_id,
    )
    return sub_id


def _sync_one_sell_call(monkeypatch, perm, *, shares=100, client_id=26):
    c = _opt(500, con_id=111)
    fake = _shares_client(shares)
    fake.add_details(c)
    t = _trade(c, order_id=perm - 900000, perm_id=perm)
    t.order.clientId = client_id
    fake.open_orders.append(t)
    _run_sync(monkeypatch, fake)


def _has_snapshot_row(perm):
    return any(r._mapping["submission_id"] == f"snapshot-{perm}" for r in _submission_rows())


def test_terminal_buy_row_is_not_adopted_by_a_new_sell(monkeypatch, client, broker_calls, fresh_quote):
    """Astra delta-3 P1: a cancelled BUY (clientId 26, orderId reused) must
    not absorb a new SELL call with the same ids — the SELL demand would be
    dropped as pure BUY. A snapshot row reserves it; the next short is
    blocked."""
    sub_id = _unstamped_uuid_row(990330, state="CANCELLED", action="BUY")
    _sync_one_sell_call(monkeypatch, 990330)

    assert _has_snapshot_row(990330)
    row = _row(sub_id)
    assert row["perm_id"] is None and row["state"] == "CANCELLED"
    resp = client.post("/orders/place", json=_sell_call_body("r6-adopt-next"))
    assert resp.status_code == 400 and resp.json()["reason_code"] == "ETF_CALL_UNCOVERED", resp.text


def test_cancelled_identical_row_is_not_adopted(monkeypatch, client, broker_calls, fresh_quote):
    """Same contract, same envelope, no fills — but CANCELLED (e.g. an
    orderId sequence reset reused clientId 26 / orderId). Only a live row
    may be adopted."""
    sub_id = _unstamped_uuid_row(990334, state="CANCELLED")
    _sync_one_sell_call(monkeypatch, 990334)

    assert _has_snapshot_row(990334)
    row = _row(sub_id)
    assert row["perm_id"] is None and row["state"] == "CANCELLED"


@pytest.mark.parametrize("state", ["FILLED", "WORKING"])
def test_row_with_recorded_fills_is_not_adopted(state, monkeypatch, client, broker_calls, fresh_quote):
    """An old order whose fills are recorded (FILLED, or a row whose state
    never moved) must not lend its fill history to a new order."""
    sub_id = _unstamped_uuid_row(990331, state=state)
    _insert_fill("r6-old-exec", sub_id=sub_id, qty=1, filled_at=_now() - timedelta(days=1))
    _sync_one_sell_call(monkeypatch, 990331)

    assert _has_snapshot_row(990331)
    assert _row(sub_id)["perm_id"] is None


def test_identity_mismatch_is_not_adopted(monkeypatch, client, broker_calls, fresh_quote):
    other = {
        **_QUALIFIED,
        "legs": [{**_QUALIFIED["legs"][0], "strike": 510.0, "con_id": 5101}],
    }
    sub_id = _unstamped_uuid_row(990332, legs=other)
    _sync_one_sell_call(monkeypatch, 990332)

    assert _has_snapshot_row(990332)
    assert _row(sub_id)["perm_id"] is None


def test_two_compatible_rows_are_not_adopted(monkeypatch, client, broker_calls, fresh_quote):
    first = _unstamped_uuid_row(990333)
    second = _insert_uuid_sell_call(990333 + 1, qty=1, legs=_QUALIFIED)
    _committed_sql(
        "UPDATE xenon.order_submissions SET perm_id=NULL, placing_client_id=26, ib_order_id=:o WHERE submission_id=:s",
        o=str(990333 - 900000),
        s=second,
    )
    _sync_one_sell_call(monkeypatch, 990333)

    assert _has_snapshot_row(990333)
    assert _row(first)["perm_id"] is None and _row(second)["perm_id"] is None


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


# ---------------------------------------------------------------------------
# round 6 item 2 — TIF is part of the request identity
# ---------------------------------------------------------------------------


def test_same_attempt_day_then_gtc_is_body_mismatch(client, broker_calls, fresh_quote):
    engine = _pg_engine()
    _route_seed_snapshot(engine, [_route_stock("SPY", 200)])
    engine.dispose()
    body = {**_sell_call_body("r6-tif-1"), "tif": "DAY"}
    assert client.post("/orders/place", json=body).status_code == 200
    assert _submission_rows()[0]._mapping["tif"] == "DAY"

    gtc = client.post("/orders/place", json={**body, "tif": "GTC"})
    assert gtc.status_code == 409 and gtc.json()["reason_code"] == "ATTEMPT_BODY_MISMATCH", gtc.text
    # Omitted / lower-case TIF normalizes like the place CLI (DAY default).
    no_tif = {k: v for k, v in body.items() if k != "tif"}
    assert client.post("/orders/place", json=no_tif).status_code == 200
    assert client.post("/orders/place", json={**body, "tif": "day"}).status_code == 200
    assert len(broker_calls) == 1
    # The stored TIF is the request's, not the column default.
    gtc_body = {**_sell_call_body("r6-tif-2"), "tif": "GTC"}
    assert client.post("/orders/place", json=gtc_body).status_code == 200
    day = client.post("/orders/place", json={**gtc_body, "tif": "DAY"})
    assert day.status_code == 409, day.text


def test_legacy_row_without_stored_tif_compares_as_day():
    """Pre-round-6 rows carry the column default ('DAY', or NULL in old
    snapshots of the row): they dedupe a DAY replay and refuse GTC."""
    from types import SimpleNamespace

    req = orders_store.RequestRow(
        ticker="SPY", security_type="OPT", action="SELL", quantity=1, multiplier=100, limit_price=5
    )
    row = SimpleNamespace(
        ticker="SPY",
        security_type="OPT",
        action="SELL",
        quantity=1,
        expiry=None,
        strike=None,
        right=None,
        multiplier=100,
        con_id=None,
        limit_price=5,
        legs=None,
        tif=None,
    )
    assert orders_store._request_body_matches(row, req, None)
    gtc = req.model_copy(update={"tif": "GTC"})
    assert not orders_store._request_body_matches(row, gtc, None)


# ---------------------------------------------------------------------------
# round 6 item 4 — the rehydrate writer keeps the watermark monotone
# ---------------------------------------------------------------------------


def test_rehydrate_state_writer_never_moves_watermark_back():
    from xenon.db.engine import get_sync_engine
    from xenon.db.queries import combo_wizard

    sub_id = _working_with_legs("r6-rehydrate", qty=1)
    later = _now() + timedelta(seconds=5)
    _committed_sql("UPDATE xenon.order_submissions SET updated_at=:t WHERE submission_id=:s", t=later, s=sub_id)
    with get_sync_engine().begin() as conn:
        combo_wizard.update_order_state(conn, sub_id, "WORKING", expected_states=("WORKING",))
    engine = _pg_engine()
    with engine.connect() as conn:
        stamp = conn.execute(
            select(order_submissions.c.updated_at).where(order_submissions.c.submission_id == sub_id)
        ).scalar_one()
    engine.dispose()
    assert stamp > later


# ---------------------------------------------------------------------------
# round 7 item 1 — unstamped stock rows are adoptable
# ---------------------------------------------------------------------------


def test_unstamped_stock_row_is_adopted(monkeypatch, client, broker_calls, fresh_quote):
    """Fable delta-4 P2: the place leg of a stock order has strike/right/exp
    None, the IB snapshot leg has strike 0.0 / right "" (ib_async Stock), and
    the place leg has no con_id. They are the same order."""
    from xenon.execution import coverage

    stock_legs = gate4.legs_payload_for(
        legs=[coverage.Leg(action="BUY", sym="SPY", sec="STK", ratio=1)], guaranteed=False
    )
    sub_id = _unstamped_uuid_row(990340, legs=stock_legs)
    _committed_sql(
        "UPDATE xenon.order_submissions SET security_type='STK', strike=NULL, \"right\"=NULL, expiry=NULL,"
        " con_id=NULL, multiplier=1 WHERE submission_id=:s",
        s=sub_id,
    )
    fake = _shares_client(200)
    t = _trade(_stk(), order_id=990340 - 900000, perm_id=990340)
    t.order.clientId = 26
    fake.open_orders.append(t)

    _run_sync(monkeypatch, fake)

    assert not _has_snapshot_row(990340)
    assert _row(sub_id)["perm_id"] == "990340"


def test_client_id_zero_row_is_adopted_and_perm_id_backfilled(monkeypatch, client, broker_calls, fresh_quote):
    """PR-08 §2.6: (scope, ib_order_id, placing_client_id) matching treats
    clientId 0 as a real client — the UUID row gets the permId, no
    snapshot row is inserted."""
    sub_id = _unstamped_uuid_row(990341, placing_client_id=0)
    _sync_one_sell_call(monkeypatch, 990341, client_id=0)

    assert not _has_snapshot_row(990341)
    assert _row(sub_id)["perm_id"] == "990341"


def test_client_id_zero_does_not_adopt_another_clients_row(monkeypatch, client, broker_calls, fresh_quote):
    sub_id = _unstamped_uuid_row(990342, placing_client_id=26)
    _sync_one_sell_call(monkeypatch, 990342, client_id=0)

    assert _has_snapshot_row(990342)
    assert _row(sub_id)["perm_id"] is None


# ---------------------------------------------------------------------------
# round 7 item 2 — one canonical TIF before the reservation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "canon"), [(None, "DAY"), ("", "DAY"), ("  day ", "DAY"), (" gtc\t", "GTC")])
def test_place_tif_is_canonicalized_once(raw, canon, client, broker_calls, fresh_quote):
    engine = _pg_engine()
    _route_seed_snapshot(engine, [_route_stock("SPY", 200)])
    engine.dispose()
    body = {**_sell_call_body("r7-tif"), "tif": raw}

    assert client.post("/orders/place", json=body).status_code == 200
    assert _submission_rows()[0]._mapping["tif"] == canon
    sent = json.loads(broker_calls[0]["args"][broker_calls[0]["args"].index("--json") + 1])
    assert sent["tif"] == canon
    # Any spelling of the same TIF replays as a duplicate; the other TIF does not.
    assert client.post("/orders/place", json={**body, "tif": canon.lower()}).status_code == 200
    other = "GTC" if canon == "DAY" else "DAY"
    assert client.post("/orders/place", json={**body, "tif": other}).status_code == 409
    assert len(broker_calls) == 1


@pytest.mark.parametrize("raw", ["IOC", "XYZ", "G T C", 7])
def test_place_unknown_tif_rejected_before_reservation(raw, client, broker_calls, fresh_quote):
    engine = _pg_engine()
    _route_seed_snapshot(engine, [_route_stock("SPY", 200)])
    engine.dispose()

    resp = client.post("/orders/place", json={**_sell_call_body("r7-tif-bad"), "tif": raw})

    assert resp.status_code == 400 and resp.json()["reason_code"] == "INVALID_ORDER_BODY", resp.text
    assert _submission_rows() == []
    assert broker_calls == []
