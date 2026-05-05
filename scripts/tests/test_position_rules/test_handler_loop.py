"""PositionRulesHandler loop semantics. Spec §8."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_protection import insert_pending_arm
from xenon.execution.account_scope import AccountScope
from xenon.execution.brackets.executor.ib_executor import PlaceResult
from xenon.execution.brackets.executor.marks import Quote
from xenon.monitor_daemon.handlers.position_rules import PositionRulesHandler


@pytest.fixture
def engine():
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key LIKE 'TEST::%'"))
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'TEST::%'"))
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key LIKE 'TEST::%'"))
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key LIKE 'TEST::%'"))


def _stock_descriptor(symbol="AAPL", price=100.0, con_id=12345):
    return {
        "asset_class": "stock",
        "opened_at": "2026-05-04T14:00:00Z",
        "source": "fastapi_orders_place",
        "anchor_price": price,
        "anchor_currency": "USD",
        "opened_qty": 100,
        "protected_qty": 100,
        "multiplier": 1,
        "qty_unit": "share",
        "legs": [
            {
                "sec_type": "STK",
                "symbol": symbol,
                "action": "BUY",
                "ratio": 1,
                "fill_price": price,
                "con_id": con_id,
            }
        ],
    }


def _long_option_descriptor(symbol="SPY", price=0.01, con_id=875935117):
    return {
        "asset_class": "long_option",
        "opened_at": "2026-05-04T14:00:00Z",
        "source": "fastapi_orders_place",
        "anchor_price": price,
        "anchor_currency": "USD",
        "opened_qty": 1,
        "protected_qty": 1,
        "multiplier": 100,
        "qty_unit": "contract",
        "legs": [
            {
                "sec_type": "OPT",
                "symbol": symbol,
                "expiry": "20260505",
                "strike": 740.0,
                "right": "C",
                "action": "BUY",
                "ratio": 1,
                "fill_price": price,
                "con_id": con_id,
            }
        ],
    }


def _credit_spread_descriptor(symbol="SPY"):
    return {
        "asset_class": "credit_spread",
        "opened_at": "2026-05-04T14:00:00Z",
        "source": "combo_wizard",
        "anchor_price": -1.0,
        "anchor_currency": "USD",
        "opened_qty": 1,
        "protected_qty": 1,
        "multiplier": 100,
        "qty_unit": "contract",
        "credit_received": 1.0,
        "short_strike": 580.0,
        "short_right": "P",
        "legs": [
            {
                "sec_type": "OPT",
                "symbol": symbol,
                "expiry": "20260516",
                "strike": 580.0,
                "right": "P",
                "action": "SELL",
                "ratio": 1,
                "fill_price": 1.40,
                "con_id": 58001,
            },
            {
                "sec_type": "OPT",
                "symbol": symbol,
                "expiry": "20260516",
                "strike": 575.0,
                "right": "P",
                "action": "BUY",
                "ratio": 1,
                "fill_price": 0.40,
                "con_id": 57501,
            },
        ],
    }


def _scope():
    return AccountScope(broker="IB", account_env="paper", broker_account="DU1234567")


def _quote_side_effect(quotes: dict[int, Quote], *, fallback_symbol: str, fallback_price: float):
    def fetch(contract, snapshot=False, generic_ticks=""):
        return quotes.get(getattr(contract, "conId", None)) or Quote(
            symbol=fallback_symbol,
            price=fallback_price,
            ts=datetime.now(timezone.utc),
        )

    return fetch


class _Ticker:
    def __init__(self, *, price: float, symbol: str = "TEST"):
        self.symbol = symbol
        self.last = price
        self.close = price
        self.bid = price - 0.01
        self.ask = price + 0.01
        self.time = datetime.now(timezone.utc)

    def marketPrice(self):
        return self.last


def test_pending_arm_transitions_to_armed(engine):
    descriptor = _stock_descriptor()
    pid = insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::AAPL_HANDLER",
        position_descriptor=descriptor,
        asset_class="stock",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    executor = MagicMock()
    executor.attach_native_stp.return_value = PlaceResult(
        perm_id=999,
        ib_order_id=1,
        status="Submitted",
        raw={},
    )
    ib_client = MagicMock()
    ib_client.get_order_state.return_value = {"status": "Submitted", "permId": 999}
    ib_client.connected = True
    ib_client.get_quote.return_value = Quote(symbol="AAPL", price=100.0, ts=datetime.now(timezone.utc))

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client, scope=_scope())
    handler.execute()

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT state, native_order_perm_id FROM xenon.position_protection "
                "WHERE protection_id = :pid"
            ),
            {"pid": pid},
        ).first()
    assert row.state == "ARMED"
    assert row.native_order_perm_id == 999
    assert executor.attach_native_stp.call_args.kwargs["order_ref"] == f"xenon-pr-native-{pid}"


def test_armed_mark_fetch_uses_contract_object_and_normalizes_ticker(engine):
    descriptor = _stock_descriptor(symbol="IBM", price=100.0, con_id=11111)
    pid = insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::IBM_CONTRACT_QUOTE",
        position_descriptor=descriptor,
        asset_class="stock",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE xenon.position_protection SET state='ARMED', armed_at=NOW() WHERE protection_id=:pid"),
            {"pid": pid},
        )

    class StrictIB:
        connected = True

        def __init__(self):
            self.contracts = []

        def positions(self):
            return [{"symbol": "IBM", "qty": 100, "con_id": 11111}]

        def get_quote(self, contract, snapshot=False, generic_ticks=""):
            self.contracts.append((contract, snapshot, generic_ticks))
            return _Ticker(price=91.0, symbol="IBM")

        def find_open_orders_by_order_ref(self, order_ref):
            return []

        def find_executions_by_order_ref(self, order_ref):
            return []

    executor = MagicMock()
    executor.flatten_mkt.return_value = PlaceResult(perm_id=12340, ib_order_id=1, status="Submitted", raw={})
    ib_client = StrictIB()

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client, scope=_scope())
    handler.execute()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT state FROM xenon.position_protection WHERE protection_id=:pid"),
            {"pid": pid},
        ).first()

    assert row.state == "TRIGGERED"
    assert len(ib_client.contracts) == 1
    contract, snapshot, generic_ticks = ib_client.contracts[0]
    assert getattr(contract, "conId") == 11111
    assert snapshot is True
    assert generic_ticks == ""
    executor.flatten_mkt.assert_called_once()


def test_armed_below_threshold_triggers_and_claims(engine):
    descriptor = _stock_descriptor(symbol="MSFT", price=100.0, con_id=22222)
    pid = insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::MSFT_HANDLER",
        position_descriptor=descriptor,
        asset_class="stock",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE xenon.position_protection SET state='ARMED', armed_at=NOW() WHERE protection_id=:pid"),
            {"pid": pid},
        )

    executor = MagicMock()
    executor.flatten_mkt.return_value = PlaceResult(perm_id=12345, ib_order_id=1, status="Submitted", raw={})
    ib_client = MagicMock()
    ib_client.connected = True
    ib_client.get_quote.return_value = Quote(symbol="MSFT", price=91.0, ts=datetime.now(timezone.utc))
    ib_client.find_open_orders_by_order_ref.return_value = []
    ib_client.find_executions_by_order_ref.return_value = []
    ib_client.positions.return_value = [{"symbol": "MSFT", "qty": 100, "con_id": 22222}]

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client, scope=_scope())
    handler.execute()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT state FROM xenon.position_protection WHERE protection_id=:pid"),
            {"pid": pid},
        ).first()
        claim = conn.execute(
            text(
                "SELECT status, broker_perm_id, order_ref FROM xenon.position_close_claims "
                "WHERE position_key='TEST::MSFT_HANDLER'"
            )
        ).first()
    assert row.state == "TRIGGERED"
    assert claim.status == "SUBMITTED"
    assert claim.broker_perm_id == 12345
    executor.flatten_mkt.assert_called_once()
    assert executor.flatten_mkt.call_args.kwargs["order_ref"] == claim.order_ref


def test_transient_close_error_reuses_same_pending_claim(engine):
    descriptor = _stock_descriptor(symbol="AMD", price=100.0, con_id=22446)
    pid = insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::AMD_RETRY_HANDLER",
        position_descriptor=descriptor,
        asset_class="stock",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE xenon.position_protection SET state='ARMED', armed_at=NOW() WHERE protection_id=:pid"),
            {"pid": pid},
        )

    executor = MagicMock()
    executor.flatten_mkt.side_effect = [
        RuntimeError("subprocess timeout"),
        PlaceResult(perm_id=12346, ib_order_id=2, status="Submitted", raw={}),
    ]
    ib_client = MagicMock()
    ib_client.connected = True
    ib_client.get_quote.return_value = Quote(symbol="AMD", price=91.0, ts=datetime.now(timezone.utc))
    ib_client.find_open_orders_by_order_ref.return_value = []
    ib_client.find_executions_by_order_ref.return_value = []
    ib_client.positions.return_value = [{"symbol": "AMD", "qty": 100, "con_id": 22446}]

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client, scope=_scope())
    handler.execute()

    with engine.connect() as conn:
        after_first = conn.execute(
            text("SELECT state FROM xenon.position_protection WHERE protection_id=:pid"),
            {"pid": pid},
        ).first()
        pending_claim = conn.execute(
            text(
                """
                SELECT claim_id, status, attempts, claimed_by_protection_id
                FROM xenon.position_close_claims
                WHERE position_key='TEST::AMD_RETRY_HANDLER'
                """
            )
        ).one()
    assert after_first.state == "ARMED"
    assert pending_claim.status == "PENDING"
    assert pending_claim.attempts == 1
    assert pending_claim.claimed_by_protection_id == pid

    handler.execute()

    with engine.connect() as conn:
        after_second = conn.execute(
            text("SELECT state FROM xenon.position_protection WHERE protection_id=:pid"),
            {"pid": pid},
        ).first()
        submitted_claim = conn.execute(
            text(
                """
                SELECT claim_id, status, broker_perm_id
                FROM xenon.position_close_claims
                WHERE position_key='TEST::AMD_RETRY_HANDLER'
                """
            )
        ).one()

    assert after_second.state == "TRIGGERED"
    assert submitted_claim.claim_id == pending_claim.claim_id
    assert submitted_claim.status == "SUBMITTED"
    assert submitted_claim.broker_perm_id == 12346
    assert executor.flatten_mkt.call_count == 2


def test_two_rules_same_position_only_one_mkt(engine):
    descriptor = _stock_descriptor(symbol="GOOG", price=100.0, con_id=33333)
    insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::GOOG_HANDLER",
        position_descriptor=descriptor,
        asset_class="stock",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::GOOG_HANDLER",
        position_descriptor=descriptor,
        asset_class="stock",
        rule_kind="trailing_tp",
        config={"trail_pct": 0.05, "activation_pct": 0.0, "anchor": "mfe"},
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE xenon.position_protection "
                "SET state='ARMED', state_data='{\"mfe\": 110.0}'::jsonb "
                "WHERE position_key='TEST::GOOG_HANDLER'"
            )
        )

    executor = MagicMock()
    executor.flatten_mkt.return_value = PlaceResult(perm_id=12345, ib_order_id=1, status="Submitted", raw={})
    ib_client = MagicMock()
    ib_client.connected = True
    ib_client.get_quote.return_value = Quote(symbol="GOOG", price=91.0, ts=datetime.now(timezone.utc))
    ib_client.find_open_orders_by_order_ref.return_value = []
    ib_client.find_executions_by_order_ref.return_value = []
    ib_client.positions.return_value = [{"symbol": "GOOG", "qty": 100, "con_id": 33333}]

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client, scope=_scope())
    handler.execute()

    assert executor.flatten_mkt.call_count == 1
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT state FROM xenon.position_protection
                WHERE position_key='TEST::GOOG_HANDLER'
                ORDER BY protection_id
                """
            )
        ).all()
    assert sorted(row.state for row in rows) == ["SUPERSEDED", "TRIGGERED"]


