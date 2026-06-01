"""CLI entry point — invoked by launchd at 16:30 ET daily, and by the operator
on-demand for catch-up syncs.

Usage:
    uv run xenon-futu-history-sync --account ACC [--env paper|live]
                                                 [--since auto|YYYY-MM-DD]
                                                 [--rematch]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from xenon.api.services.futu_history import (
    SinceOutOfRange,
    SinceUnparseable,
    futu_history_sync,
    parse_since,
)
from xenon.execution.account_scope import AccountScope

log = logging.getLogger("xenon.futu_history_sync")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xenon-futu-history-sync")
    parser.add_argument(
        "--account",
        required=True,
        help="Futu broker_account (scope identifier)",
    )
    parser.add_argument(
        "--env",
        default="live",
        choices=["paper", "live", "sim", "legacy_unknown"],
    )
    parser.add_argument(
        "--since",
        default="auto",
        help="auto|YYYY-MM-DD (floor 2024-05-01)",
    )
    parser.add_argument(
        "--rematch",
        action="store_true",
        help="Force full FIFO rematch + walk-back recompute",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        since = parse_since(args.since)
    except SinceUnparseable as exc:
        print(f"error: --since unparseable: {exc.bad_value!r}", file=sys.stderr)
        return 2
    except SinceOutOfRange as exc:
        print(f"error: --since out of range: {exc.bad_value!r}", file=sys.stderr)
        return 2

    scope = AccountScope(
        broker="FUTU",
        account_env=args.env,
        broker_account=args.account,
    )
    result = futu_history_sync(scope=scope, since=since, rematch=args.rematch)
    print(json.dumps(result, default=str, indent=2))
    return 0 if result.get("status") in {"ok", "in_progress"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
