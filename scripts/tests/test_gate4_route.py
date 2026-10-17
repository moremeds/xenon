"""Gate-4 route tests (PR-03b item 6).

These drive the REAL /orders/place control flow — universe precheck,
advisory-lock evaluate+reserve, quote gate, idempotent replay — with ONLY
the broker subprocess stubbed (`_run_ib_script_with_recovery`). The
test-mode shortcut is disabled so the subprocess stub is the only fake:
assertions cover broker-call counts AND the durable order_submissions row.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.committed_db

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from xenon._test_db import sync_test_db_url as _sync_url
from xenon.api import server as server_mod
from xenon.api.subprocess import ScriptResult
from xenon.db.schema import account_snapshots, order_submissions

SCOPE = {"broker": "IB", "account_env": "paper", "broker_account": "DU0000000"}
NOW = datetime.now(timezone.utc)


def _pg_engine():
    return create_engine(_sync_url(), pool_pre_ping=True)


def _truncate(engine):
    with engine.begin() as conn:
        for table in ("order_fills", "order_events", "order_submissions", "account_snapshots"):
            conn.execute(text(f"TRUNCATE xenon.{table} CASCADE"))


def _seed_snapshot(engine, positions, *, complete=True):
    at = datetime.now(timezone.utc)
    payload = {
        "positions": positions,
        "available_funds": 0,
        "observation": {
            "as_of": at.isoformat(),
            "complete": complete,
            "account": "DU0000000",
            "reads": {"positions": True, "open_orders": True, "executions": True},
        },
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO xenon.account_snapshots
                  (account, bankroll, payload, snapshot_at, broker, account_env, broker_account)
                VALUES ('DU0000000', 0, CAST(:p AS jsonb), :at, 'IB', 'paper', 'DU0000000')
                """
            ),
            {"p": json.dumps(payload), "at": at},
        )


def _stock(sym, qty):
    return {
        "ticker": sym,
        "expiry": None,
        "legs": [
            {
                "direction": "LONG" if qty >= 0 else "SHORT",
                "type": "Stock",
                "contracts": abs(qty),
                "strike": 0,
            }
        ],
    }


@pytest.fixture(autouse=True)
def _pg(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", _sync_url())
    import xenon.db.engine as eng_mod

    monkeypatch.setattr(eng_mod, "_sync_engine", None)
    engine = _pg_engine()
    _truncate(engine)
    engine.dispose()
    yield
    engine = _pg_engine()
    _truncate(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def _test_mode_off(monkeypatch):
    # The real control flow — no test-mode shortcut; the broker subprocess
    # stub below is the ONLY fake in the path.
    monkeypatch.delenv("XENON_API_TEST_MODE", raising=False)
    yield


@pytest.fixture
def broker_calls(monkeypatch):
    calls = []

    async def fake(entry: str, args: list, timeout: float = 30):
        calls.append({"entry": entry, "args": args})
        return ScriptResult(
            ok=True,
            data={"status": "ok", "orderId": 4242, "permId": 777001, "clientId": 26},
        )

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake)
    return calls


@pytest.fixture
def fresh_quote(monkeypatch):
    # Non-combo orders pass the F3 quote gate: market hours + a live quote.
    _midday = datetime(2026, 4, 22, 13, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(server_mod, "_now", lambda: _midday, raising=False)

    async def _q(ticker: str, con_id: int):
        return {"bid": 4.90, "ask": 5.10, "bid_size": 100, "ask_size": 100}

    monkeypatch.setattr(server_mod, "_fetch_quote_snapshot", _q)


@pytest.fixture
def client():
    return TestClient(server_mod.app)


def _sell_call_body(attempt, qty=1):
    return {
        "type": "option",
        "symbol": "SPY",
        "action": "SELL",
        "quantity": qty,
        "right": "C",
        "expiry": "2026-12-18",
        "strike": 500.0,
        "con_id": 111,
        "limitPrice": 5.0,
        "client_attempt_id": attempt,
    }


def _submission_rows():
    engine = _pg_engine()
    with engine.connect() as conn:
        rows = conn.execute(select(order_submissions)).all()
    engine.dispose()
    return rows


def test_covered_sell_call_reaches_broker_and_persists_legs(client, broker_calls, fresh_quote):
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 200)])
    engine.dispose()

    resp = client.post("/orders/place", json=_sell_call_body("rt-covered-1"))

    assert resp.status_code == 200, resp.text
    assert len(broker_calls) == 1
    assert broker_calls[0]["entry"] == "xenon-ib-place-order"
    rows = _submission_rows()
    assert len(rows) == 1
    row = rows[0]._mapping
    assert row["client_attempt_id"] == "rt-covered-1"
    assert row["security_type"] == "OPT"
    assert row["broker_account"] == "DU0000000"
    assert row["ib_order_id"] == "4242"
    assert row["perm_id"] == "777001"
    legs = row["legs"]
    assert legs["legs"][0]["right"] == "C"
    assert legs["legs"][0]["sec"] == "OPT"


