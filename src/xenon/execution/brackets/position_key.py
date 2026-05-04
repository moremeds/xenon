"""Opaque deterministic position key. Spec §5.3."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _format_strike(s: float) -> str:
    """Strip trailing zeros: 315.0 -> '315', 580.5 -> '580.5'."""
    if s == int(s):
        return str(int(s))
    return str(s).rstrip("0").rstrip(".")


def _canonicalize_legs(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort legs by stable structure-defining fields for key invariance."""

    def sort_key(leg):
        return (
            leg.get("right") or "",
            leg.get("strike") or 0.0,
            leg.get("expiry") or "",
            leg.get("action") or "",
        )

    return sorted(legs, key=sort_key)


def compute_position_key(asset_class: str, descriptor: dict[str, Any]) -> str:
    legs = list(descriptor.get("legs") or [])
    if not legs:
        raise ValueError("descriptor.legs is required to compute position_key")

    if asset_class == "stock":
        return f"STK::{legs[0]['symbol']}"

    if asset_class == "long_option":
        leg = legs[0]
        return (
            f"OPT::{leg['symbol']}::{leg['expiry']}::"
            f"{_format_strike(leg['strike'])}::{leg['right']}"
        )

    if asset_class == "covered_call":
        call = next((l for l in legs if l.get("right") == "C" and l.get("action") == "SELL"), None)
        if call is None:
            raise ValueError("covered_call descriptor missing short call leg")
        return f"CC::{call['symbol']}::{call['expiry']}::{_format_strike(call['strike'])}"

    if asset_class == "credit_spread":
        short = next((l for l in legs if l.get("action") == "SELL"), None)
        long_ = next((l for l in legs if l.get("action") == "BUY"), None)
        if short is None or long_ is None:
            raise ValueError("credit_spread requires one SELL leg and one BUY leg")
        return (
            f"CS::{short['symbol']}::{short['expiry']}::"
            f"{_format_strike(short['strike'])}::{_format_strike(long_['strike'])}::{short['right']}"
        )

    if asset_class == "debit_combo":
        canon = _canonicalize_legs(legs)
        slim = [
            {
                k: leg.get(k)
                for k in ("sec_type", "symbol", "expiry", "strike", "right", "action", "ratio")
            }
            for leg in canon
        ]
        digest = hashlib.sha256(
            json.dumps(slim, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        first_symbol = canon[0]["symbol"]
        return f"COMBO::{digest}::{first_symbol}"

    if asset_class == "unclassified":
        return f"UNCL::{legs[0].get('symbol', 'UNKNOWN')}"

    raise ValueError(f"Unknown asset_class: {asset_class!r}")
