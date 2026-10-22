"""IB Flex NAV + cash-flow fetch used by ``xenon.jobs.nav_flex_refresh``.

``fetch_ib_nav_series`` polls the IB Flex Web Service (SendRequest +
GetStatement), upserts the EquitySummary rows into ``xenon.nav_history``
(``source='close'``) and any Deposits/Withdrawals CashTransactions rows into
``xenon.ib_cash_flow``. Moved here from the removed ``reports.portfolio_performance``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def safe_float(value: Any, default: float = 0.0) -> float:
    if value in (None, "", "nan", "NaN"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _split_ib_flex_csv_sections(body: str) -> List[List[str]]:
    """Split a multi-section IB Flex CSV body into per-section line groups.

    IB Flex Web Service concatenates section CSVs in a single response
    (EquitySummaryInBase, then optionally CashTransactions, Transfers, …).
    Each section starts with a header row whose first column is
    ``ClientAccountID``. ``csv.DictReader`` only remembers the FIRST header
    so iterating across sections without a split yields garbage.

    Returns a list of per-section line lists. Empty list when ``body`` has
    no recognizable section header.
    """
    sections: List[List[str]] = []
    current: List[str] = []
    for line in body.splitlines():
        if line.lstrip().startswith(('"ClientAccountID"', "ClientAccountID")):
            if current:
                sections.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        sections.append(current)
    return sections


# Columns a CashTransactions section must carry for its rows to be usable:
# TransactionID (primary key + replay dedupe), Type (Deposits/Withdrawals
# filter), Date/Time (occurred_at), Amount + CurrencyPrimary (amount_usd
# and fx_rate derivation). One shared list: the daily job validates against
# it to warn once per run, and the CSV backfill validates to refuse a
# malformed export before any write — a missing Amount column would
# otherwise silently zero previously stored flows on upsert.
IB_CASH_TRANSACTIONS_REQUIRED_COLUMNS: tuple[str, ...] = (
    "TransactionID",
    "Type",
    "Date/Time",
    "Amount",
    "CurrencyPrimary",
)

# Transfers sections share TransactionID/Type/Date-Time/CurrencyPrimary
# with CashTransactions but measure with ``positionAmount`` — never treat
# one as a malformed cash section (docs/architecture/performance-reconstruction.md).
_TRANSFERS_MARKER_COLUMN = "PositionAmount"


def _cash_section_missing_columns(field_names: Any) -> Optional[List[str]]:
    """Validate a section header against the CashTransactions contract.

    Returns ``None`` when the section is not cash-shaped (NAV, Transfers,
    Trades, … — those belong to ``sections_skipped``), ``[]`` when the
    header is complete, or the list of missing required columns when the
    section is a malformed CashTransactions export.
    """
    names = set(field_names or [])
    if _TRANSFERS_MARKER_COLUMN in names:
        return None
    if "TransactionID" not in names and not {"Date/Time", "Type"} <= names:
        return None
    return [c for c in IB_CASH_TRANSACTIONS_REQUIRED_COLUMNS if c not in names]


def _parse_ib_cash_transactions_section(
    section_lines: List[str],
    *,
    account_filter: str | None = None,
) -> List[Dict[str, Any]]:
    """Parse a CashTransactions section into deposit/withdrawal dicts.

    Only ``Type == "Deposits/Withdrawals"`` rows are returned — dividends,
    fees, interest are internal investment activity and stay out of
    ``xenon.ib_cash_flow``. Date/Time strings come in two shapes from IB
    Flex: ``YYYYMMDD`` (date-only) and ``YYYYMMDD;HHMMSS`` (datetime).

    Returns rows ready for ``upsert_ib_cash_flow_sync`` plus the parsed
    ``occurred_at`` ET datetime; the raw row is preserved as the ``raw``
    field for auditability.
    """
    import csv as _csv
    import io as _io
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    et = _ZI("America/New_York")
    out: List[Dict[str, Any]] = []
    reader = _csv.DictReader(_io.StringIO("\n".join(section_lines)))
    if not reader.fieldnames or "TransactionID" not in reader.fieldnames:
        return out
    for row in reader:
        if account_filter and row.get("ClientAccountID", "").strip() != account_filter:
            continue
        txn_type = (row.get("Type") or "").strip()
        if "Deposit" not in txn_type and "Withdraw" not in txn_type:
            continue
        txn_id = (row.get("TransactionID") or "").strip()
        if not txn_id:
            continue
        dt_raw = (row.get("Date/Time") or "").strip()
        # Format: YYYYMMDD or YYYYMMDD;HHMMSS
        date_part = dt_raw.split(";", 1)[0]
        time_part = dt_raw.split(";", 1)[1] if ";" in dt_raw else "000000"
        try:
            occurred_at = _dt(
                int(date_part[:4]),
                int(date_part[4:6]),
                int(date_part[6:8]),
                int(time_part[:2]) if len(time_part) >= 2 else 0,
                int(time_part[2:4]) if len(time_part) >= 4 else 0,
                int(time_part[4:6]) if len(time_part) >= 6 else 0,
                tzinfo=et,
            )
        except (ValueError, IndexError) as exc:
            logger.warning(
                "fetch_ib_nav_series: skipping CashTransaction %s — could not parse Date/Time %r (%s)",
                txn_id,
                dt_raw,
                exc,
            )
            continue
        amount_raw = (row.get("Amount") or "").strip()
        if not amount_raw:
            # Never default a missing amount to 0: on transaction_id replay
            # the upsert would overwrite a stored amount_usd with zero.
            logger.warning(
                "fetch_ib_nav_series: skipping CashTransaction %s — missing or blank Amount",
                txn_id,
            )
            continue
        out.append(
            {
                "transaction_id": txn_id,
                "txn_type": txn_type,
                "description": (row.get("Description") or "").strip() or None,
                "amount_native": amount_raw,
                "currency": (row.get("CurrencyPrimary") or "").strip(),
                "occurred_at": occurred_at,
                "raw": dict(row),
            }
        )
    return out


def _persist_ib_cash_transactions(rows: List[Dict[str, Any]]) -> tuple[int, int]:
    """Upsert parsed CashTransactions rows.

    Returns ``(inserted, updated)`` so the caller can log the breakdown
    accurately — a re-run against the same Flex window legitimately produces
    all-updates (the new transactions are the delta), and the prior unified
    "ingested" count made replay vs first-run indistinguishable.

    Per-row exceptions are caught and logged WARNING so one bad row never
    blocks the rest: ``ValueError`` (unsupported currency, IB-only guard,
    bad scope), ``InvalidOperation`` (malformed Amount that can't become a
    Decimal), generic ``Exception`` (DB FK error on a wrong-shape row).
    Persistence failures at the engine level (PG down, etc.) propagate to
    the caller's outer try/except.
    """
    import decimal as _decimal

    from xenon.execution.account_scope import resolve_from_env
    from xenon.utils.ib_cash_flow_loader import upsert_ib_cash_flow_sync

    scope = resolve_from_env()
    inserted = 0
    updated = 0
    for r in rows:
        try:
            was_inserted = upsert_ib_cash_flow_sync(
                scope=scope,
                transaction_id=r["transaction_id"],
                txn_type=r["txn_type"],
                description=r["description"],
                amount_native=r["amount_native"],
                currency=r["currency"],
                occurred_at=r["occurred_at"],
                raw=r["raw"],
            )
            if was_inserted:
                inserted += 1
            else:
                updated += 1
        except (ValueError, _decimal.InvalidOperation) as exc:
            logger.warning(
                "fetch_ib_nav_series: skipping cash flow %s (currency=%r, amount=%r): %s",
                r.get("transaction_id"),
                r.get("currency"),
                r.get("amount_native"),
                exc,
            )
    return inserted, updated


def fetch_ib_nav_series() -> Optional[List[Dict[str, Any]]]:
    """Fetch daily NAV from IB Flex Query (EquitySummaryInBase) and persist to PG.

    Uses ``IB_FLEX_NAV_QUERY_ID`` env var (separate from trade query).
    Each entry is upserted into ``xenon.nav_history`` so a later
    ``load_ib_nav_cache()`` call can replay it without another Flex round-trip.

    Side effect: when the saved query also includes a CashTransactions
    section (Deposits/Withdrawals), those rows are upserted into
    ``xenon.ib_cash_flow`` via :func:`_persist_ib_cash_transactions`. The
    return value still reflects only the NAV entries; cash-flow counts are
    logged.

    Returns list of ``{date, total, cash, stock, options}`` or None on failure.
    """
    token = os.environ.get("IB_FLEX_TOKEN")
    nav_query_id = os.environ.get("IB_FLEX_NAV_QUERY_ID")
    if not token or not nav_query_id:
        return None

    import time
    import xml.etree.ElementTree as ET
    from datetime import date as _date
    from decimal import Decimal as _Decimal
    from urllib.parse import urlencode
    from urllib.request import urlopen

    try:
        # AccountManagement/FlexWebService is the current IB-recommended Flex
        # endpoint. The legacy Universal/servlet/FlexStatementService.*
        # endpoint is XML-only — it returns permanent ErrorCode 1001 for
        # CSV-format saved queries. See [[flex-legacy-endpoint-xml-only]].
        url = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
        params = urlencode({"t": token, "q": nav_query_id, "v": "3"})
        resp = urlopen(f"{url}?{params}", timeout=30)
        text = resp.read().decode("utf-8")
        root = ET.fromstring(text)
        ref_node = root.find(".//ReferenceCode")
        if ref_node is None or not ref_node.text:
            return None
        ref_code = ref_node.text

        stmt_url = "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
        for _ in range(30):
            time.sleep(3)
            params2 = urlencode({"t": token, "q": ref_code, "v": "3"})
            resp2 = urlopen(f"{stmt_url}?{params2}", timeout=30)
            body = resp2.read().decode("utf-8")
            body_stripped = body.lstrip()

            entries: List[Dict[str, Any]] = []
            cash_flow_rows: List[Dict[str, Any]] = []
            if body_stripped.startswith('"ClientAccountID"'):
                # CSV-format saved query (current xenon configuration).
                # IB concatenates sections (EquitySummaryInBase,
                # CashTransactions, ...) — split into per-section line
                # groups so each can be parsed with its own DictReader.
                import csv as _csv
                import io as _io

                # Resolve scope once so we can defensively filter rows whose
                # ClientAccountID doesn't match the env. Guards against an
                # operator misconfiguring the saved Flex query to point at a
                # live account while running in paper mode (which would
                # otherwise persist live TransactionIDs under the paper
                # scope — silent cross-account corruption). When the scope
                # cannot be resolved (e.g., migration scripts running without
                # env vars), fall through to no filtering — same behavior as
                # before the guard was added.
                try:
                    from xenon.execution.account_scope import resolve_from_env as _resolve_scope

                    _expected_account = _resolve_scope().broker_account
                except Exception:  # noqa: BLE001
                    _expected_account = None

                sections = _split_ib_flex_csv_sections(body)
                saw_cash_section = False
                cash_section_missing_cols: set[str] = set()
                for section_lines in sections:
                    section_reader = _csv.DictReader(_io.StringIO("\n".join(section_lines)))
                    field_names = section_reader.fieldnames or []
                    if "ReportDate" in field_names and "Total" in field_names:
                        # EquitySummaryInBase section.
                        for csv_row in section_reader:
                            if (
                                _expected_account is not None
                                and csv_row.get("ClientAccountID", "").strip() != _expected_account
                            ):
                                logger.warning(
                                    "fetch_ib_nav_series: skipping NAV row for "
                                    "ClientAccountID=%r (expected %r) — saved "
                                    "Flex query may be misconfigured",
                                    csv_row.get("ClientAccountID"),
                                    _expected_account,
                                )
                                continue
                            dt_raw = csv_row.get("ReportDate", "").strip()
                            if len(dt_raw) == 8:
                                dt = f"{dt_raw[:4]}-{dt_raw[4:6]}-{dt_raw[6:8]}"
                            else:
                                dt = dt_raw
                            entries.append(
                                {
                                    "date": dt,
                                    "total": safe_float(csv_row.get("Total")),
                                    "cash": safe_float(csv_row.get("Cash")),
                                    "stock": safe_float(csv_row.get("Stock")),
                                    "options": safe_float(csv_row.get("Options")),
                                }
                            )
                    else:
                        missing = _cash_section_missing_columns(field_names)
                        if missing is None:
                            continue  # unrelated section (Transfers, Trades, …)
                        if missing:
                            # CashTransactions-shaped but missing required
                            # columns — the parser can't use it.
                            cash_section_missing_cols |= set(missing)
                            continue
                        # CashTransactions section (Deposits/Withdrawals
                        # subset only — see _parse_ib_cash_transactions_section).
                        saw_cash_section = True
                        cash_flow_rows.extend(
                            _parse_ib_cash_transactions_section(section_lines, account_filter=_expected_account)
                        )

                # Once per run: surface a statement whose CashTransactions
                # section is absent or unparsable. Without this, a saved
                # query that lost the section leaves xenon.ib_cash_flow
                # empty and /performance counts deposits as gains —
                # silent from the outside.
                if not saw_cash_section and not cash_section_missing_cols:
                    logger.warning(
                        "fetch_ib_nav_series: Flex response has no CashTransactions "
                        "section — xenon.ib_cash_flow gets nothing from this run; "
                        "check the saved query includes it"
                    )
                elif cash_section_missing_cols:
                    logger.warning(
                        "fetch_ib_nav_series: CashTransactions section lacks required "
                        "column(s) %s — rows cannot be parsed into xenon.ib_cash_flow; "
                        "check the saved query's column selection",
                        sorted(cash_section_missing_cols),
                    )
            elif "<FlexStatements" in body:
                # XML-format saved query — kept for forward-compat in case
                # a future saved query is configured as XML.
                logger.warning(
                    "fetch_ib_nav_series: XML-format Flex response — CashTransactions "
                    "are only ingested from CSV responses, so xenon.ib_cash_flow "
                    "gets nothing from this run; check the saved query's format"
                )
                root2 = ET.fromstring(body)
                navs = root2.findall(".//EquitySummaryByReportDateInBase")
                if not navs:
                    return None
                for nav in navs:
                    dt_raw = nav.get("reportDate", "")
                    if len(dt_raw) == 8:
                        dt = f"{dt_raw[:4]}-{dt_raw[4:6]}-{dt_raw[6:8]}"
                    else:
                        dt = dt_raw
                    entries.append(
                        {
                            "date": dt,
                            "total": safe_float(nav.get("total")),
                            "cash": safe_float(nav.get("cash")),
                            "stock": safe_float(nav.get("stock")),
                            "options": safe_float(nav.get("options")),
                        }
                    )
            else:
                # Statement still generating (Status=Warn ErrorCode=1019) —
                # poll again. Real failures (1001/1018) also fall here and
                # quietly time out after 90s, matching prior behavior.
                continue

            if not entries:
                return None

            # Persist to PG (replaces data/nav_history_ib.json cache).
            try:
                from xenon.execution.account_scope import resolve_from_env
                from xenon.utils.portfolio_loader import upsert_nav_sync

                scope = resolve_from_env()
                for e in entries:
                    try:
                        day = _date.fromisoformat(e["date"])
                    except (TypeError, ValueError):
                        continue
                    total_v = _Decimal(str(e["total"])) if e.get("total") is not None else None
                    cash_v = _Decimal(str(e["cash"])) if e.get("cash") is not None else None
                    stock_v = _Decimal(str(e["stock"])) if e.get("stock") is not None else None
                    opt_v = _Decimal(str(e["options"])) if e.get("options") is not None else None
                    # nav column mirrors total for IB Flex (single authoritative source).
                    # source='close' — EquitySummaryByReportDateInBase rows are post-close.
                    upsert_nav_sync(
                        scope=scope,
                        day=day,
                        nav=total_v if total_v is not None else _Decimal("0"),
                        total=total_v,
                        cash=cash_v,
                        stock_value=stock_v,
                        options_value=opt_v,
                        source="close",
                    )
            except Exception as exc:  # noqa: BLE001
                # PG unavailable / scope unset — return entries anyway so the
                # caller still gets a one-shot result. Cache miss next run.
                # Surface at WARNING so a silent NAV cache miss doesn't go
                # undiagnosed when the same env succeeds elsewhere.
                logger.warning(
                    "fetch_ib_nav_series PG persist failed (%s) — returning fresh entries; cache will miss next run",
                    exc,
                )

            # Side-effect: persist any CashTransactions rows from this
            # response into xenon.ib_cash_flow. Failures don't block NAV
            # return — flow-adjusted performance is the only consumer and
            # it falls back gracefully when rows are missing.
            if cash_flow_rows:
                try:
                    inserted, updated = _persist_ib_cash_transactions(cash_flow_rows)
                    logger.info(
                        "fetch_ib_nav_series: ib_cash_flow ingest — %d inserted, "
                        "%d updated (replays), %d skipped (of %d total CashTransactions row(s))",
                        inserted,
                        updated,
                        len(cash_flow_rows) - inserted - updated,
                        len(cash_flow_rows),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "fetch_ib_nav_series: ib_cash_flow persist failed (%s) — NAV rows still returned",
                        exc,
                    )
            return entries
        return None
    except Exception:
        return None
