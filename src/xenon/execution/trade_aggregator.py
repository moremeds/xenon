from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import insert, select, update

from xenon.db.engine import get_sync_engine
from xenon.db.events import CHANNEL_TRADE_CLOSED, emit_outbox_in_txn
from xenon.db.schema import order_fills, order_submissions, trades, wizard_combo_attempts

_ZERO = Decimal("0")
_MONEY_4 = Decimal("0.0001")
_MONEY_2 = Decimal("0.01")


def aggregate_trade_from_fills(
    *,
    submission_id: str | None = None,
    combo_attempt_id: str | None = None,
    legacy_id: str | None = None,
) -> None:
    """Derive one xenon.trades row from immutable execution fills."""
    keys = [submission_id is not None, combo_attempt_id is not None, legacy_id is not None]
    if sum(keys) != 1:
        raise ValueError("aggregate_trade_from_fills requires exactly one source key")

    engine = get_sync_engine()
    with engine.begin() as conn:
        fills = [
            dict(row._mapping) for row in conn.execute(_fills_stmt(submission_id, combo_attempt_id, legacy_id)).all()
        ]
        if not fills:
            return

        source = _load_source(conn, submission_id, combo_attempt_id)
        derived = _derive_trade(
            fills,
            source=source,
            submission_id=submission_id,
            combo_attempt_id=combo_attempt_id,
            legacy_id=legacy_id,
        )

        existing = conn.execute(
            _existing_trade_stmt(submission_id, combo_attempt_id, legacy_id).with_for_update()
        ).first()
        existing_state = existing._mapping["state"] if existing is not None else None

        if existing is None:
            result = conn.execute(insert(trades).values(**derived).returning(trades.c.id))
            trade_id = int(result.scalar_one())
        else:
            trade_id = int(existing._mapping["id"])
            conn.execute(update(trades).where(trades.c.id == trade_id).values(**derived))

        if derived["state"] == "CLOSED" and existing_state != "CLOSED":
            emit_outbox_in_txn(
                conn,
                channel=CHANNEL_TRADE_CLOSED,
                source="trade_aggregator",
                payload={
                    "trade_id": trade_id,
                    "submission_id": submission_id,
                    "combo_attempt_id": combo_attempt_id,
                    "legacy_id": legacy_id,
                    "ticker": derived["ticker"],
                    "realized_pnl": str(derived["realized_pnl"]) if derived["realized_pnl"] is not None else None,
                    "broker": derived["broker"],
                    "account_env": derived["account_env"],
                    "broker_account": derived["broker_account"],
                },
            )


def _fills_stmt(submission_id: str | None, combo_attempt_id: str | None, legacy_id: str | None):
    stmt = select(order_fills).order_by(order_fills.c.filled_at, order_fills.c.exec_id)
    if submission_id is not None:
        return stmt.where(order_fills.c.submission_id == submission_id)
    if combo_attempt_id is not None:
        return stmt.where(order_fills.c.combo_attempt_id == combo_attempt_id)
    return stmt.where(
        order_fills.c.submission_id.is_(None),
        order_fills.c.combo_attempt_id.is_(None),
        order_fills.c.metadata["legacy_id"].astext == legacy_id,
    )


def _existing_trade_stmt(submission_id: str | None, combo_attempt_id: str | None, legacy_id: str | None):
    stmt = select(trades)
    if submission_id is not None:
        return stmt.where(trades.c.submission_id == submission_id).limit(1)
    if combo_attempt_id is not None:
        return stmt.where(trades.c.combo_attempt_id == combo_attempt_id).limit(1)
    return stmt.where(
        trades.c.submission_id.is_(None),
        trades.c.combo_attempt_id.is_(None),
        trades.c.metadata["legacy_id"].astext == legacy_id,
    ).limit(1)


def _load_source(conn, submission_id: str | None, combo_attempt_id: str | None) -> dict[str, Any] | None:
    if submission_id is not None:
        row = conn.execute(select(order_submissions).where(order_submissions.c.submission_id == submission_id)).first()
        return dict(row._mapping) if row else None
    if combo_attempt_id is not None:
        row = conn.execute(
            select(wizard_combo_attempts).where(wizard_combo_attempts.c.attempt_id == combo_attempt_id)
        ).first()
        return dict(row._mapping) if row else None
    return None