def test_alert_only_rule_does_not_submit_close_claim(engine):
    descriptor = _stock_descriptor(symbol="META", price=100.0, con_id=77777)
    pid = insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::META_ALERT_ONLY",
        position_descriptor=descriptor,
        asset_class="stock",
        rule_kind="combo_tp_alert",
        config={"threshold_pct": 0.50, "auto_place": False, "min_realert_interval_s": 3600},
    )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE xenon.position_protection SET state='ARMED', armed_at=NOW() WHERE protection_id=:pid"),
            {"pid": pid},
        )

    executor = MagicMock()
    ib_client = MagicMock()
    ib_client.connected = True
    ib_client.get_quote.return_value = Quote(symbol="META", price=160.0, ts=datetime.now(timezone.utc))
    ib_client.find_open_orders_by_order_ref.return_value = []
    ib_client.find_executions_by_order_ref.return_value = []
    ib_client.positions.return_value = [{"symbol": "META", "qty": 100, "con_id": 77777}]

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client, scope=_scope())
    handler.execute()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT state, state_data FROM xenon.position_protection WHERE protection_id=:pid"),
            {"pid": pid},
        ).first()
        claim_count = conn.execute(
            text("SELECT COUNT(*) FROM xenon.position_close_claims WHERE position_key='TEST::META_ALERT_ONLY'")
        ).scalar_one()
        alert = conn.execute(
            text(
                """
                SELECT payload FROM events.outbox
                WHERE channel = 'position_rule.transition'
                  AND source = 'position_rules_alert'
                  AND payload->>'position_key' = 'TEST::META_ALERT_ONLY'
                ORDER BY id DESC
                LIMIT 1
                """
            )
        ).first()

    assert row.state == "ARMED"
    assert row.state_data["last_alert_at"] is not None
    assert claim_count == 0
    assert alert is not None
    assert alert.payload["reason"] == "alert_only_threshold_crossed"
    executor.flatten_mkt.assert_not_called()


