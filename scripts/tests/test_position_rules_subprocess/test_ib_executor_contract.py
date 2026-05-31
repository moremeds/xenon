"""Subprocess JSON contract for IBExecutor. Spec §13.4."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from xenon.execution.account_scope import AccountScope
from xenon.execution.brackets.executor.ib_executor import IBExecutor


@pytest.fixture
def scope():
    return AccountScope(broker="IB", account_env="paper", broker_account="DU1234567")


def _passed_json(run):
    cmd = run.call_args.args[0]
    return run.call_args.kwargs.get("input") or "".join(arg for arg in cmd if arg.startswith("{"))


def test_flatten_mkt_subprocess_payload_shape(scope):
    fake_completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"perm_id": 12345, "ib_order_id": 9999, "status": "Submitted"}),
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_completed) as run:
        executor = IBExecutor()
        result = executor.flatten_mkt(
            scope=scope,
            con_id=12345,
            symbol="AAPL",
            sec_type="STK",
            close_action="SELL",
            qty=100,
            order_ref="xenon-pr-42",
        )
        cmd = run.call_args.args[0]
        assert "xenon-ib-place-order" in cmd[0] or cmd[0].endswith("xenon-ib-place-order")
        payload = json.loads(_passed_json(run))
        assert payload["orderRef"] == "xenon-pr-42"
        assert payload["orderType"] == "MKT"
        assert payload["outsideRth"] is False
        assert payload["tif"] == "DAY"
        assert payload["action"] == "SELL"
        assert payload["symbol"] == "AAPL"
        assert payload["qty"] == 100
    assert result.perm_id == 12345


def test_flatten_mkt_subprocess_error_surfaces(scope):
    fake_completed = subprocess.CompletedProcess(
        args=[],
        returncode=2,
        stdout="",
        stderr=json.dumps({"reason_code": "IB_API_ERROR", "message": "Pacing violation"}),
    )
    with patch("subprocess.run", return_value=fake_completed):
        executor = IBExecutor()
        with pytest.raises(RuntimeError) as exc:
            executor.flatten_mkt(
                scope=scope,
                con_id=1,
                symbol="X",
                sec_type="STK",
                close_action="SELL",
                qty=1,
                order_ref="xenon-pr-1",
            )
        assert "Pacing violation" in str(exc.value)


def test_flatten_combo_mkt_subprocess_payload_shape(scope):
    fake_completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"perm_id": 54321, "ib_order_id": 7777, "status": "Submitted"}),
        stderr="",
    )
    legs = [
        {"symbol": "SPY", "expiry": "20260516", "strike": 580.0, "right": "P", "action": "SELL", "ratio": 1},
        {"symbol": "SPY", "expiry": "20260516", "strike": 575.0, "right": "P", "action": "BUY", "ratio": 1},
    ]
    with patch("subprocess.run", return_value=fake_completed) as run:
        executor = IBExecutor()
        result = executor.flatten_combo_mkt(
            scope=scope,
            symbol="SPY",
            legs=legs,
            qty=1,
            order_ref="xenon-pr-42",
        )
        payload = json.loads(_passed_json(run))
        assert payload["type"] == "combo"
        assert payload["symbol"] == "SPY"
        assert payload["action"] == "SELL"
        assert payload["quantity"] == 1
        assert payload["orderType"] == "MKT"
        assert payload["outsideRth"] is False
        assert payload["orderRef"] == "xenon-pr-42"
        assert payload["legs"] == legs
    assert result.perm_id == 54321


def test_attach_native_stp_payload_shape(scope):
    fake_completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"perm_id": 555, "ib_order_id": 6, "status": "Submitted"}),
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_completed) as run:
        executor = IBExecutor()
        result = executor.attach_native_stp(
            scope=scope,
            con_id=12345,
            symbol="AAPL",
            sec_type="STK",
            close_action="SELL",
            qty=100,
            stop_price=87.40,
            tif="GTC",
            order_ref="xenon-pr-native-123",
        )
        payload = json.loads(_passed_json(run))
        assert payload["orderType"] == "STP"
        assert payload["orderRef"] == "xenon-pr-native-123"
        assert payload["stopPrice"] == 87.40
        assert payload["tif"] == "GTC"
        assert payload["outsideRth"] is False
    assert result.perm_id == 555


def test_cancel_subprocess_uses_order_manage(scope):
    fake_completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"status": "Cancelled"}),
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_completed) as run:
        executor = IBExecutor()
        executor.cancel(scope=scope, perm_id=12345)
        cmd = run.call_args.args[0]
        assert "xenon-ib-order-manage" in cmd[0] or cmd[0].endswith("xenon-ib-order-manage")
        assert cmd[1:4] == ["cancel", "--perm-id", "12345"]
