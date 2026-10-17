# ruff: noqa: F811 -- route fixtures are imported by name and re-bound as test args
"""Gate-4 sync-pass tests (PR-03b rounds 3-4).

These run the REAL `ib_sync.main --sync` orchestration against a fake IB
client built from real ib_async objects (Position / Trade / Fill /
ContractDetails), then drive the REAL /orders/place route on the snapshot
the pass published. The only fakes are the IB socket and the broker
subprocess.

Pass read order (round 4): positions -> open orders -> executions.
`inject_after` names the read AFTER which an external event lands, so every
pair of consecutive reads gets an injection. Activity after the last read
(executions) is the accepted after-pass limitation.
"""

from __future__ import annotations

import math
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from ib_async import (
    CommissionReport,
    Contract,
    ContractDetails,
    Execution,
    Fill,
    Order,
    OrderStatus,
    Position,
    Trade,
)
from sqlalchemy import insert, select

from scripts.tests.test_gate4_route import (  # noqa: F401 — autouse fixtures
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
from xenon.db.schema import account_snapshots, order_submissions
from xenon.execution import contract_meta, ib_sync

pytestmark = pytest.mark.committed_db

ACCOUNT = "DU0000000"
FOREIGN = "DU9999999"
EXP = "20261218"


@pytest.fixture(autouse=True)
def _fresh_meta_cache():
    contract_meta.clear_cache()
    yield
    contract_meta.clear_cache()


def _stk():
    return Contract(conId=756733, symbol="SPY", secType="STK", currency="USD", exchange="SMART")


def _opt(strike, right="C", *, con_id, tc="SPY", mult="100"):
    return Contract(
        conId=con_id,
        symbol="SPY",
        secType="OPT",
        lastTradeDateOrContractMonth=EXP,
        strike=float(strike),
        right=right,
        multiplier=mult,
        tradingClass=tc,
        currency="USD",
        exchange="SMART",
    )


def _trade(contract, *, order_id, perm_id, action="SELL", qty=1, account=ACCOUNT):
    order = Order(
        orderId=order_id,
        permId=perm_id,
        clientId=0,
        action=action,
        totalQuantity=qty,
        orderType="LMT",
        lmtPrice=5.0,
        tif="DAY",
        account=account or "",
    )
    return Trade(contract=contract, order=order, orderStatus=OrderStatus(status="Submitted", filled=0, remaining=qty))


def _fill(contract, *, exec_id, perm_id, order_id, side="SLD", shares=1, account=ACCOUNT):
    now = datetime.now(timezone.utc)
    execution = Execution(
        execId=exec_id,
        time=now,
        acctNumber=account,
        side=side,
        shares=shares,
        price=5.0,
        permId=perm_id,
        orderId=order_id,
        exchange="SMART",
    )
    return Fill(contract=contract, execution=execution, commissionReport=CommissionReport(), time=now)


class _FakeIB:
    def __init__(self, accounts):
        self._accounts = accounts
        self.client = SimpleNamespace(clientId=57)

    def managedAccounts(self):
        return list(self._accounts)

    def accountValues(self):
        return []

    def reqPnL(self, account):
        return SimpleNamespace(dailyPnL=math.nan, unrealizedPnL=math.nan, realizedPnL=math.nan)


class FakeSyncClient:
    """IBClient double. `on_read[name]` runs right AFTER read `name`
    (positions, open_orders_1, executions) — the injection point for an
    event that lands between that read and the next one."""

    def __init__(self, *, positions, accounts=(ACCOUNT,)):
        self.ib = _FakeIB(accounts)
        self.positions = list(positions)
        self.open_orders = []
        self.fills = []
        self.details = {}
        self.on_read = {}
        self.reads = []
        self.detail_calls = 0
        self._oo_reads = 0

    def add_details(self, contract, und_sec="STK"):
        self.details[contract.conId] = ContractDetails(contract=contract, underSecType=und_sec)

    def _after(self, name, result):
        self.reads.append(name)
        hook = self.on_read.get(name)
        if hook:
            hook(self)
        return result

    def get_positions(self):
        return self._after("positions", list(self.positions))

    def get_open_orders(self):
        self._oo_reads += 1
        return self._after(f"open_orders_{self._oo_reads}", list(self.open_orders))

    def get_executions(self):
        return self._after("executions", list(self.fills))

    def get_fills(self):
        return list(self.fills)

    def get_contract_details(self, contract):
        self.detail_calls += 1
        d = self.details.get(getattr(contract, "conId", 0))
        return [d] if d else []

    def cancel_pnl(self, pnl):
        pass

    def is_connected(self):
        return True

    def disconnect(self):
        pass

    def sleep(self, _s):
        pass


def _run_sync(monkeypatch, fake):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", ACCOUNT)
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    monkeypatch.setattr(ib_sync, "connect_ib", lambda *a, **k: fake)
    monkeypatch.setattr(sys, "argv", ["xenon-ib-sync", "--sync", "--no-prices", "--skip-audit"])
    ib_sync.main()


def _latest_payload():
    engine = _pg_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(account_snapshots.c.payload).order_by(account_snapshots.c.snapshot_at.desc()).limit(1)
        ).first()
    engine.dispose()
    return row[0]


