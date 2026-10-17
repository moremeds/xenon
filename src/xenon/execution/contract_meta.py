"""Qualified IB contract metadata for Gate 4 (PR-03b, spec §2.3 cover unit).

Coverage keys are (symbol, tradingClass, expiry, multiplier) and share cover
needs the underlying sec type. Those values must come from IB contract
qualification (``reqContractDetails``), never from the request body, the
symbol, or a universe default. Every lookup that cannot produce exactly one
qualified contract returns ``None`` — the caller fails closed.

Callers: the sync pass (holdings), the open-order adapter (mirrored orders)
and the place route (candidates).
"""

from __future__ import annotations

from typing import Any, Iterable


def _norm_expiry(value: Any) -> str | None:
    if value is None:
        return None
    digits = str(value).replace("-", "")
    return digits if len(digits) == 8 and digits.isdigit() else None


def _int_multiplier(raw: Any) -> int | None:
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return None
    if value <= 0 or value != int(value):
        return None
    return int(value)


def meta_from_details(details: Any) -> dict | None:
    """One ``ContractDetails`` → metadata dict, or None when incomplete."""
    contract = getattr(details, "contract", None)
    if contract is None:
        return None
    con_id = getattr(contract, "conId", None)
    try:
        con_id = int(con_id) if con_id else None
    except (TypeError, ValueError):
        con_id = None
    sec = str(getattr(contract, "secType", "") or "")
    tc = str(getattr(contract, "tradingClass", "") or "") or None
    mult = _int_multiplier(getattr(contract, "multiplier", None))
    und_sec = str(getattr(details, "underSecType", "") or "") or None
    if con_id is None or not sec:
        return None
    if sec == "OPT" and (tc is None or mult is None or und_sec is None):
        return None
    strike = getattr(contract, "strike", None)
    return {
        "con_id": con_id,
        "sym": str(getattr(contract, "symbol", "") or "").upper(),
        "sec": sec,
        "tc": tc,
        "mult": mult,
        "und_sec": und_sec,
        "exp": _norm_expiry(getattr(contract, "lastTradeDateOrContractMonth", None)),
        "right": (str(getattr(contract, "right", "") or "").upper() or None),
        "strike": float(strike) if strike not in (None, "") else None,
    }


def fetch_meta(client: Any, contract: Any) -> dict | None:
    """Qualify one contract via ``reqContractDetails``; None unless unique."""
    try:
        details = client.get_contract_details(contract)
    except Exception:  # noqa: BLE001 — any IB failure fails closed
        return None
    if not details or len(details) != 1:
        return None
    return meta_from_details(details[0])


def fetch_meta_by_con_ids(client: Any, con_ids: Iterable[Any]) -> dict[int, dict]:
    """Batch lookup by conId. Missing / ambiguous ids are simply absent."""
    from ib_async import Contract

    out: dict[int, dict] = {}
    for raw in con_ids:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid <= 0 or cid in out:
            continue
        meta = fetch_meta(client, Contract(conId=cid))
        if meta is not None and meta["con_id"] == cid:
            out[cid] = meta
    return out
