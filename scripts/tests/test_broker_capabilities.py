"""Broker capability table (X3a, §11): IB unchanged, FUTU default-off.

FUTU place_single / cancel / modify need the request env in
XENON_FUTU_TRADING (a comma set over simulate, real; anything else, including
the old "1", is off for both) AND a configured account for that env
(FUTU_TRADE_ACC_ID_SIMULATE / _REAL). FUTU replace, combo and wizard are never
allowed.
"""

from __future__ import annotations

import itertools
import logging

import pytest
from xenon.execution import broker_capabilities as bc

_GATED = ("place_single", "cancel", "modify")
_NEVER = ("replace", "combo", "wizard")
ACC = "281756479345015383"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("XENON_FUTU_TRADING", "FUTU_TRADE_ACC_ID_SIMULATE", "FUTU_TRADE_ACC_ID_REAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(bc, "_bad_flags_logged", set())


def _set(monkeypatch, *, trading=None, sim=None, real=None):
    for name, value in (
        ("XENON_FUTU_TRADING", trading),
        ("FUTU_TRADE_ACC_ID_SIMULATE", sim),
        ("FUTU_TRADE_ACC_ID_REAL", real),
    ):
        if value is not None:
            monkeypatch.setenv(name, value)


def test_table_lists_every_operation_for_both_brokers():
    assert set(bc.OPERATIONS) == set(_GATED) | set(_NEVER)
    assert set(bc.CAPABILITIES) == {"IB", "FUTU"}
    for broker in ("IB", "FUTU"):
        assert set(bc.CAPABILITIES[broker]) == set(bc.OPERATIONS)
    assert all(bc.CAPABILITIES["IB"].values())
    assert not any(bc.CAPABILITIES["FUTU"][op] for op in _NEVER)


@pytest.mark.parametrize("op", bc.OPERATIONS)
def test_ib_allows_every_operation_regardless_of_futu_env(monkeypatch, op):
    assert bc.capability("IB", op) == (True, None)
    _set(monkeypatch, trading="0", sim="bad", real="NOPE")
    assert bc.capability("IB", op) == (True, None)
    assert bc.capability("ib", op, "live") == (True, None)


@pytest.mark.parametrize(
    ("raw", "envs"),
    [
        (None, set()),
        ("", set()),
        ("simulate", {"paper"}),
        ("real", {"live"}),
        ("simulate,real", {"paper", "live"}),
        (" Real , SIMULATE ", {"paper", "live"}),
        ("1", set()),
        ("true", set()),
        ("simulate,1", set()),
        ("paper", set()),
        ("simulate,", set()),
    ],
)
def test_trading_flag_is_a_set_over_simulate_and_real(monkeypatch, raw, envs):
    _set(monkeypatch, trading=raw)
    assert bc.futu_trading_envs() == envs


def test_bad_trading_flag_logs_one_error(monkeypatch, caplog):
    _set(monkeypatch, trading="1")
    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            assert bc.futu_trading_envs() == set()
    assert len([r for r in caplog.records if "XENON_FUTU_TRADING" in r.getMessage()]) == 1


# trading flag × configured accounts × request env → expected for the gated ops.
_TRADING = (None, "1", "simulate", "real", "simulate,real")
_CONFIG = ((None, None), (ACC, None), (None, ACC), (ACC, ACC), ("0", ACC))
_REQ = (None, "paper", "live", "PAPER", "sim")


@pytest.mark.parametrize(("op", "trading", "config", "req"), list(itertools.product(_GATED, _TRADING, _CONFIG, _REQ)))
def test_futu_gated_ops_need_the_env_enabled_and_configured(monkeypatch, op, trading, config, req):
    sim, real = config
    _set(monkeypatch, trading=trading, sim=sim, real=real)
    enabled = {"simulate": {"paper"}, "real": {"live"}, "simulate,real": {"paper", "live"}}.get(trading, set())
    configured = {e for e, acc in (("paper", sim), ("live", real)) if acc == ACC}
    expected = (True, None) if req in enabled & configured else (False, "READ_ONLY_BROKER")
    assert bc.capability("FUTU", op, req) == expected


def test_a_toggle_between_configured_envs_needs_both_enabled(monkeypatch):
    _set(monkeypatch, trading="simulate", sim=ACC, real="281756479345015999")
    assert bc.capability("FUTU", "place_single", "paper") == (True, None)
    assert bc.capability("FUTU", "place_single", "live") == (False, "READ_ONLY_BROKER")


@pytest.mark.parametrize("op", _NEVER)
@pytest.mark.parametrize("trading", [None, "simulate,real"])
def test_futu_replace_combo_wizard_always_denied(monkeypatch, op, trading):
    _set(monkeypatch, trading=trading, sim=ACC, real=ACC)
    for env in ("paper", "live"):
        assert bc.capability("FUTU", op, env) == (False, "READ_ONLY_BROKER")
        assert bc.capability("futu", op, env) == (False, "READ_ONLY_BROKER")


def test_futu_default_config_denies_everything():
    for op in bc.OPERATIONS:
        for env in (None, "paper", "live"):
            assert bc.capability("FUTU", op, env) == (False, "READ_ONLY_BROKER")


@pytest.mark.parametrize("broker", ["", None, "SCHWAB", "IBKR"])
def test_unknown_broker_denied(broker):
    assert bc.capability(broker, "place_single") == (False, "READ_ONLY_BROKER")


@pytest.mark.parametrize("broker", ["IB", "FUTU"])
@pytest.mark.parametrize("op", [None, "", "place", "PLACE_SINGLE"])
def test_unknown_operation_denied(monkeypatch, broker, op):
    _set(monkeypatch, trading="simulate", sim=ACC)
    assert bc.capability(broker, op, "paper") == (False, "READ_ONLY_BROKER")


def test_evaluate_names_the_cause(monkeypatch):
    assert "account_env" in bc.evaluate("FUTU", "place_single")[2]
    assert "XENON_FUTU_TRADING" in bc.evaluate("FUTU", "place_single", "paper")[2]
    _set(monkeypatch, trading="simulate")
    assert "not_configured" in bc.evaluate("FUTU", "place_single", "paper")[2]
    assert "replace" in bc.evaluate("FUTU", "replace")[2]
    assert bc.evaluate("IB", "replace") == (True, None, "")
