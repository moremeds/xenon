"""FutuClient._matched_trd_env regression tests (spec §10).

Validates the ground-truth env attribute that replaces the never-existing
`_matched_acc.trd_env` reference in the spec, AND fixes the silent-env-lie
bug at the connect-time fallback path.
"""
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from xenon.clients.futu_client import FutuClient


def _df(rows):
    """rows: list of (acc_id, trd_env_str). Returns DataFrame with TrdEnv enum values."""
    from futu import TrdEnv

    enum_map = {"REAL": TrdEnv.REAL, "SIMULATE": TrdEnv.SIMULATE}
    return pd.DataFrame([{"acc_id": aid, "trd_env": enum_map[env]} for aid, env in rows])


@pytest.fixture
def mock_ctx():
    """Yield a mock OpenSecTradeContext whose get_acc_list() the test sets."""
    from futu import RET_OK

    with patch("xenon.clients.futu_client.OpenSecTradeContext") as ctx_cls:
        ctx = MagicMock()
        ctx_cls.return_value = ctx
        ctx.get_acc_list.return_value = (RET_OK, pd.DataFrame())
        yield ctx


def test_matched_trd_env_is_REAL_when_real_account_present(mock_ctx):
    from futu import RET_OK

    mock_ctx.get_acc_list.return_value = (RET_OK, _df([(100, "REAL"), (200, "SIMULATE")]))
    c = FutuClient(trd_env="REAL")
    c.connect()
    assert c.trd_env_of_matched_account() == "REAL"
    assert c._acc_id == 100


def test_matched_trd_env_is_SIMULATE_when_fallback_path_hits(mock_ctx):
    """REGRESSION (pre-existing bug at line 184): fallback selects first row.
    Pre-fix: self.trd_env still reported "REAL" while connected to SIMULATE.
    Post-fix: trd_env_of_matched_account() returns the actual matched env."""
    from futu import RET_OK

    mock_ctx.get_acc_list.return_value = (RET_OK, _df([(300, "SIMULATE")]))
    c = FutuClient(trd_env="REAL")  # request REAL, only SIMULATE present
    c.connect()
    assert c.trd_env_of_matched_account() == "SIMULATE"  # ground truth
    assert c.trd_env == "REAL"  # legacy field — for logging only
    assert c._acc_id == 300


def test_matched_trd_env_clears_on_disconnect(mock_ctx):
    from futu import RET_OK

    mock_ctx.get_acc_list.return_value = (RET_OK, _df([(100, "REAL")]))
    c = FutuClient(trd_env="REAL")
    c.connect()
    assert c.trd_env_of_matched_account() == "REAL"
    c.disconnect()
    assert c.trd_env_of_matched_account() is None
    assert c._acc_id is None


def test_matched_trd_env_repopulates_after_reconnect(mock_ctx):
    from futu import RET_OK

    mock_ctx.get_acc_list.return_value = (RET_OK, _df([(100, "SIMULATE")]))
    c = FutuClient(trd_env="REAL")
    c.connect()
    assert c.trd_env_of_matched_account() == "SIMULATE"
    c.disconnect()
    mock_ctx.get_acc_list.return_value = (RET_OK, _df([(200, "REAL")]))
    c.connect()
    assert c.trd_env_of_matched_account() == "REAL"
    assert c._acc_id == 200
