"""xenon-position-rules CLI cancel/sweep/events/review coverage."""

from __future__ import annotations

import json

from sqlalchemy import text

from xenon.cli import position_rules as cli
from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_close_claims import find_inflight_for_position, mark_submitted, try_claim
from xenon.db.queries.position_protection import get_by_id, insert_pending_arm


def _descriptor(symbol: str = "CLICANCEL") -> dict:
    return {
        "asset_class": "stock",
        "anchor_price": 100.0,
        "opened_qty": 100,
        "protected_qty": 100,
        "multiplier": 1,
        "qty_unit": "share",
        "opened_at": "2026-05-04T14:00:00Z",
        "source": "test",
        "anchor_currency": "USD",
        "legs": [{"sec_type": "STK", "symbol": symbol, "action": "BUY", "ratio": 1, "fill_price": 100.0, "con_id": 1}],
    }


def _insert_rule(
    position_key: str = "STK::CLICANCEL",
    *,
    account_env: str = "paper",
    broker_account: str = "DU1234567",
) -> int:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM xenon.position_close_claims WHERE position_key = :position_key"),
            {"position_key": position_key},
        )
        conn.execute(
            text("DELETE FROM xenon.position_protection WHERE position_key = :position_key"),
            {"position_key": position_key},
        )
    symbol = position_key.split("::")[-1]
    return insert_pending_arm(
        engine,
        broker="IB",
        account_env=account_env,
        broker_account=broker_account,
        position_key=position_key,
        position_descriptor=_descriptor(symbol),
        asset_class="stock",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )


def test_cancel_transitions_active_row(monkeypatch, capsys):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU1234567")
    monkeypatch.setenv("XENON_BROKER", "IB")
    protection_id = _insert_rule()

    try:
        assert cli.main(["cancel", str(protection_id)]) == 0
        assert "canceled protection_id" in capsys.readouterr().out
        row = get_by_id(get_sync_engine(), protection_id=protection_id)
        assert row is not None
        assert row["state"] == "CANCELED"
    finally:
        with get_sync_engine().begin() as conn:
            conn.execute(
                text("DELETE FROM xenon.position_close_claims WHERE claimed_by_protection_id = :pid"),
                {"pid": protection_id},
            )
            conn.execute(
                text("DELETE FROM xenon.position_protection WHERE protection_id = :pid"), {"pid": protection_id}
            )


def test_cancel_live_requires_operator_user(monkeypatch, capsys):
    import xenon.api.trading_mode as trading_mode

    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "U1234567")
    monkeypatch.setenv("XENON_BROKER", "IB")
    monkeypatch.delenv("XENON_OPERATOR_USER_ID", raising=False)
    monkeypatch.setattr(trading_mode, "MODE", "live")

    assert cli.main(["cancel", "999999999"]) == 1
    body = json.loads(capsys.readouterr().err)
    assert body["reason_code"] == "live_trading_auth_unconfigured"