def _shares_client(shares=100, **kw):
    return FakeSyncClient(positions=[Position(ACCOUNT, _stk(), shares, 500.0)], **kw)


# ---------------------------------------------------------------------------
# item 1 / round-4 item 3 — one coherent pass
# ---------------------------------------------------------------------------


def test_control_pass_without_injection_accepts_covered_short(monkeypatch, client, broker_calls, fresh_quote):
    fake = _shares_client(100)
    _run_sync(monkeypatch, fake)

    assert fake.reads == ["positions", "open_orders_1", "executions"]
    obs = _latest_payload()["observation"]
    assert obs["complete"] is True, obs
    assert obs["executions_client_id"] == 57
    resp = client.post("/orders/place", json=_sell_call_body("sp-control-1"))
    assert resp.status_code == 200, resp.text


# Every pair of consecutive reads: (positions, open orders), (open orders, executions).
INJECT_POINTS = ["positions", "open_orders_1"]


def test_external_order_before_order_read_blocks_next_short(monkeypatch, client, broker_calls, fresh_quote):
    """An external SELL call placed after the positions read is persisted to
    order_submissions before the snapshot publishes — it consumes the 100
    shares and the next short is blocked."""
    fake = _shares_client(100)
    fake.add_details(_opt(500, con_id=111))

    def _appears(f):
        f.open_orders.append(_trade(_opt(500, con_id=111), order_id=9001, perm_id=990001))

    fake.on_read["positions"] = _appears
    _run_sync(monkeypatch, fake)

    assert _latest_payload()["observation"]["complete"] is True
    assert any(r._mapping["perm_id"] == "990001" for r in _submission_rows())
    resp = client.post("/orders/place", json=_sell_call_body("sp-ord-1"))
    assert resp.status_code == 400, resp.text
    assert resp.json()["reason_code"] == "ETF_CALL_UNCOVERED"
    assert broker_calls == []


@pytest.mark.parametrize("inject_after", INJECT_POINTS)
def test_external_short_placed_and_filled_between_reads_blocks_next_short(
    inject_after, monkeypatch, client, broker_calls, fresh_quote
):
    """Astra P1-3: a NEW external short (not in any open-order read) is
    placed AND filled between two reads. Its fill (after as_of) is read by
    the final executions read and persisted, so the next short is blocked."""
    c = _opt(500, con_id=111)
    fake = _shares_client(100)
    fake.add_details(c)

    def _placed_and_filled(f):
        f.fills.append(_fill(c, exec_id="0001.new.01", perm_id=990003, order_id=9003))

    fake.on_read[inject_after] = _placed_and_filled
    _run_sync(monkeypatch, fake)

    assert not any(r._mapping["perm_id"] == "990003" for r in _submission_rows())
    resp = client.post("/orders/place", json=_sell_call_body(f"sp-new-{inject_after}"))
    assert resp.status_code == 400, resp.text
    # The fill has no order row: an orphan after as_of makes the view unprovable.
    assert resp.json()["reason_code"] == "PORTFOLIO_SNAPSHOT_REQUIRED"
    assert broker_calls == []


