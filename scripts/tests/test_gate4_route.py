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

from xenon._test_db import committed_pass_stamp

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
            # xid8 ordering key + cluster id from a committed txn, as ib_sync stamps them.
            **committed_pass_stamp(),
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


ACCOUNT = "DU0000000"


def _stock(sym, qty, *, account=ACCOUNT):
    return {
        "ticker": sym,
        "expiry": None,
        "legs": [
            {
                "direction": "LONG" if qty >= 0 else "SHORT",
                "type": "Stock",
                "contracts": abs(qty),
                "strike": 0,
                "account": account,
            }
        ],
    }


def _opt_pos(sym, qty, *, strike, right="Call", expiry="2026-12-18", tc=None, mult=100, und_sec="STK"):
    # Shape ib_sync writes: raw IB account + IB-qualified option metadata.
    return {
        "ticker": sym,
        "expiry": expiry,
        "legs": [
            {
                "direction": "LONG" if qty >= 0 else "SHORT",
                "type": right,
                "contracts": abs(qty),
                "strike": strike,
                "account": ACCOUNT,
                "tc": tc or sym,
                "mult": mult,
                "und_sec": und_sec,
            }
        ],
    }


# ---------------------------------------------------------------------------
# IB data-role double: the place route qualifies every evaluated option leg
# via reqContractDetails (contract_meta). This fake pool runs the REAL
# resolver (_resolve_leg_meta_with_client) against a ContractDetails table.
# ---------------------------------------------------------------------------


def _details(sym, exp, strike, right, *, con_id, tc=None, mult="100", und_sec="STK"):
    from ib_async import Contract, ContractDetails

    return ContractDetails(
        contract=Contract(
            conId=con_id,
            symbol=sym,
            secType="OPT",
            lastTradeDateOrContractMonth=exp,
            strike=float(strike),
            right=right,
            multiplier=mult,
            tradingClass=tc or sym,
            exchange="SMART",
            currency="USD",
        ),
        underSecType=und_sec,
    )


class _FakeDataClient:
    """get_contract_details by conId or by (symbol, expiry, strike, right)."""

    def __init__(self):
        self.by_con_id = {}
        self.by_fields = {}
        self.calls = 0

    def add(self, details):
        c = details.contract
        self.by_con_id[c.conId] = details
        self.by_fields[(c.symbol, c.lastTradeDateOrContractMonth, float(c.strike), c.right)] = details

    def get_contract_details(self, contract):
        self.calls += 1
        if getattr(contract, "conId", 0):
            d = self.by_con_id.get(contract.conId)
            return [d] if d else []
        key = (contract.symbol, contract.lastTradeDateOrContractMonth, float(contract.strike), contract.right)
        d = self.by_fields.get(key)
        return [d] if d else []


class _FakePool:
    def __init__(self, client):
        self.client = client

    def acquire(self, role):
        client = self.client

        class _Ctx:
            async def __aenter__(self):
                return client

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def run_sync(self, role, fn, *args):
        return fn(*args)


@pytest.fixture(autouse=True)
def ib_data(monkeypatch):
    client = _FakeDataClient()
    for strike in (500.0, 510.0, 520.0):
        for right in ("C", "P"):
            con_id = 111 if (strike, right) == (500.0, "C") else int(strike) * 10 + (1 if right == "C" else 2)
            client.add(_details("SPY", "20261218", strike, right, con_id=con_id))
    monkeypatch.setattr(server_mod, "ib_pool", _FakePool(client))
    return client


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
    """SELL-envelope combos are NOT exempt — legs flip and demand is real.
    The flipped 2x1 ratio nets to one short call even under netting, so it
    keeps the uncovered code (never the split hint)."""
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
                {"action": "BUY", "right": "C", "expiry": "2026-12-18", "strike": 500.0, "ratio": 2},
                {"action": "SELL", "right": "C", "expiry": "2026-12-18", "strike": 510.0, "ratio": 1},
            ],
        },
    )

    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "ETF_CALL_UNCOVERED"
    assert broker_calls == []


def test_combo_split_required_reason(client, broker_calls):
    """A guaranteed BAG that only fails leg-by-leg gets the split-required
    code at top level — send the BUY leg as its own order first. Zero broker
    calls; nothing reaches IB."""
    engine = _pg_engine()
    _seed_snapshot(engine, [_opt_pos("SPY", 1, strike=500), _opt_pos("SPY", -1, strike=510)])
    engine.dispose()

    resp = client.post(
        "/orders/place",
        json={
            "type": "combo",
            "symbol": "SPY",
            "action": "SELL",
            "quantity": 1,
            "limitPrice": 1.0,
            "nonGuaranteed": 0,
            "client_attempt_id": "rt-vsplit-1",
            "legs": [
                {"action": "BUY", "right": "C", "expiry": "2026-12-18", "strike": 500.0, "ratio": 1},
                {"action": "SELL", "right": "C", "expiry": "2026-12-18", "strike": 510.0, "ratio": 1},
            ],
        },
    )

    assert resp.status_code == 400
    body = resp.json()
    assert body["reason_code"] == "COMBO_SPLIT_REQUIRED"
    assert "BUY leg as its own order first" in body["detail"]
    assert broker_calls == []
    assert _submission_rows() == []


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


@pytest.mark.parametrize(
    "data, second_status",
    [
        # #200 UNKNOWN path: IB may hold the order, so its Gate-4 demand stays.
        (
            {
                "status": "error",
                "code": 201,
                "message": "x",
                "orderId": 4244,
                "permId": 777003,
                "initialStatus": "PreSubmitted",
            },
            400,
        ),
        # Explicit rejection releases the reservation.
        ({"status": "error", "outcome": "rejected", "code": 201, "message": "x"}, 200),
    ],
    ids=["unknown-keeps-demand", "rejected-releases"],
)
def test_place_outcome_vs_gate4_reservation(data, second_status, monkeypatch, client, broker_calls, fresh_quote):
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 100)])
    engine.dispose()
    results = [ScriptResult(ok=True, data=data)]

    async def fake(entry: str, args: list, timeout: float = 30):
        broker_calls.append({"entry": entry, "args": args})
        if results:
            return results.pop(0)
        return ScriptResult(ok=True, data={"status": "ok", "orderId": 4245, "permId": 777004, "clientId": 26})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake)

    first = client.post("/orders/place", json=_sell_call_body("rt-outcome-1"))
    assert first.status_code == 502, first.text
    second = client.post("/orders/place", json=_sell_call_body("rt-outcome-2"))
    assert second.status_code == second_status, second.text
    if second_status == 400:
        assert second.json()["reason_code"] == "ETF_CALL_UNCOVERED"
        assert first.json()["reason_code"] == "PLACE_OUTCOME_UNKNOWN"


