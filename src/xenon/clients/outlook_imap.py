"""Consumer-Outlook IMAP fetcher for Futu daily-statement emails.

Connects to imap-mail.outlook.com:993. Authentication picks the right
path based on env:

  - If OUTLOOK_OAUTH_CLIENT_ID is set, use XOAUTH2 via MSAL device flow
    (see xenon.clients.outlook_oauth). Required for accounts Microsoft
    has migrated off basic auth (most consumer Outlook in 2026).
  - Else if OUTLOOK_APP_PASSWORD is set, use basic LOGIN. Kept for the
    legacy-cohort accounts that still accept app passwords.

Read-only against the mailbox: never marks, moves, deletes, or flags
messages. The caller decides what to persist locally.

Env vars (sourced via python-dotenv where the caller loads .env):

    OUTLOOK_USER             — full email address
    OUTLOOK_OAUTH_CLIENT_ID  — Azure app client id (preferred path)
    OUTLOOK_APP_PASSWORD     — 16-char Microsoft app password (legacy
                               fallback for accounts still on basic auth)

Folder name defaults to "Inbox". Consumer Outlook also exposes localised
folders; if statements are filed under a different folder, pass it via
the `folder` argument.
"""

from __future__ import annotations

import email
import imaplib
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.message import Message
from typing import Iterable, Optional

from xenon.clients.outlook_oauth import (
    CLIENT_ID_ENV,
    OAuthSetupError,
    acquire_token,
    build_xoauth2_sasl,
)

logger = logging.getLogger(__name__)

IMAP_HOST = "imap-mail.outlook.com"
IMAP_PORT = 993

# Futu's standard subject. Account suffix differs per user — match by prefix.
DEFAULT_SUBJECT_PREFIX = "daily statement of margin universal account"


@dataclass(frozen=True)
class StatementEmail:
    """One matched email with its first PDF attachment surfaced."""

    uid: str
    subject: str
    sender: str
    received_at: datetime
    attachment_name: str
    attachment_bytes: bytes


class OutlookAuthError(RuntimeError):
    """IMAP login failed — likely missing or wrong app password."""