@pytest.mark.parametrize("inject_after", INJECT_POINTS)
def test_external_fill_between_reads_blocks_next_short(inject_after, monkeypatch, client, broker_calls, fresh_quote):
    """An external SELL call that was working at pass start fills between
    two reads: it leaves open orders and appears in executions. Positions
    were read before the fill, so the holdings still show the 100 shares as
    free. The pass persists the working row and/or the fill before
    publishing, so the next short is blocked."""
    c = _opt(500, con_id=111)
    fake = _shares_client(100)
    fake.add_details(c)
    working = _trade(c, order_id=9002, perm_id=990002)
    fake.open_orders.append(working)

    def _fills(f):
        f.open_orders.remove(working)
        f.fills.append(_fill(c, exec_id="0001.ext.01", perm_id=990002, order_id=9002))

    fake.on_read[inject_after] = _fills
    _run_sync(monkeypatch, fake)

    resp = client.post("/orders/place", json=_sell_call_body(f"sp-fill-{inject_after}"))
    assert resp.status_code == 400, resp.text
    # Fill before the open-order read: the order row was never seen, so the
    # fill is an orphan after as_of and the view is unprovable. Later: the
    # working row (and the linked fill) consume the cover.
    expected = "PORTFOLIO_SNAPSHOT_REQUIRED" if inject_after == "positions" else "ETF_CALL_UNCOVERED"
    assert resp.json()["reason_code"] == expected
    assert broker_calls == []


def test_persist_failure_publishes_incomplete_snapshot(monkeypatch, client, broker_calls, fresh_quote):
    from xenon.execution import ib_orders

    def _boom(*a, **k):
        raise RuntimeError("pg down")

    monkeypatch.setattr(ib_orders, "sync_open_orders_to_postgres", _boom)
    fake = _shares_client(100)
    _run_sync(monkeypatch, fake)

    obs = _latest_payload()["observation"]
    assert obs["complete"] is False
    assert obs["reads"]["persisted"] is False
    resp = client.post("/orders/place", json=_sell_call_body("sp-persist-1"))
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "PORTFOLIO_SNAPSHOT_REQUIRED"


# ---------------------------------------------------------------------------
# item 2 — position provenance
# ---------------------------------------------------------------------------


def test_foreign_account_shares_never_cover(monkeypatch, client, broker_calls, fresh_quote):
    fake = FakeSyncClient(
        positions=[Position(FOREIGN, _stk(), 100, 500.0)],
        accounts=(ACCOUNT, FOREIGN),
    )
    _run_sync(monkeypatch, fake)

    payload = _latest_payload()
    assert payload["observation"]["complete"] is True
    assert payload["positions"] == []
    resp = client.post("/orders/place", json=_sell_call_body("sp-foreign-1"))
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "ETF_CALL_UNCOVERED"


def test_accountless_position_makes_pass_incomplete(monkeypatch, client, broker_calls, fresh_quote):
    fake = FakeSyncClient(positions=[Position(ACCOUNT, _stk(), 100, 500.0), Position("", _stk(), 100, 500.0)])
    _run_sync(monkeypatch, fake)

    obs = _latest_payload()["observation"]
    assert obs["complete"] is False
    assert obs["reads"]["positions"] is False
    resp = client.post("/orders/place", json=_sell_call_body("sp-noacct-pos"))
    assert resp.json()["reason_code"] == "PORTFOLIO_SNAPSHOT_REQUIRED"


def test_accountless_open_order_makes_pass_incomplete(monkeypatch, client, broker_calls, fresh_quote):
    fake = _shares_client(100)
    c = _opt(510, con_id=5101)
    fake.add_details(c)
    fake.open_orders.append(_trade(c, order_id=9003, perm_id=990003, action="BUY", account=None))
    _run_sync(monkeypatch, fake)

    obs = _latest_payload()["observation"]
    assert obs["complete"] is False
    assert obs["reads"]["order_accounts"] is False
    resp = client.post("/orders/place", json=_sell_call_body("sp-noacct-ord"))
    assert resp.json()["reason_code"] == "PORTFOLIO_SNAPSHOT_REQUIRED"


