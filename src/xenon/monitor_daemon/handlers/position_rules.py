"""PositionRulesHandler synthetic monitor + native-liveness loop. Spec §8."""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any

from ib_async import Contract, Stock
from sqlalchemy import and_, select, update

import xenon.execution.brackets.rules  # noqa: F401 - populate RULE_REGISTRY
from xenon.clients.ib_client import DEFAULT_GATEWAY_PORT, DEFAULT_HOST
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
from xenon.execution.brackets.executor.marks import MarkCache, Quote, SpotCache, is_quote_fresh
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
        if rows:
            self._ensure_ib_connected()
        mark_cache = MarkCache(fetcher=self._fetch_mark_quote)
        spot_cache = SpotCache(fetcher=self._fetch_spot_quote)
        positions = self._safe_positions_snapshot()

        for row in rows:
            if row["state"] == "PENDING_ARM":
                self._handle_pending_arm(row)
            elif row["state"] == "ARMED":
                self._handle_armed(row, mark_cache=mark_cache, spot_cache=spot_cache, positions=positions)
            elif row["state"] == "TRIGGERED":
                self._handle_triggered(row)

        self._emit_heartbeat(evaluated=len(rows))
        return {"evaluated": len(rows)}

    def _emit_heartbeat(self, *, evaluated: int) -> None:
        with self._engine.begin() as conn:
            emit_outbox_in_txn(
                conn,
                channel="position_rule.heartbeat",
                source="position_rules_handler",
                payload={
                    "payload_version": 1,
                    "kind": "position_rules_heartbeat",
                    "evaluated": evaluated,
                    "scope": self._scope.as_dict(),
                },
            )

    def _handle_pending_arm(self, row: dict[str, Any]) -> None:
        rule = RULE_REGISTRY[row["rule_kind"]]
        position = dict(row["position_descriptor"])
        position["protection_id"] = row["protection_id"]
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
            is_combo = descriptor.get("asset_class") in ("credit_spread", "debit_combo") and len(descriptor["legs"]) > 1
            qty = self._close_qty(descriptor) if is_combo else min(
                descriptor["protected_qty"], self._current_broker_qty(leg["symbol"], leg.get("con_id"))
            )
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
                if is_combo:
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

    def _fetch_mark_quote(self, con_id: int) -> Quote | None:
        contract = Contract(conId=int(con_id), exchange="SMART")
        qualified = self._qualify_contract_best_effort(contract)
        try:
            ticker = self._ib.get_quote(qualified, snapshot=True)
        except Exception:  # noqa: BLE001
            return None
        return self._ticker_to_quote(ticker, symbol=str(con_id))

    def _fetch_spot_quote(self, symbol: str) -> Quote | None:
        ticker_symbol = str(symbol).upper()
        contract = Stock(ticker_symbol, "SMART", "USD")
        qualified = self._qualify_contract_best_effort(contract)
        try:
            ticker = self._ib.get_quote(qualified, snapshot=True)
        except Exception:  # noqa: BLE001
            return None
        return self._ticker_to_quote(ticker, symbol=ticker_symbol)

    def _qualify_contract_best_effort(self, contract):
        qualify_one = getattr(self._ib, "qualify_contract", None)
        if not callable(qualify_one):
            return contract
        try:
            return qualify_one(contract)
        except Exception:  # noqa: BLE001
            return contract

    def _ticker_to_quote(self, ticker: Any, *, symbol: str) -> Quote | None:
        if isinstance(ticker, Quote):
            return ticker
        price = self._ticker_price(ticker)
        if price is None:
            return None
        tick_time = getattr(ticker, "time", None)
        if not isinstance(tick_time, datetime):
            tick_time = datetime.now(timezone.utc)
        elif tick_time.tzinfo is None:
            tick_time = tick_time.replace(tzinfo=timezone.utc)
        return Quote(symbol=symbol, price=price, ts=tick_time)

    @staticmethod
    def _ticker_price(ticker: Any) -> float | None:
        if ticker is None:
            return None
        market_price = getattr(ticker, "marketPrice", None)
        if callable(market_price):
            try:
                value = float(market_price())
                if math.isfinite(value) and value > 0:
                    return value
            except (TypeError, ValueError):
                pass
        for attr in ("last", "close"):
            try:
                value = float(getattr(ticker, attr))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                return value
        try:
            bid = float(getattr(ticker, "bid"))
            ask = float(getattr(ticker, "ask"))
        except (TypeError, ValueError):
            return None
        if math.isfinite(bid) and math.isfinite(ask) and bid > 0 and ask > 0 and bid <= ask:
            return (bid + ask) / 2
        return None

    def _handle_position_presence(
        self,
        row: dict[str, Any],
        descriptor: dict[str, Any],
        positions: list[dict] | None,
    ) -> bool:
        if not self._is_ib_connected():
            return False
        if positions is None:
            return True
        leg = descriptor["legs"][0]
        present = any(self._position_matches_leg(position, leg) for position in positions)
        missing_ticks = (row["state_data"] or {}).get("position_missing_ticks", 0)
        if present:
            if missing_ticks:
                self._patch_state_data(row["protection_id"], {"position_missing_ticks": 0})
            return True
        new_missing = missing_ticks + 1
        if new_missing >= _POSITION_MISSING_TICKS_FOR_CANCEL and self._is_ib_connected():
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

    def _close_qty(self, descriptor: dict[str, Any]) -> int:
        broker_qtys = [
            self._current_broker_qty(leg["symbol"], leg.get("con_id"))
            for leg in descriptor["legs"]
        ]
        if not broker_qtys:
            return 0
        return min(int(descriptor["protected_qty"]), min(broker_qtys))

    def _safe_positions_snapshot(self) -> list[dict] | None:
        try:
            positions_fn = getattr(self._ib, "positions", None)
            if callable(positions_fn):
                positions = positions_fn()
            else:
                positions = self._ib.get_positions()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(positions, list):
            return None
        normalized = [self._normalize_position(position) for position in positions]

        portfolio_fn = getattr(self._ib, "get_portfolio", None)
        if callable(portfolio_fn):
            try:
                portfolio = portfolio_fn()
            except Exception:  # noqa: BLE001
                portfolio = None
            if isinstance(portfolio, list):
                seen_con_ids = {position.get("con_id") for position in normalized if position.get("con_id") is not None}
                for item in portfolio:
                    normalized_item = self._normalize_position(item)
                    con_id = normalized_item.get("con_id")
                    if con_id is not None and con_id in seen_con_ids:
                        continue
                    normalized.append(normalized_item)
                    if con_id is not None:
                        seen_con_ids.add(con_id)
        return normalized

    def _ensure_ib_connected(self) -> None:
        if self._is_ib_connected():
            return
        connect = getattr(self._ib, "connect", None)
        if not callable(connect):
            return
        connect(
            host=os.environ.get("IB_GATEWAY_HOST", DEFAULT_HOST),
            port=int(os.environ.get("IB_GATEWAY_PORT", str(DEFAULT_GATEWAY_PORT))),
            client_id=int(os.environ.get("XENON_POSITION_RULES_CLIENT_ID", "71")),
        )

    def _is_ib_connected(self) -> bool:
        connected_attr = getattr(self._ib, "connected", None)
        if isinstance(connected_attr, bool):
            return connected_attr
        is_connected = getattr(self._ib, "is_connected", None)
        if callable(is_connected):
            try:
                return bool(is_connected())
            except Exception:  # noqa: BLE001
                return False
        return bool(getattr(self._ib, "connected", True))

    @staticmethod
    def _normalize_position(position: Any) -> dict[str, Any]:
        if isinstance(position, dict):
            out = dict(position)
            if "con_id" not in out and out.get("conId") is not None:
                out["con_id"] = out.get("conId")
            if "sec_type" not in out and out.get("secType") is not None:
                out["sec_type"] = out.get("secType")
            if "qty" not in out:
                out["qty"] = out.get("position", out.get("quantity", 0))
            return out
        contract = getattr(position, "contract", None)
        return {
            "symbol": getattr(contract, "symbol", None),
            "con_id": getattr(contract, "conId", None),
            "sec_type": getattr(contract, "secType", None),
            "expiry": getattr(contract, "lastTradeDateOrContractMonth", None),
            "strike": getattr(contract, "strike", None),
            "right": getattr(contract, "right", None),
            "qty": getattr(position, "position", 0),
        }

    @staticmethod
    def _position_matches_leg(position: dict[str, Any], leg: dict[str, Any]) -> bool:
        try:
            if abs(float(position.get("qty") or 0)) <= 0:
                return False
        except (TypeError, ValueError):
            return False
        if str(position.get("symbol") or "").upper() != str(leg.get("symbol") or "").upper():
            return False

        position_con_id = position.get("con_id")
        leg_con_id = leg.get("con_id")
        if position_con_id is not None and leg_con_id is not None:
            return int(position_con_id) == int(leg_con_id)

        leg_sec_type = str(leg.get("sec_type") or "").upper()
        position_sec_type = str(position.get("sec_type") or leg_sec_type).upper()
        if leg_sec_type == "OPT":
            return (
                position_sec_type == "OPT"
                and str(position.get("expiry") or "") == str(leg.get("expiry") or "")
                and float(position.get("strike") or 0) == float(leg.get("strike") or 0)
                and str(position.get("right") or "").upper() == str(leg.get("right") or "").upper()
            )
        return position_sec_type in ("", leg_sec_type, "STK")

    def _find_open_orders_by_order_ref(self, order_ref: str) -> list[dict[str, Any]]:
        if not hasattr(self._ib, "find_open_orders_by_order_ref"):
            return []
        return list(self._ib.find_open_orders_by_order_ref(order_ref) or [])

    def _find_executions_by_order_ref(self, order_ref: str) -> list[dict[str, Any]]:
        if not hasattr(self._ib, "find_executions_by_order_ref"):
            return []
        return list(self._ib.find_executions_by_order_ref(order_ref) or [])
