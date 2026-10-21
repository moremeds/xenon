"""IB account selection never follows IB's managedAccounts() list order.

Prod incident 2026-10-17: the live login manages two accounts. Prod .env set
XENON_BROKER_ACCOUNT to one of them, but the API lifespan and the sync CLIs
used managedAccounts()[0] — the other one — so the boot scope, the portfolio
snapshots and (after the order-account fix) new orders silently followed IB's
list order instead of the configured account.

Rule (xenon.execution.account_scope.select_ib_account):
- XENON_BROKER_ACCOUNT set → it must be managed, else unresolved.
- unset + exactly one managed account → that account.
- unset + zero or several managed accounts → unresolved.

Fake IB only — no network.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from xenon.api import server as server_mod
from xenon.api import trading_mode
from xenon.api.subprocess import ScriptResult
from xenon.execution.account_scope import IBAccountSelectionError, select_ib_account
from xenon.api.services import order_entry

# Captured at collection time, before the autouse conftest fixture swaps the
# module attribute for a constant lambda.
_REAL_GET_MANAGED_ACCOUNT = server_mod._get_managed_account_for_health

TWO = ["DU1111111", "DU2222222"]


# ---------------------------------------------------------------------------
# select_ib_account — the shared rule
# ---------------------------------------------------------------------------


def test_select_env_names_second_of_two_managed():
    assert select_ib_account(TWO, "DU2222222") == "DU2222222"


def test_select_env_names_unmanaged_account_raises():
    with pytest.raises(IBAccountSelectionError, match="not managed"):
        select_ib_account(TWO, "DU9999999")


@pytest.mark.parametrize("configured", ["", "   "])
def test_select_env_unset_with_two_managed_raises(configured):
    with pytest.raises(IBAccountSelectionError, match="2 accounts"):
        select_ib_account(TWO, configured)


def test_select_env_unset_with_one_managed_returns_it():
    assert select_ib_account(["DU1111111"], "") == "DU1111111"


def test_select_env_unset_with_none_managed_raises():
    with pytest.raises(IBAccountSelectionError):
        select_ib_account([], "")


def test_select_reads_env_by_default(monkeypatch):
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU2222222")
    assert select_ib_account(TWO) == "DU2222222"


# ---------------------------------------------------------------------------
# FastAPI lifespan scope (server._get_managed_account_for_health)
# ---------------------------------------------------------------------------


class _Pool:
    def __init__(self, managed):
        self._client = SimpleNamespace(ib=SimpleNamespace(managedAccounts=lambda: list(managed)))

    def get(self, _role):
        return self._client


def _boot_scope(monkeypatch, managed, env):
    """Mirror the lifespan: resolve the account, then verify it."""
    monkeypatch.setattr(server_mod, "ib_pool", _Pool(managed))
    if env is None:
        monkeypatch.delenv("XENON_BROKER_ACCOUNT", raising=False)
    else:
        monkeypatch.setenv("XENON_BROKER_ACCOUNT", env)
    account = _REAL_GET_MANAGED_ACCOUNT()
    return account, trading_mode.verify_account(account)


def test_boot_scope_env_names_second_of_two(monkeypatch):
    account, verified = _boot_scope(monkeypatch, TWO, "DU2222222")
    assert account == "DU2222222"
    assert verified is True


def test_boot_scope_env_unset_with_two_is_unverified(monkeypatch):
    account, verified = _boot_scope(monkeypatch, TWO, None)
    assert account == ""
    assert verified is False


def test_boot_scope_env_unset_with_one_uses_it(monkeypatch):
    account, verified = _boot_scope(monkeypatch, ["DU1111111"], None)
    assert account == "DU1111111"
    assert verified is True


@pytest.mark.asyncio
async def test_boot_scope_env_unmanaged_is_unverified_and_places_nothing(monkeypatch):
    account, verified = _boot_scope(monkeypatch, TWO, "DU9999999")
    assert account == ""
    assert verified is False

    monkeypatch.setattr(server_mod.app.state, "account", account, raising=False)
    monkeypatch.setattr(server_mod.app.state, "mode_verified", verified, raising=False)
    monkeypatch.setattr(server_mod.app.state, "trading_mode", trading_mode.MODE, raising=False)
    calls = []

    async def _runner(entry, args, timeout=30):
        calls.append(entry)
        return ScriptResult(ok=True, data={"status": "ok"})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", _runner)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await order_entry._orders_place_from_body(
            {"type": "stock", "symbol": "SPY", "action": "BUY", "quantity": 1, "limitPrice": 1.0}
        )

    assert exc_info.value.status_code == 503
    assert calls == []


# ---------------------------------------------------------------------------
# CLIs never overwrite a configured XENON_BROKER_ACCOUNT
# ---------------------------------------------------------------------------


def _ib_orders_main(monkeypatch, managed, env):
    from xenon.execution import ib_orders

    client = MagicMock()
    client.ib.managedAccounts.return_value = list(managed)
    monkeypatch.setattr(ib_orders, "connect_ib", lambda *a, **k: client)
    monkeypatch.setattr(ib_orders, "fetch_open_orders", lambda _c: [])
    monkeypatch.setattr(ib_orders, "fetch_executed_orders", lambda _c: [])
    synced = []

    def _sync(orders, **_k):
        import os

        synced.append(os.environ.get("XENON_BROKER_ACCOUNT"))
        return {"registered": 0}

    monkeypatch.setattr(ib_orders, "sync_open_orders_to_postgres", _sync)
    monkeypatch.setattr(sys, "argv", ["xenon-ib-orders", "--sync"])
    if env is None:
        monkeypatch.delenv("XENON_BROKER_ACCOUNT", raising=False)
    else:
        monkeypatch.setenv("XENON_BROKER_ACCOUNT", env)
    ib_orders.main()
    return synced


def test_ib_orders_sync_keeps_configured_second_account(monkeypatch):
    assert _ib_orders_main(monkeypatch, TWO, "DU2222222") == ["DU2222222"]


def test_ib_orders_sync_env_unset_two_accounts_refuses(monkeypatch):
    with pytest.raises(SystemExit) as exc_info:
        _ib_orders_main(monkeypatch, TWO, None)
    assert exc_info.value.code == 1


def test_ib_orders_sync_env_unmanaged_refuses(monkeypatch):
    with pytest.raises(SystemExit):
        _ib_orders_main(monkeypatch, TWO, "DU9999999")


def test_ib_orders_sync_env_unset_one_account_uses_it(monkeypatch):
    assert _ib_orders_main(monkeypatch, ["DU1111111"], None) == ["DU1111111"]


class _StopAfterScope(Exception):
    pass


def _ib_sync_main(monkeypatch, managed, env):
    from xenon.execution import ib_sync

    client = MagicMock()
    client.ib.managedAccounts.return_value = list(managed)
    monkeypatch.setattr(ib_sync, "connect_ib", lambda *a, **k: client)
    monkeypatch.setattr(ib_sync, "get_account_summary", lambda _c, _acct: {})
    monkeypatch.setattr(ib_sync, "get_fx_rates", lambda _c, _acct: {"USD": 1.0})

    def _stop(_c, _acct=None, **_k):
        raise _StopAfterScope

    monkeypatch.setattr(ib_sync, "fetch_positions", _stop)
    monkeypatch.setattr(sys, "argv", ["xenon-ib-sync", "--sync"])
    if env is None:
        monkeypatch.delenv("XENON_BROKER_ACCOUNT", raising=False)
    else:
        monkeypatch.setenv("XENON_BROKER_ACCOUNT", env)
    return client, ib_sync


def test_ib_sync_keeps_configured_second_account(monkeypatch):
    import os

    client, ib_sync = _ib_sync_main(monkeypatch, TWO, "DU2222222")
    with pytest.raises(_StopAfterScope):
        ib_sync.main()
    assert os.environ["XENON_BROKER_ACCOUNT"] == "DU2222222"
    client.ib.reqPnL.assert_called_once_with("DU2222222")


@pytest.mark.parametrize("env", [None, "DU9999999"])
def test_ib_sync_unresolved_account_aborts_before_positions(monkeypatch, env):
    client, ib_sync = _ib_sync_main(monkeypatch, TWO, env)
    with pytest.raises(SystemExit) as exc_info:
        ib_sync.main()
    assert exc_info.value.code == 1
    client.ib.reqPnL.assert_not_called()


def test_ib_sync_env_unset_one_account_exports_it(monkeypatch):
    import os

    client, ib_sync = _ib_sync_main(monkeypatch, ["DU1111111"], None)
    with pytest.raises(_StopAfterScope):
        ib_sync.main()
    assert os.environ["XENON_BROKER_ACCOUNT"] == "DU1111111"


def test_ib_sync_pnl_helpers_do_not_default_to_first_account(monkeypatch):
    from xenon.execution import ib_sync

    monkeypatch.delenv("XENON_BROKER_ACCOUNT", raising=False)
    client = MagicMock()
    client.ib.managedAccounts.return_value = list(TWO)

    positions = [{"conId": 1, "symbol": "SPY"}]
    assert ib_sync.fetch_position_daily_pnl(client, positions) == positions
    client.ib.reqPnLSingle.assert_not_called()

    assert ib_sync.get_pnl(client) == {}
    client.get_pnl.assert_not_called()


# ---------------------------------------------------------------------------
# ib_sync reads only the scope account's data
# ---------------------------------------------------------------------------
# ib.accountValues() / ib.positions() without an account span every managed
# account. Unfiltered, each summary tag kept whichever account's row came
# last, so NAV / bankroll / summary mixed both accounts.


def _av(account, tag, value, currency="USD"):
    return SimpleNamespace(account=account, tag=tag, value=value, currency=currency)


def test_account_summary_uses_only_scope_account_values():
    from xenon.execution import ib_sync

    # Scope account's rows first, so an unfiltered read is overwritten by
    # the foreign account's values.
    avs = [
        _av("DU2222222", "NetLiquidation", "200000"),
        _av("DU2222222", "TotalCashValue", "50000"),
        _av("DU1111111", "NetLiquidation", "999999"),
        _av("DU1111111", "TotalCashValue", "888888"),
    ]
    client = SimpleNamespace(ib=SimpleNamespace(accountValues=lambda: list(avs)))

    summary = ib_sync.get_account_summary(client, "DU2222222")

    assert summary["NetLiquidation"] == 200000.0
    assert summary["TotalCashValue"] == 50000.0


def test_fx_rates_use_only_scope_account_rows():
    from xenon.execution import ib_sync

    avs = [
        _av("DU2222222", "ExchangeRate", "0.006186", "JPY"),
        _av("DU1111111", "ExchangeRate", "0.5", "JPY"),
    ]
    client = SimpleNamespace(ib=SimpleNamespace(accountValues=lambda: list(avs)))

    assert ib_sync.get_fx_rates(client, "DU2222222")["JPY"] == 0.006186


def test_fetch_positions_keeps_only_scope_account_rows():
    from xenon.execution import ib_sync

    def _pos(account, symbol):
        contract = SimpleNamespace(
            symbol=symbol, secType="STK", conId=hash(symbol) % 1000, currency="USD", primaryExchange="", exchange=""
        )
        return SimpleNamespace(account=account, contract=contract, avgCost=1.0, position=10)

    client = SimpleNamespace(
        get_positions=lambda: [_pos("DU2222222", "SPY"), _pos("DU1111111", "QQQ"), _pos("", "IWM")]
    )

    assert [p["symbol"] for p in ib_sync.fetch_positions(client, "DU2222222")] == ["SPY"]


def test_ib_sync_main_reads_summary_and_positions_for_scope_account(monkeypatch):
    client, ib_sync = _ib_sync_main(monkeypatch, TWO, "DU2222222")
    seen = {}
    monkeypatch.setattr(ib_sync, "get_account_summary", lambda _c, acct: seen.setdefault("summary", acct) and {})
    monkeypatch.setattr(ib_sync, "get_fx_rates", lambda _c, acct: seen.setdefault("fx", acct) and {"USD": 1.0})

    def _positions(_c, acct, **_k):
        seen["positions"] = acct
        raise _StopAfterScope

    monkeypatch.setattr(ib_sync, "fetch_positions", _positions)

    with pytest.raises(_StopAfterScope):
        ib_sync.main()

    assert seen == {"summary": "DU2222222", "fx": "DU2222222", "positions": "DU2222222"}