def test_credit_spread_take_profit_uses_net_debit_and_combo_close(engine):
    descriptor = _credit_spread_descriptor()
    pid = insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::SPY_CREDIT_SPREAD",
        position_descriptor=descriptor,
        asset_class="credit_spread",
        rule_kind="take_profit_fixed",
        config={"close_at_credit_pct": 0.50, "anchor": "synthetic_mark"},
    )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE xenon.position_protection SET state='ARMED', armed_at=NOW() WHERE protection_id=:pid"),
            {"pid": pid},
        )

    executor = MagicMock()
    executor.flatten_combo_mkt.return_value = PlaceResult(perm_id=24680, ib_order_id=2, status="Submitted", raw={})
    ib_client = MagicMock()
    ib_client.connected = True
    quotes = {
        58001: Quote(symbol="SPY", price=0.80, ts=datetime.now(timezone.utc)),
        57501: Quote(symbol="SPY", price=0.30, ts=datetime.now(timezone.utc)),
    }
    ib_client.qualify_contract.side_effect = lambda contract: contract
    ib_client.get_quote.side_effect = _quote_side_effect(quotes, fallback_symbol="SPY", fallback_price=579.0)
    ib_client.find_open_orders_by_order_ref.return_value = []
    ib_client.find_executions_by_order_ref.return_value = []
    ib_client.positions.return_value = [
        {"symbol": "SPY", "qty": -1, "con_id": 58001},
        {"symbol": "SPY", "qty": 1, "con_id": 57501},
    ]

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client, scope=_scope())
    handler.execute()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT state FROM xenon.position_protection WHERE protection_id=:pid"),
            {"pid": pid},
        ).first()
        claim = conn.execute(
            text(
                "SELECT status, broker_perm_id, order_ref FROM xenon.position_close_claims "
                "WHERE position_key='TEST::SPY_CREDIT_SPREAD'"
            )
        ).first()
    assert row.state == "TRIGGERED"
    assert claim.status == "SUBMITTED"
    assert claim.broker_perm_id == 24680
    executor.flatten_combo_mkt.assert_called_once()
    combo_args = executor.flatten_combo_mkt.call_args.kwargs
    assert combo_args["legs"] == descriptor["legs"]
    assert combo_args["qty"] == 1
    assert combo_args["order_ref"] == claim.order_ref
    executor.flatten_mkt.assert_not_called()