# PR-07 (OP-1): a place subprocess that may have reached IB never ends FAILED.
@pytest.mark.parametrize(
    "script_result",
    [
        ScriptResult(ok=False, error="Entry point timed out after 30s"),
        ScriptResult(ok=False, error="Entry point exited with code -9", exit_code=-9),
        ScriptResult(ok=True, data={}),
        ScriptResult(
            ok=True,
            data={"status": "uncertain", "outcome": "uncertain", "orderId": 4246, "permId": 0, "clientId": 31},
        ),
    ],
    ids=["timeout", "crash", "empty-stdout", "crash-after-place"],
)
def test_uncertain_place_is_unknown_502_and_keeps_reservation(
    script_result, monkeypatch, client, broker_calls, fresh_quote
):
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 100)])
    engine.dispose()
    results = [script_result]

    async def fake(entry: str, args: list, timeout: float = 30):
        broker_calls.append({"entry": entry, "args": args, "timeout": timeout})
        if results:
            return results.pop(0)
        return ScriptResult(ok=True, data={"status": "ok", "orderId": 4247, "permId": 777005, "clientId": 26})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake)

    first = client.post("/orders/place", json=_sell_call_body("rt-unc-1"))
    assert first.status_code == 502, first.text
    assert first.json()["reason_code"] == "PLACE_OUTCOME_UNKNOWN"
    row = _submission_rows()[0]._mapping
    assert row["state"] == "UNKNOWN"
    assert row["reason_code"] == "PLACE_OUTCOME_UNKNOWN"
    # The CLI carries orderRef "xo:<submission_id>" and a budget > connect+qualify+wait.
    sent = json.loads(broker_calls[0]["args"][1])
    assert sent["submission_id"] == row["submission_id"]
    assert broker_calls[0]["timeout"] >= 30
    # Reservation kept: the 100 shares stay committed to the unknown call.
    second = client.post("/orders/place", json=_sell_call_body("rt-unc-2"))
    assert second.status_code == 400, second.text
    assert second.json()["reason_code"] == "ETF_CALL_UNCOVERED"
    # Same attempt id again never reaches the broker a second time, and it
    # answers the uncertain 502 — never a 200 the client reads as success.
    again = client.post("/orders/place", json=_sell_call_body("rt-unc-1"))
    assert again.status_code == 502, again.text
    assert (again.json()["reason_code"], again.json()["state"]) == ("PLACE_OUTCOME_UNKNOWN", "UNKNOWN")
    assert len(broker_calls) == 1


def test_refused_before_spawn_is_failed_and_releases(monkeypatch, client, broker_calls, fresh_quote):
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 100)])
    engine.dispose()

    async def refused(entry: str, args: list, timeout: float = 30):
        broker_calls.append({"entry": entry})
        return ScriptResult(ok=False, error="IB Gateway is not accepting connections.", spawned=False)

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", refused)
    resp = client.post("/orders/place", json=_sell_call_body("rt-refused-1"))
    assert resp.status_code == 502
    assert resp.json()["reason_code"] == "SUBPROCESS_ERROR"
    row = _submission_rows()[0]._mapping
    assert (row["state"], row["reason_code"]) == ("FAILED", "SUBPROCESS_ERROR")


def _bind_and_fill_like_poller(args: list) -> str:
    """What the activity poller does while the route still awaits the CLI:
    bind the row by orderRef from an execution, then mark it FILLED."""
    from xenon.api.services import ib_activity_mirror
    from xenon.execution import orders_store
    from xenon.execution.account_scope import AccountScope

    sid = json.loads(args[1])["submission_id"]
    scope = AccountScope(**SCOPE)
    execution = {
        "order_ref": orders_store.order_ref_for(sid),
        "perm_id": 777100,
        "ib_order_id": 4300,
        "client_id": 31,
        "account": "DU0000000",
    }
    assert ib_activity_mirror.recover_by_order_ref([], [execution], scope=scope)["recovered"] == 1
    orders_store.mark_terminal(
        submission_id=sid,
        state="FILLED",
        reason_code=None,
        filled_qty=1,
        avg_fill_price=None,
        expected_states=("WORKING",),
    )
    return sid


@pytest.mark.parametrize(
    "late_result",
    [
        ScriptResult(ok=False, error="Entry point timed out after 30s"),
        ScriptResult(ok=True, data={"status": "ok", "outcome": "accepted", "orderId": 4300, "permId": 777100}),
        ScriptResult(ok=True, data={"status": "error", "outcome": "rejected", "code": 201, "message": "x"}),
    ],
    ids=["late-timeout", "late-ack", "late-reject"],
)
def test_late_route_outcome_never_demotes_poller_fill(late_result, monkeypatch, client, fresh_quote):
    """Interleaving: the poller binds and fills the row while the CLI is still
    awaited; the route's late outcome must leave FILLED / filled_qty intact."""
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 100)])
    engine.dispose()

    async def fake(entry: str, args: list, timeout: float = 30):
        _bind_and_fill_like_poller(args)
        return late_result

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake)
    resp = client.post("/orders/place", json=_sell_call_body("rt-race-1"))
    # Never "rejected" for an order the broker shows filled.
    assert resp.json().get("reason_code") != "IB_REJECT", resp.text
    row = _submission_rows()[0]._mapping
    assert (row["state"], row["filled_qty"], row["perm_id"]) == ("FILLED", 1, "777100")


