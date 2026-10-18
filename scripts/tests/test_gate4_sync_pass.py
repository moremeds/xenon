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

import json
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
from sqlalchemy import insert, select, text, update

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
from xenon.db.schema import account_snapshots, nav_history, order_submissions
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


def test_accountless_fill_makes_pass_incomplete(monkeypatch, client, broker_calls, fresh_quote):
    """#198 drops an execution with no acctNumber; the pass must then not
    claim it saw every fill (an unattributed short fill could need cover)."""
    fake = _shares_client(100)
    c = _opt(510, con_id=5102)
    fake.add_details(c)
    fake.fills.append(_fill(c, exec_id="0001.noacct.01", perm_id=990004, order_id=9004, account=""))
    _run_sync(monkeypatch, fake)

    obs = _latest_payload()["observation"]
    assert obs["complete"] is False
    assert obs["reads"]["fill_accounts"] is False


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


# ---------------------------------------------------------------------------
# merge-fix P1 — the activity poller drops accountless rows (#198 filter);
# the scope's Gate-4 view must stop proving cover until a later pass clears it
# ---------------------------------------------------------------------------


def _poller_tick(fake):
    from xenon.api.services.ib_activity_mirror import run_activity_poll_tick
    from xenon.execution.account_scope import resolve_from_env

    return run_activity_poll_tick(ib_client_factory=lambda: fake, scope=resolve_from_env())


@pytest.mark.parametrize("account, allowed", [("", False), (FOREIGN, True)], ids=["accountless", "foreign"])
def test_poller_open_order_without_scope_account(account, allowed, monkeypatch, client, broker_calls, fresh_quote):
    fake = _shares_client(100)
    _run_sync(monkeypatch, fake)
    assert _latest_payload()["observation"]["complete"] is True
    c = _opt(500, con_id=111)
    fake.add_details(c)
    fake.open_orders.append(_trade(c, order_id=9101, perm_id=991001, account=account))

    _poller_tick(fake)

    resp = client.post("/orders/place", json=_sell_call_body(f"sp-poll-oo-{account or 'none'}"))
    if allowed:
        assert resp.status_code == 200, resp.text
    else:
        assert resp.status_code == 400, resp.text
        assert resp.json()["reason_code"] == "PORTFOLIO_SNAPSHOT_REQUIRED"


@pytest.mark.parametrize("account, allowed", [("", False), (FOREIGN, True)], ids=["accountless", "foreign"])
def test_poller_fill_without_scope_account(account, allowed, monkeypatch, client, broker_calls, fresh_quote):
    fake = _shares_client(100)
    _run_sync(monkeypatch, fake)
    assert _latest_payload()["observation"]["complete"] is True
    c = _opt(500, con_id=111)
    fake.add_details(c)
    fake.fills.append(_fill(c, exec_id="0001.poll.01", perm_id=991002, order_id=9102, account=account))

    _poller_tick(fake)

    resp = client.post("/orders/place", json=_sell_call_body(f"sp-poll-fill-{account or 'none'}"))
    if allowed:
        assert resp.status_code == 200, resp.text
    else:
        assert resp.status_code == 400, resp.text
        assert resp.json()["reason_code"] == "PORTFOLIO_SNAPSHOT_REQUIRED"


def _accountless_poller_view():
    view = _shares_client(100)
    c = _opt(500, con_id=111)
    view.add_details(c)
    view.open_orders.append(_trade(c, order_id=9103, perm_id=991003, account=""))
    return view


def _insert_legacy_snapshot():
    engine = _pg_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(account_snapshots).values(
                account=ACCOUNT,
                bankroll=Decimal("0"),
                payload={"positions": []},
                broker="IB",
                account_env="paper",
                broker_account=ACCOUNT,
            )
        )
    engine.dispose()


@pytest.mark.parametrize("prior", ["empty", "legacy"])
def test_first_publication_after_poller_mark_is_refused(prior, monkeypatch, client, broker_calls, fresh_quote):
    """No PR-03b snapshot yet: the in-flight first pass started before the
    poller's mark, so its publication cannot prove cover."""
    if prior == "legacy":
        _insert_legacy_snapshot()
    inflight = _shares_client(100)
    inflight.on_read["executions"] = lambda _f: _poller_tick(_accountless_poller_view())

    _run_sync(monkeypatch, inflight)

    assert _latest_payload()["observation"]["complete"] is True  # the pass itself saw a whole view
    resp = client.post("/orders/place", json=_sell_call_body(f"sp-first-{prior}"))
    assert resp.status_code == 400, resp.text
    assert resp.json()["reason_code"] == "PORTFOLIO_SNAPSHOT_REQUIRED"


