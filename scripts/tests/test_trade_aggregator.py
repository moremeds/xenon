from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import insert, select

from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_submissions, outbox, trades, wizard_combo_attempts, wizard_sessions
from xenon.execution.orders_store import record_fill
from xenon.execution.trade_aggregator import aggregate_trade_from_fills


BROKER_SCOPE = {
    "broker": "IB",
    "account_env": "paper",
    "broker_account": "DU123456",
}


def _insert_submission(submission_id: str, *, ticker: str = "AAPL", quantity: int = 100, action: str = "BUY") -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=submission_id,
                user_id="user-1",
                client_attempt_id=f"attempt-{submission_id}",
                ticker=ticker,
                security_type="STK",
                action=action,
                quantity=quantity,
                multiplier=1,
                con_id=265598,
                state="WORKING",
                submitted_at=datetime.now(timezone.utc),
                **BROKER_SCOPE,
            )
        )


def _insert_combo_attempt(attempt_id: str) -> None:
    now = datetime.now(timezone.utc)
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(wizard_sessions).values(
                session_id=f"session-{attempt_id}",
                ticker="AAPL",
                state="working",
                structure_name="Bull Call Spread",
                created_at=now,
                updated_at=now,
                **BROKER_SCOPE,
            )
        )
        conn.execute(
            insert(wizard_combo_attempts).values(
                attempt_id=attempt_id,
                session_id=f"session-{attempt_id}",
                ticker="AAPL",
                structure_name="Bull Call Spread",
                legs=[
                    {"conId": 111, "action": "BUY", "ratio": 1},
                    {"conId": 222, "action": "SELL", "ratio": 1},
                ],
                combo_contract={"action": "BUY", "quantity": 1},
                state="WORKING",
                submitted_at=now,
                updated_at=now,
                **BROKER_SCOPE,
            )
        )


def _fill(
    exec_id: str,
    *,
    submission_id: str | None = "sub-agg-001",
    combo_attempt_id: str | None = None,
    ticker: str = "AAPL",
    con_id: int | None = 265598,
    side: str = "BUY",
    qty: int = 100,
    price: str = "10.00",
    filled_at: datetime | None = None,
    metadata: dict | None = None,
) -> None:
    record_fill(
        exec_id=exec_id,
        submission_id=submission_id,
        combo_attempt_id=combo_attempt_id,
        perm_id="777",
        ib_order_id="42",
        con_id=con_id,
        ticker=ticker,
        side=side,
        qty=qty,
        price=Decimal(price),
        commission=Decimal("0"),
        filled_at=filled_at or datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc),
        metadata=metadata,
        **BROKER_SCOPE,
    )


def _trade_rows():
    engine = get_sync_engine()
    with engine.connect() as conn:
        return [row._mapping for row in conn.execute(select(trades).order_by(trades.c.id)).all()]


def _trade_closed_events():
    engine = get_sync_engine()
    with engine.connect() as conn:
        return [row._mapping for row in conn.execute(select(outbox).where(outbox.c.channel == "trade.closed")).all()]


def test_single_leg_two_partial_fills_yields_one_trades_row():
    _insert_submission("sub-agg-001", quantity=100)
    _fill("exec-open-1", qty=40, price="10.00")
    _fill("exec-open-2", qty=60, price="11.00", filled_at=datetime(2026, 4, 28, 14, 31, tzinfo=timezone.utc))

    aggregate_trade_from_fills(submission_id="sub-agg-001")

    rows = _trade_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["submission_id"] == "sub-agg-001"
    assert row["ticker"] == "AAPL"
    assert row["action"] == "BUY"
    assert row["quantity"] == 100
    assert row["entry_cost"] == Decimal("1060.0000")
    assert row["exit_cost"] is None
    assert row["realized_pnl"] is None
    assert row["state"] == "OPEN"
    assert row["opened_at"] == datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc)


def test_combo_legs_yield_one_trades_row_with_metadata_legs():
    _insert_combo_attempt("combo-agg-001")
    _fill(
        "exec-combo-buy",
        submission_id=None,
        combo_attempt_id="combo-agg-001",
        con_id=111,
        side="BUY",
        qty=1,
        price="4.00",
    )
    _fill(
        "exec-combo-sell",
        submission_id=None,
        combo_attempt_id="combo-agg-001",
        con_id=222,
        side="SELL",
        qty=1,
        price="1.50",
        filled_at=datetime(2026, 4, 28, 14, 31, tzinfo=timezone.utc),
    )

    aggregate_trade_from_fills(combo_attempt_id="combo-agg-001")

    rows = _trade_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["combo_attempt_id"] == "combo-agg-001"
    assert row["structure"] == "Bull Call Spread"
    assert row["action"] == "BUY"
    assert row["quantity"] == 1
    assert row["entry_cost"] == Decimal("2.5000")
    assert row["state"] == "OPEN"
    assert [leg["con_id"] for leg in row["metadata"]["legs"]] == [111, 222]
    assert [leg["side"] for leg in row["metadata"]["legs"]] == ["BUY", "SELL"]


def test_close_emits_trade_closed_outbox_once():
    _insert_submission("sub-close-001", quantity=100)
    _fill("exec-close-open", submission_id="sub-close-001", qty=100, price="10.00")
    aggregate_trade_from_fills(submission_id="sub-close-001")
    assert _trade_closed_events() == []

    _fill(
        "exec-close-exit",
        submission_id="sub-close-001",
        side="SELL",
        qty=100,
        price="12.00",
        filled_at=datetime(2026, 4, 28, 15, 30, tzinfo=timezone.utc),
    )
    aggregate_trade_from_fills(submission_id="sub-close-001")
    aggregate_trade_from_fills(submission_id="sub-close-001")

    rows = _trade_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["state"] == "CLOSED"
    assert row["closed_at"] == datetime(2026, 4, 28, 15, 30, tzinfo=timezone.utc)
    assert row["entry_cost"] == Decimal("1000.0000")
    assert row["exit_cost"] == Decimal("1200.0000")
    assert row["realized_pnl"] == Decimal("200.00")

    events = _trade_closed_events()
    assert len(events) == 1
    assert events[0]["source"] == "trade_aggregator"
    assert events[0]["payload"]["submission_id"] == "sub-close-001"
    assert events[0]["payload"]["realized_pnl"] == "200.00"


def test_aggregate_is_idempotent():
    _insert_submission("sub-idem-001", quantity=100)
    _fill("exec-idem-open", submission_id="sub-idem-001", qty=100, price="10.00")

    aggregate_trade_from_fills(submission_id="sub-idem-001")
    first = _trade_rows()[0]
    aggregate_trade_from_fills(submission_id="sub-idem-001")
    second = _trade_rows()[0]

    assert len(_trade_rows()) == 1
    assert second["id"] == first["id"]
    assert second["entry_cost"] == first["entry_cost"]
    assert _trade_closed_events() == []


def test_aggregate_handles_legacy_only_fills_without_submission():
    _fill(
        "legacy-exec-001",
        submission_id=None,
        con_id=265598,
        qty=10,
        price="20.00",
        metadata={"legacy_source": "trade_log_json", "legacy_id": "legacy-001"},
    )

    aggregate_trade_from_fills(legacy_id="legacy-001")

    rows = _trade_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["submission_id"] is None
    assert row["combo_attempt_id"] is None
    assert row["ticker"] == "AAPL"
    assert row["quantity"] == 10
    assert row["entry_cost"] == Decimal("200.0000")
    assert row["metadata"]["legacy_id"] == "legacy-001"
    assert row["metadata"]["legacy_source"] == "trade_log_json"