@pytest.mark.parametrize("kind", ["missing-binary", "exec-oserror"])
def test_spawn_failure_is_not_sent_failed_and_releases(kind, tmp_path, monkeypatch, client, fresh_quote):
    """The process never started → spawned=False → FAILED (not UNKNOWN),
    explicit SUBPROCESS_ERROR, reservation released."""
    import xenon.api.subprocess as sub_mod

    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 100)])
    engine.dispose()
    monkeypatch.setattr(sub_mod, "VENV_BIN", tmp_path)
    if kind == "exec-oserror":
        (tmp_path / "xenon-ib-place-order").write_text("not executable")  # no +x → PermissionError
    monkeypatch.setattr(server_mod, "_pool_has_any_connection", lambda: True)
    monkeypatch.setitem(server_mod._ib_last_failure, "order", 0.0)

    resp = client.post("/orders/place", json=_sell_call_body("rt-spawn-1"))
    assert resp.status_code == 502, resp.text
    assert resp.json()["reason_code"] == "SUBPROCESS_ERROR"
    row = _submission_rows()[0]._mapping
    assert (row["state"], row["reason_code"]) == ("FAILED", "SUBPROCESS_ERROR")


def test_body_order_ref_is_never_forwarded(client, broker_calls, fresh_quote):
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 100)])
    engine.dispose()
    body = {**_sell_call_body("rt-ref-1"), "orderRef": "leg_wizard:spoof"}
    resp = client.post("/orders/place", json=body)
    assert resp.status_code == 200, resp.text
    sent = json.loads(broker_calls[0]["args"][1])
    assert "orderRef" not in sent
    assert sent["submission_id"] == _submission_rows()[0]._mapping["submission_id"]


def test_db_failure_after_ack_returns_200_with_persist_warning(monkeypatch, client, broker_calls, fresh_quote):
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 100)])
    engine.dispose()

    def db_down(**_kwargs):
        raise RuntimeError("connection to server at 127.0.0.1 failed")

    monkeypatch.setattr(server_mod.orders_store, "mark_submitted", db_down)
    resp = client.post("/orders/place", json=_sell_call_body("rt-persist-1"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["orderId"], body["permId"]) == (4242, 777001)
    assert body["persist_warning"]
    assert body["submission_id"]


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


def _manage_reply(monkeypatch, broker_calls, *, price, qty, before=None):
    """Modify CLI fake: the broker confirms `price` / `qty` (may differ from
    the request within the CLI tolerance). `before` runs first, inside the
    subprocess window (e.g. an activity-mirror pass)."""

    async def fake(entry: str, args: list, timeout: float = 30):
        broker_calls.append({"entry": entry, "args": args})
        if before is not None:
            before()
        return ScriptResult(
            ok=True,
            data={
                "status": "ok",
                "orderId": 4242,
                "newPrice": price,
                "confirmedPrice": price,
                "confirmedQuantity": qty,
            },
        )

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake)


def test_modify_success_persists_broker_confirmed_price_and_quantity(client, broker_calls, monkeypatch):
    """PR-08: a confirmed modify stores what the broker holds, not the
    request. Request 1.9005 confirms against broker 1.9000 (<0.001
    tolerance); the row gets 1.9000."""
    _place_bag(client, "rt-mod-persist-1", shares=250)

    _manage_reply(monkeypatch, broker_calls, price=1.9000, qty=1)
    resp = client.post(
        "/orders/modify",
        json={"orderId": 4242, "permId": 777001, "newPrice": 1.9005, "modifySequence": 1},
    )
    assert resp.status_code == 200, resp.text
    row = _submission_rows()[0]._mapping
    assert float(row["limit_price"]) == 1.9
    assert row["quantity"] == 1

    _manage_reply(monkeypatch, broker_calls, price=1.75, qty=2)
    resp = client.post(
        "/orders/modify",
        json={"orderId": 4242, "permId": 777001, "newPrice": 1.7502, "newQuantity": 2, "modifySequence": 2},
    )
    assert resp.status_code == 200, resp.text
    row = _submission_rows()[0]._mapping
    assert float(row["limit_price"]) == 1.75
    assert row["quantity"] == 2
    assert row["legs"].get("pending_amend_id") is None


def test_modify_mirror_before_reply_keeps_price(client, broker_calls, monkeypatch):
    """PR-08 interleaving: a fresh activity-mirror read sees the amended
    order (qty 2, 1.75) before the CLI reply lands. The mirror confirms the
    amend and clears its token, so the route settlement is a no-op; the
    price must still reach the row (from the fresh observation)."""
    from xenon.execution import orders_store

    _place_bag(client, "rt-mod-race-1", shares=250)
    mirror_out = {}

    def mirror_pass():
        mirror_out.update(
            orders_store.register_from_snapshot(
                perm_id="777001",
                ib_order_id="4242",
                ticker="SPY",
                security_type="BAG",
                action="BUY",
                quantity=2,
                limit_price=1.75,
                multiplier=100,
                observed_at=datetime.now(timezone.utc),
                **SCOPE,
            )
        )

    _manage_reply(monkeypatch, broker_calls, price=1.75, qty=2, before=mirror_pass)
    resp = client.post(
        "/orders/modify",
        json={"orderId": 4242, "permId": 777001, "newPrice": 1.75, "newQuantity": 2, "modifySequence": 1},
    )
    assert resp.status_code == 200, resp.text
    assert mirror_out.get("reconciled") is True  # the mirror cleared the token first
    row = _submission_rows()[0]._mapping
    assert row["quantity"] == 2
    assert float(row["limit_price"]) == 1.75
    assert row["legs"].get("pending_amend_id") is None


def test_stale_mirror_read_never_overwrites_settled_price(client, broker_calls, monkeypatch):
    """The freshness fence holds: a read that began before the settlement
    (it saw the pre-modify price) does not undo the confirmed price."""
    from xenon.execution import orders_store

    _place_bag(client, "rt-mod-stale-1", shares=250)
    read_started = datetime.now(timezone.utc)
    _manage_reply(monkeypatch, broker_calls, price=1.9, qty=1)
    assert (
        client.post(
            "/orders/modify",
            json={"orderId": 4242, "permId": 777001, "newPrice": 1.9, "modifySequence": 1},
        ).status_code
        == 200
    )
    orders_store.register_from_snapshot(
        perm_id="777001",
        ib_order_id="4242",
        ticker="SPY",
        security_type="BAG",
        action="BUY",
        quantity=1,
        limit_price=1.5,
        multiplier=100,
        observed_at=read_started,
        **SCOPE,
    )
    assert float(_submission_rows()[0]._mapping["limit_price"]) == 1.9


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
    """A qty-up modify that exhausts cover is blocked before the broker.

    The stored mode is guaranteed, so under netting the doubled legs would
    still net internally — the reject carries the split-required code, not
    the generic uncovered one."""
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
    assert resp.json()["reason_code"] == "COMBO_SPLIT_REQUIRED"
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


