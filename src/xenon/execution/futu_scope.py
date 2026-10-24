"""Selected Futu trade scope: the one account Futu orders may act on.

`configured_scope()` reads the durable selection from env:
`FUTU_TRADE_ACC_ID` (exact canonical digits, nonzero, kept as a string) and
`FUTU_TRADE_ENV` (SIMULATE → paper, REAL → live). It is independent of the
capability flag `XENON_FUTU_TRADING`, so reads and reconciliation keep a scope
while trading is off or OpenD is offline.

`verify_live()` checks that scope against `OpenSecTradeContext.get_acc_list()`
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

from xenon.execution.account_scope import AccountScope, env_from_trd_env

ACC_ID_MISSING = "FUTU_TRADE_ACC_ID_MISSING"
ACC_ID_INVALID = "FUTU_TRADE_ACC_ID_INVALID"
TRD_ENV_MISSING = "FUTU_TRADE_ENV_MISSING"
TRD_ENV_INVALID = "FUTU_TRADE_ENV_INVALID"
SCOPE_INVALID = "FUTU_SCOPE_INVALID"
ACCOUNT_ABSENT = "FUTU_ACCOUNT_ABSENT"
ACCOUNT_AMBIGUOUS = "FUTU_ACCOUNT_AMBIGUOUS"
ACCOUNT_ENV_MISMATCH = "FUTU_ACCOUNT_ENV_MISMATCH"
ACCOUNT_NO_US_AUTHORITY = "FUTU_ACCOUNT_NO_US_AUTHORITY"
ACCOUNT_NOT_ACTIVE = "FUTU_ACCOUNT_NOT_ACTIVE"

_ACC_ID_RE = re.compile(r"[1-9][0-9]*")
_ENV_TO_TRD = {"paper": "SIMULATE", "live": "REAL"}


def configured_scope() -> tuple[AccountScope | None, str | None]:
    """Return `(scope, None)` or `(None, reason)` from FUTU_TRADE_ACC_ID + FUTU_TRADE_ENV."""
    acc_id = os.environ.get("FUTU_TRADE_ACC_ID", "")
    if not acc_id:
        return None, ACC_ID_MISSING
    if not _ACC_ID_RE.fullmatch(acc_id):
        return None, ACC_ID_INVALID
    trd_env = os.environ.get("FUTU_TRADE_ENV", "")
    if not trd_env:
        return None, TRD_ENV_MISSING
    try:
        account_env = env_from_trd_env(trd_env)
    except ValueError:
        return None, TRD_ENV_INVALID
    return AccountScope(broker="FUTU", account_env=account_env, broker_account=acc_id), None


def trd_env_for(scope: AccountScope) -> str:
    """Futu TrdEnv name ('SIMULATE' / 'REAL') of a Futu scope."""
    return _ENV_TO_TRD[scope.account_env]


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