def test_naked_sell_call_blocked_before_broker(client, broker_calls, fresh_quote):
    engine = _pg_engine()
    _seed_snapshot(engine, [])
    engine.dispose()

    resp = client.post("/orders/place", json=_sell_call_body("rt-naked-1"))

    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "ETF_CALL_UNCOVERED"
    assert broker_calls == []
    assert _submission_rows() == []


def test_incomplete_view_blocks_before_broker(client, broker_calls, fresh_quote):
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 100)], complete=False)
    engine.dispose()

    resp = client.post("/orders/place", json=_sell_call_body("rt-incomplete-1"))

    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "PORTFOLIO_SNAPSHOT_REQUIRED"
    assert broker_calls == []
    assert _submission_rows() == []


def test_sell_envelope_combo_evaluated_and_blocked(client, broker_calls):
    """SELL-envelope combos are NOT exempt — legs flip and demand is real."""
    engine = _pg_engine()
    _seed_snapshot(engine, [])
    engine.dispose()

    resp = client.post(
        "/orders/place",
        json={
            "type": "combo",
            "symbol": "SPY",
            "action": "SELL",
            "quantity": 1,
            "limitPrice": 1.0,
            "client_attempt_id": "rt-sellenv-1",
            "legs": [
                {"action": "BUY", "right": "C", "expiry": "2026-12-18", "strike": 500.0, "ratio": 1},
                {"action": "SELL", "right": "C", "expiry": "2026-12-18", "strike": 510.0, "ratio": 1},
            ],
        },
    )

    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "ETF_CALL_UNCOVERED"
    assert broker_calls == []


def test_guaranteed_bag_mode_reaches_broker_payload(client, broker_calls):
    """nonGuaranteed=0 persists legs.guaranteed AND reaches the broker JSON."""
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 100)])
    engine.dispose()

    resp = client.post(
        "/orders/place",
        json={
            "type": "combo",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 2.0,
            "nonGuaranteed": 0,
            "client_attempt_id": "rt-bag-1",
            "legs": [
                {"action": "BUY", "right": "C", "expiry": "2026-12-18", "strike": 500.0, "ratio": 1},
                {"action": "SELL", "right": "C", "expiry": "2026-12-18", "strike": 510.0, "ratio": 1},
            ],
        },
    )

    assert resp.status_code == 200, resp.text
    assert len(broker_calls) == 1
    broker_body = json.loads(broker_calls[0]["args"][1])
    assert broker_body["nonGuaranteed"] == 0
    row = _submission_rows()[0]._mapping
    assert row["legs"]["guaranteed"] is True
    assert {leg["right"] for leg in row["legs"]["legs"]} == {"C"}


def test_replay_returns_duplicate_without_second_broker_call(client, broker_calls, fresh_quote):
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 200)])
    engine.dispose()

    first = client.post("/orders/place", json=_sell_call_body("rt-replay-1"))
    second = client.post("/orders/place", json=_sell_call_body("rt-replay-1"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json().get("duplicate_of") or second.json().get("submission_id")
    assert len(broker_calls) == 1
    assert len(_submission_rows()) == 1


def test_body_mismatch_same_attempt_409(client, broker_calls, fresh_quote):
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 200)])
    engine.dispose()

    first = client.post("/orders/place", json=_sell_call_body("rt-mm-1"))
    second = client.post("/orders/place", json=_sell_call_body("rt-mm-1", qty=2))

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["reason_code"] == "ATTEMPT_BODY_MISMATCH"
    assert len(broker_calls) == 1
    assert len(_submission_rows()) == 1


def test_second_sell_after_reservation_exhausts_cover(client, broker_calls, fresh_quote):
    """The first reservation consumes cover durably — a second identical
    SELL on a new attempt id is uncovered."""
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 100)])
    engine.dispose()

    first = client.post("/orders/place", json=_sell_call_body("rt-alloc-1"))
    second = client.post("/orders/place", json=_sell_call_body("rt-alloc-2"))

    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["reason_code"] == "ETF_CALL_UNCOVERED"
    assert len(broker_calls) == 1
    assert len(_submission_rows()) == 1