def _real_cli_modify_reply(*, reject_code=None, new_quantity=2):
    """Run the REAL `ib_order_manage.modify_order` against a fake IB and
    return the JSON it prints — the exact reply shape the route settles on.

    The fake's refreshed open orders never show the new quantity. With
    `reject_code`, IB emits that error for the order right after placeOrder
    (an explicit rejection); without it the amend is sent and simply never
    confirmed.
    """
    import contextlib
    import io
    from unittest.mock import MagicMock

    from xenon.execution import ib_order_manage as iom

    class _Event:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

        def __isub__(self, handler):
            if handler in self.handlers:
                self.handlers.remove(handler)
            return self

    def _trade(qty):
        t = MagicMock()
        t.order.orderId = 4242
        t.order.permId = 777001
        t.order.clientId = 0
        t.order.orderType = "LMT"
        t.order.lmtPrice = 2.0
        t.order.totalQuantity = qty
        t.order.outsideRth = False
        t.orderStatus.status = "Submitted"
        t.contract.secType = "BAG"
        return t

    client = MagicMock()
    client.ib.client.clientId = 0
    client.ib.errorEvent = _Event()
    client.get_open_orders.side_effect = [[_trade(1)]] + [[_trade(1)]] * 50

    def _place(contract, order):
        if reject_code is not None:
            for handler in list(client.ib.errorEvent.handlers):
                handler(4242, reject_code, "Order rejected - reason: amend refused")

    client.place_order.side_effect = _place
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        iom.modify_order(client, 4242, 777001, None, "127.0.0.1", 4002, new_quantity=new_quantity, non_guaranteed=0)
    return json.loads(buf.getvalue())


def _manage_returns(monkeypatch, broker_calls, reply):
    async def fake(entry: str, args: list, timeout: float = 30):
        broker_calls.append({"entry": entry, "args": args})
        if entry == "xenon-ib-order-manage":
            # run_entry_point hands back exit-1 JSON as ok=True + data.
            return ScriptResult(ok=True, data=reply)
        return ScriptResult(ok=True, data={"status": "ok", "orderId": 5151, "permId": 888001, "clientId": 26})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake)


def _modify_qty(client, qty, seq):
    return client.post(
        "/orders/modify",
        json={"orderId": 4242, "permId": 777001, "newQuantity": qty, "modifySequence": seq},
    )


def test_modify_unconfirmed_cli_reply_keeps_max_reservation(client, broker_calls, monkeypatch, fresh_quote):
    """Item 4: the real CLI reply for a sent-but-unconfirmed amend
    (classification ib_reject, amend_outcome unconfirmed) is NOT proof the
    amend failed — the route keeps max(old,new) and the next short on the
    released cover is blocked."""
    _place_bag(client, "rt-mod-unconf-1", shares=200)
    reply = _real_cli_modify_reply()
    assert reply["status"] == "error"
    assert reply["classification"] == "ib_reject"
    assert reply["amend_outcome"] == "unconfirmed"
    _manage_returns(monkeypatch, broker_calls, reply)

    resp = _modify_qty(client, 2, 1)

    assert resp.status_code == 400
    row = _submission_rows()[0]._mapping
    assert row["legs"]["pending_amend_qty"] == 2
    assert row["quantity"] == 1
    # max(1,2)=2 short-call units consume all 200 shares.
    nxt = client.post("/orders/place", json=_sell_call_body("rt-after-unconf-1"))
    assert nxt.status_code == 400
    assert nxt.json()["reason_code"] == "ETF_CALL_UNCOVERED"


def test_modify_explicit_ib_rejection_releases_pending(client, broker_calls, monkeypatch, fresh_quote):
    """Item 4: an explicit IB rejection of the amend (code 201 on the
    order) is provably unapplied — pending clears and cover is released."""
    _place_bag(client, "rt-mod-rej-1", shares=200)
    reply = _real_cli_modify_reply(reject_code=201)
    assert reply["amend_outcome"] == "rejected"
    _manage_returns(monkeypatch, broker_calls, reply)

    resp = _modify_qty(client, 2, 1)

    assert resp.status_code == 400
    row = _submission_rows()[0]._mapping
    assert row["legs"]["pending_amend_qty"] is None
    assert row["legs"]["pending_amend_id"] is None
    nxt = client.post("/orders/place", json=_sell_call_body("rt-after-rej-1"))
    assert nxt.status_code == 200, nxt.text


def test_modify_refused_while_amend_pending(client, broker_calls, monkeypatch, fresh_quote):
    """Item 5 (review counterexample 1): 200 shares, qty 1 → uncertain
    increase to 2 reserves 2. A later decrease to 1 must NOT overwrite the
    pending maximum: it is refused with MODIFY_PENDING before the broker,
    and another short stays blocked."""
    _place_bag(client, "rt-mod-pend-1", shares=200)
    _manage_returns(monkeypatch, broker_calls, _real_cli_modify_reply())
    assert _modify_qty(client, 2, 1).status_code == 400
    calls_before = len(broker_calls)

    resp = _modify_qty(client, 1, 2)

    assert resp.status_code == 409
    assert resp.json()["reason_code"] == "MODIFY_PENDING"
    assert len(broker_calls) == calls_before
    row = _submission_rows()[0]._mapping
    assert row["legs"]["pending_amend_qty"] == 2
    nxt = client.post("/orders/place", json=_sell_call_body("rt-after-pend-1"))
    assert nxt.status_code == 400


