"""Read futu_daily_statement rows and compute simple / TWR returns.

Direct PG read (no OpenD calls). Cross-validates the screenshot:
expected YTD simple ≈ -0.43%, TWR ≈ +1.93% for 2026-01-01..2026-06-02.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from dotenv import load_dotenv

from xenon.db.engine import get_engine, init_engine
from xenon.db.queries.futu_history import list_cashflows, list_daily_statements
from xenon.db.schema import futu_cash_flow
from xenon.execution.account_scope import AccountScope


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", default="2026-01-01", help="period start YYYY-MM-DD")
    p.add_argument("--until", default=None, help="period end YYYY-MM-DD (default: today)")
    p.add_argument(
        "--broker-account",
        default=os.environ.get("XENON_BROKER_ACCOUNT", "281756478831553263"),
    )
    p.add_argument("--account-env", default=os.environ.get("XENON_TRADING_MODE", "live"))
    return p.parse_args(argv)


def _is_external_cashflow(row: dict) -> bool:
    """Matches futu_nav_backfill's v1 rule: cashflow_type='Others' AND empty remark."""
    if str(row.get("cashflow_type") or "") != "Others":
        return False
    raw = row.get("raw") or {}
    remark = str(raw.get("cashflow_remark") or "").strip()
    return remark == ""


async def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    load_dotenv(Path.cwd() / ".env")
    since = date.fromisoformat(args.since)
    until = date.fromisoformat(args.until) if args.until else date.today()

    init_engine()
    eng = get_engine()
    scope = AccountScope(broker="FUTU", account_env=args.account_env, broker_account=args.broker_account)

    rows = await list_daily_statements(eng, scope, since=since, until=until)
    if not rows:
        print(f"no statements in [{since}, {until}]", file=sys.stderr)
        return 1

    # Build daily NAV series: prepend the first statement's STARTING NAV at
    # day -1 so the TWR walk has a prior point for day 0.
    nav_by_date: list[tuple[date, Decimal]] = []
    first = rows[0]
    nav_by_date.append((first["statement_date"], Decimal(first["starting_nav_base"])))
    for r in rows:
        nav_by_date.append((r["statement_date"], Decimal(r["ending_nav_base"])))

    # External cashflow per statement date (HKD). Daily-statement is base-
    # currency HKD; futu_cash_flow is USD on the 5668 account. Convert
    # USD → HKD using the first statement's USD/HKD rate as a proxy. This
    # is approximate for validation — the production path uses each day's
    # own statement rate.
    rate_usd_hkd = Decimal(rows[-1]["exchange_rates"].get("USD/HKD") or "7.8")
    cashflows = await list_cashflows(
        eng,
        scope,
        since=None,
        until=None,
    )
    inflow_by_date: dict[date, Decimal] = {}
    for cf in cashflows:
        if not _is_external_cashflow(cf):
            continue
        d = cf["occurred_at"].date() if hasattr(cf["occurred_at"], "date") else cf["occurred_at"]
        if d < since or d > until:
            continue
        amount_usd = Decimal(str(cf["amount"] or 0))
        inflow_by_date[d] = inflow_by_date.get(d, Decimal("0")) + amount_usd * rate_usd_hkd

    # TWR + simple
    twr = Decimal("1")
    total_inflow = Decimal("0")
    daily_returns: list[tuple[date, Decimal]] = []
    for prev, curr in zip(nav_by_date, nav_by_date[1:]):
        prev_d, prev_nav = prev
        curr_d, curr_nav = curr
        flow = inflow_by_date.get(curr_d, Decimal("0"))
        total_inflow += flow
        denom = prev_nav + (flow / 2)
        if denom == 0:
            continue
        r = (curr_nav - prev_nav - flow) / denom
        twr *= 1 + r
        daily_returns.append((curr_d, r))

    period_income = nav_by_date[-1][1] - nav_by_date[0][1] - total_inflow
    simple_denom = nav_by_date[0][1] + (total_inflow / 2)
    simple = period_income / simple_denom if simple_denom else Decimal("0")

    print(f"rows: {len(rows)}")
    print(f"period: {nav_by_date[0][0]} → {nav_by_date[-1][0]}")
    print(f"start NAV (HKD): {nav_by_date[0][1]}")
    print(f"end   NAV (HKD): {nav_by_date[-1][1]}")
    print(f"external inflow (HKD): {total_inflow}")
    print(f"comprehensive income (HKD): {period_income}")
    print(f"simple return : {simple * 100:.4f}%")
    print(f"TWR           : {(twr - 1) * 100:.4f}%")
    print("\nfirst 5 daily returns:")
    for d, r in daily_returns[:5]:
        print(f"  {d}: {r * 100:.4f}%")
    print("\nlast 5 daily returns:")
    for d, r in daily_returns[-5:]:
        print(f"  {d}: {r * 100:.4f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
