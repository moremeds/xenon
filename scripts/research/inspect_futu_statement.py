"""Bootstrap inspection: pull the most recent Futu daily statement and
dump its decrypted text + tables so we can design the typed parser.

Usage:

    uv run python scripts/research/inspect_futu_statement.py
    uv run python scripts/research/inspect_futu_statement.py --uid 12345
    uv run python scripts/research/inspect_futu_statement.py --since 2026-05-01 --limit 1

Reads OUTLOOK_USER + OUTLOOK_APP_PASSWORD + FUTU_STATEMENT_PASSWORD from
xenon/.env. Does not write to the mailbox. Does not persist anything to
PG. Writes the decrypted PDF to a local tmp file for reference and prints
the extracted text/tables so a human (or a future iteration of this code)
can identify the NAV / cash / market_value fields.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

from xenon.clients.futu_statement_pdf import StatementDecryptError, inspect
from xenon.clients.outlook_imap import OutlookAuthError, OutlookFetcher

logger = logging.getLogger("inspect_futu_statement")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--uid", help="fetch a specific IMAP UID instead of the latest")
    p.add_argument("--since", help="ISO date YYYY-MM-DD; defaults to 30 days back")
    p.add_argument("--limit", type=int, default=1, help="how many statements to dump (default 1)")
    p.add_argument("--folder", default="Inbox", help="IMAP folder name (default Inbox)")
    p.add_argument(
        "--dump-dir",
        default=None,
        help="where to write decrypted PDFs (default: $TMPDIR/futu-statements/)",
    )
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def _since_default(arg: str | None) -> date:
    if arg:
        return date.fromisoformat(arg)
    today = datetime.utcnow().date()
    return (
        today.replace(day=max(1, today.day - 30))
        if today.day > 30
        else date(today.year, max(1, today.month - 1), today.day)
    )


def _dump_path(root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    return root / name


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()

    dump_dir = Path(args.dump_dir) if args.dump_dir else Path(tempfile.gettempdir()) / "futu-statements"

    try:
        with OutlookFetcher(folder=args.folder) as fx:
            uids = [args.uid] if args.uid else fx.search(since=_since_default(args.since))
            if not uids:
                print("no matching statements found", file=sys.stderr)
                return 1
            uids = uids[-args.limit :]
            print(f"# matched {len(uids)} statement(s); inspecting…", file=sys.stderr)
            for uid in uids:
                stmt = fx.fetch(uid)
                if stmt is None:
                    print(f"# UID {uid}: no PDF attachment", file=sys.stderr)
                    continue
                target = _dump_path(dump_dir, f"{stmt.uid}_{stmt.attachment_name}")
                target.write_bytes(stmt.attachment_bytes)
                print(f"\n=== UID {stmt.uid} ===")
                print(f"subject : {stmt.subject}")
                print(f"sender  : {stmt.sender}")
                print(f"received: {stmt.received_at.isoformat()}")
                print(f"size    : {len(stmt.attachment_bytes):,} bytes")
                print(f"saved   : {target}")
                try:
                    raw = inspect(stmt.attachment_bytes)
                except StatementDecryptError as exc:
                    print(f"DECRYPT FAILED: {exc}", file=sys.stderr)
                    print("Set FUTU_STATEMENT_PASSWORD in .env and retry.", file=sys.stderr)
                    return 2
                print(f"pages   : {raw.page_count}")
                for i, txt in enumerate(raw.text_by_page, 1):
                    print(f"\n--- page {i} text ---")
                    print(txt)
                for i, tables in enumerate(raw.tables_by_page, 1):
                    if not tables:
                        continue
                    print(f"\n--- page {i} tables ({len(tables)}) ---")
                    for j, t in enumerate(tables, 1):
                        print(f"\n  table {j}:")
                        print(json.dumps(t, ensure_ascii=False, indent=2))
    except OutlookAuthError as exc:
        print(f"AUTH FAILED: {exc}", file=sys.stderr)
        print(
            "Set OUTLOOK_USER + OUTLOOK_APP_PASSWORD in xenon/.env. "
            "Generate the app password at https://account.microsoft.com/security "
            "(Advanced security options → App passwords).",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
