"""PositionRulesHandler synthetic monitor + native-liveness loop. Spec §8."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, select, update

import xenon.execution.brackets.rules  # noqa: F401 - populate RULE_REGISTRY
from xenon.db.events import emit_outbox_in_txn
from xenon.db.queries.position_close_claims import (
    find_inflight_for_position,
    increment_attempts,
    mark_submitted,
    mark_terminal,
    try_claim,
)
from xenon.db.queries.position_protection import cas_transition, list_active_rows
from xenon.db.schema import position_close_claims, position_protection
from xenon.execution.account_scope import AccountScope
from xenon.execution.brackets.close_claim import derive_order_ref, should_skip_resubmit
from xenon.execution.brackets.executor.ib_executor import IBExecutor
from xenon.execution.brackets.executor.marks import MarkCache, SpotCache, is_quote_fresh
from xenon.execution.brackets.executor.native_liveness import (
    NativeOrderState,
    verify_native_order_live,
)
from xenon.execution.brackets.rules.base import RULE_REGISTRY
from xenon.monitor_daemon.handlers.base import BaseHandler

_STALENESS_MAX_AGE_S = 60
_POSITION_MISSING_TICKS_FOR_CANCEL = 2


class PositionRulesHandler(BaseHandler):
    name = "position_rules"
    interval_seconds = 30
    requires_market_hours = True

    def __init__(self, *, engine, executor: IBExecutor, ib_client, scope: AccountScope):
        super().__init__()
        self._engine = engine
        self._executor = executor
        self._ib = ib_client
        self._scope = scope

    def execute(self) -> dict[str, Any]:
        rows = list_active_rows(
            self._engine,
            broker=self._scope.broker,
            account_env=self._scope.account_env,
            broker_account=self._scope.broker_account,
        )
        mark_cache = MarkCache(fetcher=lambda con_id: self._ib.get_quote(con_id=con_id))
        spot_cache = SpotCache(fetcher=lambda symbol: self._ib.get_quote(symbol=symbol))
        positions = self._safe_positions_snapshot()

        for row in rows:
            if row["state"] == "PENDING_ARM":
                self._handle_pending_arm(row)
            elif row["state"] == "ARMED":
                self._handle_armed(row, mark_cache=mark_cache, spot_cache=spot_cache, positions=positions)
            elif row["state"] == "TRIGGERED":
                self._handle_triggered(row)

        return {"evaluated": len(rows)}

    def _handle_pending_arm(self, row: dict[str, Any]) -> None:
        rule = RULE_REGISTRY[row["rule_kind"]]
        position = dict(row["position_descriptor"])
        result = rule.arm(
            scope=self._scope,
            position=position,
            config=row["config"],
            state_data=row["state_data"] or {},
            executor=self._executor,
        )
        if result.kind == "NATIVE_ARMED":
            cas_transition(
                self._engine,
                protection_id=row["protection_id"],
                expected_state="PENDING_ARM",
                new_state="ARMED",
                reason="native_armed",
                state_data_patch=result.state_data_patch,
                native_order_perm_id=result.perm_id,
            )
        elif result.kind == "SYNTHETIC_ONLY":
            cas_transition(
                self._engine,
                protection_id=row["protection_id"],
                expected_state="PENDING_ARM",
                new_state="ARMED",
                reason="synthetic_only",
            )
        elif result.kind == "FAILED":
            cas_transition(
                self._engine,
                protection_id=row["protection_id"],
                expected_state="PENDING_ARM",
                new_state="FAILED",
                reason=result.reason or "arm_failed",
            )
        else:
            state_data = dict(row["state_data"] or {})
            attempts = state_data.get("arm_attempts", 0) + 1
            state_data.update({"arm_attempts": attempts, "last_arm_error": result.reason})
            if attempts >= 4:
                cas_transition(
                    self._engine,
                    protection_id=row["protection_id"],
                    expected_state="PENDING_ARM",
                    new_state="FAILED",
                    reason="arm_failed_max_attempts",
                    state_data_patch=state_data,
                )
            else:
                self._patch_state_data(row["protection_id"], state_data)

    def _handle_armed(
        self,
        row: dict[str, Any],
        *,
        mark_cache: MarkCache,
        spot_cache: SpotCache,
        positions: list[dict] | None,
    ) -> None:
        descriptor = row["position_descriptor"]
        if not self._handle_position_presence(row, descriptor, positions):
            return

        native_perm_id = row["native_order_perm_id"]
        if native_perm_id is not None:
            state = verify_native_order_live(ib_client=self._ib, perm_id=native_perm_id)
            if state in (NativeOrderState.CANCELLED, NativeOrderState.INACTIVE):
                cas_transition(
                    self._engine,
                    protection_id=row["protection_id"],
                    expected_state="ARMED",
                    new_state="CANCELED",
                    reason="native_order_externally_cancelled",
                )
                return
            if state == NativeOrderState.FILLED:
                self._mark_native_filled(row, native_perm_id)
                return

        marks = self._build_marks(descriptor, mark_cache=mark_cache, spot_cache=spot_cache)
        if marks is None:
            self._handle_stale_quote(row)
            return

        if (row["state_data"] or {}).get("consecutive_stale_ticks", 0):
            self._patch_state_data(row["protection_id"], {"consecutive_stale_ticks": 0})

        rule = RULE_REGISTRY[row["rule_kind"]]
        decision = rule.evaluate(
            scope=self._scope,
            position=descriptor,
            config=row["config"],
            state_data=row["state_data"] or {},
            marks=marks,
        )
        if decision.kind == "TRIGGERED":
            if self._is_alert_only(row, decision):
                self._record_alert_only(row, decision)
            else:
                self._submit_close(row, decision)
        elif decision.kind == "UPDATE_STATE":
            self._patch_state_data(row["protection_id"], decision.state_data_patch or {})

    def _handle_triggered(self, row: dict[str, Any]) -> None:
        claim = self._find_pending_claim(row["position_key"])
        if claim is None:
            return
        order_ref = claim["order_ref"]
        executions = self._find_executions_by_order_ref(order_ref)
        open_orders = self._find_open_orders_by_order_ref(order_ref)
        if executions:
            mark_terminal(self._engine, claim_id=claim["claim_id"], status="FILLED")
            cas_transition(
                self._engine,
                protection_id=row["protection_id"],
                expected_state="TRIGGERED",
                new_state="CLOSED",
                reason="claim_filled",
            )
        elif not open_orders and claim["attempts"] >= 4:
            mark_terminal(self._engine, claim_id=claim["claim_id"], status="FAILED")
            cas_transition(
                self._engine,
                protection_id=row["protection_id"],
                expected_state="TRIGGERED",
                new_state="FAILED",
                reason="claim_failed_max_attempts",
            )

    def _submit_close(self, row: dict[str, Any], decision) -> None:
        claim_id = try_claim(
            self._engine,
            broker=self._scope.broker,
            account_env=self._scope.account_env,
            broker_account=self._scope.broker_account,
            position_key=row["position_key"],
            claimed_by_protection_id=row["protection_id"],
            claim_kind="synthetic_close",
        )
        if claim_id is None:
            existing_claim = find_inflight_for_position(
                self._engine,
                broker=self._scope.broker,
                account_env=self._scope.account_env,
                broker_account=self._scope.broker_account,
                position_key=row["position_key"],
            )
            if existing_claim is not None and existing_claim["claimed_by_protection_id"] == row["protection_id"]:
                claim_id = existing_claim["claim_id"]
            else:
                cas_transition(
                    self._engine,
                    protection_id=row["protection_id"],
                    expected_state="ARMED",
                    new_state="SUPERSEDED",
                    reason="claim_held_by_other_rule",
                )
                return

        order_ref = derive_order_ref(claim_id=claim_id)
        open_orders = self._find_open_orders_by_order_ref(order_ref)
        executions = self._find_executions_by_order_ref(order_ref)
        skip, existing_perm_id = should_skip_resubmit(
            order_ref=order_ref,
            open_orders=open_orders,
            executions=executions,
        )
        if skip:
            mark_submitted(self._engine, claim_id=claim_id, broker_perm_id=existing_perm_id)
        else:
            descriptor = row["position_descriptor"]
            leg = descriptor["legs"][0]
            qty = min(descriptor["protected_qty"], self._current_broker_qty(leg["symbol"], leg.get("con_id")))
            if qty <= 0:
                mark_terminal(
                    self._engine,
                    claim_id=claim_id,
                    status="ABANDONED",
                    last_error="position_already_flat",
                )
                cas_transition(
                    self._engine,
                    protection_id=row["protection_id"],
                    expected_state="ARMED",
                    new_state="CANCELED",
                    reason="position_already_flat",
                )
                return
            try:
                if descriptor.get("asset_class") in ("credit_spread", "debit_combo") and len(descriptor["legs"]) > 1:
                    result = self._executor.flatten_combo_mkt(
                        scope=self._scope,
                        symbol=leg["symbol"],
                        legs=descriptor["legs"],
                        qty=qty,
                        order_ref=order_ref,
                    )
                else:
                    close_action = "SELL" if leg["action"] == "BUY" else "BUY"
                    result = self._executor.flatten_mkt(
                        scope=self._scope,
                        con_id=leg["con_id"],
                        symbol=leg["symbol"],
                        sec_type=leg["sec_type"],
                        close_action=close_action,
                        qty=qty,
                        order_ref=order_ref,
                    )
            except Exception as exc:  # noqa: BLE001
                increment_attempts(self._engine, claim_id=claim_id, last_error=str(exc))
                return
            mark_submitted(self._engine, claim_id=claim_id, broker_perm_id=result.perm_id)

        cas_transition(
            self._engine,
            protection_id=row["protection_id"],
            expected_state="ARMED",
            new_state="TRIGGERED",
            reason=decision.reason or "trigger",
            context=decision.context,
        )

    def _mark_native_filled(self, row: dict[str, Any], native_perm_id: int) -> None:
        claim_id = try_claim(
            self._engine,
            broker=self._scope.broker,
            account_env=self._scope.account_env,
            broker_account=self._scope.broker_account,
            position_key=row["position_key"],
            claimed_by_protection_id=row["protection_id"],
            claim_kind="native_reconcile_close",
        )
        if claim_id is not None:
            mark_submitted(self._engine, claim_id=claim_id, broker_perm_id=native_perm_id)
            mark_terminal(self._engine, claim_id=claim_id, status="FILLED")
        cas_transition(
            self._engine,
            protection_id=row["protection_id"],
            expected_state="ARMED",
            new_state="CLOSED",
            reason="native_bracket_filled",
        )

    def _is_alert_only(self, row: dict[str, Any], decision) -> bool:
        return row["config"].get("auto_place") is False or bool((decision.context or {}).get("alert_only"))

    def _record_alert_only(self, row: dict[str, Any], decision) -> None:
        patch = dict(decision.state_data_patch or {})
        patch["last_alert_reason"] = decision.reason or "alert_only_trigger"
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(position_protection.c.state_data).where(
                    position_protection.c.protection_id == row["protection_id"]
                )
            ).first()
            state_data = dict(existing.state_data or {}) if existing else {}
            state_data.update(patch)
            conn.execute(
                update(position_protection)
                .where(
                    position_protection.c.protection_id == row["protection_id"],
                    position_protection.c.state == "ARMED",
                )
                .values(state_data=state_data, last_evaluated_at=now, updated_at=now)
            )
            emit_outbox_in_txn(
                conn,
                channel="position_rule.transition",
                source="position_rules_alert",
                payload={
                    "payload_version": 1,
                    "protection_id": row["protection_id"],
                    "position_key": row["position_key"],
                    "rule_kind": row["rule_kind"],
                    "old_state": "ARMED",
                    "new_state": "ARMED",
                    "reason": decision.reason or "alert_only_trigger",
                    "context": decision.context or {},
                    "scope": self._scope.as_dict(),
                },
            )

    def _build_marks(
        self,
        descriptor: dict[str, Any],
        *,
        mark_cache: MarkCache,
        spot_cache: SpotCache,
    ) -> dict[str, Any] | None:
        legs = descriptor["legs"]
        asset_class = descriptor.get("asset_class")
        now = datetime.now(timezone.utc)

        if asset_class == "credit_spread":
            quotes = []
            for leg in legs:
                quote = mark_cache.get(con_id=leg["con_id"])
                if quote is None or not is_quote_fresh(quote, max_age_s=_STALENESS_MAX_AGE_S):
                    return None
                quotes.append((leg, quote))
            debit_to_close = sum(
                (1 if leg["action"] == "SELL" else -1) * quote.price * int(leg.get("ratio", 1))
                for leg, quote in quotes
            )
            marks = {"mark": max(debit_to_close, 0.0), "debit_to_close": max(debit_to_close, 0.0), "now": now}
            spot = spot_cache.get(symbol=legs[0]["symbol"])
            if spot is not None and is_quote_fresh(spot, max_age_s=_STALENESS_MAX_AGE_S):
                marks["underlying_spot"] = spot.price
            return marks

        if asset_class == "debit_combo" and len(legs) > 1:
            quotes = []
            for leg in legs:
                quote = mark_cache.get(con_id=leg["con_id"])
                if quote is None or not is_quote_fresh(quote, max_age_s=_STALENESS_MAX_AGE_S):
                    return None
                quotes.append((leg, quote))
            mark = sum(
                (1 if leg["action"] == "BUY" else -1) * quote.price * int(leg.get("ratio", 1))
                for leg, quote in quotes
            )
            return {"mark": max(mark, 0.0), "now": now}

        leg = legs[0]
        quote = mark_cache.get(con_id=leg["con_id"])
        if quote is None or not is_quote_fresh(quote, max_age_s=_STALENESS_MAX_AGE_S):
            return None
        return {"mark": quote.price, "now": now}

    def _handle_position_presence(
        self,
        row: dict[str, Any],
        descriptor: dict[str, Any],
        positions: list[dict] | None,
    ) -> bool:
        if positions is None:
            return True
        leg = descriptor["legs"][0]
        present = any(
            position.get("symbol") == leg["symbol"]
            and (position.get("con_id") in (None, leg.get("con_id")))
            for position in positions
        )
        missing_ticks = (row["state_data"] or {}).get("position_missing_ticks", 0)
        if present:
            if missing_ticks:
                self._patch_state_data(row["protection_id"], {"position_missing_ticks": 0})
            return True
        new_missing = missing_ticks + 1
        if new_missing >= _POSITION_MISSING_TICKS_FOR_CANCEL and getattr(self._ib, "connected", True):
            cas_transition(
                self._engine,
                protection_id=row["protection_id"],
                expected_state="ARMED",
                new_state="CANCELED",
                reason="position_closed_externally",
                state_data_patch={"position_missing_ticks": new_missing},
            )
        else:
            self._patch_state_data(row["protection_id"], {"position_missing_ticks": new_missing})
        return False

    def _handle_stale_quote(self, row: dict[str, Any]) -> None:
        current = dict(row["state_data"] or {})
        current["consecutive_stale_ticks"] = (current.get("consecutive_stale_ticks") or 0) + 1
        self._patch_state_data(row["protection_id"], current)

    def _patch_state_data(self, protection_id: int, patch: dict[str, Any]) -> None:
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(position_protection.c.state_data).where(
                    position_protection.c.protection_id == protection_id
                )
            ).first()
            state_data = dict(existing.state_data or {}) if existing else {}
            state_data.update(patch)
            conn.execute(
                update(position_protection)
                .where(position_protection.c.protection_id == protection_id)
                .values(state_data=state_data)
            )

    def _find_pending_claim(self, position_key: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(position_close_claims).where(
                    and_(
                        position_close_claims.c.broker == self._scope.broker,
                        position_close_claims.c.account_env == self._scope.account_env,
                        position_close_claims.c.broker_account == self._scope.broker_account,
                        position_close_claims.c.position_key == position_key,
                        position_close_claims.c.status.in_(("PENDING", "SUBMITTED")),
                    )
                )
            ).first()
        return dict(row._mapping) if row else None

    def _current_broker_qty(self, symbol: str, con_id: int | None) -> int:
        for position in self._safe_positions_snapshot() or []:
            if position.get("symbol") == symbol and (con_id is None or position.get("con_id") == con_id):
                return abs(int(position.get("qty", 0)))
        return 0

    def _safe_positions_snapshot(self) -> list[dict] | None:
        try:
            positions = self._ib.positions()
        except Exception:  # noqa: BLE001
            return None
        return positions if isinstance(positions, list) else None

    def _find_open_orders_by_order_ref(self, order_ref: str) -> list[dict[str, Any]]:
        if not hasattr(self._ib, "find_open_orders_by_order_ref"):
            return []
        return list(self._ib.find_open_orders_by_order_ref(order_ref) or [])

    def _find_executions_by_order_ref(self, order_ref: str) -> list[dict[str, Any]]:
        if not hasattr(self._ib, "find_executions_by_order_ref"):
            return []
        return list(self._ib.find_executions_by_order_ref(order_ref) or [])