def _derive_trade(
    fills: list[dict[str, Any]],
    *,
    source: dict[str, Any] | None,
    submission_id: str | None,
    combo_attempt_id: str | None,
    legacy_id: str | None,
) -> dict[str, Any]:
    normalized_all = [{**fill, "side": _normalize_side(str(fill["side"]))} for fill in fills]
    normalized = _economic_fills(normalized_all, source=source)
    first = normalized[0]
    reported_realized_pnl = _reported_realized_pnl(normalized)
    closed = _is_closed(normalized) or reported_realized_pnl is not None
    entry_cost, exit_cost, realized_pnl = _costs(
        normalized,
        closed=closed,
        reported_realized_pnl=reported_realized_pnl,
    )
    quantity = _quantity(normalized)
    opened_at = min(fill["filled_at"] for fill in normalized)
    closed_at = max(fill["filled_at"] for fill in normalized) if closed else None
    metadata = _metadata(normalized, legacy_id=legacy_id)
    if source and source.get("structure_name"):
        structure = source["structure_name"]
    elif combo_attempt_id is not None:
        structure = "Combo"
    elif _has_bag_signal(fills, source):
        # snapshot-* BAG submissions and legacy_id BAG groups have no
        # structure_name. Derive a label from the economic legs so the
        # blotter never shows a combo as "Stock".
        leg_count = len({_instrument_key(fill) for fill in normalized})
        structure = "Spread" if leg_count == 2 else "Combo"
    else:
        structure = "Stock"
    state = _state(normalized, closed=closed, quantity=quantity, source=source)

    return {
        "ticker": first["ticker"],
        "structure": structure,
        "action": _action(normalized, source),
        "quantity": quantity,
        "entry_cost": entry_cost,
        "exit_cost": exit_cost,
        "realized_pnl": realized_pnl,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "submission_id": submission_id,
        "combo_attempt_id": combo_attempt_id,
        "state": state,
        "metadata": metadata,
        "broker": first["broker"],
        "account_env": first["account_env"],
        "broker_account": first["broker_account"],
    }


def _normalize_side(side: str) -> str:
    normalized = side.upper()
    if normalized in {"BOT", "BOUGHT"}:
        return "BUY"
    if normalized in {"SLD", "SOLD"}:
        return "SELL"
    return normalized


def _instrument_key(fill: dict[str, Any]) -> str:
    con_id = fill.get("con_id")
    return str(con_id) if con_id is not None else str(fill["ticker"])


def _has_bag_signal(fills: list[dict[str, Any]], source: dict[str, Any] | None) -> bool:
    """True if any fill or the source row carries a BAG marker — used to
    pick the structure label for snapshot/legacy combos that lack a
    structure_name. Both surfaces matter: TWS-imported snapshot rows have
    security_type=BAG on the source; legacy_id-grouped fills have sec_type=BAG
    in metadata on the envelope row.
    """
    if source and str(source.get("security_type") or "") == "BAG":
        return True
    for fill in fills:
        metadata = fill.get("metadata") or {}
        if metadata.get("sec_type") == "BAG":
            return True
    return False


def _is_bag_envelope(fill: dict[str, Any], source: dict[str, Any] | None) -> bool:
    metadata = fill.get("metadata") or {}
    if metadata.get("sec_type") == "BAG":
        return True
    if source and source.get("security_type") == "BAG":
        source_con_id = source.get("con_id")
        return source_con_id is not None and fill.get("con_id") == source_con_id
    return False


def _economic_fills(fills: list[dict[str, Any]], *, source: dict[str, Any] | None) -> list[dict[str, Any]]:
    non_bag = [fill for fill in fills if not _is_bag_envelope(fill, source)]
    return non_bag or fills


def _reported_realized_pnl(fills: list[dict[str, Any]]) -> Decimal | None:
    total = _ZERO
    found = False
    for fill in fills:
        metadata = fill.get("metadata") or {}
        value = metadata.get("realized_pnl")
        if value is None or value == "":
            continue
        decimal_value = Decimal(str(value))
        if decimal_value == 0:
            continue
        total += decimal_value
        found = True
    return _money2(total) if found else None


def _is_closed(fills: list[dict[str, Any]]) -> bool:
    net: dict[str, int] = defaultdict(int)
    has_buy = False
    has_sell = False
    for fill in fills:
        qty = Decimal(str(fill["qty"]))
        if fill["side"] == "BUY":
            net[_instrument_key(fill)] += qty
            has_buy = True
        elif fill["side"] == "SELL":
            net[_instrument_key(fill)] -= qty
            has_sell = True
    return has_buy and has_sell and all(qty == 0 for qty in net.values())