def test_combo_close_caps_qty_to_minimum_leg_position(engine):
    descriptor = _credit_spread_descriptor(symbol="IWM")
    descriptor["opened_qty"] = 3
    descriptor["protected_qty"] = 3
    pid = insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::IWM_CREDIT_SPREAD",
        position_descriptor=descriptor,
        asset_class="credit_spread",
        rule_kind="take_profit_fixed",
        config={"close_at_credit_pct": 0.50, "anchor": "synthetic_mark"},
    )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE xenon.position_protection SET state='ARMED', armed_at=NOW() WHERE protection_id=:pid"),
            {"pid": pid},
        )

    executor = MagicMock()
    executor.flatten_combo_mkt.return_value = PlaceResult(perm_id=24681, ib_order_id=3, status="Submitted", raw={})
    ib_client = MagicMock()
    ib_client.connected = True
    quotes = {
        58001: Quote(symbol="IWM", price=0.80, ts=datetime.now(timezone.utc)),
        57501: Quote(symbol="IWM", price=0.30, ts=datetime.now(timezone.utc)),
    }
    ib_client.qualify_contract.side_effect = lambda contract: contract
    ib_client.get_quote.side_effect = _quote_side_effect(quotes, fallback_symbol="IWM", fallback_price=220.0)
    ib_client.find_open_orders_by_order_ref.return_value = []
    ib_client.find_executions_by_order_ref.return_value = []
    ib_client.positions.return_value = [
        {"symbol": "IWM", "qty": -3, "con_id": 58001},
        {"symbol": "IWM", "qty": 1, "con_id": 57501},
    ]

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client, scope=_scope())
    handler.execute()

    executor.flatten_combo_mkt.assert_called_once()
    assert executor.flatten_combo_mkt.call_args.kwargs["qty"] == 1


