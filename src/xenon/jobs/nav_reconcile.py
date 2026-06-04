"""xenon-nav-reconcile — daily intraday-vs-close NAV discrepancy report.

Pass-1 finding C3 + Pass-2 E1(a): nav_history is the audit table; intraday
and close rows coexist for the same (broker, account_env, broker_account,
date) under the 2026-06-03 5-col PK. This CLI is the operator-facing reader
— for a given scope and date range, surface dates where the per-date
intraday NAV diverges from the close NAV by more than the tolerance.

Read-only — never writes nav_history. ``XENON_READ_ONLY=1`` is logged for
uniformity with sibling jobs but does not change behavior.

Exit codes:
  0 — clean: no rows OR every reconcilable date within tolerance
  1 — usage error (bad --since / --until)
  4 — at least one date exceeds tolerance (operator must investigate)
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text

from xenon.db.engine import get_sync_engine


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    except ImportError:
        pass


def _ensure_broker_account_env() -> None:
    """Derive XENON_BROKER_ACCOUNT from XENON_TRADING_MODE (mirror of dev.sh)."""
    if os.environ.get("XENON_BROKER_ACCOUNT"):
        return
    mode = os.environ.get("XENON_TRADING_MODE", "").strip().lower()
    env_key = {
        "live": "XENON_LIVE_ACCOUNT",
        "paper": "XENON_PAPER_ACCOUNT",
    }.get(mode)
    if env_key and os.environ.get(env_key):
        os.environ["XENON_BROKER_ACCOUNT"] = os.environ[env_key]


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


# Same SQL shape as the plan's Background section. Tolerance is applied
# in Python so we can produce a flagged-row report even when nothing
# exceeds the threshold.
_QUERY = text(
    """
    SELECT date,
           MAX(nav) FILTER (WHERE source='intraday') AS intra_nav,
           MAX(nav) FILTER (WHERE source='close')    AS close_nav
      FROM xenon.nav_history
     WHERE broker = :broker
       AND account_env = :env
       AND broker_account = :acct
       AND date BETWEEN :since AND :until
     GROUP BY date
    HAVING MAX(nav) FILTER (WHERE source='intraday') IS NOT NULL
       AND MAX(nav) FILTER (WHERE source='close')    IS NOT NULL
     ORDER BY date ASC
    """
)


def main(argv: list[str] | None = None) -> int:
    _load_env()

    if os.environ.get("XENON_READ_ONLY") == "1":
        # Read-only by design; just log for uniformity with sibling CLIs.
        print(
            "⏭  XENON_READ_ONLY=1 — proceeding (nav-reconcile is read-only).",
            file=sys.stderr,
        )

    _ensure_broker_account_env()

    parser = argparse.ArgumentParser(
        prog="xenon-nav-reconcile",
        description=(
            "Compare same-date intraday vs close NAV rows in xenon.nav_history "
            "and flag rows where the diff exceeds the tolerance."
        ),
    )
    parser.add_argument("--since", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--until", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument(
        "--tolerance-bps",
        type=float,
        default=10.0,
        help="Discrepancy threshold in basis points (default 10 = 0.1%%).",
    )
    parser.add_argument(
        "--broker",
        default=os.environ.get("XENON_BROKER", "IB"),
        help="Broker (default IB or $XENON_BROKER).",
    )
    parser.add_argument(
        "--account-env",
        default=os.environ.get("XENON_TRADING_MODE", "live"),
        help="paper / live / sim (default $XENON_TRADING_MODE).",
    )
    parser.add_argument(
        "--broker-account",
        default=os.environ.get("XENON_BROKER_ACCOUNT"),
        help="Broker account id (default $XENON_BROKER_ACCOUNT).",
    )

    args = parser.parse_args(argv)

    try:
        since = _parse_date(args.since)
        until = _parse_date(args.until)
    except ValueError as exc:
        print(f"FATAL: bad date format: {exc}", file=sys.stderr)
        return 1
    if since > until:
        print("FATAL: --since must be <= --until", file=sys.stderr)
        return 1
    if not args.broker_account:
        print(
            "FATAL: broker_account is required (set XENON_BROKER_ACCOUNT or pass --broker-account)",
            file=sys.stderr,
        )
        return 1

    engine = get_sync_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            _QUERY,
            {
                "broker": args.broker,
                "env": args.account_env,
                "acct": args.broker_account,
                "since": since,
                "until": until,
            },
        ).fetchall()

    print(
        f"xenon-nav-reconcile: scope=({args.broker}, {args.account_env}, "
        f"{args.broker_account}) window={since}→{until} "
        f"tolerance={args.tolerance_bps}bps"
    )

    if not rows:
        print("no rows reconcilable in the window (need both intraday + close).")
        return 0

    tolerance_ratio = args.tolerance_bps / 10_000.0
    flagged: list[tuple] = []
    print()
    print(f"{'date':<12} {'intra_nav':>14} {'close_nav':>14} {'diff_bps':>12} {'over_tol':>10}")
    print("-" * 64)
    for r in rows:
        intra = float(r.intra_nav)
        close = float(r.close_nav)
        if intra == 0:
            continue
        diff_ratio = abs(close - intra) / abs(intra)
        diff_bps = diff_ratio * 10_000.0
        over = diff_ratio > tolerance_ratio
        if over:
            flagged.append((r.date, intra, close, diff_bps))
        print(f"{str(r.date):<12} {intra:>14.2f} {close:>14.2f} {diff_bps:>12.2f} {'YES' if over else 'no':>10}")
    print()

    if flagged:
        print(
            f"{len(flagged)} flagged date(s) exceed tolerance "
            f"({args.tolerance_bps}bps). Investigate the IB sync window vs "
            f"the close NAV writer."
        )
        return 4
    print(f"OK: {len(rows)} dates reconciled, all within tolerance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
