"""One-shot repair: order_fills rows recorded with qty=0.

Root cause (fixed 2026-06-13): fractional-share executions truncated by
int() into an Integer column. This script patches the historical rows;
quantities must come from IB activity statements / Flex — never guessed.

Usage:
    uv run python scripts/migrations/_2026_06_13_repair_zero_qty_fills.py --list
    uv run python scripts/migrations/_2026_06_13_repair_zero_qty_fills.py --apply repairs.json

repairs.json shape: {"<exec_id>": "<qty>", ...}, e.g.
    {"00020ac8.6a2aeca9.01.01": "0.4977"}
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, update

from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_fills


def list_zero_qty() -> int:
    engine = get_sync_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                order_fills.c.exec_id,
                order_fills.c.ticker,
                order_fills.c.side,
                order_fills.c.price,
                order_fills.c.commission,
                order_fills.c.filled_at,
            ).where(order_fills.c.qty == 0)
        ).all()
    for row in rows:
        print(
            f"{row.exec_id}  {row.ticker:<5} {row.side:<4} price={row.price} "
            f"commission={row.commission} filled_at={row.filled_at}"
        )
    print(f"{len(rows)} row(s) with qty=0")
    return 0


def apply_repairs(mapping_path: Path) -> int:
    """All-or-nothing repair of qty=0 fills, then re-derive affected trades.

    Money data: validate the entire mapping up-front and apply every patch
    in ONE transaction. Any problem (non-positive qty, a missing/already-
    changed row) rolls the whole batch back and exits non-zero — never a
    silent partial apply. After committing, re-run the trade aggregator for
    every affected group so xenon.trades (and the blotter that reads it)
    stops showing the stale qty=0-derived quantity/cost.
    """
    mapping = json.loads(mapping_path.read_text())
    parsed: dict[str, Decimal] = {}
    for exec_id, qty_str in mapping.items():
        qty = Decimal(str(qty_str))
        if qty <= 0:
            print(f"refusing non-positive qty for {exec_id}", file=sys.stderr)
            return 1
        parsed[exec_id] = qty

    engine = get_sync_engine()
    groups: set[tuple[str, str]] = set()  # (kind, key) for re-aggregation
    try:
        with engine.begin() as conn:
            # Fetch ALL targeted rows regardless of current qty so the script
            # is idempotent/re-runnable: a row already patched to qty>0 (e.g.
            # a prior run committed the patch but its re-aggregation failed)
            # is "already done", not "missing". Only a truly absent exec_id
            # is a hard error.
            rows = {
                r.exec_id: r
                for r in conn.execute(
                    select(
                        order_fills.c.exec_id,
                        order_fills.c.submission_id,
                        order_fills.c.metadata,
                        order_fills.c.qty,
                    ).where(order_fills.c.exec_id.in_(list(parsed)))
                ).all()
            }
            absent = sorted(set(parsed) - set(rows))
            if absent:
                raise RuntimeError(f"no order_fills row for exec_ids {absent}; nothing applied")

            for exec_id, qty in parsed.items():
                row = rows[exec_id]
                if row.qty == 0:
                    res = conn.execute(
                        update(order_fills)
                        .where(order_fills.c.exec_id == exec_id, order_fills.c.qty == 0)
                        .values(qty=qty)
                    )
                    if res.rowcount != 1:
                        raise RuntimeError(f"{exec_id} changed under us (rowcount={res.rowcount}); rolled back")
                elif Decimal(str(row.qty)) != qty:
                    # Already non-zero but disagrees with the mapping — refuse
                    # to silently overwrite money data; operator must reconcile.
                    raise RuntimeError(f"{exec_id} already has qty={row.qty}, mapping says {qty}; rolled back")
                # else: already at the requested qty — no-op, still re-aggregate.
                if row.submission_id:
                    groups.add(("submission", row.submission_id))
                elif (row.metadata or {}).get("legacy_id"):
                    groups.add(("legacy", row.metadata["legacy_id"]))
    except RuntimeError as exc:
        print(f"abort: {exc}", file=sys.stderr)
        return 1

    # Re-derive affected trades from the now-correct fills. Mirrors how
    # record_external_fills groups (submission_id when known, else the
    # metadata legacy_id). VERIFIED 2026-06-13: record_external_fills stores
    # legacy_id in order_fills.metadata["legacy_id"], and
    # aggregate_trade_from_fills(legacy_id=...) filters on exactly that key
    # (_fills_stmt -> metadata["legacy_id"].astext == legacy_id,
    # trade_aggregator.py:87) — the grouping is identical.
    from xenon.execution.trade_aggregator import aggregate_trade_from_fills

    for kind, key in groups:
        if kind == "submission":
            aggregate_trade_from_fills(submission_id=key)
        else:
            aggregate_trade_from_fills(legacy_id=key)
    print(f"patched {len(parsed)} row(s); re-aggregated {len(groups)} trade group(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true")
    group.add_argument("--apply", type=Path, metavar="MAPPING_JSON")
    args = parser.parse_args()
    if args.list:
        return list_zero_qty()
    return apply_repairs(args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
