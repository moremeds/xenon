"""Pull Futu daily statements from Outlook, decrypt, parse, persist.

Reads (Outlook IMAP via xenon.clients.outlook_imap, OAuth via
xenon.clients.outlook_oauth) → decrypts + parses (xenon.clients.futu_statement_pdf)
→ writes (xenon.db.queries.futu_history.insert_daily_statement). Read-only
against the mailbox.

Cross-validation runs after each sync batch:
  * continuity — every consecutive pair of statements must have
    statement_N.ending_nav_base ≈ statement_(N+1).starting_nav_base
    within `_CONTINUITY_TOLERANCE`. Anomalies are logged, not raised.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy.ext.asyncio import AsyncEngine

from xenon.clients.futu_statement_pdf import (
    FutuDailyStatement,
    StatementDecryptError,
    StatementParseError,
    parse,
)
from xenon.clients.outlook_imap import OutlookFetcher, StatementEmail
from xenon.db.queries.futu_history import (
    insert_daily_statement,
    insert_statement_inbox,
    list_daily_statements,
)
from xenon.execution.account_scope import AccountScope

logger = logging.getLogger(__name__)

# HKD tolerance for continuity check. Statements round at HKD 0.01 per cell;
# 5 HKD covers rounding + the occasional EOD price tweak between statements.
_CONTINUITY_TOLERANCE = Decimal("5.00")


@dataclass(frozen=True)
class SyncReport:
    fetched: int  # IMAP messages downloaded
    parsed: int  # successfully decrypted + parsed
    inserted: int  # rows upserted (rowcount sum)
    inbox: int  # raw-only rows persisted to inbox (parse/decrypt failure)
    skipped: list[tuple[str, str]]  # (uid, reason)
    continuity_anomalies: list[str]  # human-readable warnings


def _statement_to_row(stmt: FutuDailyStatement, raw_pdf: bytes, email: StatementEmail) -> dict:
    """Map parsed dataclass + source PDF/email into the PG row shape."""
    return {
        "statement_date": stmt.statement_date,
        "preparation_date": stmt.preparation_date,
        "account_number": stmt.account_number,
        "account_suffix": stmt.account_suffix,
        "client_name": stmt.client_name,
        "base_currency": stmt.base_currency,
        "starting_portfolio_base": stmt.starting_portfolio_base,
        "ending_portfolio_base": stmt.ending_portfolio_base,
        "starting_funds_base": stmt.starting_funds_base,
        "ending_funds_base": stmt.ending_funds_base,
        "starting_cash_base": stmt.starting_cash_base,
        "ending_cash_base": stmt.ending_cash_base,
        "starting_nav_base": stmt.starting_nav_base,
        "ending_nav_base": stmt.ending_nav_base,
        # JSONB columns — convert Decimal to str so json round-trip is lossless
        "starting_nav_by_currency": {k: str(v) for k, v in stmt.starting_nav_by_currency.items()},
        "ending_nav_by_currency": {k: str(v) for k, v in stmt.ending_nav_by_currency.items()},
        "exchange_rates": {k: str(v) for k, v in stmt.exchange_rates.items()},
        "page_text": stmt.page_text,
        "financing": _jsonable(stmt.financing),
        "transaction_totals": _jsonable(stmt.transaction_totals),
        "raw_pdf": raw_pdf,
        "source_uid": email.uid,
        "source_subject": email.subject,
    }


def _jsonable(obj):
    """Recursively convert Decimal/date to JSON-safe primitives."""
    from datetime import date as _date

    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, _date):
        return obj.isoformat()
    return obj


async def sync_statements(
    engine: AsyncEngine,
    scope: AccountScope,
    since: Optional[date] = None,
    until: Optional[date] = None,
    folder: str = "Inbox",
) -> SyncReport:
    """Fetch matching statements, parse, UPSERT, run continuity validation.

    `since` / `until` filter on email received-date via IMAP SEARCH SINCE.
    Pass None to fetch everything available in the mailbox.
    """
    fetched = 0
    parsed = 0
    inserted = 0
    inbox = 0
    skipped: list[tuple[str, str]] = []
    parsed_dates: list[date] = []

    with OutlookFetcher(folder=folder) as fx:
        uids = fx.search(since=since)
        logger.info("matched %d statement email(s) in %s", len(uids), folder)
        for uid in uids:
            email = fx.fetch(uid)
            fetched += 1
            if email is None:
                skipped.append((uid, "no PDF attachment"))
                continue
            try:
                stmt = parse(email.attachment_bytes)
            except (StatementDecryptError, StatementParseError) as exc:
                # Preserve the raw bytes so a later parser revision can drain
                # the inbox without re-fetching from IMAP. Older statement
                # layouts (no "Ending Assets Overview" header) land here.
                kind = type(exc).__name__
                logger.warning("%s UID=%s: %s — saving to inbox", kind, uid, exc)
                inbox_row = _inbox_row(email, parse_error=f"{kind}: {exc}")
                inbox += await insert_statement_inbox(engine, scope, inbox_row)
                skipped.append((uid, f"{kind}: {exc}"))
                continue

            if until is not None and stmt.statement_date > until:
                skipped.append((uid, f"after until: {stmt.statement_date}"))
                continue
            if since is not None and stmt.statement_date < since:
                # IMAP SINCE filters on received-date; statement_date can lag
                skipped.append((uid, f"before since: {stmt.statement_date}"))
                continue

            row = _statement_to_row(stmt, email.attachment_bytes, email)
            inserted += await insert_daily_statement(engine, scope, row)
            parsed += 1
            parsed_dates.append(stmt.statement_date)

    anomalies = await validate_continuity(engine, scope, since=since, until=until)
    logger.info(
        "sync done: fetched=%d parsed=%d inserted=%d inbox=%d skipped=%d anomalies=%d",
        fetched,
        parsed,
        inserted,
        inbox,
        len(skipped),
        len(anomalies),
    )
    return SyncReport(
        fetched=fetched,
        parsed=parsed,
        inserted=inserted,
        inbox=inbox,
        skipped=skipped,
        continuity_anomalies=anomalies,
    )


def _inbox_row(email: StatementEmail, parse_error: str) -> dict:
    """Map a fetched email + error string into the futu_statement_inbox row shape."""
    return {
        "source_uid": email.uid,
        "subject": email.subject,
        "sender": email.sender,
        "received_at": email.received_at,
        "attachment_name": email.attachment_name,
        "raw_pdf": email.attachment_bytes,
        "parse_error": parse_error,
    }


async def validate_continuity(
    engine: AsyncEngine,
    scope: AccountScope,
    since: Optional[date] = None,
    until: Optional[date] = None,
) -> list[str]:
    """Check that consecutive statements have matching ending→starting NAV.

    Returns a list of human-readable anomaly descriptions. Tolerance is
    `_CONTINUITY_TOLERANCE` HKD per boundary; finer mismatch usually means
    a missing trading day between the two statements (weekend/holiday is
    fine and skipped by this check).
    """
    rows = await list_daily_statements(engine, scope, since=since, until=until)
    anomalies: list[str] = []
    for prev, curr in zip(rows, rows[1:]):
        end = Decimal(prev["ending_nav_base"])
        start = Decimal(curr["starting_nav_base"])
        gap = (start - end).copy_abs()
        if gap > _CONTINUITY_TOLERANCE:
            anomalies.append(
                f"NAV jump {prev['statement_date']}→{curr['statement_date']}: ending={end} starting={start} gap={gap}"
            )
    return anomalies


__all__ = ("SyncReport", "sync_statements", "validate_continuity")