def test_combo_close_abandons_when_any_leg_is_flat(engine):
    descriptor = _credit_spread_descriptor(symbol="DIA")
    pid = insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::DIA_CREDIT_SPREAD",
        position_descriptor=descriptor,
        asset_class="credit_spread",
        rule_kind="take_profit_fixed",
        config={"close_at_credit_pct": 0.50, "anchor": "synthetic_mark"},
    )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE xenon.position_protection SET state='ARMED', armed_at=NOW() WHERE protection_id=:pid"),
            {"pid": pid},
        )

    executor = MagicMock()
    ib_client = MagicMock()
    ib_client.connected = True
    quotes = {
        58001: Quote(symbol="DIA", price=0.80, ts=datetime.now(timezone.utc)),
        57501: Quote(symbol="DIA", price=0.30, ts=datetime.now(timezone.utc)),
    }
    ib_client.qualify_contract.side_effect = lambda contract: contract
    ib_client.get_quote.side_effect = _quote_side_effect(quotes, fallback_symbol="DIA", fallback_price=390.0)
    ib_client.find_open_orders_by_order_ref.return_value = []
    ib_client.find_executions_by_order_ref.return_value = []
    ib_client.positions.return_value = [{"symbol": "DIA", "qty": -1, "con_id": 58001}]

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client, scope=_scope())
    handler.execute()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT state FROM xenon.position_protection WHERE protection_id=:pid"),
            {"pid": pid},
        ).one()
        claim = conn.execute(
            text("SELECT status, last_error FROM xenon.position_close_claims WHERE position_key='TEST::DIA_CREDIT_SPREAD'")
        ).one()
    assert row.state == "CANCELED"
    assert claim.status == "ABANDONED"
    assert claim.last_error == "position_already_flat"
    executor.flatten_combo_mkt.assert_not_called()


def test_armed_with_native_perm_id_detects_external_cancel(engine):
    descriptor = _stock_descriptor(symbol="NVDA", price=100.0, con_id=44444)
    pid = insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::NVDA_HANDLER",
        position_descriptor=descriptor,
        asset_class="stock",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE xenon.position_protection SET state='ARMED', "
                "native_order_perm_id=777, armed_at=NOW() WHERE protection_id=:pid"
            ),
            {"pid": pid},
        )

    executor = MagicMock()
    ib_client = MagicMock()
    ib_client.connected = True
    ib_client.get_order_state.return_value = {"status": "Cancelled", "permId": 777}
    ib_client.positions.return_value = [{"symbol": "NVDA", "qty": 100, "con_id": 44444}]

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client, scope=_scope())
    handler.execute()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT state FROM xenon.position_protection WHERE protection_id=:pid"),
            {"pid": pid},
        ).first()
    assert row.state == "CANCELED"
    executor.flatten_mkt.assert_not_called()


