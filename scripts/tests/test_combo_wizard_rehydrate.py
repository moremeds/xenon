"""Tests for combo_wizard.rehydrate — restart-safe reconcile for wizard sessions.

Covers the critical BAG per-leg aggregation rule from spec §13 and plan lines
416-422: IB reports combo fills as per-leg execution rows sharing one parent
`permId`. The rehydrate MUST group executions by permId, sum each leg's
`shares` against the expected ratio, and only mark FILLED when every leg
reached ratio * totalQuantity.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from xenon.db.queries import combo_wizard as cwq
from xenon.execution import orders_store
from xenon.execution.combo_wizard import rehydrate as wiz_rehydrate

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
    import xenon.db.engine as eng_mod

    monkeypatch.setattr(eng_mod, "_sync_engine", None)

    engine = _pg_engine()
    _cleanup(engine)
    engine.dispose()
    yield
    engine = _pg_engine()
    _cleanup(engine)
    engine.dispose()


# --------------------------------------------------------------------------
# Seed helper
# --------------------------------------------------------------------------


def _seed_session(
    db_path: Path,
    *,
    state: str = "working",
    perm_id: str = "P-1",
    ib_order_id: str = "IB-1",
    quantity: int = 1,
    legs: list[dict] | None = None,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
) -> tuple[str, str]:
    sid = f"wiz-{uuid.uuid4().hex[:12]}"
    aid = uuid.uuid4().hex
    legs = legs or [
        {"conId": 1001, "action": "BUY", "ratio": 1},
        {"conId": 1002, "action": "SELL", "ratio": 1},
    ]
    payload = {
        "symbol": "AAPL",
        "type": "combo",
        "action": "BUY",
        "quantity": quantity,
        "legs": legs,
    }
    now = datetime.now(timezone.utc)
    engine = _pg_engine()
    with engine.begin() as conn:
        cwq.create_session(
            conn,
            session_id=sid,
            ticker="AAPL",
            state=state,
            structure_name="Bull Call Spread",
            intent="OPEN",
            payload=payload,
            created_at=now,
            updated_at=now,
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
        )
        cwq.update_session(conn, sid, current_attempt_id=aid)
        cwq.create_attempt(
            conn,
            attempt_id=aid,
            session_id=sid,
            ticker="AAPL",
            structure_name="Bull Call Spread",
            ib_order_id=ib_order_id,
            perm_id=perm_id,
            limit_price=Decimal("2.50"),
            state="WORKING",
            submitted_at=now,
            updated_at=now,
            combo_contract={
                "client_attempt_id": f"wiz:{sid}:combo:{aid}",
                "intent": "OPEN",
                "price_basis": "MID",
                "action": "BUY",
                "quantity": quantity,
            },
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
        )
    engine.dispose()
    return sid, aid


def _fetch_combo_fill_rows():
    engine = _pg_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT exec_id, combo_attempt_id, con_id, side, qty, price "
                "FROM xenon.order_fills ORDER BY exec_id"
            )
        ).fetchall()
    engine.dispose()
    return rows


def _fetch_combo_trade_rows():
    engine = _pg_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT combo_attempt_id, structure, action, quantity, entry_cost, state, metadata "
                "FROM xenon.trades ORDER BY id"
            )
        ).fetchall()
    engine.dispose()
    return rows


def _fetch_combo_outbox(channel: str):
    engine = _pg_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT channel, source, payload FROM events.outbox WHERE channel=:channel ORDER BY id"),
            {"channel": channel},
        ).fetchall()
    engine.dispose()
    return rows


class _StubIB:
    def __init__(self, *, open_orders=None, executions=None, positions=None):
        self._open = open_orders or []
        self._execs = executions or []
        self._positions = positions or []

    def get_open_orders(self):
        return self._open

    def get_executions(self):
        return self._execs

    def get_positions(self):
        return self._positions


# ---------------------------------------------------------------------------
# BAG per-leg aggregation — the critical regression rule
# ---------------------------------------------------------------------------


def test_combo_rehydrate_partial_leg_fills_stays_partially_filled(tmp_path, monkeypatch):
    """Two leg executions (one partial, one full) must NOT mark the attempt
    FILLED — the session must remain PARTIALLY_FILLED."""
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid, aid = _seed_session(tmp_path, state="working", perm_id="P-1", quantity=1)

    # Order qty=1, ratio=1 per leg → each leg needs shares=1 to be FILLED.
    # Leg 1001: shares=1 (full). Leg 1002: shares=0 (none yet).
    # permId is shared across legs for a BAG.
    execs = [
        {"perm_id": "P-1", "con_id": 1001, "shares": 1, "avg_price": 3.10},
        # No execution row at all for leg 1002.
    ]
    ib = _StubIB(open_orders=[], executions=execs)

    decisions = wiz_rehydrate.rehydrate_combo_sessions(ib_client_factory=lambda: ib, db_path=tmp_path)

    assert len(decisions) == 1
    assert decisions[0].to_state == "PARTIALLY_FILLED"

    engine = _pg_engine()
    with engine.connect() as conn:
        state = conn.execute(
            text("SELECT state FROM xenon.wizard_sessions WHERE session_id = :sid"),
            {"sid": sid},
        ).fetchone()[0]
    engine.dispose()
    assert state.upper() == "PARTIALLY_FILLED"


def test_combo_rehydrate_all_legs_full_ratio_marks_filled(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid, _ = _seed_session(tmp_path, state="working", perm_id="P-2", quantity=2)

    # qty=2, ratio=1 each → each leg needs shares=2 to be FILLED.
    execs = [
        {"perm_id": "P-2", "con_id": 1001, "shares": 2, "avg_price": 3.10},
        {"perm_id": "P-2", "con_id": 1002, "shares": 2, "avg_price": 1.30},
    ]
    ib = _StubIB(executions=execs)

    decisions = wiz_rehydrate.rehydrate_combo_sessions(ib_client_factory=lambda: ib, db_path=tmp_path)

    assert decisions[0].to_state == "FILLED"

    engine = _pg_engine()
    with engine.connect() as conn:
        state = conn.execute(
            text("SELECT state FROM xenon.wizard_sessions WHERE session_id = :sid"),
            {"sid": sid},
        ).fetchone()[0]
    engine.dispose()
    assert state.upper() == "FILLED"


def test_combo_rehydrate_records_leg_fills_and_aggregates_trade(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    _sid, aid = _seed_session(
        tmp_path,
        state="working",
        perm_id="P-ledger",
        quantity=1,
        broker="IB",
        account_env="paper",
        broker_account="DU123456",
    )
    execs = [
        {
            "exec_id": "exec-combo-ledger-buy",
            "perm_id": "P-ledger",
            "ib_order_id": "IB-1",
            "con_id": 1001,
            "ticker": "AAPL",
            "side": "BOT",
            "shares": 1,
            "avg_price": 3.10,
            "time": datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc),
        },
        {
            "exec_id": "exec-combo-ledger-sell",
            "perm_id": "P-ledger",
            "ib_order_id": "IB-1",
            "con_id": 1002,
            "ticker": "AAPL",
            "side": "SLD",
            "shares": 1,
            "avg_price": 1.30,
            "time": datetime(2026, 4, 28, 14, 31, tzinfo=timezone.utc),
        },
    ]
    ib = _StubIB(executions=execs)

    wiz_rehydrate.rehydrate_combo_sessions(
        ib_client_factory=lambda: ib,
        broker="IB",
        account_env="paper",
        broker_account="DU123456",
    )
    wiz_rehydrate.rehydrate_combo_sessions(
        ib_client_factory=lambda: ib,
        broker="IB",
        account_env="paper",
        broker_account="DU123456",
    )

    fill_rows = _fetch_combo_fill_rows()
    assert len(fill_rows) == 2
    assert {row.exec_id for row in fill_rows} == {"exec-combo-ledger-buy", "exec-combo-ledger-sell"}
    assert {row.combo_attempt_id for row in fill_rows} == {aid}
    assert [row.side for row in fill_rows] == ["BUY", "SELL"]

    trade_rows = _fetch_combo_trade_rows()
    assert len(trade_rows) == 1
    trade = trade_rows[0]
    assert trade.combo_attempt_id == aid
    assert trade.structure == "Bull Call Spread"
    assert trade.action == "BUY"
    assert trade.quantity == 1
    assert Decimal(str(trade.entry_cost)) == Decimal("1.8000")
    assert trade.state == "OPEN"
    assert [leg["con_id"] for leg in trade.metadata["legs"]] == [1001, 1002]

    assert len(_fetch_combo_outbox("fill.recorded")) == 2


def test_combo_rehydrate_marks_attempt_filled_when_every_leg_filled(tmp_path, monkeypatch):
    """Session and attempt rows must agree after a filled BAG rehydrate."""
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid, aid = _seed_session(
        tmp_path,
        state="working",
        perm_id="P-attempt-filled",
        broker="IB",
        account_env="paper",
        broker_account="DU123456",
    )

    execs = [
        {
            "perm_id": "P-attempt-filled",
            "con_id": 1001,
            "exec_id": "exec-attempt-buy",
            "ticker": "AAPL",
            "side": "BOT",
            "shares": 1,
            "avg_price": 3.10,
            "time": datetime(2026, 4, 28, 14, 31, tzinfo=timezone.utc),
        },
        {
            "perm_id": "P-attempt-filled",
            "con_id": 1002,
            "exec_id": "exec-attempt-sell",
            "ticker": "AAPL",
            "side": "SLD",
            "shares": 1,
            "avg_price": 1.30,
            "time": datetime(2026, 4, 28, 14, 31, tzinfo=timezone.utc),
        },
    ]
    ib = _StubIB(executions=execs)

    decisions = wiz_rehydrate.rehydrate_combo_sessions(
        ib_client_factory=lambda: ib,
        broker="IB",
        account_env="paper",
        broker_account="DU123456",
    )

    assert decisions[0].to_state == "FILLED"

    engine = _pg_engine()
    with engine.connect() as conn:
        session_state = conn.execute(
            text("SELECT state FROM xenon.wizard_sessions WHERE session_id = :sid"),
            {"sid": sid},
        ).scalar_one()
        attempt_state = conn.execute(
            text("SELECT state FROM xenon.wizard_combo_attempts WHERE attempt_id = :aid"),
            {"aid": aid},
        ).scalar_one()
    engine.dispose()

    assert session_state == "filled"
    assert attempt_state == "FILLED"


def test_combo_rehydrate_one_leg_missing_stays_partially_filled(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    _seed_session(tmp_path, state="working", perm_id="P-3", quantity=1)

    execs = [
        {"perm_id": "P-3", "con_id": 1001, "shares": 1, "avg_price": 3.10},
        # Leg 1002 fully absent.
    ]
    ib = _StubIB(executions=execs)
    decisions = wiz_rehydrate.rehydrate_combo_sessions(ib_client_factory=lambda: ib, db_path=tmp_path)
    assert decisions[0].to_state == "PARTIALLY_FILLED"


def test_combo_rehydrate_overfill_on_one_leg_still_partially_filled(tmp_path, monkeypatch):
    """If one leg overfills (e.g. a ratio miscount at IB) but the other leg
    hasn't reached its target, we stay PARTIALLY_FILLED — we never claim
    FILLED on a mismatched combo."""
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    _seed_session(tmp_path, state="working", perm_id="P-4", quantity=1)

    execs = [
        {"perm_id": "P-4", "con_id": 1001, "shares": 3, "avg_price": 3.10},  # overfill
        {"perm_id": "P-4", "con_id": 1002, "shares": 0, "avg_price": 0.0},  # nothing
    ]
    ib = _StubIB(executions=execs)
    decisions = wiz_rehydrate.rehydrate_combo_sessions(ib_client_factory=lambda: ib, db_path=tmp_path)
    assert decisions[0].to_state == "PARTIALLY_FILLED"


def test_combo_rehydrate_ratio_2_for_one_leg(tmp_path, monkeypatch):
    """Ratio 1:2 (e.g. a ratio spread) — FILLED only when leg 2 has 2x shares."""
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    legs = [
        {"conId": 1001, "action": "BUY", "ratio": 1},
        {"conId": 1002, "action": "SELL", "ratio": 2},
    ]
    _seed_session(tmp_path, state="working", perm_id="P-5", quantity=1, legs=legs)

    # qty=1, ratio 1 & 2 → need shares 1 and 2 respectively.
    # Here leg2 only has 1 share → PARTIALLY_FILLED.
    execs = [
        {"perm_id": "P-5", "con_id": 1001, "shares": 1, "avg_price": 3.10},
        {"perm_id": "P-5", "con_id": 1002, "shares": 1, "avg_price": 1.30},
    ]
    ib = _StubIB(executions=execs)
    decisions = wiz_rehydrate.rehydrate_combo_sessions(ib_client_factory=lambda: ib, db_path=tmp_path)
    assert decisions[0].to_state == "PARTIALLY_FILLED"


def test_aggregate_leg_fills_reads_ib_fill_objects():
    fills = [
        SimpleNamespace(
            execution=SimpleNamespace(permId="P-obj", shares=1),
            contract=SimpleNamespace(conId=1001),
        ),
        SimpleNamespace(
            execution=SimpleNamespace(permId="P-obj", shares=2),
            contract=SimpleNamespace(conId=1002),
        ),
    ]

    assert wiz_rehydrate._aggregate_leg_fills(perm_id="P-obj", executions=fills) == {1001: 1, 1002: 2}


# ---------------------------------------------------------------------------
# Open-orders path: still WORKING
# ---------------------------------------------------------------------------


def test_combo_rehydrate_open_order_stays_working(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    _seed_session(tmp_path, state="working", perm_id="P-6")

    ib = _StubIB(open_orders=[{"perm_id": "P-6", "status": "Submitted"}], executions=[])

    decisions = wiz_rehydrate.rehydrate_combo_sessions(ib_client_factory=lambda: ib, db_path=tmp_path)
    assert decisions[0].to_state == "WORKING"


# ---------------------------------------------------------------------------
# Only rehydratable states are picked up
# ---------------------------------------------------------------------------


def test_combo_rehydrate_skips_terminal_states(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    _seed_session(tmp_path, state="aborted", perm_id="P-7")
    _seed_session(tmp_path, state="rejected", perm_id="P-8")
    _seed_session(tmp_path, state="filled", perm_id="P-9")  # FILLED not in rehydrate set

    ib = _StubIB()
    decisions = wiz_rehydrate.rehydrate_combo_sessions(ib_client_factory=lambda: ib, db_path=tmp_path)
    assert decisions == []


def test_combo_rehydrate_picks_up_protection_pending_session(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid, _ = _seed_session(tmp_path, state="protection_pending", perm_id="P-10")

    ib = _StubIB()
    decisions = wiz_rehydrate.rehydrate_combo_sessions(ib_client_factory=lambda: ib, db_path=tmp_path)
    # PROTECTION_PENDING sessions surface an "awaiting protection retry" decision.
    assert len(decisions) == 1
    assert decisions[0].detail.get("reason_code") in {
        "PROTECTION_RETRY_REQUIRED",
        "PROTECTION_REDRIVE",
    }
