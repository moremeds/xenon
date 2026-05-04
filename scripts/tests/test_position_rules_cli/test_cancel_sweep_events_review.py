"""xenon-position-rules CLI cancel/sweep/events/review coverage."""
from __future__ import annotations

import json

from sqlalchemy import text

from xenon.cli import position_rules as cli
from xenon.db.engine import get_sync_engine
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


def _insert_rule(position_key: str = "STK::CLICANCEL") -> int:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key = :position_key"), {"position_key": position_key})
    symbol = position_key.split("::")[-1]
    return insert_pending_arm(
        engine,
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
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
            conn.execute(text("DELETE FROM xenon.position_protection WHERE protection_id = :pid"), {"pid": protection_id})


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


def test_positions_from_ib_connects_with_auto_client_id(monkeypatch):
    import xenon.clients.ib_client as ib_client_mod

    class Contract:
        symbol = "CLIAUTO"
        conId = 77

    class Position:
        contract = Contract()
        position = 3

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

    assert rows == [{"symbol": "CLIAUTO", "qty": 3, "con_id": 77}]
    assert FakeIBClient.instance.connect_kwargs["client_id"] == "auto"
    assert FakeIBClient.instance.disconnected is True


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
            conn.execute(text("DELETE FROM xenon.position_rules_review WHERE event_id = :event_id"), {"event_id": event_id})
            conn.execute(text("DELETE FROM events.outbox WHERE id = :event_id"), {"event_id": event_id})
