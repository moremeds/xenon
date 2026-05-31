"""IB executor subprocess shim. Spec §4.3 and §10.2."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from xenon.execution.account_scope import AccountScope


@dataclass(frozen=True)
class PlaceResult:
    perm_id: int | None
    ib_order_id: int | None
    status: str
    raw: dict[str, Any]


def _scope_env(scope: AccountScope) -> dict[str, str]:
    env = os.environ.copy()
    env["XENON_TRADING_MODE"] = scope.account_env
    env["XENON_BROKER_ACCOUNT"] = scope.broker_account
    env["XENON_BROKER"] = scope.broker
    return env


def _parse_place_result(stdout: str) -> PlaceResult:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"xenon-ib-place-order produced unparseable stdout: {stdout[:200]}") from exc

    return PlaceResult(
        perm_id=payload.get("perm_id") or payload.get("permId"),
        ib_order_id=payload.get("ib_order_id") or payload.get("orderId"),
        status=payload.get("status") or payload.get("initialStatus") or "Unknown",
        raw=payload,
    )


def _extract_error(completed: subprocess.CompletedProcess) -> str:
    for stream in (completed.stderr, completed.stdout):
        if not stream:
            continue
        try:
            payload = json.loads(stream)
        except json.JSONDecodeError:
            if stream.strip():
                return stream.strip()
            continue
        return payload.get("message") or payload.get("detail") or payload.get("error") or str(payload)
    return f"subprocess exited {completed.returncode}"


def _run_place_order(payload: dict[str, Any], scope: AccountScope) -> PlaceResult:
    cmd = ["xenon-ib-place-order", "--json", json.dumps(payload)]
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_scope_env(scope),
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"xenon-ib-place-order failed: {_extract_error(completed)}")
    return _parse_place_result(completed.stdout)


class IBExecutor:
    def attach_native_stp(
        self,
        *,
        scope: AccountScope,
        con_id: int,
        symbol: str,
        sec_type: str,
        close_action: str,
        qty: int,
        stop_price: float,
        tif: str = "GTC",
        order_ref: str | None = None,
    ) -> PlaceResult:
        payload = {
            "conId": con_id,
            "symbol": symbol,
            "secType": sec_type,
            "action": close_action,
            "quantity": qty,
            "qty": qty,
            "orderType": "STP",
            "stopPrice": stop_price,
            "tif": tif,
            "outsideRth": False,
        }
        if order_ref is not None:
            payload["orderRef"] = order_ref
        return _run_place_order(payload, scope)

    def flatten_mkt(
        self,
        *,
        scope: AccountScope,
        con_id: int,
        symbol: str,
        sec_type: str,
        close_action: str,
        qty: int,
        order_ref: str,
    ) -> PlaceResult:
        payload = {
            "conId": con_id,
            "symbol": symbol,
            "secType": sec_type,
            "action": close_action,
            "quantity": qty,
            "qty": qty,
            "orderType": "MKT",
            "tif": "DAY",
            "outsideRth": False,
            "orderRef": order_ref,
        }
        return _run_place_order(payload, scope)

    def flatten_combo_mkt(
        self,
        *,
        scope: AccountScope,
        symbol: str,
        legs: list[dict[str, Any]],
        qty: int,
        order_ref: str,
    ) -> PlaceResult:
        payload = {
            "type": "combo",
            "symbol": symbol,
            "action": "SELL",
            "quantity": qty,
            "qty": qty,
            "orderType": "MKT",
            "tif": "DAY",
            "outsideRth": False,
            "orderRef": order_ref,
            "legs": legs,
        }
        return _run_place_order(payload, scope)

    def cancel(self, *, scope: AccountScope, perm_id: int) -> dict[str, Any]:
        cmd = ["xenon-ib-order-manage", "cancel", "--perm-id", str(perm_id)]
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=_scope_env(scope),
            timeout=30,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"xenon-ib-order-manage cancel failed: {_extract_error(completed)}")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"xenon-ib-order-manage produced unparseable stdout: {completed.stdout[:200]}"
            ) from exc