def test_overlapping_older_pass_cannot_replace_newer_snapshot(monkeypatch, client, broker_calls, fresh_quote):
    """A starts -> poller mark -> B (started after the mark) publishes ->
    A publishes. A's older 200-share view must not replace B's 100 shares."""
    pass_b = _shares_client(100)
    pass_a = FakeSyncClient(positions=[Position(ACCOUNT, _stk(), 200, 500.0)])

    def _mark_then_b(_f):
        _poller_tick(_accountless_poller_view())
        _run_sync(monkeypatch, pass_b)

    pass_a.on_read["positions"] = _mark_then_b
    _run_sync(monkeypatch, pass_a)

    payload = _latest_payload()
    assert payload["observation"]["complete"] is True
    assert [leg["contracts"] for p in payload["positions"] for leg in p["legs"]] == [100]
    resp = client.post("/orders/place", json=_sell_call_body("sp-overlap-2", qty=2))
    assert resp.status_code == 400, resp.text
    assert resp.json()["reason_code"] == "ETF_CALL_UNCOVERED"


def test_pass_after_poller_mark_without_accountless_rows_is_allowed(monkeypatch, client, broker_calls, fresh_quote):
    _run_sync(monkeypatch, _shares_client(100))
    _poller_tick(_accountless_poller_view())
    resp = client.post("/orders/place", json=_sell_call_body("sp-mark-blocked"))
    assert resp.json()["reason_code"] == "PORTFOLIO_SNAPSHOT_REQUIRED"

    _run_sync(monkeypatch, _shares_client(100))

    resp = client.post("/orders/place", json=_sell_call_body("sp-mark-cleared"))
    assert resp.status_code == 200, resp.text


def test_view_watermark_only_moves_forward():
    """The watermark seq is an xid8; a later invalidation never lowers it."""
    from xenon.execution import gate4

    scope = {"broker": "IB", "account_env": "paper", "broker_account": ACCOUNT}
    gate4.invalidate_view(scope, "first")
    engine = gate4.get_sync_engine()
    with engine.connect() as conn:
        first = gate4.view_watermark(conn, scope)
    gate4.invalidate_view(scope, "second")
    with engine.connect() as conn:
        second = gate4.view_watermark(conn, scope)
    assert first is not None and second > first

    # An older transaction (lower xid) that upserts after a newer one
    # committed never moves the watermark back.
    with engine.connect() as older:
        tx = older.begin()
        older_xid = int(older.execute(text("SELECT pg_current_xact_id()::text")).scalar())
        gate4.invalidate_view(scope, "newer")
        with engine.connect() as conn:
            newer = gate4.view_watermark(conn, scope)
        assert newer > older_xid
        older.execute(
            gate4._INVALIDATE_SQL,
            {"service": gate4.VIEW_WATERMARK_SERVICE, **scope, "reason": "older", "at": datetime.now(timezone.utc)},
        )
        tx.commit()
    with engine.connect() as conn:
        assert gate4.view_watermark(conn, scope) == newer


def test_clock_regression_pass_with_higher_seq_is_published(monkeypatch, client, broker_calls, fresh_quote):
    """Ordering is by pass_seq, not wall time: a later pass whose clock went
    back still publishes, and its holdings are the ones Gate 4 reads."""
    _run_sync(monkeypatch, _shares_client(100))
    first_as_of = datetime.fromisoformat(_latest_payload()["observation"]["as_of"])

    class _BackClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return first_as_of - timedelta(seconds=60)

    monkeypatch.setattr(ib_sync, "datetime", _BackClock)
    _run_sync(monkeypatch, FakeSyncClient(positions=[Position(ACCOUNT, _stk(), 200, 500.0)]))
    monkeypatch.setattr(ib_sync, "datetime", datetime)

    payload = _latest_payload()
    assert datetime.fromisoformat(payload["observation"]["as_of"]) < first_as_of
    assert [leg["contracts"] for p in payload["positions"] for leg in p["legs"]] == [200]
    resp = client.post("/orders/place", json=_sell_call_body("sp-clock-back", qty=2))
    assert resp.status_code == 200, resp.text


def test_reader_orders_by_pass_seq_not_snapshot_at(monkeypatch, client, broker_calls, fresh_quote):
    """A DB clock step back can give the newer pass an earlier snapshot_at;
    load_inputs still reads the snapshot with the highest pass_seq."""
    _run_sync(monkeypatch, _shares_client(100))
    _run_sync(monkeypatch, FakeSyncClient(positions=[Position(ACCOUNT, _stk(), 200, 500.0)]))
    engine = _pg_engine()
    with engine.begin() as conn:
        rows = conn.execute(select(account_snapshots.c.id).order_by(account_snapshots.c.id)).all()
        conn.execute(
            update(account_snapshots)
            .where(account_snapshots.c.id == rows[0].id)
            .values(snapshot_at=datetime.now(timezone.utc) + timedelta(hours=1))
        )
    engine.dispose()

    resp = client.post("/orders/place", json=_sell_call_body("sp-seq-order", qty=2))
    assert resp.status_code == 200, resp.text


