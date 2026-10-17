# ruff: noqa: F811 -- route fixtures are imported by name and re-bound as test args
"""Gate-4 sync-pass tests (PR-03b round 3, items 1-3).

These run the REAL `ib_sync.main --sync` orchestration against a fake IB
client built from real ib_async objects (Position / Trade / Fill /
ContractDetails), then drive the REAL /orders/place route on the snapshot
the pass published. The only fakes are the IB socket and the broker
subprocess.

Pass read order: positions -> open orders -> executions -> open orders.
`inject_at` names the read AFTER which an external event lands, so every
pair of consecutive reads gets an injection.
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
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
from sqlalchemy import select

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
from xenon.db.schema import account_snapshots
from xenon.execution import ib_sync

pytestmark = pytest.mark.committed_db

ACCOUNT = "DU0000000"
FOREIGN = "DU9999999"
EXP = "20261218"


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

    def managedAccounts(self):
        return list(self._accounts)

    def accountValues(self):
        return []

    def reqPnL(self, account):
        return SimpleNamespace(dailyPnL=math.nan, unrealizedPnL=math.nan, realizedPnL=math.nan)


class FakeSyncClient:
    """IBClient double. `on_read[name]` runs right AFTER read `name`
    (positions, open_orders_1, executions, open_orders_2) — the injection
    point for an event that lands between that read and the next one."""

    def __init__(self, *, positions, accounts=(ACCOUNT,)):
        self.ib = _FakeIB(accounts)
        self.positions = list(positions)
        self.open_orders = []
        self.fills = []
        self.details = {}
        self.on_read = {}
        self.reads = []
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
# item 1 — one coherent pass
# ---------------------------------------------------------------------------


def test_control_pass_without_injection_accepts_covered_short(monkeypatch, client, broker_calls, fresh_quote):
    fake = _shares_client(100)
    _run_sync(monkeypatch, fake)

    assert fake.reads == ["positions", "open_orders_1", "executions", "open_orders_2"]
    obs = _latest_payload()["observation"]
    assert obs["complete"] is True, obs
    resp = client.post("/orders/place", json=_sell_call_body("sp-control-1"))
    assert resp.status_code == 200, resp.text


INJECT_POINTS = ["positions", "open_orders_1", "executions"]


def _external_order_appears(fake):
    c = _opt(500, con_id=111)
    fake.open_orders.append(_trade(c, order_id=9001, perm_id=990001))


@pytest.mark.parametrize("inject_after", INJECT_POINTS)
def test_external_order_between_reads_blocks_next_short(inject_after, monkeypatch, client, broker_calls, fresh_quote):
    """An external SELL call placed between any two reads is persisted to
    order_submissions before the snapshot publishes — it consumes the 100
    shares and the next short is blocked."""
    fake = _shares_client(100)
    fake.add_details(_opt(500, con_id=111))
    fake.on_read[inject_after] = _external_order_appears
    _run_sync(monkeypatch, fake)

    assert _latest_payload()["observation"]["complete"] is True
    assert any(r._mapping["perm_id"] == "990001" for r in _submission_rows())
    resp = client.post("/orders/place", json=_sell_call_body(f"sp-ord-{inject_after}"))
    assert resp.status_code == 400, resp.text
    assert resp.json()["reason_code"] == "ETF_CALL_UNCOVERED"
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
    # Fill before the first open-order read: the order row was never seen,
    # so the fill is an orphan after as_of and the view is unprovable.
    # Later: the working row (and the linked fill) consume the cover.
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