def _costs(
    fills: list[dict[str, Any]],
    *,
    closed: bool,
    reported_realized_pnl: Decimal | None = None,
) -> tuple[Decimal, Decimal | None, Decimal | None]:
    buy_cost = _ZERO
    sell_proceeds = _ZERO
    entry_cash = _ZERO
    exit_cash = _ZERO
    net_qty: dict[str, int] = defaultdict(int)
    for fill in fills:
        value = Decimal(fill["qty"]) * Decimal(fill["price"])
        commission = Decimal(fill["commission"] or 0)
        side = fill["side"]
        key = _instrument_key(fill)
        direction = 1 if side == "BUY" else -1
        signed_cash = value + commission if side == "BUY" else -value + commission
        if _is_entry_fill(net_qty[key], direction):
            entry_cash += signed_cash
        else:
            exit_cash += signed_cash
        net_qty[key] += direction * Decimal(str(fill["qty"]))
        if side == "BUY":
            buy_cost += value + commission
        elif side == "SELL":
            sell_proceeds += value - commission

    if not closed:
        return _money4(entry_cash), None, None

    exit_cost = exit_cash if entry_cash < 0 else -exit_cash
    realized_pnl = reported_realized_pnl if reported_realized_pnl is not None else _money2(sell_proceeds - buy_cost)
    return _money4(entry_cash), _money4(exit_cost), realized_pnl


def _is_entry_fill(current_net_qty: int, fill_direction: int) -> bool:
    return (
        current_net_qty == 0
        or (current_net_qty > 0 and fill_direction > 0)
        or (current_net_qty < 0 and fill_direction < 0)
    )


def _quantity(fills: list[dict[str, Any]]) -> Decimal:
    by_instrument: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"BUY": Decimal(0), "SELL": Decimal(0)})
    for fill in fills:
        by_instrument[_instrument_key(fill)][fill["side"]] += Decimal(str(fill["qty"]))
    return max(max(sides.values()) for sides in by_instrument.values())


def _state(
    fills: list[dict[str, Any]],
    *,
    closed: bool,
    quantity: int,
    source: dict[str, Any] | None,
) -> str:
    if closed:
        return "CLOSED"
    expected = _expected_quantity(source)
    if expected is not None and quantity < expected:
        return "PARTIALLY_FILLED"
    return "OPEN"


def _expected_quantity(source: dict[str, Any] | None) -> int | None:
    if not source:
        return None
    if source.get("quantity") is not None:
        return int(source["quantity"])
    combo_contract = source.get("combo_contract")
    if isinstance(combo_contract, dict) and combo_contract.get("quantity") is not None:
        return int(combo_contract["quantity"])
    return None


def _action(fills: list[dict[str, Any]], source: dict[str, Any] | None) -> str:
    if source and source.get("action"):
        return str(source["action"])
    combo_contract = source.get("combo_contract") if source else None
    if isinstance(combo_contract, dict) and combo_contract.get("action"):
        return str(combo_contract["action"])
    return fills[0]["side"]


def _metadata(fills: list[dict[str, Any]], *, legacy_id: str | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "exec_ids": [fill["exec_id"] for fill in fills],
        "legs": [
            {
                "exec_id": fill["exec_id"],
                "con_id": fill["con_id"],
                "ticker": fill["ticker"],
                "side": fill["side"],
                "qty": float(fill["qty"]),
                "price": str(fill["price"]),
                "commission": str(fill["commission"] or 0),
                "filled_at": _iso(fill["filled_at"]),
                **_leg_contract_metadata(fill),
            }
            for fill in fills
        ],
    }
    if legacy_id is not None:
        metadata["legacy_id"] = legacy_id
        first_metadata = fills[0].get("metadata") or {}
        if first_metadata.get("legacy_source"):
            metadata["legacy_source"] = first_metadata["legacy_source"]
    return metadata


def _leg_contract_metadata(fill: dict[str, Any]) -> dict[str, Any]:
    metadata = fill.get("metadata") or {}
    out: dict[str, Any] = {}
    for key in ("sec_type", "strike", "expiry", "right"):
        value = metadata.get(key)
        if value is not None and value != "":
            out[key] = value
    return out


def _iso(value: datetime) -> str:
    return value.isoformat()


def _money4(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_4)


def _money2(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_2)