# ---------------------------------------------------------------------------
# item 3 — contract metadata from IB objects
# ---------------------------------------------------------------------------


def test_synced_adjusted_long_call_does_not_cover_standard_short(monkeypatch, client, broker_calls, fresh_quote):
    adjusted = _opt(490, con_id=4901, tc="SPY1", mult="150")
    fake = FakeSyncClient(positions=[Position(ACCOUNT, adjusted, 1, 1200.0)])
    fake.add_details(adjusted)
    _run_sync(monkeypatch, fake)

    leg = _latest_payload()["positions"][0]["legs"][0]
    assert (leg["tc"], leg["mult"], leg["und_sec"]) == ("SPY1", 150, "STK")
    resp = client.post("/orders/place", json=_sell_call_body("sp-adj-1"))
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "ETF_CALL_UNCOVERED"


def test_synced_standard_long_call_covers_standard_short(monkeypatch, client, broker_calls, fresh_quote):
    std = _opt(490, con_id=4902)
    fake = FakeSyncClient(positions=[Position(ACCOUNT, std, 1, 1200.0)])
    fake.add_details(std)
    _run_sync(monkeypatch, fake)

    resp = client.post("/orders/place", json=_sell_call_body("sp-std-1"))
    assert resp.status_code == 200, resp.text


def test_synced_option_without_contract_details_fails_closed(monkeypatch, client, broker_calls, fresh_quote):
    std = _opt(490, con_id=4903)
    fake = FakeSyncClient(positions=[Position(ACCOUNT, std, 1, 1200.0)])
    _run_sync(monkeypatch, fake)  # no details registered → lookup fails

    leg = _latest_payload()["positions"][0]["legs"][0]
    assert leg["tc"] is None and leg["mult"] is None
    resp = client.post("/orders/place", json=_sell_call_body("sp-nodet-1"))
    assert resp.status_code == 400
    assert resp.json()["reason_code"] in ("GATE4_UNRESOLVED", "ETF_CALL_UNCOVERED")


# ---------------------------------------------------------------------------
# round 4 item 2 — UUID-authored rows in a complete pass
# ---------------------------------------------------------------------------


def _insert_uuid_sell_call(perm_id, *, qty=1, legs=None):
    """A Xenon-placed (UUID) WORKING SELL call row, last written 1h ago."""
    sub_id = str(uuid.uuid4())
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    engine = _pg_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=sub_id,
                user_id="local",
                client_attempt_id=f"att-{perm_id}",
                ticker="SPY",
                security_type="OPT",
                action="SELL",
                quantity=qty,
                expiry=datetime(2026, 12, 18).date(),
                strike=Decimal("500"),
                right="C",
                multiplier=100,
                con_id=111,
                limit_price=Decimal("5"),
                state="WORKING",
                submitted_at=past,
                updated_at=past,
                legs=legs,
                ib_order_id=str(perm_id - 900000),
                perm_id=str(perm_id),
                broker="IB",
                account_env="paper",
                broker_account=ACCOUNT,
            )
        )
    engine.dispose()
    return sub_id


def _row(sub_id):
    engine = _pg_engine()
    with engine.connect() as conn:
        row = conn.execute(select(order_submissions).where(order_submissions.c.submission_id == sub_id)).first()
    engine.dispose()
    return row._mapping


_QUALIFIED = {
    "legs": [
        {"action": "BUY", "sym": "SPY", "sec": "OPT", "ratio": 1, "right": "C", "exp": "20261218",
         "mult": 100, "tc": "SPY", "strike": 500.0, "con_id": 111, "und_sec": "STK"}
    ],
    "guaranteed": None,
    "pending_amend_qty": None,
    "pending_amend_id": None,
}


