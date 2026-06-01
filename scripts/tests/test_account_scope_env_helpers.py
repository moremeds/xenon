"""Tests for env_from_trd_env helper + FUTU rejection (spec §10).

Correction #18 (perf-rebuild plan): SIMULATE→"paper" (aligned with IB's
existing account_env convention) NOT "sim".
"""
import os

import pytest

from xenon.execution.account_scope import env_from_trd_env, resolve_from_env


def test_env_from_trd_env_REAL_maps_to_live():
    assert env_from_trd_env("REAL") == "live"


def test_env_from_trd_env_SIMULATE_maps_to_paper():
    """Correction #18: SIMULATE→paper, NOT sim. Aligned with IB convention."""
    assert env_from_trd_env("SIMULATE") == "paper"


@pytest.mark.parametrize("bad", ["", "real", "simulate", "XYZ", "PAPER", None])
def test_env_from_trd_env_rejects_unknown(bad):
    with pytest.raises((ValueError, TypeError)):
        env_from_trd_env(bad)


def test_resolve_from_env_rejects_FUTU(monkeypatch):
    monkeypatch.setenv("XENON_BROKER", "FUTU")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "42")
    with pytest.raises(ValueError, match="FUTU"):
        resolve_from_env()


def test_resolve_from_env_still_works_for_IB_paper(monkeypatch):
    monkeypatch.setenv("XENON_BROKER", "IB")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU0000000")
    # importlib reload of trading_mode happens inside resolve_from_env
    scope = resolve_from_env()
    assert scope.broker == "IB"
    assert scope.account_env == "paper"
    assert scope.broker_account == "DU0000000"
