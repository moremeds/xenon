"""Verify IB_GATEWAY_HOST is loaded from env / .env before defaults are snapshotted.

`IB_GATEWAY_PORT` is no longer consulted — port selection is owned by
`XENON_TRADING_MODE` via `xenon.api.trading_mode`. These tests cover only
the host-side env loading. Mode→port wiring lives in
`src/xenon/api/tests/test_trading_mode.py`.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parent.parent.parent / "src" / "xenon" / "clients" / "ib_client.py"


def _load_ib_client_module(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules.pop(module_name, None)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_ib_client_default_host_reads_env():
    """After reloading ib_client with IB_GATEWAY_HOST set, DEFAULT_HOST must reflect it."""
    with patch.dict(os.environ, {"IB_GATEWAY_HOST": "test-cloud-host"}):
        ib_client = _load_ib_client_module("tests._isolated_ib_client_env_set")
        assert ib_client.DEFAULT_HOST == "test-cloud-host"


def test_ib_client_default_host_fallback():
    """Without IB_GATEWAY_HOST in env, DEFAULT_HOST falls back to 127.0.0.1."""
    with patch.dict(os.environ, {}, clear=True):
        # Patch at dotenv module level so reload can't re-import the real one
        import dotenv

        with patch.object(dotenv, "load_dotenv", return_value=False):
            ib_client = _load_ib_client_module("tests._isolated_ib_client_env_fallback")
            assert ib_client.DEFAULT_HOST == "127.0.0.1"


def test_ib_client_loads_env_before_defaults_are_snapshotted():
    """ib_client must observe env populated by load_dotenv during import."""
    with patch.dict(os.environ, {}, clear=True):
        import dotenv

        def fake_load_dotenv(*args, **kwargs):
            os.environ["IB_GATEWAY_HOST"] = "dotenv-host"
            return True

        with patch.object(dotenv, "load_dotenv", side_effect=fake_load_dotenv):
            ib_client = _load_ib_client_module("tests._isolated_ib_client_env_dotenv")
            assert ib_client.DEFAULT_HOST == "dotenv-host"
