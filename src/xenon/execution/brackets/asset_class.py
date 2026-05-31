"""Asset-class classifier. Spec §6.2."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AssetClass(StrEnum):
    STOCK = "stock"
    LONG_OPTION = "long_option"
    DEBIT_COMBO = "debit_combo"
    CREDIT_SPREAD = "credit_spread"
    COVERED_CALL = "covered_call"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class ClassifyResult:
    asset_class: AssetClass
    reason: str | None = None


def _is_credit_spread(legs: list[dict[str, Any]]) -> bool:
    if len(legs) != 2:
        return False
    a, b = legs
    if a.get("sec_type") != "OPT" or b.get("sec_type") != "OPT":
        return False
    if a.get("symbol") != b.get("symbol"):
        return False
    if a.get("expiry") != b.get("expiry"):
        return False
    if a.get("right") != b.get("right"):
        return False
    actions = {a.get("action"), b.get("action")}
    return actions == {"BUY", "SELL"}


def _is_credit(legs: list[dict[str, Any]]) -> bool:
    """Return True for short-put-credit or short-call-credit vertical shape."""
    short = next((l for l in legs if l.get("action") == "SELL"), None)
    long_ = next((l for l in legs if l.get("action") == "BUY"), None)
    if not short or not long_:
        return False
    if short["right"] == "P":
        return short["strike"] > long_["strike"]
    if short["right"] == "C":
        return short["strike"] < long_["strike"]
    return False


def _is_debit_combo(legs: list[dict[str, Any]]) -> bool:
    if len(legs) != 2:
        return False
    if not all(l.get("sec_type") == "OPT" for l in legs):
        return False
    if _is_credit_spread(legs) and _is_credit(legs):
        return False
    return True


def _is_covered_call(legs: list[dict[str, Any]]) -> bool:
    if len(legs) != 2:
        return False
    stock = next((l for l in legs if l.get("sec_type") == "STK" and l.get("action") == "BUY"), None)
    short_call = next(
        (
            l
            for l in legs
            if l.get("sec_type") == "OPT" and l.get("action") == "SELL" and l.get("right") == "C"
        ),
        None,
    )
    if not stock or not short_call:
        return False
    return stock["symbol"] == short_call["symbol"]


def classify_position(
    *,
    legs: list[dict[str, Any]],
    wizard_session_payload: dict[str, Any] | None,
    sibling_legs: list[dict[str, Any]] | None,
) -> ClassifyResult:
    if wizard_session_payload is not None:
        asset_class = wizard_session_payload.get("asset_class")
        if asset_class in {a.value for a in AssetClass}:
            return ClassifyResult(asset_class=AssetClass(asset_class))

    if not legs:
        return ClassifyResult(asset_class=AssetClass.UNCLASSIFIED, reason="empty_legs")

    if len(legs) == 1:
        if sibling_legs:
            return ClassifyResult(
                asset_class=AssetClass.UNCLASSIFIED,
                reason="manual_multi_leg_unsupported",
            )
        leg = legs[0]
        if leg.get("sec_type") == "STK":
            return ClassifyResult(asset_class=AssetClass.STOCK)
        if leg.get("sec_type") == "OPT" and leg.get("action") == "BUY":
            return ClassifyResult(asset_class=AssetClass.LONG_OPTION)
        return ClassifyResult(asset_class=AssetClass.UNCLASSIFIED, reason="single_leg_unsupported_shape")

    if _is_covered_call(legs):
        return ClassifyResult(asset_class=AssetClass.COVERED_CALL)

    if _is_credit_spread(legs) and _is_credit(legs):
        return ClassifyResult(asset_class=AssetClass.CREDIT_SPREAD)

    if _is_debit_combo(legs):
        return ClassifyResult(asset_class=AssetClass.DEBIT_COMBO)

    return ClassifyResult(asset_class=AssetClass.UNCLASSIFIED, reason="unrecognized_structure")