@pytest.mark.parametrize(("ib_qty", "expect"), [(1, 200), (2, 400)])
def test_uuid_row_tws_drift_persists_conservative_demand(
    ib_qty, expect, monkeypatch, client, broker_calls, fresh_quote
):
    """Astra P1-2: a Xenon SELL call row (qty 1) that TWS raised to 2. The
    pass keeps the request identity (quantity column stays 1) but persists
    observed_qty=2, so with 200 shares the next short is blocked. Control:
    no drift → the second short fits."""
    sub_id = _insert_uuid_sell_call(990010, qty=1, legs=_QUALIFIED)
    c = _opt(500, con_id=111)
    fake = _shares_client(200)
    fake.add_details(c)
    fake.open_orders.append(_trade(c, order_id=90010, perm_id=990010, qty=ib_qty))
    _run_sync(monkeypatch, fake)

    assert _latest_payload()["observation"]["complete"] is True
    row = _row(sub_id)
    assert row["quantity"] == 1
    assert row["legs"].get("observed_qty") == (2 if ib_qty == 2 else None)
    resp = client.post("/orders/place", json=_sell_call_body(f"sp-uuid-{ib_qty}"))
    assert resp.status_code == expect, resp.text


def test_legacy_uuid_row_metadata_is_restamped_by_the_pass(monkeypatch, client, broker_calls, fresh_quote):
    """Fable residual d: a pre-03a Xenon SELL call row (legs NULL) fails its
    symbol closed. The pass re-stamps qualified metadata from IB contract
    details, after which the covered short is accepted."""
    sub_id = _insert_uuid_sell_call(990011, qty=1, legs=None)
    c = _opt(500, con_id=111)
    fake = _shares_client(200)
    fake.add_details(c)
    fake.open_orders.append(_trade(c, order_id=90011, perm_id=990011))

    # Before any pass: unqualified demand on SPY → fail closed.
    no_pass = FakeSyncClient(positions=[Position(ACCOUNT, _stk(), 200, 500.0)])
    _run_sync(monkeypatch, no_pass)  # IB shows no open orders: nothing re-stamped
    blocked = client.post("/orders/place", json=_sell_call_body("sp-legacy-0"))
    assert blocked.status_code == 400 and blocked.json()["reason_code"] == "GATE4_UNRESOLVED"

    _run_sync(monkeypatch, fake)

    leg = _row(sub_id)["legs"]["legs"][0]
    assert (leg["tc"], leg["mult"], leg["und_sec"], leg["con_id"]) == ("SPY", 100, "STK", 111)
    resp = client.post("/orders/place", json=_sell_call_body("sp-legacy-1"))
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# round 4 items 6-7 — unkeyed open orders; metadata cache
# ---------------------------------------------------------------------------


def test_open_order_without_perm_or_order_id_makes_pass_incomplete(monkeypatch, client, broker_calls, fresh_quote):
    c = _opt(500, con_id=111)
    fake = _shares_client(100)
    fake.add_details(c)
    fake.open_orders.append(_trade(c, order_id=0, perm_id=0))
    _run_sync(monkeypatch, fake)

    obs = _latest_payload()["observation"]
    assert obs["complete"] is False
    assert obs["reads"]["persisted"] is False
    resp = client.post("/orders/place", json=_sell_call_body("sp-noid-1"))
    assert resp.json()["reason_code"] == "PORTFOLIO_SNAPSHOT_REQUIRED"


def test_second_pass_reuses_cached_contract_metadata(monkeypatch, client, broker_calls, fresh_quote):
    held = _opt(490, con_id=4902)
    working = _opt(500, con_id=111)
    fake = FakeSyncClient(positions=[Position(ACCOUNT, held, 1, 1200.0)])
    fake.add_details(held)
    fake.add_details(working)
    fake.open_orders.append(_trade(working, order_id=9020, perm_id=990020, action="BUY"))

    _run_sync(monkeypatch, fake)
    first = fake.detail_calls
    assert first == 2  # one reqContractDetails per distinct conId
    fake.reads.clear()
    fake._oo_reads = 0
    _run_sync(monkeypatch, fake)

    assert fake.detail_calls == first  # second pass: no IB call for known conIds
    leg = _latest_payload()["positions"][0]["legs"][0]
    assert (leg["tc"], leg["mult"]) == ("SPY", 100)