def test_cancel_keeps_filled_quantity(client, broker_calls, monkeypatch, fresh_quote):
    """Item 8: a user cancel of a partially filled SELL call keeps
    filled_qty — the executed contract still consumes cover until a newer
    snapshot shows it."""
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 200)])
    engine.dispose()
    assert client.post("/orders/place", json=_sell_call_body("rt-cx-1", qty=2)).status_code == 200
    engine = _pg_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE xenon.order_submissions SET state='PARTIALLY_FILLED', filled_qty=1 "
                "WHERE client_attempt_id='rt-cx-1'"
            )
        )
    engine.dispose()

    async def cancel_ok(entry: str, args: list, timeout: float = 30):
        broker_calls.append({"entry": entry, "args": args})
        return ScriptResult(ok=True, data={"status": "ok", "message": "Order cancelled"})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", cancel_ok)
    resp = client.post("/orders/cancel", json={"orderId": 4242, "permId": 777001})
    assert resp.status_code == 200, resp.text

    row = _submission_rows()[0]._mapping
    assert row["state"] == "CANCELLED"
    assert row["filled_qty"] == 1
    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", broker_calls_fake(broker_calls))
    # 200 shares − 1 executed short call (100) leaves 100: SELL 2 is uncovered.
    nxt = client.post("/orders/place", json=_sell_call_body("rt-cx-2", qty=2))
    assert nxt.status_code == 400
    assert nxt.json()["reason_code"] == "ETF_CALL_UNCOVERED"


def broker_calls_fake(calls):
    async def fake(entry: str, args: list, timeout: float = 30):
        calls.append({"entry": entry, "args": args})
        return ScriptResult(ok=True, data={"status": "ok", "orderId": 4343, "permId": 777002, "clientId": 26})

    return fake


def test_ui_vertical_close_without_mode_gets_split_code(client, broker_calls):
    """Item 9: the UI never sends nonGuaranteed. A held-vertical close that
    passes only as a guaranteed BAG under netting still gets
    COMBO_SPLIT_REQUIRED, not the generic uncovered code."""
    engine = _pg_engine()
    _seed_snapshot(engine, [_opt_pos("SPY", 1, strike=500), _opt_pos("SPY", -1, strike=510)])
    engine.dispose()

    resp = client.post(
        "/orders/place",
        json={
            "type": "combo",
            "symbol": "SPY",
            "action": "SELL",
            "quantity": 1,
            "limitPrice": 1.0,
            "client_attempt_id": "rt-ui-vclose-1",
            "legs": [
                {"action": "BUY", "right": "C", "expiry": "2026-12-18", "strike": 500.0, "ratio": 1},
                {"action": "SELL", "right": "C", "expiry": "2026-12-18", "strike": 510.0, "ratio": 1},
            ],
        },
    )

    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "COMBO_SPLIT_REQUIRED"
    assert broker_calls == []


def test_candidate_without_ib_qualification_fails_closed(client, broker_calls, fresh_quote, monkeypatch):
    """Item 3: no IB contract details for the candidate → GATE4_UNRESOLVED.
    The gate never substitutes tradingClass=symbol / multiplier=100."""
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 100)])
    engine.dispose()
    monkeypatch.setattr(server_mod, "ib_pool", None)

    resp = client.post("/orders/place", json=_sell_call_body("rt-nometa-1"))

    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "GATE4_UNRESOLVED"
    assert broker_calls == []
    assert _submission_rows() == []


def test_adjusted_candidate_not_covered_by_standard_long_call(client, broker_calls, fresh_quote, ib_data):
    """Item 3: the candidate's key comes from IB qualification. An adjusted
    call (tradingClass SPY1, multiplier 150) is not covered by a held
    standard SPY call of the same expiry."""
    ib_data.add(_details("SPY", "20261218", 500.0, "C", con_id=9001, tc="SPY1", mult="150"))
    engine = _pg_engine()
    _seed_snapshot(engine, [_opt_pos("SPY", 1, strike=490)])
    engine.dispose()
    body = dict(_sell_call_body("rt-adj-1"), con_id=9001)

    resp = client.post("/orders/place", json=body)

    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "ETF_CALL_UNCOVERED"
    assert broker_calls == []
    # Control: the standard contract IS covered by the same long call.
    ok = client.post("/orders/place", json=_sell_call_body("rt-adj-2"))
    assert ok.status_code == 200, ok.text
    legs = _submission_rows()[0]._mapping["legs"]["legs"][0]
    assert (legs["tc"], legs["mult"], legs["und_sec"], legs["con_id"]) == ("SPY", 100, "STK", 111)


def test_quantity_modify_of_resting_buy_order_is_accepted(client, broker_calls, fresh_quote):
    """Round 4 item 5 (Fable P1): a pure-BUY row owes no cover, so it is
    excluded from the demand list — but it must stay a valid modify target.
    A quantity modify of a resting BUY call reaches the broker and lands."""
    engine = _pg_engine()
    _seed_snapshot(engine, [])
    engine.dispose()
    body = dict(_sell_call_body("rt-buy-mod-1"), action="BUY")
    assert client.post("/orders/place", json=body).status_code == 200

    resp = _modify_qty(client, 2, 1)

    assert resp.status_code == 200, resp.text
    assert broker_calls[-1]["entry"] == "xenon-ib-order-manage"
    row = _submission_rows()[0]._mapping
    assert row["quantity"] == 2
    assert row["legs"]["pending_amend_id"] is None


# ---------------------------------------------------------------------------
# POST /orders/replace (PR-07 fix3): claim → cancel → place in ONE process.
# ---------------------------------------------------------------------------


class _Broker:
    """Subprocess double for cancel (xenon-ib-order-manage) + place."""

    def __init__(self):
        self.calls: list[dict] = []
        self.cancel_results: list[ScriptResult] = []
        self.place_results: list[ScriptResult] = []
        self.cancel_hook = None
        self._perm = 880000

    async def __call__(self, entry: str, args: list, timeout: float = 30):
        self.calls.append({"entry": entry, "args": args})
        if entry == "xenon-ib-order-manage":
            if self.cancel_hook is not None:
                await self.cancel_hook()
            if self.cancel_results:
                return self.cancel_results.pop(0)
            return ScriptResult(ok=True, data={"status": "ok", "message": "Cancelled"})
        if self.place_results:
            return self.place_results.pop(0)
        self._perm += 1
        return ScriptResult(
            ok=True, data={"status": "ok", "orderId": self._perm - 875000, "permId": self._perm, "clientId": 26}
        )

    def count(self, entry: str) -> int:
        return sum(1 for c in self.calls if c["entry"] == entry)

    @property
    def cancels(self) -> int:
        return self.count("xenon-ib-order-manage")

    @property
    def places(self) -> int:
        return self.count("xenon-ib-place-order")


@pytest.fixture
def broker(monkeypatch):
    b = _Broker()
    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", b)
    return b


