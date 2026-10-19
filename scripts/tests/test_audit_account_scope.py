# ruff: noqa: F811 -- fixtures are imported by name and re-bound as test args
"""Naked-short audit only judges the scope account's orders (#198).

PR #198 review (Astra P1-1, Fable confirmed): after positions were scoped to
the scope account, open orders were still read for every managed account. A
covered SELL call on the OTHER account (that account holds the shares) then
looked naked against the scope account's positions, and the audit cancelled
it. PR-06 made the audit shadow-only (no cancel path); these tests keep the
#198 scenario: the other account's covered order is not a violation, and
nothing is cancelled. Fake IB only — no network.
"""

from __future__ import annotations

import json

import pytest
from ib_async import Position

from scripts.tests.test_gate4_sync_pass import (  # noqa: F401 — autouse fixture
    ACCOUNT,
    _fresh_meta_cache,
    _opt,
    _stk,
    _trade,
)
from scripts.tests.test_ib_sync_audit_shadow import CancelRecordingClient, _health_row, _run
from scripts.tests.test_naked_short_audit import ACCT, FOREIGN, _opt_contract, _order, _payload
from xenon.execution import naked_short_audit as nsa

OTHER = "DU2222222"


@pytest.mark.committed_db
def test_ib_sync_other_account_covered_sell_is_not_a_violation(monkeypatch):
    # OTHER holds the 100 shares and the open SELL call; the scope holds nothing.
    fake = CancelRecordingClient(positions=[Position(OTHER, _stk(), 100, 500.0)], accounts=(ACCOUNT, OTHER))
    fake.add_details(_opt(500, con_id=111))
    fake.open_orders.append(_trade(_opt(500, con_id=111), order_id=9001, perm_id=990001, account=OTHER))

    _run(monkeypatch, fake)

    row = _health_row()
    assert row is not None and row.state == "ok"
    assert json.loads(row.detail)["violations"] == 0
    assert fake.cancels == []


def test_cli_ignores_other_account_order(tmp_path, capsys):
    pf = tmp_path / "portfolio.json"
    of = tmp_path / "orders.json"
    pf.write_text(json.dumps(_payload()))
    of.write_text(json.dumps({"open_orders": [_order(_opt_contract(), account=FOREIGN)]}))

    summary = nsa.main(["--portfolio", str(pf), "--orders", str(of), "--account", ACCT])

    assert summary["status"] == "ok"
    assert summary["violations"] == []
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
