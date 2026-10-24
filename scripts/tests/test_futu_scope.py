"""Selected Futu trade scope (X3a): configured_scope() + verify_live().

Rows mirror futu-api 10.3.6308 `OpenSecTradeContext.get_acc_list()`:
`acc_id` int, `trd_env` 'REAL'/'SIMULATE', `trdmarket_auth` list of market
strings, `acc_status` 'ACTIVE'/'DISABLED'. Account ids below are made up.
"""

from __future__ import annotations

import pandas as pd
import pytest
from xenon.execution import futu_scope
from xenon.execution.account_scope import AccountScope

ACC = "281756479345015383"
OTHER = "281756479345015999"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("XENON_FUTU_TRADING", "FUTU_TRADE_ACC_ID", "FUTU_TRADE_ENV"):
        monkeypatch.delenv(name, raising=False)


def _row(acc_id=ACC, trd_env="SIMULATE", auth=("US",), status="ACTIVE"):
    return {
        "acc_id": int(acc_id),
        "trd_env": trd_env,
        "acc_type": "MARGIN",
        "trdmarket_auth": list(auth),
        "acc_status": status,
    }


def _scope(env="paper", acc=ACC):
    return AccountScope(broker="FUTU", account_env=env, broker_account=acc)


# ── configured_scope ────────────────────────────────────────────


@pytest.mark.parametrize(("trd_env", "account_env"), [("SIMULATE", "paper"), ("REAL", "live")])
def test_configured_scope_maps_simulate_and_real(monkeypatch, trd_env, account_env):
    monkeypatch.setenv("FUTU_TRADE_ACC_ID", ACC)
    monkeypatch.setenv("FUTU_TRADE_ENV", trd_env)
    scope, reason = futu_scope.configured_scope()
    assert reason is None
    assert scope == AccountScope(broker="FUTU", account_env=account_env, broker_account=ACC)
    assert isinstance(scope.broker_account, str)


@pytest.mark.parametrize("trading", [None, "0", "1"])
def test_configured_scope_ignores_trading_flag(monkeypatch, trading):
    if trading is not None:
        monkeypatch.setenv("XENON_FUTU_TRADING", trading)
    monkeypatch.setenv("FUTU_TRADE_ACC_ID", ACC)
    monkeypatch.setenv("FUTU_TRADE_ENV", "SIMULATE")
    assert futu_scope.configured_scope() == (_scope(), None)


def test_configured_scope_missing_acc_id(monkeypatch):
    monkeypatch.setenv("FUTU_TRADE_ENV", "SIMULATE")
    assert futu_scope.configured_scope() == (None, futu_scope.ACC_ID_MISSING)
    monkeypatch.setenv("FUTU_TRADE_ACC_ID", "")
    assert futu_scope.configured_scope() == (None, futu_scope.ACC_ID_MISSING)


@pytest.mark.parametrize("acc", ["0", "000", "0123", "-5", "12a", " 123", "123 ", "123\n", "1.0", "１２３"])
def test_configured_scope_rejects_non_exact_acc_id(monkeypatch, acc):
    monkeypatch.setenv("FUTU_TRADE_ACC_ID", acc)
    monkeypatch.setenv("FUTU_TRADE_ENV", "SIMULATE")
    assert futu_scope.configured_scope() == (None, futu_scope.ACC_ID_INVALID)


def test_configured_scope_missing_env(monkeypatch):
    monkeypatch.setenv("FUTU_TRADE_ACC_ID", ACC)
    assert futu_scope.configured_scope() == (None, futu_scope.TRD_ENV_MISSING)


@pytest.mark.parametrize("env", ["simulate", "real", "PAPER", "live", " REAL", "SIM"])
def test_configured_scope_rejects_unknown_env(monkeypatch, env):
    monkeypatch.setenv("FUTU_TRADE_ACC_ID", ACC)
    monkeypatch.setenv("FUTU_TRADE_ENV", env)
    assert futu_scope.configured_scope() == (None, futu_scope.TRD_ENV_INVALID)