def test_cancel_is_scoped_and_cannot_cancel_live_row_from_paper_cli(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU1234567")
    monkeypatch.setenv("XENON_BROKER", "IB")
    protection_id = _insert_rule(
        "STK::CLICROSSLIVE",
        account_env="live",
        broker_account="U1234567",
    )

    try:
        assert cli.main(["cancel", str(protection_id)]) == 1
        row = get_by_id(get_sync_engine(), protection_id=protection_id)
        assert row is not None
        assert row["state"] == "PENDING_ARM"
    finally:
        with get_sync_engine().begin() as conn:
            conn.execute(
                text("DELETE FROM xenon.position_close_claims WHERE claimed_by_protection_id = :pid"),
                {"pid": protection_id},
            )
            conn.execute(
                text("DELETE FROM xenon.position_protection WHERE protection_id = :pid"), {"pid": protection_id}
            )


def test_cancel_cancels_native_order_and_abandons_inflight_claim(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU1234567")
    monkeypatch.setenv("XENON_BROKER", "IB")
    engine = get_sync_engine()
    position_key = "STK::CLINATIVE"
    protection_id = _insert_rule(position_key)
    claim_id = try_claim(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key=position_key,
        claimed_by_protection_id=protection_id,
        claim_kind="synthetic_close",
    )
    assert claim_id is not None
    mark_submitted(engine, claim_id=claim_id, broker_perm_id=222)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE xenon.position_protection
                SET state = 'ARMED', native_order_perm_id = 111
                WHERE protection_id = :pid
                """
            ),
            {"pid": protection_id},
        )

    canceled: list[int] = []

    class FakeExecutor:
        def cancel(self, *, scope, perm_id):
            canceled.append(perm_id)
            return {"status": "Cancelled", "perm_id": perm_id}

    monkeypatch.setattr("xenon.api.services.position_rules_cancel.IBExecutor", lambda: FakeExecutor())

    try:
        assert cli.main(["cancel", str(protection_id)]) == 0
        assert canceled == [111, 222]
        row = get_by_id(engine, protection_id=protection_id)
        assert row is not None
        assert row["state"] == "CANCELED"
        claim = find_inflight_for_position(
            engine,
            broker="IB",
            account_env="paper",
            broker_account="DU1234567",
            position_key=position_key,
        )
        assert claim is None
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM xenon.position_close_claims WHERE claimed_by_protection_id = :pid"),
                {"pid": protection_id},
            )
            conn.execute(
                text("DELETE FROM xenon.position_protection WHERE protection_id = :pid"), {"pid": protection_id}
            )


def test_sweep_dry_run_reports_unprotected_ib_position(monkeypatch, capsys):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU1234567")
    monkeypatch.setenv("XENON_BROKER", "IB")
    monkeypatch.setattr(cli, "_positions_from_ib", lambda: [{"symbol": "CLISWEEP", "qty": 100, "con_id": 9}])
    with get_sync_engine().begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key = 'STK::CLISWEEP'"))

    assert cli.main(["sweep"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["count"] == 1
    assert body["would_insert"][0]["symbol"] == "CLISWEEP"


def test_sweep_dry_run_does_not_treat_option_rule_as_stock_protection(monkeypatch, capsys):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU1234567")
    monkeypatch.setenv("XENON_BROKER", "IB")
    monkeypatch.setattr(
        cli, "_positions_from_ib", lambda: [{"symbol": "CLISAME", "qty": 100, "con_id": 9, "sec_type": "STK"}]
    )
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM xenon.position_protection WHERE position_key IN ('STK::CLISAME', 'OPT::CLISAME::20260619::100::C')"
            )
        )
    insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key="OPT::CLISAME::20260619::100::C",
        position_descriptor={
            "asset_class": "long_option",
            "anchor_price": 2.0,
            "opened_qty": 1,
            "protected_qty": 1,
            "multiplier": 100,
            "qty_unit": "contract",
            "opened_at": "2026-05-04T14:00:00Z",
            "source": "test",
            "anchor_currency": "USD",
            "legs": [
                {
                    "sec_type": "OPT",
                    "symbol": "CLISAME",
                    "expiry": "20260619",
                    "strike": 100.0,
                    "right": "C",
                    "action": "BUY",
                    "ratio": 1,
                    "fill_price": 2.0,
                    "con_id": 99,
                }
            ],
        },
        asset_class="long_option",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.2, "anchor": "entry_price"},
    )

    assert cli.main(["sweep"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["count"] == 1
    assert body["would_insert"][0]["symbol"] == "CLISAME"


def test_sweep_dry_run_skips_option_legs_owned_by_active_combo(monkeypatch, capsys):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU1234567")
    monkeypatch.setenv("XENON_BROKER", "IB")
    monkeypatch.setattr(
        cli,
        "_positions_from_ib",
        lambda: [
            {
                "symbol": "CLICOMBO",
                "qty": 1,
                "con_id": 7001,
                "sec_type": "OPT",
                "avg_cost": 0.20,
                "expiry": "20260619",
                "strike": 95.0,
                "right": "P",
            },
            {
                "symbol": "CLICOMBO",
                "qty": -1,
                "con_id": 7002,
                "sec_type": "OPT",
                "avg_cost": 0.45,
                "expiry": "20260619",
                "strike": 100.0,
                "right": "P",
            },
            {"symbol": "CLIGM", "qty": 1, "con_id": 8001, "sec_type": "STK", "avg_cost": 79.0},
        ],
    )
    engine = get_sync_engine()
    combo_key = "CS::CLICOMBO::20260619::100::95::P"
    stock_key = "STK::CLIGM"
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM xenon.position_protection WHERE position_key IN (:combo_key, :stock_key)"),
            {"combo_key": combo_key, "stock_key": stock_key},
        )
    descriptor = {
        "asset_class": "credit_spread",
        "anchor_price": -0.25,
        "opened_qty": 1,
        "protected_qty": 1,
        "multiplier": 100,
        "qty_unit": "contract",
        "opened_at": "2026-05-08T14:00:00Z",
        "source": "test",
        "anchor_currency": "USD",
        "legs": [
            {
                "sec_type": "OPT",
                "symbol": "CLICOMBO",
                "expiry": "20260619",
                "strike": 100.0,
                "right": "P",
                "action": "SELL",
                "ratio": 1,
                "con_id": 7002,
            },
            {
                "sec_type": "OPT",
                "symbol": "CLICOMBO",
                "expiry": "20260619",
                "strike": 95.0,
                "right": "P",
                "action": "BUY",
                "ratio": 1,
                "con_id": 7001,
            },
        ],
    }
    insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        position_key=combo_key,
        position_descriptor=descriptor,
        asset_class="credit_spread",
        rule_kind="stop_loss",
        config={"short_strike": 100.0},
    )

    try:
        assert cli.main(["sweep"]) == 0
        body = json.loads(capsys.readouterr().out)
        assert body["count"] == 1
        assert body["would_insert"][0]["symbol"] == "CLIGM"
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM xenon.position_protection WHERE position_key IN (:combo_key, :stock_key)"),
                {"combo_key": combo_key, "stock_key": stock_key},
            )


def test_sweep_apply_does_not_report_negative_skipped(monkeypatch, capsys):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU1234567")
    monkeypatch.setenv("XENON_BROKER", "IB")
    monkeypatch.setattr(
        cli,
        "_positions_from_ib",
        lambda: [{"symbol": "CLISWEEPAPPLY", "qty": 1, "con_id": 91, "sec_type": "STK", "avg_cost": 42.0}],
    )
    engine = get_sync_engine()
    position_key = "STK::CLISWEEPAPPLY"
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key = :position_key"), {"position_key": position_key})

    def fake_sweep_insert(engine, *, scope, candidate):
        descriptor = _descriptor(candidate["symbol"])
        insert_pending_arm(
            engine,
            broker=scope.broker,
            account_env=scope.account_env,
            broker_account=scope.broker_account,
            position_key=position_key,
            position_descriptor=descriptor,
            asset_class="stock",
            rule_kind="stop_loss",
            config={"threshold_pct": -0.08, "anchor": "entry_price"},
        )
        insert_pending_arm(
            engine,
            broker=scope.broker,
            account_env=scope.account_env,
            broker_account=scope.broker_account,
            position_key=position_key,
            position_descriptor=descriptor,
            asset_class="stock",
            rule_kind="trailing_tp",
            config={"trigger_pct": 0.15, "trail_pct": 0.05, "anchor": "entry_price"},
        )

    import xenon.execution.brackets.arm_hook as arm_hook

    monkeypatch.setattr(arm_hook, "sweep_insert", fake_sweep_insert)

    try:
        assert cli.main(["sweep", "--apply"]) == 0
        body = json.loads(capsys.readouterr().out)
        assert body == {"applied": 2, "skipped": 0}
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key = :position_key"), {"position_key": position_key})


def test_positions_from_ib_connects_with_auto_client_id(monkeypatch):
    import xenon.clients.ib_client as ib_client_mod

    class Contract:
        symbol = "CLIAUTO"
        secType = "STK"
        conId = 77
        lastTradeDateOrContractMonth = ""
        strike = 0.0
        right = ""
        multiplier = ""

    class Position:
        contract = Contract()
        position = 3
        avgCost = 42.5

    class FakeIBClient:
        instance = None

        def __init__(self):
            self.connected = False
            self.connect_kwargs = None
            self.disconnected = False
            FakeIBClient.instance = self

        def is_connected(self):
            return self.connected

        def connect(self, **kwargs):
            self.connected = True
            self.connect_kwargs = kwargs

        def get_positions(self):
            assert self.connected
            return [Position()]

        def disconnect(self):
            self.disconnected = True
            self.connected = False

    monkeypatch.setattr(ib_client_mod, "IBClient", FakeIBClient)

    rows = cli._positions_from_ib()

    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "CLIAUTO"
    assert row["qty"] == 3
    assert row["con_id"] == 77
    assert row["sec_type"] == "STK"
    assert row["avg_cost"] == 42.5
    assert FakeIBClient.instance.connect_kwargs["client_id"] == "auto"
    assert FakeIBClient.instance.disconnected is True


def test_positions_from_ib_preserves_option_contract_details(monkeypatch):
    """Regression: option positions surfaced via get_positions() must keep
    sec_type/expiry/strike/right/avg_cost so _candidate_position_key emits
    OPT::SYM::expiry::strike::right (not the STK fallback) and downstream
    sweep_insert has a non-zero anchor.
    """
    import xenon.clients.ib_client as ib_client_mod

    class Contract:
        symbol = "CLIOPT"
        secType = "OPT"
        conId = 88
        lastTradeDateOrContractMonth = "20260619"
        strike = 100.0
        right = "C"
        multiplier = "100"

    class Position:
        contract = Contract()
        position = 2
        avgCost = 150.0  # ib_async: per-contract dollars (premium * 100)

    class FakeIBClient:
        def __init__(self):
            self._connected = False

        def is_connected(self):
            return self._connected

        def connect(self, **kwargs):
            self._connected = True

        def get_positions(self):
            return [Position()]

        def disconnect(self):
            self._connected = False

    monkeypatch.setattr(ib_client_mod, "IBClient", FakeIBClient)

    rows = cli._positions_from_ib()

    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "CLIOPT"
    assert row["sec_type"] == "OPT"
    assert row["expiry"] == "20260619"
    assert row["strike"] == 100.0
    assert row["right"] == "C"
    assert row["qty"] == 2
    assert row["con_id"] == 88
    # ib_async OPT avgCost is per-contract dollars (premium * multiplier);
    # _positions_from_ib normalizes to per-contract premium so downstream
    # anchor_price math (entry * 0.92 etc.) operates in price-quote units.
    assert row["avg_cost"] == 1.5

    # Smoke-check the position_key uses the OPT scheme, not the STK fallback.
    assert cli._candidate_position_key(row) == "OPT::CLIOPT::20260619::100::C"


def test_sweep_apply_live_requires_operator_user(monkeypatch, capsys):
    import xenon.api.trading_mode as trading_mode

    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "U1234567")
    monkeypatch.setenv("XENON_BROKER", "IB")
    monkeypatch.delenv("XENON_OPERATOR_USER_ID", raising=False)
    monkeypatch.setattr(trading_mode, "MODE", "live")

    assert cli.main(["sweep", "--apply"]) == 1
    body = json.loads(capsys.readouterr().err)
    assert body["reason_code"] == "live_trading_auth_unconfigured"


def test_events_and_review_commands(monkeypatch, capsys):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU1234567")
    monkeypatch.setenv("XENON_BROKER", "IB")
    engine = get_sync_engine()
    with engine.begin() as conn:
        event_id = conn.execute(
            text(
                """
                INSERT INTO events.outbox(channel, source, payload)
                VALUES ('position_rule.transition', 'test', '{"protection_id": -42, "state": "TRIGGERED"}'::jsonb)
                RETURNING id
                """
            )
        ).scalar_one()
        conn.execute(text("DELETE FROM xenon.position_rules_review WHERE event_id = :event_id"), {"event_id": event_id})

    try:
        assert cli.main(["events", "--since", "1d"]) == 0
        events = json.loads(capsys.readouterr().out)
        assert any(row["event_id"] == event_id and row["state"] == "TRIGGERED" for row in events)

        assert (
            cli.main(
                [
                    "review",
                    "--event-id",
                    str(event_id),
                    "--protection-id",
                    "-42",
                    "--reviewed-by",
                    "codex-test",
                    "--verdict",
                    "expected",
                    "--note",
                    "fixture",
                ]
            )
            == 0
        )
        body = json.loads(capsys.readouterr().out)
        assert body["review_id"] is not None
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM xenon.position_rules_review WHERE event_id = :event_id"), {"event_id": event_id}
            )
            conn.execute(text("DELETE FROM events.outbox WHERE id = :event_id"), {"event_id": event_id})