class OutlookFetcher:
    """Stateful IMAP session helper.

    Use as a context manager:

        with OutlookFetcher() as fx:
            for stmt in fx.iter_statements(since=date(2026, 1, 1)):
                ...

    Each `iter_statements` call issues a single IMAP SEARCH followed by
    one FETCH per match.
    """

    def __init__(
        self,
        user: Optional[str] = None,
        password: Optional[str] = None,
        folder: str = "Inbox",
    ) -> None:
        self.user = user or os.environ.get("OUTLOOK_USER")
        self.password = password or os.environ.get("OUTLOOK_APP_PASSWORD")
        self.oauth_client_id = os.environ.get(CLIENT_ID_ENV)
        self.folder = folder
        self._conn: Optional[imaplib.IMAP4_SSL] = None
        if not self.user:
            raise OutlookAuthError("OUTLOOK_USER must be set in .env")
        if not self.oauth_client_id and not self.password:
            raise OutlookAuthError(
                f"set either {CLIENT_ID_ENV} (OAuth, preferred) or OUTLOOK_APP_PASSWORD (legacy basic auth) in .env"
            )

    def __enter__(self) -> "OutlookFetcher":
        # Acquire the OAuth token BEFORE opening the IMAP socket. The device-flow
        # path blocks for minutes while the user authenticates; an IMAP socket
        # opened first would time out idle and break on AUTHENTICATE.
        if self.oauth_client_id:
            try:
                token = acquire_token(client_id=self.oauth_client_id)
            except OAuthSetupError as exc:
                raise OutlookAuthError(f"OAuth token acquisition failed: {exc}") from exc
            import base64

            sasl = build_xoauth2_sasl(self.user, token.access_token)
            b64 = base64.b64encode(sasl).decode("ascii")
            self._conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            # Outlook IMAP XOAUTH2 requires the SASL response inline (SASL-IR)
            # — imaplib.authenticate() uses the challenge/response flow and
            # Outlook closes the socket if no inline body arrives.
            try:
                typ, _ = self._conn._simple_command("AUTHENTICATE", "XOAUTH2", b64)
                if typ != "OK":
                    raise OutlookAuthError(f"IMAP XOAUTH2 rejected: {typ}")
                self._conn.state = "AUTH"
            except imaplib.IMAP4.error as exc:
                raise OutlookAuthError(f"IMAP XOAUTH2 failed: {exc}") from exc
        else:
            self._conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            try:
                self._conn.login(self.user, self.password)
            except imaplib.IMAP4.error as exc:
                raise OutlookAuthError(f"IMAP login failed: {exc}") from exc
        typ, _ = self._conn.select(self.folder, readonly=True)
        if typ != "OK":
            raise OutlookAuthError(f"cannot SELECT folder {self.folder!r}")
        return self

    def __exit__(self, *exc: object) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except imaplib.IMAP4.error:
                pass
            try:
                self._conn.logout()
            except imaplib.IMAP4.error:
                pass
            self._conn = None

    def _require_conn(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            raise RuntimeError("OutlookFetcher used outside `with` block")
        return self._conn

    def search(
        self,
        subject_prefix: str = DEFAULT_SUBJECT_PREFIX,
        since: Optional[date] = None,
    ) -> list[str]:
        """Return IMAP message UIDs whose subject starts with the prefix.

        IMAP SUBJECT search is substring + case-insensitive, so the prefix
        match is enforced on the client side after the cheaper server-side
        filter narrows the candidate set.
        """
        conn = self._require_conn()
        criteria: list[bytes] = []
        if since is not None:
            criteria.append(b"SINCE")
            criteria.append(since.strftime("%d-%b-%Y").encode("ascii"))
        criteria.append(b"SUBJECT")
        # Server-side SUBJECT must match a single token; use the most
        # distinctive word from the prefix.
        token = _imap_subject_token(subject_prefix)
        criteria.append(b'"' + token.encode("ascii") + b'"')
        typ, data = conn.uid("SEARCH", None, *criteria)
        if typ != "OK":
            raise RuntimeError(f"IMAP SEARCH failed: {typ} {data!r}")
        if not data or not data[0]:
            return []
        uids = data[0].decode("ascii").split()
        # Now apply the full client-side prefix filter via per-UID
        # ENVELOPE fetch — cheaper than fetching full bodies.
        keep: list[str] = []
        for uid in uids:
            subj = self._fetch_subject(uid)
            if subj.lower().startswith(subject_prefix.lower()):
                keep.append(uid)
        return keep

    def _fetch_subject(self, uid: str) -> str:
        conn = self._require_conn()
        typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return ""
        raw = data[0][1] or b""
        msg = email.message_from_bytes(raw)
        return _decode_header(msg.get("Subject", ""))

    def iter_statements(
        self,
        subject_prefix: str = DEFAULT_SUBJECT_PREFIX,
        since: Optional[date] = None,
    ) -> Iterable[StatementEmail]:
        """Yield decoded statements for every matching message in `folder`."""
        for uid in self.search(subject_prefix=subject_prefix, since=since):
            stmt = self.fetch(uid)
            if stmt is not None:
                yield stmt

    def fetch(self, uid: str) -> Optional[StatementEmail]:
        """Fetch a single message by UID and return its first PDF attachment."""
        conn = self._require_conn()
        typ, data = conn.uid("FETCH", uid, "(RFC822)")
        if typ != "OK" or not data:
            return None
        for part in data:
            if not isinstance(part, tuple):
                continue
            raw = part[1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            msg = email.message_from_bytes(bytes(raw))
            attachment = _first_pdf_attachment(msg)
            if attachment is None:
                logger.warning("no PDF attachment for UID %s", uid)
                return None
            name, content = attachment
            return StatementEmail(
                uid=uid,
                subject=_decode_header(msg.get("Subject", "")),
                sender=_decode_header(msg.get("From", "")),
                received_at=_parse_date(msg.get("Date")),
                attachment_name=name,
                attachment_bytes=content,
            )
        return None


def _decode_header(value: str) -> str:
    from email.header import decode_header, make_header

    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError):
        return value


def _parse_date(raw: Optional[str]) -> datetime:
    from email.utils import parsedate_to_datetime

    if not raw:
        return datetime.now(timezone.utc)
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _first_pdf_attachment(msg: Message) -> Optional[tuple[str, bytes]]:
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = _decode_header(filename)
        if not filename.lower().endswith(".pdf"):
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, (bytes, bytearray)):
            return filename, bytes(payload)
    return None


_TOKEN_RE = re.compile(r"[A-Za-z]+")


def _imap_subject_token(prefix: str) -> str:
    """Pick the most distinctive word in the subject prefix for SEARCH.

    IMAP SUBJECT matches one phrase at a time; multi-word phrase support
    varies by server. Using the longest alphabetic token keeps the server-
    side filter tight without depending on phrase support.
    """
    tokens = [t for t in _TOKEN_RE.findall(prefix) if len(t) >= 4]
    tokens.sort(key=len, reverse=True)
    return tokens[0] if tokens else "statement"