def _combo(cid, legs=((500.0, "BUY", 1), (510.0, "SELL", 1)), qty=1):
    return {
        "type": "combo",
        "symbol": "SPY",
        "action": "BUY",
        "quantity": qty,
        "limitPrice": 2.0,
        "client_attempt_id": cid,
        "legs": [{"action": a, "right": "C", "expiry": "2026-12-18", "strike": k, "ratio": r} for k, a, r in legs],
    }


def _original(client, broker, cid="orig-1", shares=100, **combo_kw):
    """A working combo placed through /orders/place → its IB ids."""
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", shares)])
    engine.dispose()
    resp = client.post("/orders/place", json=_combo(cid, **combo_kw))
    assert resp.status_code == 200, resp.text
    broker.calls.clear()
    return {"orderId": resp.json()["orderId"], "permId": resp.json()["permId"]}


def _replace_body(target, cid, **combo_kw):
    return {**target, "replaceOrder": _combo(cid, **combo_kw)}


def _row(cid):
    return next(r._mapping for r in _submission_rows() if r.client_attempt_id == cid)


def _race(client, broker, first_body, second_body):
    """first holds its cancel open until second has returned."""
    import asyncio
    import threading

    started, release = threading.Event(), threading.Event()

    async def hold_first_cancel():
        if not started.is_set():
            started.set()
            for _ in range(200):
                if release.is_set():
                    break
                await asyncio.sleep(0.05)

    broker.cancel_hook = hold_first_cancel
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("first", client.post("/orders/replace", json=first_body)))
    t.start()
    assert started.wait(10)
    second = client.post("/orders/replace", json=second_body)
    release.set()
    t.join(20)
    return out["first"], second


def test_replace_happy_path_cancels_then_places_once(client, broker):
    target = _original(client, broker)
    resp = client.post(
        "/orders/replace", json=_replace_body(target, "rep-1", legs=((500.0, "BUY", 1), (520.0, "SELL", 1)))
    )
    assert resp.status_code == 200, resp.text
    assert [c["entry"] for c in broker.calls] == ["xenon-ib-order-manage", "xenon-ib-place-order"]
    assert "--perm-id" in broker.calls[0]["args"] and str(target["permId"]) in broker.calls[0]["args"]
    rep = _row("rep-1")
    assert rep["state"] == "WORKING"
    assert rep["legs"]["replaces"] == [
        {
            "perm_id": str(target["permId"]),
            "ib_order_id": str(target["orderId"]),
            "client_id": "26",
            "submission_id": _row("orig-1")["submission_id"],
        }
    ]
    assert json.loads(broker.calls[1]["args"][1])["submission_id"] == rep["submission_id"]
    assert _row("orig-1")["state"] == "CANCELLED"


def test_concurrent_same_id_one_cancel_loser_cancels_nothing(client, broker):
    target = _original(client, broker)
    first, second = _race(client, broker, _replace_body(target, "rep-same"), _replace_body(target, "rep-same"))
    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert second.json()["reason_code"] == "REPLACEMENT_IN_PROGRESS"
    assert (broker.cancels, broker.places) == (1, 1)


def test_concurrent_different_ids_same_target_one_wins(client, broker):
    target = _original(client, broker)
    first, second = _race(client, broker, _replace_body(target, "rep-tab-1"), _replace_body(target, "rep-tab-2"))
    assert first.status_code == 200, first.text
    assert (second.status_code, second.json()["reason_code"]) == (409, "REPLACEMENT_IN_PROGRESS")
    assert (broker.cancels, broker.places) == (1, 1)
    assert [r.client_attempt_id for r in _submission_rows() if r.client_attempt_id.startswith("rep-")] == ["rep-tab-1"]


def test_second_replacement_of_a_replaced_target_is_refused(client, broker):
    """Tab 2 arrives after tab 1 finished: the target is gone, its cancel
    would 'succeed' and place a duplicate — refused before any cancel."""
    target = _original(client, broker)
    assert client.post("/orders/replace", json=_replace_body(target, "rep-a")).status_code == 200
    broker.calls.clear()
    late = client.post("/orders/replace", json=_replace_body(target, "rep-b"))
    assert (late.status_code, late.json()["reason_code"]) == (409, "REPLACEMENT_IN_PROGRESS")
    assert broker.calls == []


def test_replace_cancel_fails_claim_failed_nothing_placed(client, broker):
    target = _original(client, broker)
    broker.cancel_results = [
        ScriptResult(ok=True, data={"status": "error", "classification": "ownership", "message": "not owner"})
    ]
    resp = client.post("/orders/replace", json=_replace_body(target, "rep-cf"))
    assert resp.status_code == 409, resp.text
    assert resp.json()["reason_code"] == "OWNERSHIP"
    assert resp.json()["replacement_placed"] is False
    assert (broker.cancels, broker.places) == (1, 0)
    assert (_row("rep-cf")["state"], _row("rep-cf")["reason_code"]) == ("FAILED", "REPLACE_CANCEL_FAILED")
    # A FAILED claim placed nothing: a fresh replacement of the target may run.
    assert client.post("/orders/replace", json=_replace_body(target, "rep-cf-2")).status_code == 200


def test_replace_cancel_uncertain_places_nothing(client, broker):
    target = _original(client, broker)
    broker.cancel_results = [ScriptResult(ok=False, error="timeout", spawned=True)]
    resp = client.post("/orders/replace", json=_replace_body(target, "rep-cu"))
    assert resp.status_code == 502, resp.text
    assert resp.json()["reason_code"] == "PLACE_OUTCOME_UNKNOWN"
    assert resp.json()["replacement_placed"] is False
    assert (broker.cancels, broker.places) == (1, 0)
    assert _row("rep-cu")["state"] == "UNKNOWN"
    # Unresolved: the target stays claimed.
    again = client.post("/orders/replace", json=_replace_body(target, "rep-cu-2"))
    assert (again.status_code, again.json()["reason_code"]) == (409, "REPLACEMENT_IN_PROGRESS")
    assert broker.cancels == 1


