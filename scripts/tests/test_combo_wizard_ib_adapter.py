"""Contract tests for combo_wizard.ib_adapter — concrete ib_insync-backed
adapter satisfying protect.py + rehydrate.py handles.

Uses stubbed ib_insync objects (no live broker). Covers:
- place_combo_tp builds BAG + LimitOrder with Order.action=SELL envelope for
  closing a long-debit combo; leg actions preserve the structure (no flip).
- Gate-4 naked-short guard: TP that would create naked short exposure is
  refused (route to Risk Alert), not silently placed.
- get_executions passes through Fill / Execution rows without dropping permId
  or conId.
- get_open_orders / get_positions delegate to IBClient.
- Signed target_price preserved end-to-end (no abs()).
- register_risk_alert persists a wizard session event (no broker-side order).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from xenon.db.queries import combo_wizard as cwq
from xenon.execution import orders_store
from xenon.execution.combo_wizard import ib_adapter as adapter_mod

# --------------------------------------------------------------------------
# Postgres helpers
# --------------------------------------------------------------------------

_TEST_DB_URL = os.environ.get(
    "DATABASE_URL_TEST",
    "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
)
_SYNC_URL = _TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def _pg_engine():
    return create_engine(_SYNC_URL, pool_pre_ping=True)


def _cleanup(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE xenon.wizard_events CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_combo_attempts CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_sessions CASCADE"))


@pytest.fixture(autouse=True)
def _setup_pg(monkeypatch):
    """Point get_sync_engine() at the test database and clean tables."""
    monkeypatch.setenv("DATABASE_URL", _SYNC_URL)
    # Reset the cached singleton so get_sync_engine() picks up the new URL.
    import xenon.db.engine as eng_mod

    monkeypatch.setattr(eng_mod, "_sync_engine", None)

    engine = _pg_engine()
    _cleanup(engine)
    engine.dispose()
    yield
    # Post-test cleanup
    engine = _pg_engine()
    _cleanup(engine)
    engine.dispose()


# --------------------------------------------------------------------------
# Stubs that look like ib_insync objects but don't import the real thing.
# --------------------------------------------------------------------------


class _StubOrder:
    def __init__(self, orderId=9001, permId=111, **kwargs):
        self.orderId = orderId
        self.permId = permId
        for k, v in kwargs.items():
            setattr(self, k, v)


class _StubOrderStatus:
    def __init__(self, status="PreSubmitted"):
        self.status = status


class _StubTrade:
    def __init__(self, order, orderStatus=None):
        self.order = order
        self.orderStatus = orderStatus or _StubOrderStatus()


class _StubExecution:
    def __init__(self, permId, shares, price=1.23, execId="e-1"):
        self.execId = execId
        self.permId = permId
        self.shares = shares
        self.price = price


class _StubContract:
    def __init__(self, conId, secType="OPT", symbol="AAPL"):
        self.conId = conId
        self.secType = secType
        self.symbol = symbol


class _StubFill:
    def __init__(self, contract, execution):
        self.contract = contract
        self.execution = execution


class _StubIBClient:
    """Mimics just the `IBClient` surface our adapter needs."""

    def __init__(
        self,
        *,
        qualified=None,
        trade=None,
        executions=None,
        open_orders=None,
        positions=None,
    ):
        self._qualified = qualified or []
        self._trade = trade
        self._executions = executions or []
        self._open_orders = open_orders or []
        self._positions = positions or []
        # capture what was sent to place_order
        self.placed: list[tuple] = []

    def qualify_contracts(self, *contracts):
        # Return one stub per input, mirroring real behavior.
        if self._qualified:
            return list(self._qualified)
        return [_StubContract(conId=1000 + i) for i, _ in enumerate(contracts)]

    def place_order(self, contract, order):
        self.placed.append((contract, order))
        if self._trade is None:
            return _StubTrade(_StubOrder())
        return self._trade

    def get_executions(self, exec_filter=None):
        return list(self._executions)

    def get_open_orders(self):
        return list(self._open_orders)

    def get_positions(self):
        return list(self._positions)


def _session_payload(*, uncovered_short_call: bool = False) -> dict:
    if uncovered_short_call:
        legs = [
            # Naked short call — no BUY call coverage
            {
                "conId": 2001,
                "action": "SELL",
                "ratio": 1,
                "right": "C",
                "strike": "100",
                "expiry": "20260620",
                "symbol": "AAPL",
            },
        ]
    else:
        legs = [
            {
                "conId": 1001,
                "action": "BUY",
                "ratio": 1,
                "right": "C",
                "strike": "100",
                "expiry": "20260620",
                "symbol": "AAPL",
            },
            {
                "conId": 1002,
                "action": "SELL",
                "ratio": 1,
                "right": "C",
                "strike": "105",
                "expiry": "20260620",
                "symbol": "AAPL",
            },
        ]
    return {
        "symbol": "AAPL",
        "type": "combo",
        "action": "BUY",
        "quantity": 2,
        "legs": legs,
    }


def _seed_session(tmp_path: Path, *, uncovered_short_call: bool = False) -> str:
    sid = f"wiz-{uuid.uuid4().hex[:12]}"
    payload = _session_payload(uncovered_short_call=uncovered_short_call)
    engine = _pg_engine()
    with engine.begin() as conn:
        cwq.create_session(
            conn,
            session_id=sid,
            ticker="AAPL",
            state="FILLED",
            structure_name="Bull Call Spread",
            intent="OPEN",
            payload=payload,
        )
    engine.dispose()
    return sid


# --------------------------------------------------------------------------
# place_combo_tp — BAG + LMT with SELL envelope for closing a long-debit combo
# --------------------------------------------------------------------------


def test_place_combo_tp_builds_bag_with_sell_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid = _seed_session(tmp_path)

    ib = _StubIBClient(
        qualified=[_StubContract(conId=1001), _StubContract(conId=1002)],
        trade=_StubTrade(_StubOrder(orderId=77, permId=42)),
    )
    a = adapter_mod.ComboWizardIbAdapter(ib)

    legs = [
        {
            "conId": 1001,
            "action": "BUY",
            "ratio": 1,
            "right": "C",
            "strike": "100",
            "expiry": "20260620",
            "symbol": "AAPL",
        },
        {
            "conId": 1002,
            "action": "SELL",
            "ratio": 1,
            "right": "C",
            "strike": "105",
            "expiry": "20260620",
            "symbol": "AAPL",
        },
    ]
    ack = a.place_combo_tp(
        session_id=sid,
        legs=legs,
        target_price=Decimal("3.50"),
        quantity=2,
    )

    assert ack["order_id"] == 77
    assert ack["perm_id"] == 42

    assert len(ib.placed) == 1
    contract, order = ib.placed[0]

    # BAG envelope
    assert contract.secType == "BAG"
    assert contract.symbol == "AAPL"
    # Closing a long-debit combo -> Order.action = SELL
    assert order.action == "SELL"
    assert order.totalQuantity == 2
    # Signed price preserved — no abs()
    assert float(order.lmtPrice) == 3.50
    # ComboLeg.actions preserve the structure (not flipped)
    leg_actions = [(cl.conId, cl.action) for cl in contract.comboLegs]
    assert leg_actions == [(1001, "BUY"), (1002, "SELL")]


def test_place_combo_tp_preserves_signed_negative_price(tmp_path, monkeypatch):
    """Credit combos carry negative signed prices. TP must preserve the sign."""
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid = _seed_session(tmp_path)

    ib = _StubIBClient(
        qualified=[_StubContract(conId=1001), _StubContract(conId=1002)],
        trade=_StubTrade(_StubOrder()),
    )
    a = adapter_mod.ComboWizardIbAdapter(ib)

    legs = _session_payload()["legs"]
    a.place_combo_tp(
        session_id=sid,
        legs=legs,
        target_price=Decimal("-1.25"),  # CREDIT
        quantity=1,
    )
    _, order = ib.placed[0]
    assert float(order.lmtPrice) == -1.25  # NOT abs()


# --------------------------------------------------------------------------
# Gate-4 guard: refuses TP that would create naked short exposure
# --------------------------------------------------------------------------


def test_place_combo_tp_refuses_naked_short(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid = _seed_session(tmp_path, uncovered_short_call=True)

    ib = _StubIBClient()
    a = adapter_mod.ComboWizardIbAdapter(ib)

    naked_legs = [
        {
            "conId": 2001,
            "action": "SELL",
            "ratio": 1,
            "right": "C",
            "strike": "100",
            "expiry": "20260620",
            "symbol": "AAPL",
        },
    ]
    with pytest.raises(adapter_mod.NakedShortGuardError):
        a.place_combo_tp(
            session_id=sid,
            legs=naked_legs,
            target_price=Decimal("2.0"),
            quantity=1,
        )
    assert ib.placed == []  # no order hit IB


# --------------------------------------------------------------------------
# get_executions / get_open_orders / get_positions pass-through
# --------------------------------------------------------------------------


def test_get_executions_preserves_permId_and_conId():
    fill = _StubFill(
        contract=_StubContract(conId=4242),
        execution=_StubExecution(permId=9999, shares=3, price=2.5, execId="e-1"),
    )
    ib = _StubIBClient(executions=[fill])
    a = adapter_mod.ComboWizardIbAdapter(ib)

    rows = a.get_executions()
    assert len(rows) == 1
    r = rows[0]
    # Must flatten Fill -> dict preserving permId AND conId for BAG per-leg
    # aggregation in rehydrate._aggregate_leg_fills.
    assert r["permId"] == 9999
    assert r["conId"] == 4242
    assert int(r["shares"]) == 3


def test_get_open_orders_and_positions_delegate():
    ib = _StubIBClient(open_orders=[{"perm_id": "A"}], positions=[{"con_id": 1}])
    a = adapter_mod.ComboWizardIbAdapter(ib)
    assert a.get_open_orders() == [{"perm_id": "A"}]
    assert a.get_positions() == [{"con_id": 1}]


# --------------------------------------------------------------------------
# register_risk_alert writes a session event, no broker order placed
# --------------------------------------------------------------------------


def test_register_risk_alert_persists_event_no_broker_order(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid = _seed_session(tmp_path)

    ib = _StubIBClient()
    a = adapter_mod.ComboWizardIbAdapter(ib)
    ack = a.register_risk_alert(
        session_id=sid,
        threshold=Decimal("-0.25"),  # signed
        polarity="CREDIT",
    )
    assert ack["virtual_id"]
    assert ib.placed == []

    engine = _pg_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT kind, detail FROM xenon.wizard_events WHERE session_id = :sid"),
            {"sid": sid},
        ).fetchall()
    engine.dispose()
    kinds = [r[0] for r in rows]
    assert "RISK_ALERT_REGISTERED" in kinds
    # signed threshold preserved in the event detail
    detail = next(r[1] for r in rows if r[0] == "RISK_ALERT_REGISTERED")
    # detail is already a dict (JSONB)
    assert detail["threshold"] == "-0.25"
    assert detail["polarity"] == "CREDIT"


# --------------------------------------------------------------------------
# permId=0 race: Trade.order.permId is 0 right after placeOrder; the real
# value only lands on the openOrder ack. Adapter must poll past the race.
# --------------------------------------------------------------------------


class _RacyOrder:
    """Order stub whose permId flips from 0 to the real value after N reads."""

    def __init__(self, orderId=77, real_perm_id=42, flips_after=1):
        self.orderId = orderId
        self._real = real_perm_id
        self._flips_after = flips_after
        self._reads = 0
        self._perm = 0

    @property
    def permId(self):
        val = self._perm
        self._reads += 1
        if self._reads >= self._flips_after:
            self._perm = self._real
        return val


class _RacyIBClient(_StubIBClient):
    """IBClient stub that exposes a `.ib.sleep` hook so the adapter's
    `_wait_for_perm_id` polling can advance without real blocking."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.sleep_calls: list[float] = []

        parent = self

        class _InnerIB:
            def sleep(self, secs):
                parent.sleep_calls.append(float(secs))

        self.ib = _InnerIB()