def test_handler_connects_ib_client_and_uses_get_positions_for_native_liveness(engine):
    descriptor = _stock_descriptor(symbol="CRM", price=100.0, con_id=99991)
    pid = insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::CRM_NATIVE_CONNECT",
        position_descriptor=descriptor,
        asset_class="stock",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE xenon.position_protection SET state='ARMED', "
                "native_order_perm_id=888, armed_at=NOW() WHERE protection_id=:pid"
            ),
            {"pid": pid},
        )

    class Position:
        def __init__(self):
            self.contract = type("Contract", (), {"symbol": "CRM", "conId": 99991})()
            self.position = 100

    class DisconnectedIB:
        connected = False

        def __init__(self):
            self.connect_calls = []

        def is_connected(self):
            return self.connected

        def connect(self, **kwargs):
            self.connect_calls.append(kwargs)
            self.connected = True

        def get_positions(self):
            return [Position()]

        def get_order_state(self, perm_id):
            return {"status": "Cancelled", "permId": perm_id}

    executor = MagicMock()
    ib_client = DisconnectedIB()

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client, scope=_scope())
    handler.execute()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT state FROM xenon.position_protection WHERE protection_id=:pid"),
            {"pid": pid},
        ).one()

    assert ib_client.connect_calls
    assert row.state == "CANCELED"
    executor.flatten_mkt.assert_not_called()


def test_stale_quote_skips_evaluation(engine):
    descriptor = _stock_descriptor(symbol="TSLA", price=100.0, con_id=55555)
    pid = insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::TSLA_HANDLER",
        position_descriptor=descriptor,
        asset_class="stock",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE xenon.position_protection "
                "SET state='ARMED', state_data='{\"consecutive_stale_ticks\": 0}'::jsonb "
                "WHERE protection_id=:pid"
            ),
            {"pid": pid},
        )

    executor = MagicMock()
    ib_client = MagicMock()
    ib_client.connected = True
    ib_client.get_quote.return_value = Quote(
        symbol="TSLA",
        price=91.0,
        ts=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client, scope=_scope())
    handler.execute()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT state, state_data FROM xenon.position_protection WHERE protection_id=:pid"),
            {"pid": pid},
        ).first()
    assert row.state == "ARMED"
    assert row.state_data["consecutive_stale_ticks"] >= 1
    executor.flatten_mkt.assert_not_called()


def test_missing_position_ticks_do_not_increment_while_ib_disconnected(engine):
    descriptor = _stock_descriptor(symbol="ORCL", price=100.0, con_id=88888)
    pid = insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::ORCL_DISCONNECTED",
        position_descriptor=descriptor,
        asset_class="stock",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE xenon.position_protection "
                "SET state='ARMED', state_data='{\"position_missing_ticks\": 1}'::jsonb "
                "WHERE protection_id=:pid"
            ),
            {"pid": pid},
        )

    executor = MagicMock()
    ib_client = MagicMock()
    ib_client.connected = False
    ib_client.positions.return_value = []

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client, scope=_scope())
    handler.execute()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT state, state_data FROM xenon.position_protection WHERE protection_id=:pid"),
            {"pid": pid},
        ).one()
    assert row.state == "ARMED"
    assert row.state_data["position_missing_ticks"] == 1


def test_option_liveness_uses_portfolio_when_positions_snapshot_is_empty(engine):
    descriptor = _long_option_descriptor()
    pid = insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="TEST::SPY_OPTION_PORTFOLIO_FALLBACK",
        position_descriptor=descriptor,
        asset_class="long_option",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.50, "anchor": "entry_price"},
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE xenon.position_protection "
                "SET state='ARMED', state_data='{\"position_missing_ticks\": 1}'::jsonb, "
                "native_order_perm_id=777, armed_at=NOW() WHERE protection_id=:pid"
            ),
            {"pid": pid},
        )

    class OptionContract:
        symbol = "SPY"
        secType = "OPT"
        conId = 875935117
        lastTradeDateOrContractMonth = "20260505"
        strike = 740.0
        right = "C"

    class PortfolioItem:
        contract = OptionContract()
        position = 1

    executor = MagicMock()
    ib_client = MagicMock()
    ib_client.connected = True
    ib_client.positions.return_value = []
    ib_client.get_portfolio.return_value = [PortfolioItem()]
    ib_client.get_order_state.return_value = {"status": "Submitted", "permId": 777}
    ib_client.get_quote.return_value = Quote(symbol="SPY", price=0.01, ts=datetime.now(timezone.utc))

    handler = PositionRulesHandler(engine=engine, executor=executor, ib_client=ib_client, scope=_scope())
    handler.execute()
    handler.execute()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT state, state_data FROM xenon.position_protection WHERE protection_id=:pid"),
            {"pid": pid},
        ).one()

    assert row.state == "ARMED"
    assert row.state_data.get("position_missing_ticks", 0) == 0
    executor.flatten_mkt.assert_not_called()
