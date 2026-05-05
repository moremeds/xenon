"""Arm-hook outbox consumer. Spec §6.1, §6.2, §6.3."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from xenon.db.engine import get_sync_engine
from xenon.db.events import emit_outbox_in_txn
from xenon.db.queries.bracket_policies import resolve_for_scope
from xenon.db.queries.position_protection import insert_pending_arm
from xenon.db.schema import order_fills, wizard_combo_attempts
from xenon.execution.brackets.asset_class import AssetClass, classify_position
from xenon.execution.brackets.policies import deduplicate_by_specificity
from xenon.execution.brackets.position_key import compute_position_key

logger = logging.getLogger(__name__)


def on_fill_event(engine: Engine | None = None, payload: dict[str, Any] | None = None) -> None:
    """Handle one durable fill.recorded payload in an independent transaction."""
    if engine is None:
        engine = get_sync_engine()
    if payload is None:
        return
    _process(engine, payload)


def _process(engine: Engine, payload: dict[str, Any]) -> None:
    exec_id = payload["exec_id"]
    broker = payload["broker"]
    account_env = payload["account_env"]
    broker_account = payload["broker_account"]
    combo_attempt_id = payload.get("combo_attempt_id")

    with engine.connect() as conn:
        fill_row = conn.execute(select(order_fills).where(order_fills.c.exec_id == exec_id)).first()
    if fill_row is None:
        logger.warning("arm_hook: fill %s not found", exec_id)
        return
    fill = dict(fill_row._mapping)

    sibling_legs = None
    wizard_session_payload = None

    if combo_attempt_id:
        legs, wizard_session_payload = _combo_legs_if_complete(engine, combo_attempt_id, payload)
        if legs is None:
            return
    else:
        legs = [_single_fill_leg(fill)]
        sibling_legs = _detect_manual_siblings(engine, fill)

    classified = classify_position(
        legs=legs,
        wizard_session_payload=wizard_session_payload,
        sibling_legs=sibling_legs,
    )

    if classified.asset_class in (AssetClass.UNCLASSIFIED, AssetClass.COVERED_CALL):
        _emit_unsupported(engine, payload, reason=classified.reason or classified.asset_class.value)
        return

    policies = deduplicate_by_specificity(
        resolve_for_scope(
            engine,
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
            asset_class=classified.asset_class.value,
        )
    )
    if not policies:
        _emit_unsupported(engine, payload, reason="no_matching_policies")
        return

    descriptor = {
        "asset_class": classified.asset_class.value,
        "opened_at": fill["filled_at"].isoformat(),
        "source": "combo_wizard" if combo_attempt_id else "fastapi_orders_place",
        "first_fill_id": None,
        "anchor_price": float(fill["price"]),
        "anchor_currency": "USD",
        "opened_qty": int(fill["qty"]),
        "protected_qty": int(fill["qty"]),
        "multiplier": 100 if any(leg.get("sec_type") == "OPT" for leg in legs) else 1,
        "qty_unit": "contract" if any(leg.get("sec_type") == "OPT" for leg in legs) else "share",
        "legs": legs,
    }
    descriptor.update(_asset_class_descriptor_fields(classified.asset_class, descriptor))
    position_key = compute_position_key(classified.asset_class.value, descriptor)

    for policy in policies:
        insert_pending_arm(
            engine,
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
            position_key=position_key,
            position_descriptor=descriptor,
            asset_class=classified.asset_class.value,
            rule_kind=policy.rule_kind,
            config=policy.config,
        )


def _single_fill_leg(fill: dict[str, Any]) -> dict[str, Any]:
    metadata = fill.get("metadata") or {}
    return {
        "sec_type": metadata.get("sec_type", "STK"),
        "symbol": fill["ticker"],
        "expiry": metadata.get("expiry"),
        "strike": metadata.get("strike"),
        "right": metadata.get("right"),
        "action": fill["side"],
        "ratio": 1,
        "fill_price": float(fill["price"]),
        "con_id": fill["con_id"] or 0,
    }


def _detect_manual_siblings(engine: Engine, fill: dict[str, Any]) -> list[dict[str, Any]] | None:
    with engine.connect() as conn:
        siblings = conn.execute(
            text(
                """
                SELECT exec_id FROM xenon.order_fills
                WHERE broker = :broker
                  AND account_env = :account_env
                  AND broker_account = :broker_account
                  AND ticker = :ticker
                  AND combo_attempt_id IS NULL
                  AND exec_id != :exec_id
                  AND filled_at >= :filled_at - INTERVAL '60 seconds'
                  AND filled_at <= :filled_at + INTERVAL '60 seconds'
                """
            ),
            {
                "broker": fill["broker"],
                "account_env": fill["account_env"],
                "broker_account": fill["broker_account"],
                "ticker": fill["ticker"],
                "exec_id": fill["exec_id"],
                "filled_at": fill["filled_at"],
            },
        ).all()
    if not siblings:
        return None
    return [{"sec_type": "OPT", "symbol": fill["ticker"]}]


def _combo_legs_if_complete(
    engine: Engine,
    combo_attempt_id: str,
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    with engine.connect() as conn:
        attempt = conn.execute(
            select(wizard_combo_attempts).where(wizard_combo_attempts.c.attempt_id == combo_attempt_id)
        ).first()
    if attempt is None:
        _emit_unsupported(engine, payload, reason="wizard_attempt_missing")
        return None, None

    attempt_dict = dict(attempt._mapping)
    manifest_legs = attempt_dict.get("legs") or []
    expected_leg_count = len(manifest_legs)
    with engine.connect() as conn:
        sibling_count = conn.execute(
            text("SELECT COUNT(*) FROM xenon.order_fills WHERE combo_attempt_id = :attempt_id"),
            {"attempt_id": combo_attempt_id},
        ).scalar_one()

    if sibling_count < expected_leg_count:
        logger.info(
            "arm_hook: combo %s partial (%d/%d), deferring",
            combo_attempt_id,
            sibling_count,
            expected_leg_count,
        )
        return None, None

    try:
        normalized_legs = _normalize_combo_legs(attempt_dict.get("ticker") or payload.get("ticker"), manifest_legs)
    except ValueError as exc:
        _emit_unsupported(engine, payload, reason=str(exc))
        return None, None
    return normalized_legs, {"asset_class": attempt_dict.get("structure_name")}


def _normalize_combo_legs(ticker: str | None, legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for leg in legs:
        out = dict(leg)
        out["sec_type"] = str(out.get("sec_type") or out.get("secType") or "OPT").upper()
        out["symbol"] = str(out.get("symbol") or out.get("localSymbol") or ticker or "").upper()
        out["action"] = str(out.get("action") or "").upper()
        if out.get("right") is not None:
            out["right"] = str(out["right"]).upper()
        if out.get("strike") is not None:
            out["strike"] = float(out["strike"])
        if "con_id" not in out and out.get("conId") is not None:
            out["con_id"] = int(out["conId"])
        if "ratio" not in out:
            out["ratio"] = 1
        if out.get("fill_price") is not None:
            out["fill_price"] = float(out["fill_price"])
        if out["sec_type"] == "OPT":
            _require_combo_leg_field(out, "expiry")
            _require_combo_leg_field(out, "strike")
            _require_combo_leg_field(out, "right")
            _require_combo_leg_field(out, "action")
        normalized.append(out)
    return normalized


def _require_combo_leg_field(leg: dict[str, Any], field: str) -> None:
    if leg.get(field) in (None, ""):
        raise ValueError(f"combo_leg_missing_{field}")


def _asset_class_descriptor_fields(asset_class: AssetClass, descriptor: dict[str, Any]) -> dict[str, Any]:
    if asset_class != AssetClass.CREDIT_SPREAD:
        return {}
    legs = descriptor["legs"]
    short = next((leg for leg in legs if leg.get("action") == "SELL"), None)
    long_ = next((leg for leg in legs if leg.get("action") == "BUY"), None)
    if short is None or long_ is None:
        return {}

    anchor_price = abs(float(descriptor.get("anchor_price") or 0.0))
    leg_credit = float(short.get("fill_price") or 0.0) - float(long_.get("fill_price") or 0.0)
    credit_received = anchor_price if anchor_price > 0 else max(leg_credit, 0.0)
    return {
        "credit_received": credit_received,
        "short_strike": float(short["strike"]),
        "short_right": short["right"],
    }


def _emit_unsupported(engine: Engine, payload: dict[str, Any], *, reason: str) -> None:
    with engine.begin() as conn:
        emit_outbox_in_txn(
            conn,
            channel="position_rule.transition",
            source="arm_hook_unsupported",
            payload={
                "payload_version": 1,
                "kind": "arm_hook_unsupported",
                "reason": reason,
                "exec_id": payload.get("exec_id"),
                "scope": {
                    "broker": payload.get("broker"),
                    "account_env": payload.get("account_env"),
                    "broker_account": payload.get("broker_account"),
                },
            },
        )


def sweep_insert(engine: Engine, *, scope, candidate: dict[str, Any]) -> None:
    """Operator-driven sweep insert for an existing unprotected position."""
    legs = [
        {
            "sec_type": candidate.get("sec_type", "STK"),
            "symbol": candidate["symbol"],
            "action": "BUY" if int(candidate.get("qty", 0)) >= 0 else "SELL",
            "ratio": 1,
            "fill_price": float(candidate.get("mark") or candidate.get("price") or 0.0),
            "con_id": candidate.get("con_id") or 0,
        }
    ]
    classified = classify_position(legs=legs, wizard_session_payload=None, sibling_legs=None)
    if classified.asset_class not in (AssetClass.STOCK, AssetClass.LONG_OPTION):
        return

    policies = deduplicate_by_specificity(
        resolve_for_scope(
            engine,
            broker=scope.broker,
            account_env=scope.account_env,
            broker_account=scope.broker_account,
            asset_class=classified.asset_class.value,
        )
    )
    if not policies:
        return

    now = datetime.now(timezone.utc)
    qty = abs(int(candidate.get("qty") or 0))
    descriptor = {
        "asset_class": classified.asset_class.value,
        "opened_at": now.isoformat(),
        "source": "sweep_cli",
        "anchor_price": legs[0]["fill_price"],
        "anchor_currency": "USD",
        "opened_qty": qty,
        "protected_qty": qty,
        "multiplier": 100 if legs[0]["sec_type"] == "OPT" else 1,
        "qty_unit": "contract" if legs[0]["sec_type"] == "OPT" else "share",
        "legs": legs,
    }
    position_key = compute_position_key(classified.asset_class.value, descriptor)

    for policy in policies:
        insert_pending_arm(
            engine,
            broker=scope.broker,
            account_env=scope.account_env,
            broker_account=scope.broker_account,
            position_key=position_key,
            position_descriptor=descriptor,
            asset_class=classified.asset_class.value,
            rule_kind=policy.rule_kind,
            config=policy.config,
        )