def _nav_rows():
    engine = _pg_engine()
    with engine.connect() as conn:
        rows = conn.execute(select(nav_history.c.nav).where(nav_history.c.source == "intraday")).all()
    engine.dispose()
    return [float(r.nav) for r in rows]


def _bare_portfolio(net_liq):
    from xenon.execution import gate4

    seq, cluster = gate4.next_pass_seq()
    return {
        "positions": [],
        "account_summary": {"net_liquidation": net_liq},
        "observation": {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "pass_seq": str(seq),
            "pass_cluster": cluster,
        },
    }


def test_overlapping_passes_cannot_interleave_nav(monkeypatch):
    """A commits its snapshot, then B commits snapshot + NAV, then A would
    write NAV. NAV is part of each publish transaction, so B's NAV stands."""
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", ACCOUNT)
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    pass_a = _bare_portfolio(1000.0)
    pass_b = _bare_portfolio(2000.0)
    real_save = ib_sync._save_portfolio_to_postgres

    def _a_commits_then_b(portfolio):
        published = real_save(portfolio)
        if portfolio is pass_a:
            ib_sync.save_portfolio(pass_b)
        return published

    monkeypatch.setattr(ib_sync, "_save_portfolio_to_postgres", _a_commits_then_b)
    ib_sync.save_portfolio(pass_a)

    assert _nav_rows() == [2000.0]


def test_save_portfolio_skips_nav_when_pass_not_published(monkeypatch):
    """A stale (or read-only) pass publishes nothing and writes no NAV."""
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", ACCOUNT)
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    older = _bare_portfolio(1000.0)
    newer = _bare_portfolio(2000.0)
    ib_sync.save_portfolio(older)
    ib_sync.save_portfolio(newer)
    ib_sync.save_portfolio(older)  # stale: pass_seq not after the newest published

    assert _nav_rows() == [2000.0]


# ---------------------------------------------------------------------------
# Every seq carries its cluster id (pg_control_system().system_identifier).
# A seq from another cluster (pg_dump/pg_restore) or without one is foreign
# forever, however far this cluster's xids advance.
# ---------------------------------------------------------------------------

FOREIGN_CLUSTER = "7000000000000000001"
SCOPE = {"broker": "IB", "account_env": "paper", "broker_account": ACCOUNT}


def _sql(stmt, **params):
    engine = _pg_engine()
    with engine.begin() as conn:
        res = conn.execute(text(stmt), params)
        out = res.first() if res.returns_rows else None
    engine.dispose()
    return out


def _restamp_latest(*, seq, cluster, copy=False):
    """Give the latest snapshot (or a copy of it) another pass_seq/pass_cluster,
    as a restore from another cluster leaves it. cluster=None drops the key."""
    obs_expr = "jsonb_set(payload, '{observation,pass_seq}', to_jsonb(CAST(:seq AS text)))"
    obs_expr = (
        f"jsonb_set({obs_expr}, '{{observation,pass_cluster}}', to_jsonb(CAST(:cluster AS text)))"
        if cluster is not None
        else f"({obs_expr}) #- '{{observation,pass_cluster}}'"
    )
    latest = "(SELECT max(id) FROM xenon.account_snapshots)"
    if copy:
        _sql(
            "INSERT INTO xenon.account_snapshots (account, bankroll, peak_value, net_liquidation, payload, "
            "broker, account_env, broker_account) SELECT account, bankroll, peak_value, net_liquidation, "
            f"{obs_expr}, broker, account_env, broker_account FROM xenon.account_snapshots WHERE id = {latest}",
            seq=str(seq),
            cluster=cluster,
        )
    else:
        _sql(
            f"UPDATE xenon.account_snapshots SET payload = {obs_expr} WHERE id = {latest}",
            seq=str(seq),
            cluster=cluster,
        )


def _set_watermark(seq, cluster):
    from xenon.execution import gate4

    gate4.invalidate_view(SCOPE, "seed")
    _sql(
        "UPDATE xenon.service_health SET detail = :d WHERE service = :s",
        d=json.dumps({"reason": "restored", "seq": str(seq), "cluster": cluster}),
        s=gate4.VIEW_WATERMARK_SERVICE,
    )


def _xmax():
    return int(_sql("SELECT pg_snapshot_xmax(pg_current_snapshot())::text")[0])


