# ruff: noqa: F811 -- fixtures are imported by name and re-bound as test args
"""PR-06: `ib_sync.main(["--sync"])` runs the naked-short audit in shadow mode.

Real `ib_sync.main` orchestration against the gate-4 fake IB client (real
ib_async Position / Trade / ContractDetails objects). The audit must run on
the pass's in-process holdings, record the violation in
`service_health("naked_short_audit")`, and never cancel anything.
"""

from __future__ import annotations

import json

import pytest
from ib_async import Position
from sqlalchemy import select

from scripts.tests.test_gate4_route import _pg_engine
from scripts.tests.test_gate4_sync_pass import (  # noqa: F401 — autouse fixture
    ACCOUNT,
    FakeSyncClient,
    _fresh_meta_cache,
    _opt,
    _stk,
    _trade,
)
from xenon.db.schema import service_health
from xenon.execution import ib_sync

pytestmark = pytest.mark.committed_db


class CancelRecordingClient(FakeSyncClient):
    """Fake client that records every cancel attempt (none expected)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.cancels = []
        self.ib.cancelOrder = lambda *a, **k: self.cancels.append(("ib.cancelOrder", a))

    def cancel_order(self, *a, **k):
        self.cancels.append(("cancel_order", a))


def _run(monkeypatch, fake, *extra):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", ACCOUNT)
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    monkeypatch.setattr(ib_sync, "connect_ib", lambda *a, **k: fake)
    ib_sync.main(["--sync", "--no-prices", *extra])


def _health_row():
    engine = _pg_engine()
    try:
        with engine.connect() as conn:
            return conn.execute(
                select(service_health).where(
                    service_health.c.service == "naked_short_audit",
                    service_health.c.broker_account == ACCOUNT,
                )
            ).first()
    finally:
        engine.dispose()


def _naked_call_client(**kw):
    fake = CancelRecordingClient(positions=[], **kw)
    fake.add_details(_opt(500, con_id=111))
    fake.open_orders.append(_trade(_opt(500, con_id=111), order_id=9001, perm_id=990001))
    return fake


def test_sync_runs_shadow_audit_records_violation_and_cancels_nothing(monkeypatch):
    fake = _naked_call_client()
    _run(monkeypatch, fake)

    row = _health_row()
    assert row is not None, "audit did not record service_health"
    assert row.state == "violations"
    detail = json.loads(row.detail)
    assert detail["mode"] == "shadow"
    assert detail["violations"] == 1
    assert detail["orders"][0]["perm_id"] == 990001
    assert fake.cancels == []


def test_sync_covered_short_call_records_ok(monkeypatch):
    fake = CancelRecordingClient(positions=[Position(ACCOUNT, _stk(), 100, 500.0)])
    fake.add_details(_opt(500, con_id=111))
    fake.open_orders.append(_trade(_opt(500, con_id=111), order_id=9001, perm_id=990001))
    _run(monkeypatch, fake)

    row = _health_row()
    assert row is not None and row.state == "ok"
    assert fake.cancels == []


def test_sync_skip_audit_flag_skips_audit(monkeypatch):
    fake = _naked_call_client()
    _run(monkeypatch, fake, "--skip-audit")
    assert _health_row() is None
    assert fake.cancels == []


def test_sync_open_order_read_failure_skips_audit(monkeypatch):
    fake = _naked_call_client()

    def _fail():
        raise RuntimeError("socket closed")

    fake.get_open_orders = _fail
    _run(monkeypatch, fake)
    row = _health_row()
    assert row is not None and row.state == "skipped"
    assert json.loads(row.detail)["reason"] == "observation_incomplete"
    assert fake.cancels == []


def test_sync_order_without_account_makes_pass_incomplete_for_audit(monkeypatch):
    """Astra P2-1: the sync passes its observation stamp, so an accountless
    open order (reads.order_accounts=False) skips the audit for the pass."""
    fake = CancelRecordingClient(positions=[])
    fake.add_details(_opt(500, con_id=111))
    fake.open_orders.append(_trade(_opt(500, con_id=111), order_id=9001, perm_id=990001, account=""))
    fake.open_orders.append(_trade(_opt(505, con_id=112), order_id=9002, perm_id=990002))
    fake.add_details(_opt(505, con_id=112))
    _run(monkeypatch, fake)
    row = _health_row()
    assert row is not None and row.state == "skipped"
    assert json.loads(row.detail)["reason"] == "observation_incomplete"


def test_read_only_sync_still_audits_what_it_sees(monkeypatch, caplog):
    """The audit reads no Postgres, so a read-only pass (which persists
    nothing) still evaluates its in-process view; only the health write
    no-ops under XENON_READ_ONLY."""
    fake = _naked_call_client()
    monkeypatch.setattr(ib_sync, "connect_ib", lambda *a, **k: fake)
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", ACCOUNT)
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    with caplog.at_level("WARNING", logger="xenon.naked_short_audit"):
        ib_sync.main(["--sync", "--no-prices"])
    assert "NAKED SHORT AUDIT (shadow): 1 violation(s)" in caplog.text
    assert _health_row() is None
    assert fake.cancels == []


# -- PR-11: machine-readable audit summary for the API -------------------------


def _parsed_like_run_entry_point(out: str) -> dict | None:
    """Same parse as `xenon.api.subprocess.run_entry_point` builds `data`."""
    start = out.find("{")
    if start == -1:
        return None
    try:
        data = json.loads(out[start:])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def test_sync_emits_audit_summary_as_script_result_data(monkeypatch, capsys):
    fake = _naked_call_client()
    _run(monkeypatch, fake)
    data = _parsed_like_run_entry_point(capsys.readouterr().out)
    assert data is not None, "ib_sync stdout does not parse into ScriptResult.data"
    audit = data["naked_short_audit"]
    assert audit["status"] == "violations"
    assert audit["account"] == ACCOUNT
    (v,) = audit["violations"]
    assert (v["order_id"], v["perm_id"], v["account"]) == (9001, 990001, ACCOUNT)
    assert fake.cancels == []


def test_sync_emits_skipped_audit_summary(monkeypatch, capsys):
    fake = _naked_call_client()

    def _fail():
        raise RuntimeError("socket closed")

    fake.get_open_orders = _fail
    _run(monkeypatch, fake)
    audit = _parsed_like_run_entry_point(capsys.readouterr().out)["naked_short_audit"]
    assert (audit["status"], audit["reason"]) == ("skipped", "observation_incomplete")


def test_sync_skip_audit_emits_no_audit_summary(monkeypatch, capsys):
    fake = _naked_call_client()
    _run(monkeypatch, fake, "--skip-audit")
    data = _parsed_like_run_entry_point(capsys.readouterr().out)
    assert not (data or {}).get("naked_short_audit")


def test_flag_on_sync_subprocess_still_cancels_nothing(monkeypatch):
    """XENON_AUDIT_CANCEL is read by the API, never by the sync subprocess."""
    monkeypatch.setenv("XENON_AUDIT_CANCEL", "1")
    fake = _naked_call_client()
    _run(monkeypatch, fake)
    assert fake.cancels == []
    assert json.loads(_health_row().detail)["mode"] == "shadow"
