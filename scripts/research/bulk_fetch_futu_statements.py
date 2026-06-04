"""Bulk-download every Futu daily statement from Outlook → futu_statement_inbox.

Decoupled from parsing. Saves raw encrypted PDFs only — classification and
format-specific parsers come later. One-off historical job; intended to be
run once and then archived.

Subject patterns surfaced from a 2020→2026 mailbox survey:

  * `Daily Statement of US Stock(s) Margin Account-YYYY/MM/DD`    (2021→2024)
  * `Daily Statement of HK Stocks Margin Account-YYYY/MM/DD`     (2022→2024)
  * `Daily Statement of Universal Account - Securities-...`      (Aug→Dec 2024 transition)
  * `Daily Statement of Margin Universal Account (5668) - Securities-...`  (Dec 2024 onward)
  * `港股保證金賬戶日結單-YYYY/MM/DD`                              (Trad Chinese, HK)
  * `美股保证金账户日结单-YYYY/MM/DD`                              (Simp Chinese, US)
  * `美股孖展賬戶日結單-YYYY/MM/DD`                                (Trad Chinese, US — 孖展 = Cantonese for margin)

We DO NOT include monthly statements (`Monthly Statement of ...`, `月結單`).

Storage scope: broker=FUTU, account_env=live, broker_account=5668
(operator's current composite). When a parser learns to read the older
formats and emits a real broker_account, the inbox row can be drained
into futu_daily_statement with the correct (legacy) account number.

CLI
---
    uv run python scripts/research/bulk_fetch_futu_statements.py [--dry-run] [--limit N]

Env
---
    OUTLOOK_USER, OUTLOOK_OAUTH_CLIENT_ID    — same as xenon-futu-statement-sync
    DATABASE_URL                             — Postgres
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import imaplib
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Optional

from sqlalchemy.ext.asyncio import create_async_engine

from xenon.clients.outlook_oauth import acquire_token, build_xoauth2_sasl
from xenon.db.queries.futu_history import insert_statement_inbox, list_statement_inbox_uids
from xenon.execution.account_scope import AccountScope

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bulk-fetch")

IMAP_HOST = "outlook.office365.com"
IMAP_PORT = 993

# All subject substrings that mark a Futu DAILY statement (case-insensitive
# for English; exact for Chinese). Monthly variants are deliberately excluded.
DAILY_PATTERNS = [
    "daily statement",  # all English variants
    "日結單",  # traditional Chinese daily statement
    "日结单",  # simplified Chinese daily statement
]

# Futu sender domains observed.
FUTU_SENDERS = ["futuhk.com", "futu5.com"]


@dataclass(frozen=True)
class StatementBlob:
    uid: str
    subject: str
    sender: str
    received_at: datetime
    attachment_name: str
    attachment_bytes: bytes


def _decode_subject(raw: str) -> str:
    try:
        parts = decode_header(raw)
        return "".join(
            (p[0].decode(p[1] or "utf-8", errors="replace") if isinstance(p[0], bytes) else p[0]) for p in parts
        )
    except Exception:
        return raw


def _matches_daily(subject: str) -> bool:
    lower = subject.lower()
    if "monthly statement" in lower or "月結單" in subject or "月结单" in subject:
        return False
    if any(p in lower for p in (s.lower() for s in DAILY_PATTERNS if s.isascii())):
        return True
    return any(p in subject for p in DAILY_PATTERNS if not p.isascii())


def _connect() -> imaplib.IMAP4_SSL:
    user = os.environ["OUTLOOK_USER"]
    token = acquire_token(client_id=os.environ["OUTLOOK_OAUTH_CLIENT_ID"])
    sasl = build_xoauth2_sasl(user, token.access_token)
    b64 = base64.b64encode(sasl).decode("ascii")
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    typ, _ = conn._simple_command("AUTHENTICATE", "XOAUTH2", b64)
    if typ != "OK":
        raise RuntimeError(f"IMAP auth rejected: {typ}")
    conn.state = "AUTH"
    return conn


def _collect_uids(conn: imaplib.IMAP4_SSL) -> list[str]:
    """Union of UIDs across all Futu senders."""
    all_uids: set[str] = set()
    for domain in FUTU_SENDERS:
        typ, data = conn.uid("SEARCH", None, "FROM", f'"{domain}"')
        if typ != "OK" or not data or not data[0]:
            continue
        for uid in data[0].split():
            all_uids.add(uid.decode("ascii"))
    return sorted(all_uids, key=int)


def _fetch_one(conn: imaplib.IMAP4_SSL, uid: str) -> Optional[StatementBlob]:
    # Header-only first — cheap subject filter
    typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
    if typ != "OK" or not data or not data[0]:
        return None
    body = data[0][1] if isinstance(data[0], tuple) else b""
    m_subj = re.search(rb"Subject:\s*(.*)", body)
    m_from = re.search(rb"From:\s*(.*)", body)
    m_date = re.search(rb"Date:\s*(.*)", body)
    if not (m_subj and m_from):
        return None
    subject = _decode_subject(m_subj.group(1).decode("utf-8", errors="replace").strip())
    if not _matches_daily(subject):
        return None
    sender = m_from.group(1).decode("utf-8", errors="replace").strip()
    try:
        received_at = parsedate_to_datetime(m_date.group(1).decode("ascii", errors="replace").strip())
    except Exception:
        received_at = None

    # Full message — extract first PDF attachment
    typ, data = conn.uid("FETCH", uid, "(RFC822)")
    if typ != "OK" or not data or not data[0]:
        return None
    msg_bytes = data[0][1] if isinstance(data[0], tuple) else b""
    msg = message_from_bytes(msg_bytes)
    for part in msg.walk():
        ctype = part.get_content_type()
        raw_filename = part.get_filename() or ""
        # Filenames may arrive RFC 2047 encoded (older Chinese statements):
        #   =?utf-8?b?...?=
        # Decode so .pdf detection + the persisted name are human-readable.
        try:
            parts_ = decode_header(raw_filename)
            filename = "".join(
                (p[0].decode(p[1] or "utf-8", errors="replace") if isinstance(p[0], bytes) else p[0]) for p in parts_
            )
        except Exception:
            filename = raw_filename
        # PDF detection: by MIME type, by extension, OR by magic bytes (older
        # Chinese statements arrive as application/octet-stream with %PDF magic).
        looks_pdf = "pdf" in ctype.lower() or filename.lower().endswith(".pdf") or raw_filename.lower().endswith(".pdf")
        if not looks_pdf and ctype.lower() == "application/octet-stream":
            head = part.get_payload(decode=True) or b""
            looks_pdf = head[:5] == b"%PDF-"
        if looks_pdf:
            payload = part.get_payload(decode=True)
            if payload:
                return StatementBlob(
                    uid=uid,
                    subject=subject,
                    sender=sender,
                    received_at=received_at,
                    attachment_name=filename or "statement.pdf",
                    attachment_bytes=payload,
                )
    return None


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None, help="cap on emails to process")
    p.add_argument("--dry-run", action="store_true", help="don't write to DB")
    args = p.parse_args(argv)

    db_url = os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://")
    if "+psycopg" in db_url:
        db_url = db_url.replace("postgresql+psycopg://", "postgresql+asyncpg://")
    engine = create_async_engine(db_url, future=True)
    scope = AccountScope(broker="FUTU", account_env="live", broker_account="5668")

    # Skip UIDs already in the inbox to make this idempotent / re-runnable.
    seen_uids: set[str] = set()
    if not args.dry_run:
        seen_uids = await list_statement_inbox_uids(engine, scope)
        logger.info("inbox already contains %d UIDs — skipping those", len(seen_uids))

    conn = _connect()
    conn.select("Inbox", readonly=True)
    try:
        uids = _collect_uids(conn)
        logger.info("found %d emails across Futu senders", len(uids))
        if args.limit:
            uids = uids[: args.limit]

        saved = 0
        inspected = 0
        skipped_seen = 0
        skipped_nonmatch = 0
        for uid in uids:
            if uid in seen_uids:
                skipped_seen += 1
                continue
            inspected += 1
            blob = _fetch_one(conn, uid)
            if blob is None:
                skipped_nonmatch += 1
                continue
            if args.dry_run:
                saved += 1
                if saved <= 10:
                    print(f"  would save UID={uid} [{blob.received_at}] {blob.subject[:80]}")
                continue
            row = {
                "source_uid": blob.uid,
                "subject": blob.subject,
                "sender": blob.sender,
                "received_at": blob.received_at,
                "attachment_name": blob.attachment_name,
                "raw_pdf": blob.attachment_bytes,
                "parse_error": None,
            }
            await insert_statement_inbox(engine, scope, row)
            saved += 1
            if saved % 25 == 0:
                logger.info("saved %d so far (current: %s)", saved, blob.subject[:60])
    finally:
        try:
            conn.logout()
        except Exception:
            pass
        await engine.dispose()

    logger.info(
        "bulk-fetch done: inspected=%d saved=%d skipped_seen=%d skipped_nonmatch=%d",
        inspected,
        saved,
        skipped_seen,
        skipped_nonmatch,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
