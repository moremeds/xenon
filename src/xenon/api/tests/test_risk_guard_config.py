"""Risk guard runtime settings (registry entries + effective values)."""

import pytest

from xenon import runtime_settings as rs
from xenon.api.services.risk_guard import config
from xenon.api.services.risk_guard.incidents import Thresholds


def test_enabled_follows_env(monkeypatch):
    monkeypatch.delenv("XENON_RISK_GUARD", raising=False)
    assert config.enabled() is False
    monkeypatch.setenv("XENON_RISK_GUARD", "1")
    assert config.enabled() is True


def test_db_override_wins(monkeypatch):
    monkeypatch.setenv("XENON_RISK_GUARD", "1")
    monkeypatch.setattr(rs, "override", lambda key: {"XENON_RISK_GUARD": False}.get(key))
    assert config.enabled() is False


def test_turning_off_needs_confirm():
    assert rs.REGISTRY["XENON_RISK_GUARD"].confirm(True, False) is True
    assert rs.REGISTRY["XENON_RISK_GUARD"].confirm(False, True) is False


def test_thresholds_default_and_partial_override(monkeypatch):
    assert config.thresholds() == Thresholds()
    over = {"XENON_RISK_THRESHOLDS": {"crash_day": -0.04, "leverage_levels": [0.6, 0.8, 1.2]}}
    monkeypatch.setattr(rs, "override", lambda key: over.get(key))
    th = config.thresholds()
    assert th.crash_day == -0.04 and th.leverage_levels == (0.6, 0.8, 1.2) and th.fast_day == -0.02


@pytest.mark.parametrize(
    "value",
    [
        {"nope": 1},
        {"crash_day": 0.1},
        {"crash_day": "x"},
        {"leverage_levels": [1.0, 0.5, 0.75]},
        {"drawdown_levels": [-0.15, -0.10, -0.05]},
        {"leverage_levels": [0.5, 0.75]},
        [],
    ],
)
def test_threshold_validator_refuses(value):
    with pytest.raises(rs.SettingError):
        rs.validate("XENON_RISK_THRESHOLDS", value)


def test_threshold_validator_accepts():
    assert rs.validate("XENON_RISK_THRESHOLDS", {"drawdown_levels": [-0.04, -0.08, -0.12]})[1] == {
        "drawdown_levels": [-0.04, -0.08, -0.12]
    }


def test_key_levels(monkeypatch):
    assert config.key_levels() == ()
    _, levels = rs.validate("XENON_RISK_KEY_LEVELS", [7500, 7400.5, 7500])
    assert levels == [7500, 7400.5]
    with pytest.raises(rs.SettingError):
        rs.validate("XENON_RISK_KEY_LEVELS", [-1])
    monkeypatch.setattr(rs, "override", lambda key: {"XENON_RISK_KEY_LEVELS": levels}.get(key))
    assert config.key_levels() == (7500, 7400.5)


def test_watch_symbols(monkeypatch):
    assert config.watch_symbols() == ()
    _, syms = rs.validate("XENON_RISK_WATCH_SYMBOLS", ["RUT", "NDX", "RUT"])
    assert syms == ["NDX", "RUT"]
    for bad in (["VIX"], ["SPX"], "NDX", [1]):
        with pytest.raises(rs.SettingError):
            rs.validate("XENON_RISK_WATCH_SYMBOLS", bad)
    monkeypatch.setattr(rs, "override", lambda key: {"XENON_RISK_WATCH_SYMBOLS": syms}.get(key))
    assert config.watch_symbols() == ("NDX", "RUT")