# ── verify_live ─────────────────────────────────────────────────


def test_verify_live_exact_match():
    assert futu_scope.verify_live(_scope(), [_row()]) == (True, None)
    assert futu_scope.verify_live(_scope("live"), [_row(trd_env="REAL")]) == (True, None)


def test_verify_live_accepts_dataframe():
    df = pd.DataFrame([_row(OTHER), _row()])
    assert futu_scope.verify_live(_scope(), df) == (True, None)


def test_verify_live_picks_configured_of_two_accounts_in_one_env():
    rows = [_row(OTHER, status="ACTIVE"), _row(ACC, status="ACTIVE")]
    assert futu_scope.verify_live(_scope(), rows) == (True, None)
    # The configured one disabled: the other ACTIVE account in the env is not used.
    rows = [_row(OTHER), _row(ACC, status="DISABLED")]
    assert futu_scope.verify_live(_scope(), rows) == (False, futu_scope.ACCOUNT_NOT_ACTIVE)


def test_verify_live_absent_has_no_first_account_fallback():
    rows = [_row(OTHER), _row("281756479345010000")]
    assert futu_scope.verify_live(_scope(), rows) == (False, futu_scope.ACCOUNT_ABSENT)
    assert futu_scope.verify_live(_scope(), []) == (False, futu_scope.ACCOUNT_ABSENT)


def test_verify_live_duplicate_rows_are_ambiguous():
    rows = [_row(), _row(trd_env="REAL")]
    assert futu_scope.verify_live(_scope(), rows) == (False, futu_scope.ACCOUNT_AMBIGUOUS)


@pytest.mark.parametrize(("env", "trd_env"), [("paper", "REAL"), ("live", "SIMULATE"), ("paper", "N/A")])
def test_verify_live_env_mismatch(env, trd_env):
    assert futu_scope.verify_live(_scope(env), [_row(trd_env=trd_env)]) == (False, futu_scope.ACCOUNT_ENV_MISMATCH)


@pytest.mark.parametrize("status", ["DISABLED", "N/A", None, "active"])
def test_verify_live_requires_active(status):
    assert futu_scope.verify_live(_scope(), [_row(status=status)]) == (False, futu_scope.ACCOUNT_NOT_ACTIVE)


@pytest.mark.parametrize("auth", [(), ("HK",), ("HK", "CN"), ("USA",)])
def test_verify_live_requires_us_market_authority(auth):
    assert futu_scope.verify_live(_scope(), [_row(auth=auth)]) == (False, futu_scope.ACCOUNT_NO_US_AUTHORITY)


def test_verify_live_us_among_several_markets():
    assert futu_scope.verify_live(_scope(), [_row(auth=("HK", "US"))]) == (True, None)


def test_verify_live_auth_must_be_a_list():
    row = _row()
    row["trdmarket_auth"] = "US"
    assert futu_scope.verify_live(_scope(), [row]) == (False, futu_scope.ACCOUNT_NO_US_AUTHORITY)


@pytest.mark.parametrize(
    "scope",
    [
        AccountScope(broker="IB", account_env="paper", broker_account=ACC),
        AccountScope(broker="FUTU", account_env="legacy_unknown", broker_account=ACC),
        AccountScope(broker="FUTU", account_env="sim", broker_account=ACC),
    ],
)
def test_verify_live_rejects_non_futu_or_unmapped_scope(scope):
    assert futu_scope.verify_live(scope, [_row()]) == (False, futu_scope.SCOPE_INVALID)


def test_verify_live_acc_id_compared_exactly():
    # Leading-zero or padded scope strings never match the integer acc_id.
    assert futu_scope.verify_live(_scope(acc="0" + ACC), [_row()]) == (False, futu_scope.ACCOUNT_ABSENT)
    assert futu_scope.verify_live(_scope(acc=ACC + " "), [_row()]) == (False, futu_scope.ACCOUNT_ABSENT)