def test_place_combo_tp_polls_past_perm_id_race(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid = _seed_session(tmp_path)

    # Speed up polling for the test.
    monkeypatch.setattr(adapter_mod, "_PERM_ID_POLL_DEADLINE_S", 1.0)
    monkeypatch.setattr(adapter_mod, "_PERM_ID_POLL_INTERVAL_S", 0.01)

    # permId returns 0 on the first read (inside place_combo_tp's initial
    # getattr) and then flips to 42 on the next read after one sleep tick.
    racy_order = _RacyOrder(orderId=77, real_perm_id=42, flips_after=2)
    trade = _StubTrade(racy_order)
    ib = _RacyIBClient(
        qualified=[_StubContract(conId=1001), _StubContract(conId=1002)],
        trade=trade,
    )
    a = adapter_mod.ComboWizardIbAdapter(ib)

    legs = _session_payload()["legs"]
    ack = a.place_combo_tp(
        session_id=sid,
        legs=legs,
        target_price=Decimal("3.50"),
        quantity=2,
    )
    # Real permId surfaces (not 0, not None)
    assert ack["perm_id"] == 42
    assert ack["order_id"] == 77
    # We slept at least once to cross the race
    assert len(ib.sleep_calls) >= 1


def test_place_combo_tp_returns_none_perm_id_on_deadline(tmp_path, monkeypatch):
    """If permId stays 0 past the deadline, return None (caller falls back to
    order_id via `ack.get("perm_id") or ack.get("order_id")`)."""
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid = _seed_session(tmp_path)

    monkeypatch.setattr(adapter_mod, "_PERM_ID_POLL_DEADLINE_S", 0.05)
    monkeypatch.setattr(adapter_mod, "_PERM_ID_POLL_INTERVAL_S", 0.01)

    # Never flips — permId stays 0 forever.
    class _ZeroOrder:
        orderId = 77
        permId = 0

    trade = _StubTrade(_ZeroOrder())
    ib = _RacyIBClient(
        qualified=[_StubContract(conId=1001), _StubContract(conId=1002)],
        trade=trade,
    )
    a = adapter_mod.ComboWizardIbAdapter(ib)

    ack = a.place_combo_tp(
        session_id=sid,
        legs=_session_payload()["legs"],
        target_price=Decimal("3.50"),
        quantity=2,
    )
    assert ack["perm_id"] is None
    assert ack["order_id"] == 77


# --------------------------------------------------------------------------
# IB error 201 classification: terminal broker reject surfaces as
# NakedShortGuardError so protect.py's retry loop doesn't churn on it.
# --------------------------------------------------------------------------


class _ExplodingIBClient(_StubIBClient):
    def __init__(self, *, exc, **kw):
        super().__init__(**kw)
        self._exc = exc

    def place_order(self, contract, order):
        self.placed.append((contract, order))
        raise self._exc


def test_place_combo_tp_classifies_ib_error_201_as_naked_short(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid = _seed_session(tmp_path)

    # Mirror the IBClient.place_order wrapping: generic exception string
    # carrying "error 201" — our classifier keys off the message payload
    # since IBOrderError does not preserve a .code attribute today.
    exc = RuntimeError("Failed to place order: IB error 201: Order rejected - contract not allowed for short")
    ib = _ExplodingIBClient(
        exc=exc,
        qualified=[_StubContract(conId=1001), _StubContract(conId=1002)],
    )
    a = adapter_mod.ComboWizardIbAdapter(ib)

    with pytest.raises(adapter_mod.NakedShortGuardError):
        a.place_combo_tp(
            session_id=sid,
            legs=_session_payload()["legs"],
            target_price=Decimal("3.50"),
            quantity=2,
        )

    # Event recorded matches existing PROTECTION_TP_REFUSED_* style
    engine = _pg_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT kind FROM xenon.wizard_events WHERE session_id = :sid"),
            {"sid": sid},
        ).fetchall()
    engine.dispose()
    kinds = [r[0] for r in rows]
    assert "PROTECTION_TP_REFUSED_BROKER_201" in kinds


def test_place_combo_tp_classifies_ib_error_201_via_code_attribute(tmp_path, monkeypatch):
    """If the underlying exception carries a .code attribute (e.g. some
    ib_insync error shapes), classify by code without relying on message."""
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid = _seed_session(tmp_path)

    class _Coded(RuntimeError):
        code = 201

    exc = _Coded("Failed to place order: rejected")
    ib = _ExplodingIBClient(
        exc=exc,
        qualified=[_StubContract(conId=1001), _StubContract(conId=1002)],
    )
    a = adapter_mod.ComboWizardIbAdapter(ib)

    with pytest.raises(adapter_mod.NakedShortGuardError):
        a.place_combo_tp(
            session_id=sid,
            legs=_session_payload()["legs"],
            target_price=Decimal("3.50"),
            quantity=2,
        )


def test_place_combo_tp_reraises_non_201_errors(tmp_path, monkeypatch):
    """Non-201 broker errors must re-raise unchanged so protect.py's retry
    loop still applies (network blips, pacing violations, etc.)."""
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid = _seed_session(tmp_path)

    exc = RuntimeError("Failed to place order: IB error 504: Not connected")
    ib = _ExplodingIBClient(
        exc=exc,
        qualified=[_StubContract(conId=1001), _StubContract(conId=1002)],
    )
    a = adapter_mod.ComboWizardIbAdapter(ib)

    with pytest.raises(RuntimeError, match="504"):
        a.place_combo_tp(
            session_id=sid,
            legs=_session_payload()["legs"],
            target_price=Decimal("3.50"),
            quantity=2,
        )
    # Must NOT be a NakedShortGuardError
    try:
        a.place_combo_tp(
            session_id=sid,
            legs=_session_payload()["legs"],
            target_price=Decimal("3.50"),
            quantity=2,
        )
    except adapter_mod.NakedShortGuardError:
        pytest.fail("non-201 error incorrectly classified as naked-short")
    except RuntimeError:
        pass


# --------------------------------------------------------------------------
# Gate-4 contract tests: short risk reversal + 1x2 ratio must BLOCK
# (per src/xenon/CLAUDE.md Naked Short Protection table).
# --------------------------------------------------------------------------


def test_place_combo_tp_refuses_short_risk_reversal(tmp_path, monkeypatch):
    """Short risk reversal: SELL Call + BUY Put. The long put does NOT cover
    the short call — this is naked short call exposure. Must BLOCK."""
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid = _seed_session(tmp_path)

    ib = _StubIBClient()
    a = adapter_mod.ComboWizardIbAdapter(ib)

    legs = [
        {
            "conId": 3001,
            "action": "SELL",
            "ratio": 1,
            "right": "C",
            "strike": "110",
            "expiry": "20260620",
            "symbol": "AAPL",
        },
        {
            "conId": 3002,
            "action": "BUY",
            "ratio": 1,
            "right": "P",
            "strike": "90",
            "expiry": "20260620",
            "symbol": "AAPL",
        },
    ]

    with pytest.raises(adapter_mod.NakedShortGuardError):
        a.place_combo_tp(
            session_id=sid,
            legs=legs,
            target_price=Decimal("1.50"),
            quantity=1,
        )
    assert ib.placed == []

    # PROTECTION_TP_REFUSED_ADAPTER event persisted
    engine = _pg_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT kind FROM xenon.wizard_events WHERE session_id = :sid"),
            {"sid": sid},
        ).fetchall()
    engine.dispose()
    assert "PROTECTION_TP_REFUSED_ADAPTER" in [r[0] for r in rows]