# ---------------------------------------------------------------------------
# /orders/modify — Gate 4 quantity reserve + persisted guarantee mode
# ---------------------------------------------------------------------------


def _guaranteed_bag_body(attempt):
    return {
        "type": "combo",
        "symbol": "SPY",
        "action": "BUY",
        "quantity": 1,
        "limitPrice": 2.0,
        "nonGuaranteed": 0,
        "client_attempt_id": attempt,
        "legs": [
            {"action": "BUY", "right": "C", "expiry": "2026-12-18", "strike": 500.0, "ratio": 1},
            {"action": "SELL", "right": "C", "expiry": "2026-12-18", "strike": 510.0, "ratio": 1},
        ],
    }


def _place_bag(client, attempt, *, guaranteed=True, shares=100):
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", shares)])
    engine.dispose()
    body = _guaranteed_bag_body(attempt)
    if not guaranteed:
        body["nonGuaranteed"] = 1
    resp = client.post("/orders/place", json=body)
    assert resp.status_code == 200, resp.text
    return resp


def test_modify_bag_sends_persisted_guarantee_mode(client, broker_calls):
    """place→modify keeps the stored mode in the broker args: a BAG placed
    guaranteed (NonGuaranteed=0) modifies with --non-guaranteed 0 — the
    client cannot flip the persisted mode."""
    _place_bag(client, "rt-mod-bag-1")
    assert len(broker_calls) == 1

    resp = client.post(
        "/orders/modify",
        json={"orderId": 4242, "permId": 777001, "newPrice": 1.90, "modifySequence": 1},
    )

    assert resp.status_code == 200, resp.text
    assert len(broker_calls) == 2
    manage = broker_calls[1]
    assert manage["entry"] == "xenon-ib-order-manage"
    args = manage["args"]
    assert "--non-guaranteed" in args
    assert args[args.index("--non-guaranteed") + 1] == "0"


def test_modify_rejects_guarantee_mode_change(client, broker_calls):
    _place_bag(client, "rt-mod-flip-1")

    resp = client.post(
        "/orders/modify",
        json={
            "orderId": 4242,
            "permId": 777001,
            "newPrice": 1.90,
            "nonGuaranteed": 1,
            "modifySequence": 1,
        },
    )

    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "MODIFY_GUARANTEE_MODE"
    # The broker never saw a modify.
    assert len(broker_calls) == 1


def test_modify_quantity_reserves_cover(client, broker_calls):
    """A qty-up modify that exhausts cover is blocked before the broker."""
    _place_bag(client, "rt-mod-qty-1", shares=100)

    # The BAG's SELL leg consumes 100 shares; bumping qty to 2 needs 200.
    resp = client.post(
        "/orders/modify",
        json={
            "orderId": 4242,
            "permId": 777001,
            "newQuantity": 2,
            "modifySequence": 1,
        },
    )

    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "ETF_CALL_UNCOVERED"
    assert len(broker_calls) == 1


def test_modify_uncertain_keeps_max_reservation(client, broker_calls, monkeypatch):
    """A broker outcome with no provable disposition keeps pending_amend_qty
    — the max(old,new) reservation must not release cover silently."""
    _place_bag(client, "rt-mod-unc-1", shares=250)

    async def flaky(entry: str, args: list, timeout: float = 30):
        broker_calls.append({"entry": entry, "args": args})
        return ScriptResult(ok=False, error="gateway dropped mid-ack")

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", flaky)

    resp = client.post(
        "/orders/modify",
        json={
            "orderId": 4242,
            "permId": 777001,
            "newQuantity": 2,
            "modifySequence": 1,
        },
    )

    assert resp.status_code == 503
    row = _submission_rows()[0]._mapping
    assert row["legs"]["pending_amend_qty"] == 2


def test_modify_rejected_clears_pending(client, broker_calls, monkeypatch):
    """A classified ib_reject is a broker-side NO — release the pending
    reservation (provably unapplied)."""
    _place_bag(client, "rt-mod-rej-1", shares=250)

    async def rejected(entry: str, args: list, timeout: float = 30):
        broker_calls.append({"entry": entry, "args": args})
        return ScriptResult(
            ok=True,
            data={
                "status": "error",
                "classification": "ib_reject",
                "message": "IB rejected",
                "upstream": {"code": "201"},
            },
        )

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", rejected)

    resp = client.post(
        "/orders/modify",
        json={
            "orderId": 4242,
            "permId": 777001,
            "newQuantity": 2,
            "modifySequence": 1,
        },
    )

    assert resp.status_code == 400
    row = _submission_rows()[0]._mapping
    assert row["legs"]["pending_amend_qty"] is None