def test_replace_place_uncertain_after_cancel_is_unknown(client, broker):
    target = _original(client, broker)
    broker.place_results = [ScriptResult(ok=False, error="timeout", spawned=True)]
    resp = client.post("/orders/replace", json=_replace_body(target, "rep-pu"))
    assert resp.status_code == 502, resp.text
    assert resp.json()["reason_code"] == "PLACE_OUTCOME_UNKNOWN"
    assert "Do not resubmit" in resp.json()["error"]
    assert _row("rep-pu")["state"] == "UNKNOWN"


def test_replace_reused_attempt_id_refused_before_cancel(client, broker):
    target = _original(client, broker)
    resp = client.post("/orders/replace", json=_replace_body(target, "orig-1"))
    assert resp.status_code == 409, resp.text
    assert resp.json()["reason_code"] == "ATTEMPT_BODY_MISMATCH"
    assert broker.calls == []


def test_replace_post_cancel_gate4_recheck_blocks_place(client, broker):
    """Cover gone while the cancel ran: Gate 4 at place time refuses."""
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 200)])
    engine.dispose()

    async def shares_sold_meanwhile():
        e = _pg_engine()
        _seed_snapshot(e, [])
        e.dispose()

    broker.cancel_hook = shares_sold_meanwhile
    ratio = ((500.0, "BUY", 1), (510.0, "SELL", 2))  # two short calls: 200 shares cover them leg by leg
    resp = client.post("/orders/replace", json=_replace_body({"permId": 999}, "rep-rc", legs=ratio))
    assert resp.status_code == 400, resp.text
    assert resp.json()["reason_code"] == "ETF_CALL_UNCOVERED"
    assert resp.json()["error"].startswith("CRITICAL")
    assert (broker.cancels, broker.places) == (1, 0)
    assert _row("rep-rc")["state"] == "REJECTED"


def test_replace_in_process_path_runs_every_guard(monkeypatch, client, broker):
    """MEMORY 'In-process route bypass': the helper itself refuses."""
    import asyncio

    target = _original(client, broker)
    # Gate 4 (claim): a naked replacement is refused with nothing cancelled.
    naked = _replace_body(target, "rep-naked", legs=((510.0, "SELL", 1), (520.0, "SELL", 1)))
    resp = asyncio.run(server_mod._orders_replace_from_body(naked))
    assert resp.status_code == 400 and json.loads(resp.body)["reason_code"].endswith("UNCOVERED")
    # Read-only and non-IB broker: refused in-process and over HTTP.
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    resp = asyncio.run(server_mod._orders_replace_from_body(_replace_body(target, "rep-ro")))
    assert json.loads(resp.body)["reason_code"] == "READ_ONLY_MODE"
    assert client.post("/orders/replace", json=_replace_body(target, "rep-ro")).status_code == 403
    monkeypatch.delenv("XENON_READ_ONLY")
    monkeypatch.setattr(server_mod.app.state, "broker", "FUTU", raising=False)
    resp = asyncio.run(server_mod._orders_replace_from_body(_replace_body(target, "rep-futu")))
    assert json.loads(resp.body)["reason_code"] == "READ_ONLY_BROKER"
    assert broker.calls == []
    assert {r.client_attempt_id for r in _submission_rows()} == {"orig-1"}


def test_concurrent_claims_serialize_on_the_scope_lock(monkeypatch, client, broker):
    """Two claims inside the claim txn at once: the advisory lock holds the
    second until the first is inserted, so it sees the claim and refuses."""
    import threading
    import time

    from xenon.execution import gate4

    target = _original(client, broker)
    in_window = threading.Event()

    def hold_first_claim_window():
        if not in_window.is_set():
            in_window.set()
            time.sleep(1.0)

    monkeypatch.setattr(gate4, "_after_check_hook", hold_first_claim_window)
    out: dict = {}
    t = threading.Thread(
        target=lambda: out.setdefault("first", client.post("/orders/replace", json=_replace_body(target, "rep-l1")))
    )
    t.start()
    assert in_window.wait(10)
    second = client.post("/orders/replace", json=_replace_body(target, "rep-l2"))
    t.join(20)
    assert out["first"].status_code == 200, out["first"].text
    assert (second.status_code, second.json()["reason_code"]) == (409, "REPLACEMENT_IN_PROGRESS")
    assert (broker.cancels, broker.places) == (1, 1)


def test_replace_claim_releases_the_target_cover(client, broker):
    """The target's reservation is the cover the successor needs: the claim
    evaluates it as if cancelled (it is, before the place)."""
    ratio = ((500.0, "BUY", 1), (510.0, "SELL", 2))
    target = _original(client, broker, shares=200, legs=ratio)
    # A NEW order needing the same 200 shares is refused: the target holds them.
    other = client.post("/orders/place", json=_combo("other-1", legs=((500.0, "BUY", 1), (520.0, "SELL", 2))))
    assert other.status_code == 400, other.text
    resp = client.post(
        "/orders/replace", json=_replace_body(target, "rep-rel", legs=((500.0, "BUY", 1), (520.0, "SELL", 2)))
    )
    assert resp.status_code == 200, resp.text
    assert (broker.cancels, broker.places) == (1, 1)


# --- target identity (PR-07 fix4): orderIds repeat across IB clientIds ------

_PURE_BUY = ((500.0, "BUY", 1), (510.0, "BUY", 1))  # owes no cover


def _placed(oid, perm, client):
    return ScriptResult(ok=True, data={"status": "ok", "orderId": oid, "permId": perm, "clientId": client})


def _two_clients_same_order_id(client, broker):
    """A (pure-BUY combo, client 30) and B (short call holding all 100
    shares, client 31) share orderId 7 with distinct permIds."""
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 100)])
    engine.dispose()
    broker.place_results = [_placed(7, 9101, 30), _placed(7, 9102, 31)]
    assert client.post("/orders/place", json=_combo("ord-a", legs=_PURE_BUY)).status_code == 200
    assert client.post("/orders/place", json=_sell_call_body("ord-b")).status_code == 200
    broker.calls.clear()


