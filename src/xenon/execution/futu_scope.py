"""Configured Futu trade scopes: the accounts Futu orders may act on.

One optional account per environment (§11, SIMULATE / REAL switch):
`FUTU_TRADE_ACC_ID_SIMULATE` (paper) and `FUTU_TRADE_ACC_ID_REAL` (live),
each exact canonical digits, nonzero, kept as a string. The request picks the
environment with the query parameter `account_env` (`paper` | `live`); the
client never sends an account id. The selection is independent of the
capability flag `XENON_FUTU_TRADING`, so reads and reconciliation keep a scope
while trading is off or OpenD is offline.

`verify_live()` checks a scope against `OpenSecTradeContext.get_acc_list()`
rows (futu-api 10.3: `acc_id` int, `trd_env` 'REAL'/'SIMULATE',
`trdmarket_auth` list of market strings, `acc_status` 'ACTIVE'/'DISABLED').
Exact acc_id + trd_env + US market authority + ACTIVE. There is no
first-account fallback.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

from xenon.execution.account_scope import AccountScope

ACCOUNT_ENVS = ("paper", "live")  # SIMULATE first
ACC_ID_ENV = {"paper": "FUTU_TRADE_ACC_ID_SIMULATE", "live": "FUTU_TRADE_ACC_ID_REAL"}

NOT_CONFIGURED = "not_configured"
ACC_ID_INVALID = "FUTU_TRADE_ACC_ID_INVALID"
SCOPE_INVALID = "FUTU_SCOPE_INVALID"
ACCOUNT_ABSENT = "FUTU_ACCOUNT_ABSENT"
ACCOUNT_AMBIGUOUS = "FUTU_ACCOUNT_AMBIGUOUS"
ACCOUNT_ENV_MISMATCH = "FUTU_ACCOUNT_ENV_MISMATCH"
ACCOUNT_NO_US_AUTHORITY = "FUTU_ACCOUNT_NO_US_AUTHORITY"
ACCOUNT_NOT_ACTIVE = "FUTU_ACCOUNT_NOT_ACTIVE"

_ACC_ID_RE = re.compile(r"[1-9][0-9]*")
_ENV_TO_TRD = {"paper": "SIMULATE", "live": "REAL"}


def parse_account_env(value: Any) -> str | None:
    """`paper` / `live`, else None (missing or invalid)."""
    return value if isinstance(value, str) and value in ACC_ID_ENV else None


def configured_scope(account_env: str | None) -> tuple[AccountScope | None, str | None]:
    """Return `(scope, None)` or `(None, reason)` for one environment."""
    if parse_account_env(account_env) is None:
        return None, SCOPE_INVALID
    acc_id = os.environ.get(ACC_ID_ENV[account_env], "")
    if not acc_id:
        return None, NOT_CONFIGURED
    if not _ACC_ID_RE.fullmatch(acc_id):
        return None, ACC_ID_INVALID
    return AccountScope(broker="FUTU", account_env=account_env, broker_account=acc_id), None


def configured_scopes() -> dict[str, AccountScope]:
    """`{account_env: scope}` for every configured environment, paper first."""
    return {env: scope for env in ACCOUNT_ENVS if (scope := configured_scope(env)[0]) is not None}


def default_read_env() -> str:
    """Read env when the request names none: paper when SIMULATE is configured, else live."""
    return "paper" if configured_scope("paper")[0] is not None else "live"


def trd_env_for(scope: AccountScope) -> str:
    """Futu TrdEnv name ('SIMULATE' / 'REAL') of a Futu scope."""
    return _ENV_TO_TRD[scope.account_env]


def new_client(scope: AccountScope | None):
    """A FutuClient for one scope, OpenD address from env (`FUTU_OPEND_HOST`/`_PORT`).

    With a scope: exactly that account and TrdEnv, no fallback. None: the
    legacy client (`FUTU_TRD_ENV`, first account of that env).
    """
    from xenon.clients.futu_client import FutuClient

    return FutuClient(
        host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
        port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
        security_firm=os.environ.get("FUTU_SECURITY_FIRM", "FUTUSECURITIES"),
        trd_env=trd_env_for(scope) if scope else os.environ.get("FUTU_TRD_ENV", "REAL"),
        filter_trading_market=os.environ.get("FUTU_MARKET", "US"),
        acc_id=int(scope.broker_account) if scope else None,
    )


def verify_live(scope: AccountScope, acc_rows: Iterable[Mapping[str, Any]] | Any) -> tuple[bool, str | None]:
    """Pure check of `scope` against `get_acc_list()` rows (a DataFrame or row dicts)."""
    trd_env = _ENV_TO_TRD.get(scope.account_env)
    if scope.broker != "FUTU" or trd_env is None:
        return False, SCOPE_INVALID
    rows = acc_rows.to_dict("records") if hasattr(acc_rows, "to_dict") else list(acc_rows)
    matches = [r for r in rows if str(r.get("acc_id")) == scope.broker_account]
    if not matches:
        return False, ACCOUNT_ABSENT
    if len(matches) > 1:
        return False, ACCOUNT_AMBIGUOUS
    row = matches[0]
    if row.get("trd_env") != trd_env:
        return False, ACCOUNT_ENV_MISMATCH
    auth = row.get("trdmarket_auth")
    if not isinstance(auth, (list, tuple)) or "US" not in auth:
        return False, ACCOUNT_NO_US_AUTHORITY
    if row.get("acc_status") != "ACTIVE":
        return False, ACCOUNT_NOT_ACTIVE
    return True, None
