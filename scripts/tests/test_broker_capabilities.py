"""Broker capability table (X3a): IB unchanged, FUTU default-off.

FUTU place_single / cancel / modify need XENON_FUTU_TRADING=1 (exactly) AND a
valid configured scope (FUTU_TRADE_ACC_ID + FUTU_TRADE_ENV). FUTU replace,
combo and wizard are never allowed.
"""

from __future__ import annotations

import itertools

import pytest
from xenon.execution import broker_capabilities as bc

_GATED = ("place_single", "cancel", "modify")
_NEVER = ("replace", "combo", "wizard")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("XENON_FUTU_TRADING", "FUTU_TRADE_ACC_ID", "FUTU_TRADE_ENV"):
        monkeypatch.delenv(name, raising=False)


def _set(monkeypatch, *, trading=None, acc=None, env=None):
    for name, value in (("XENON_FUTU_TRADING", trading), ("FUTU_TRADE_ACC_ID", acc), ("FUTU_TRADE_ENV", env)):
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
    _set(monkeypatch, trading="0", acc="bad", env="NOPE")
    assert bc.capability("IB", op) == (True, None)
    assert bc.capability("ib", op) == (True, None)


# trading flag × scope validity → expected for the gated ops.
_TRADING = (None, "", "0", "true", "simulate", " 1", "1")
_SCOPES = (
    (None, None, False),
    ("281756479345015383", None, False),
    (None, "SIMULATE", False),
    ("281756479345015383", "SIMULATE", True),
    ("281756479345015383", "REAL", True),
    ("0", "SIMULATE", False),
    ("281756479345015383", "simulate", False),
)


@pytest.mark.parametrize(
    ("op", "trading", "scope"),
    list(itertools.product(_GATED, _TRADING, _SCOPES)),
)
def test_futu_gated_ops_need_flag_and_valid_scope(monkeypatch, op, trading, scope):
    acc, env, scope_ok = scope
    _set(monkeypatch, trading=trading, acc=acc, env=env)
    ok, reason = bc.capability("FUTU", op)
    if trading == "1" and scope_ok:
        assert (ok, reason) == (True, None)
    else:
        assert (ok, reason) == (False, "READ_ONLY_BROKER")


@pytest.mark.parametrize("op", _NEVER)
@pytest.mark.parametrize("trading", [None, "1"])
def test_futu_replace_combo_wizard_always_denied(monkeypatch, op, trading):
    _set(monkeypatch, trading=trading, acc="281756479345015383", env="SIMULATE")
    assert bc.capability("FUTU", op) == (False, "READ_ONLY_BROKER")
    assert bc.capability("futu", op) == (False, "READ_ONLY_BROKER")


def test_futu_default_config_denies_everything():
    for op in bc.OPERATIONS:
        assert bc.capability("FUTU", op) == (False, "READ_ONLY_BROKER")


@pytest.mark.parametrize("broker", ["", None, "SCHWAB", "IBKR"])
def test_unknown_broker_denied(broker):
    assert bc.capability(broker, "place_single") == (False, "READ_ONLY_BROKER")


@pytest.mark.parametrize("broker", ["IB", "FUTU"])
@pytest.mark.parametrize("op", [None, "", "place", "PLACE_SINGLE"])
def test_unknown_operation_denied(monkeypatch, broker, op):
    _set(monkeypatch, trading="1", acc="281756479345015383", env="SIMULATE")
    assert bc.capability(broker, op) == (False, "READ_ONLY_BROKER")


def test_evaluate_names_the_cause(monkeypatch):
    assert "XENON_FUTU_TRADING" in bc.evaluate("FUTU", "place_single")[2]
    _set(monkeypatch, trading="1")
    assert "FUTU_TRADE_ACC_ID" in bc.evaluate("FUTU", "place_single")[2]
    assert "replace" in bc.evaluate("FUTU", "replace")[2]
    assert bc.evaluate("IB", "replace") == (True, None, "")