@pytest.mark.parametrize("which", ["lower", "higher"])
def test_other_cluster_rows_never_rank_or_block(which, monkeypatch, client, broker_calls, fresh_quote):
    """(a)/(b) A restored 200-share snapshot and watermark from another
    cluster, with a seq below or far above every local one: a new local
    100-share pass publishes, is the one Gate 4 reads, and is not blocked."""
    _run_sync(monkeypatch, FakeSyncClient(positions=[Position(ACCOUNT, _stk(), 200, 500.0)]))
    foreign_seq = 1 if which == "lower" else _xmax() + 1_000_000
    _restamp_latest(seq=foreign_seq, cluster=FOREIGN_CLUSTER)
    _set_watermark(foreign_seq if which == "higher" else _xmax() + 1_000_000, FOREIGN_CLUSTER)

    _run_sync(monkeypatch, _shares_client(100))

    assert [leg["contracts"] for p in _latest_payload()["positions"] for leg in p["legs"]] == [100]
    two = client.post("/orders/place", json=_sell_call_body(f"sp-cluster-{which}-2", qty=2))
    assert two.status_code == 400, two.text
    assert two.json()["reason_code"] == "ETF_CALL_UNCOVERED"
    one = client.post("/orders/place", json=_sell_call_body(f"sp-cluster-{which}-1"))
    assert one.status_code == 200, one.text


def test_restored_row_stays_foreign_after_xids_advance(monkeypatch, client, broker_calls, fresh_quote):
    """(c) Review horizon case: a restored row with seq F just above xmax
    stays foreign after local xids pass F, so it never outranks the local
    snapshot nor beats the local invalidation."""
    from xenon.execution import gate4

    _run_sync(monkeypatch, _shares_client(100))
    restored = _xmax() + 20
    _restamp_latest(seq=restored, cluster=FOREIGN_CLUSTER, copy=True)
    gate4.invalidate_view(SCOPE, "unprovenanced_open_orders")
    for _ in range(40):
        gate4.next_pass_seq()  # each call commits one local xid
    assert _xmax() > restored

    resp = client.post("/orders/place", json=_sell_call_body("sp-horizon"))
    assert resp.status_code == 400, resp.text
    assert resp.json()["reason_code"] == "PORTFOLIO_SNAPSHOT_REQUIRED"


def test_local_invalidation_replaces_foreign_watermark(monkeypatch, client, broker_calls, fresh_quote):
    """(d) A stored other-cluster watermark (even with a huge seq) is replaced
    by a local invalidation, which then blocks the earlier local snapshot."""
    from xenon.execution import gate4

    _run_sync(monkeypatch, _shares_client(100))
    pass_seq = int(_latest_payload()["observation"]["pass_seq"])
    _set_watermark(_xmax() + 1_000_000, FOREIGN_CLUSTER)

    gate4.invalidate_view(SCOPE, "unprovenanced_open_orders")

    with gate4.get_sync_engine().connect() as conn:
        mark = gate4.view_watermark(conn, SCOPE)
    assert mark is not None and mark > pass_seq
    resp = client.post("/orders/place", json=_sell_call_body("sp-foreign-mark-replaced"))
    assert resp.status_code == 400, resp.text
    assert resp.json()["reason_code"] == "PORTFOLIO_SNAPSHOT_REQUIRED"


@pytest.mark.parametrize("cluster", [None, FOREIGN_CLUSTER], ids=["legacy-no-cluster", "other-cluster"])
def test_snapshot_without_local_cluster_is_incomplete(cluster, monkeypatch, client, broker_calls, fresh_quote):
    """(e) The only snapshot has no cluster (legacy) or another cluster's."""
    _run_sync(monkeypatch, _shares_client(100))
    seq = int(_latest_payload()["observation"]["pass_seq"])
    _restamp_latest(seq=seq, cluster=cluster)

    resp = client.post("/orders/place", json=_sell_call_body(f"sp-nocluster-{cluster}"))
    assert resp.status_code == 400, resp.text
    assert resp.json()["reason_code"] == "PORTFOLIO_SNAPSHOT_REQUIRED"


def test_pass_stamped_by_another_cluster_is_not_published(monkeypatch):
    """A pass whose seq came from another cluster (DB switched mid-pass)
    cannot be ordered here, so it is not published."""
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", ACCOUNT)
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    portfolio = _bare_portfolio(1000.0)
    portfolio["observation"]["pass_cluster"] = FOREIGN_CLUSTER

    ib_sync.save_portfolio(portfolio)

    assert _sql("SELECT count(*) FROM xenon.account_snapshots")[0] == 0
    assert _nav_rows() == []