def test_replace_cross_client_same_order_id_leaves_the_other_order(client, broker, fresh_quote):
    _two_clients_same_order_id(client, broker)
    # A successor needing B's 100 shares is still blocked: B's demand stays.
    needs_cover = client.post(
        "/orders/replace",
        json=_replace_body({"orderId": 7, "permId": 9101}, "rep-x1", legs=((500.0, "BUY", 1), (520.0, "SELL", 1))),
    )
    assert needs_cover.status_code == 400, needs_cover.text
    assert broker.calls == []
    # A pure-BUY successor of A goes through and marks ONLY A cancelled.
    ok = client.post("/orders/replace", json=_replace_body({"orderId": 7, "permId": 9101}, "rep-x2", legs=_PURE_BUY))
    assert ok.status_code == 200, ok.text
    assert (_row("ord-a")["state"], _row("ord-b")["state"]) == ("CANCELLED", "WORKING")
    assert "9101" in broker.calls[0]["args"] and "30" in broker.calls[0]["args"]


def test_replace_ambiguous_order_id_only_target_refused(client, broker, fresh_quote):
    _two_clients_same_order_id(client, broker)
    resp = client.post("/orders/replace", json=_replace_body({"orderId": 7}, "rep-amb", legs=_PURE_BUY))
    assert (resp.status_code, resp.json()["reason_code"]) == (409, "ORDER_TARGET_AMBIGUOUS")
    assert "nothing was cancelled" in resp.json()["detail"]
    assert broker.calls == []
    # The placing clientId disambiguates.
    body = {"cancelOrders": [{"orderId": 7, "clientId": 30}], "replaceOrder": _combo("rep-amb-2", legs=_PURE_BUY)}
    assert client.post("/orders/replace", json=body).status_code == 200
    assert (_row("ord-a")["state"], _row("ord-b")["state"]) == ("CANCELLED", "WORKING")


def test_replace_distinct_perm_ids_sharing_an_order_id_do_not_conflict(client, broker, fresh_quote):
    _two_clients_same_order_id(client, broker)
    assert (
        client.post("/orders/replace", json=_replace_body({"permId": 9101}, "rep-d1", legs=_PURE_BUY)).status_code
        == 200
    )
    resp = client.post(
        "/orders/replace", json=_replace_body({"permId": 9102}, "rep-d2", legs=((500.0, "BUY", 1), (510.0, "SELL", 1)))
    )
    assert resp.status_code == 200, resp.text
    assert broker.cancels == 2


def test_replace_perm_only_and_order_id_only_aliases_conflict(client, broker):
    target = _original(client, broker)  # unique orderId in scope
    first, second = _race(
        client,
        broker,
        _replace_body({"permId": target["permId"]}, "rep-al1"),
        _replace_body({"orderId": target["orderId"]}, "rep-al2"),
    )
    assert first.status_code == 200, first.text
    assert (second.status_code, second.json()["reason_code"]) == (409, "REPLACEMENT_IN_PROGRESS")
    assert (broker.cancels, broker.places) == (1, 1)


def test_replace_same_attempt_id_different_target_cancels_nothing(client, broker, fresh_quote):
    _two_clients_same_order_id(client, broker)
    first, second = _race(
        client,
        broker,
        _replace_body({"permId": 9101}, "rep-sid", legs=_PURE_BUY),
        _replace_body({"permId": 9102}, "rep-sid", legs=_PURE_BUY),
    )
    assert first.status_code == 200, first.text
    assert second.status_code in (409, 502), second.text
    assert (broker.cancels, broker.places) == (1, 1)
    assert _row("ord-b")["state"] == "WORKING"


def test_plain_cancel_marks_only_the_canonical_row(client, broker, fresh_quote):
    """/orders/cancel shares the persistence: an ambiguous orderId is refused
    before the CLI; a permId cancels and marks exactly its row; an orderId
    with no row at all (TWS order) still goes to the CLI."""
    _two_clients_same_order_id(client, broker)
    amb = client.post("/orders/cancel", json={"orderId": 7})
    assert (amb.status_code, amb.json()["reason_code"]) == (409, "ORDER_TARGET_AMBIGUOUS")
    assert broker.calls == []
    assert (_row("ord-a")["state"], _row("ord-b")["state"]) == ("WORKING", "WORKING")
    assert client.post("/orders/cancel", json={"orderId": 7, "permId": 9102}).status_code == 200
    assert (_row("ord-a")["state"], _row("ord-b")["state"]) == ("WORKING", "CANCELLED")
    assert client.post("/orders/cancel", json={"orderId": 4711}).status_code == 200
    assert broker.cancels == 2


def test_client_id_zero_is_a_real_client(client, broker, fresh_quote):
    """IB clientId 0 is valid: only a missing clientId means unknown."""
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 100)])
    engine.dispose()
    broker.place_results = [_placed(7, 9102, 31)]
    assert client.post("/orders/place", json=_sell_call_body("ord-b")).status_code == 200
    broker.calls.clear()
    # Only (client 31, order 7) is mirrored: (client 0, order 7) is missing.
    body = {"cancelOrders": [{"orderId": 7, "clientId": 0}], "replaceOrder": _combo("rep-c0", legs=_PURE_BUY)}
    resp = client.post("/orders/replace", json=body)
    assert (resp.status_code, resp.json()["reason_code"]) == (409, "ORDER_TARGET_AMBIGUOUS")
    assert broker.calls == []
    # Now (client 0, order 7) exists too: clientId 0 selects it, never B.
    broker.place_results = [_placed(7, 9101, 0)]
    assert client.post("/orders/place", json=_combo("ord-a", legs=_PURE_BUY)).status_code == 200
    assert _row("ord-a")["placing_client_id"] == 0
    broker.calls.clear()
    body["replaceOrder"] = _combo("rep-c0-2", legs=_PURE_BUY)
    resp = client.post("/orders/replace", json=body)
    assert resp.status_code == 200, resp.text
    args = broker.calls[0]["args"]
    assert "9101" in args and args[args.index("--client-id") + 1] == "0"
    assert (_row("ord-a")["state"], _row("ord-b")["state"]) == ("CANCELLED", "WORKING")


def test_uncertain_place_keeps_client_id_zero(client, broker, fresh_quote):
    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 100)])
    engine.dispose()
    broker.place_results = [
        ScriptResult(ok=True, data={"status": "uncertain", "outcome": "uncertain", "orderId": 5, "clientId": 0})
    ]
    resp = client.post("/orders/place", json=_sell_call_body("unc-c0"))
    assert resp.status_code == 502, resp.text
    row = _row("unc-c0")
    assert (row["state"], row["ib_order_id"], row["placing_client_id"]) == ("UNKNOWN", "5", 0)
