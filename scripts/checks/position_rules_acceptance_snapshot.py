#!/usr/bin/env python3
"""Write one daily position-rules paper acceptance snapshot.

This automates the three commands in
docs/runbooks/position-rules-acceptance-gate.md while keeping artifacts under
the ignored logs/ directory.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable


Runner = Callable[[list[str]], str]


def _run_command(cmd: list[str]) -> str:
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout


def run_snapshot(
    *,
    day: str,
    logs_dir: Path,
    duplicate_close_since: str = "1d",
    events_since: str = "24h",
    runner: Runner = _run_command,
) -> dict[str, object]:
    logs_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "duplicate_close": logs_dir / f"no-dup-close-{day}.json",
        "transitions": logs_dir / f"transitions-{day}.json",
        "health": logs_dir / f"health-{day}.json",
    }

    outputs = {
        "duplicate_close": runner(
            [
                sys.executable,
                "scripts/checks/no_duplicate_close_audit.py",
                "--since",
                duplicate_close_since,
            ]
        ),
        "transitions": runner(
            ["xenon-position-rules", "events", "--since", events_since]
        ),
        "health": runner(["xenon-position-rules", "health", "--json"]),
    }

    for name, output in outputs.items():
        files[name].write_text(output, encoding="utf-8")

    return {
        "day": day,
        "duplicate_close_since": duplicate_close_since,
        "events_since": events_since,
        "files": {name: str(path) for name, path in files.items()},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write ignored logs/* snapshots for the position-rules 14-day paper acceptance gate."
    )
    parser.add_argument(
        "--day",
        default=datetime.now(UTC).date().isoformat(),
        help="UTC day stamp for output filenames, YYYY-MM-DD. Defaults to today in UTC.",
    )
    parser.add_argument(
        "--logs-dir",
        type=Path,
        default=Path("logs"),
        help="Directory for snapshot artifacts. Defaults to logs/.",
    )
    parser.add_argument(
        "--since",
        default="1d",
        help="Window passed to no_duplicate_close_audit.py. Defaults to 1d.",
    )
    parser.add_argument(
        "--events-since",
        default="24h",
        help="Window passed to xenon-position-rules events. Defaults to 24h.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_snapshot(
        day=args.day,
        logs_dir=args.logs_dir,
        duplicate_close_since=args.since,
        events_since=args.events_since,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