def test_place_combo_tp_refuses_1x2_ratio_spread(tmp_path, monkeypatch):
    """1x2 ratio spread: BUY 1 Call + SELL 2 Calls. One uncovered short call
    tail (sellCallRatio - buyCallRatio = 1). Must BLOCK."""
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid = _seed_session(tmp_path)

    ib = _StubIBClient()
    a = adapter_mod.ComboWizardIbAdapter(ib)

    legs = [
        {
            "conId": 4001,
            "action": "BUY",
            "ratio": 1,
            "right": "C",
            "strike": "100",
            "expiry": "20260620",
            "symbol": "AAPL",
        },
        {
            "conId": 4002,
            "action": "SELL",
            "ratio": 2,
            "right": "C",
            "strike": "110",
            "expiry": "20260620",
            "symbol": "AAPL",
        },
    ]

    with pytest.raises(adapter_mod.NakedShortGuardError):
        a.place_combo_tp(
            session_id=sid,
            legs=legs,
            target_price=Decimal("0.75"),
            quantity=1,
        )
    assert ib.placed == []

    engine = _pg_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT kind FROM xenon.wizard_events WHERE session_id = :sid"),
            {"sid": sid},
        ).fetchall()
    engine.dispose()
    assert "PROTECTION_TP_REFUSED_ADAPTER" in [r[0] for r in rows]
