"""Decrypt and (eventually) parse Futu daily-statement PDFs.

Stage 1 (this commit): decryption + raw text/table dump for inspection.
Stage 2 (next commit, once we've seen the structure): typed field extractor.

Why pikepdf for decrypt + pdfplumber for read: pikepdf wraps qpdf which
handles AES-256/V6 reliably (modern PDF spec), while pdfplumber is the
table-aware reader. Doing both in one library (PyMuPDF) is possible but
its AGPL clause complicates redistribution; pikepdf+pdfplumber are MIT/BSD.

Password source: env var FUTU_STATEMENT_PASSWORD by default. Override per
call for tests.
"""

from __future__ import annotations

import io
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

import pdfplumber
import pikepdf

logger = logging.getLogger(__name__)

PASSWORD_ENV = "FUTU_STATEMENT_PASSWORD"


class StatementDecryptError(RuntimeError):
    """PDF refused the password — likely wrong or missing."""


class StatementParseError(RuntimeError):
    """Failed to extract a required field from the decrypted statement text."""


@dataclass
class RawStatement:
    """Unstructured dump of a decrypted PDF — for inspection / parser design."""

    page_count: int
    text_by_page: list[str] = field(default_factory=list)
    tables_by_page: list[list[list[list[Optional[str]]]]] = field(default_factory=list)


@dataclass(frozen=True)
class FutuDailyStatement:
    """Structured fields lifted from a Futu daily statement PDF.

    All currency amounts are Decimal — Numeric(20,4) on the PG side. The
    base-currency columns (*_base) carry HKD values when base_currency=='HKD'
    (the only case seen). The per-currency dicts preserve the breakdown
    the statement prints on pages 2 (starting) and 10 (ending).

    page_text, financing, transaction_totals: best-effort richer extraction.
    None when the section is absent from a particular statement; callers
    should defensively handle missing data rather than failing.
    """

    statement_date: date
    preparation_date: Optional[date]
    account_number: str
    account_suffix: Optional[str]
    client_name: str
    base_currency: str
    starting_portfolio_base: Decimal
    ending_portfolio_base: Decimal
    starting_funds_base: Decimal
    ending_funds_base: Decimal
    starting_cash_base: Decimal
    ending_cash_base: Decimal
    starting_nav_base: Decimal
    ending_nav_base: Decimal
    starting_nav_by_currency: dict[str, Decimal]
    ending_nav_by_currency: dict[str, Decimal]
    exchange_rates: dict[str, Decimal]
    page_text: list[str]
    financing: dict
    transaction_totals: dict


def decrypt(pdf_bytes: bytes, password: Optional[str] = None) -> bytes:
    """Return decrypted PDF bytes. Raises StatementDecryptError on bad password."""
    pwd = password if password is not None else os.environ.get(PASSWORD_ENV)
    if not pwd:
        raise StatementDecryptError(f"no PDF password supplied (env {PASSWORD_ENV} unset)")
    src = io.BytesIO(pdf_bytes)
    try:
        with pikepdf.open(src, password=pwd) as pdf:
            out = io.BytesIO()
            # encryption=False writes a plaintext PDF; without this, pikepdf
            # preserves the source PDF's encryption with the original password.
            pdf.save(out, encryption=False)
            return out.getvalue()
    except pikepdf.PasswordError as exc:
        raise StatementDecryptError("PDF password rejected") from exc


_DOUBLED_WORD = re.compile(r"[A-Za-z]{4,}")
_DOUBLED_NUM = re.compile(r"[\d.,+\-]{6,}")


def _dedupe_doubled_letters(text: str) -> str:
    """Collapse doubled-character spans: `AAccccoouunntt` → `Account`,
    `iinn` → `in`, `11,,880044,,661100..8877` → `1,804,610.87`.

    Some Futu PDF variants (e.g. Universal Account - Securities transition
    Aug-Dec 2024, and 2024-2025 5668 composite statements) render certain
    spans in a heavier font weight that pdfplumber reads as every character
    duplicated. The doubling can apply selectively to alphabetic headers,
    numeric values, or both — on the same line.

    Strategy: collapse any [A-Za-z] run ≥4 chars OR any [\\d.,+\\-] run ≥6
    chars where every 2-char window has matching characters. Length cutoffs
    protect real tokens (4-char tickers like AABB; small numbers like 1100
    that would falsely match pair-equality).
    """

    def fix_alpha(m: re.Match) -> str:
        w = m.group(0)
        if len(w) % 2 != 0:
            return w
        if len(w) == 4 and w.isupper():
            return w  # ticker-like (e.g. AABB)
        pairs = [w[i : i + 2] for i in range(0, len(w), 2)]
        if all(p[0] == p[1] for p in pairs):
            return "".join(p[0] for p in pairs)
        return w

    def fix_num(m: re.Match) -> str:
        w = m.group(0)
        if len(w) % 2 != 0:
            return w
        pairs = [w[i : i + 2] for i in range(0, len(w), 2)]
        if all(p[0] == p[1] for p in pairs):
            return "".join(p[0] for p in pairs)
        return w

    text = _DOUBLED_WORD.sub(fix_alpha, text)
    text = _DOUBLED_NUM.sub(fix_num, text)
    return text


def inspect(pdf_bytes: bytes, password: Optional[str] = None) -> RawStatement:
    """Decrypt and return per-page text + table layouts for inspection."""
    clear = decrypt(pdf_bytes, password=password)
    text_pages: list[str] = []
    table_pages: list[list[list[list[Optional[str]]]]] = []
    with pdfplumber.open(io.BytesIO(clear)) as pdf:
        for page in pdf.pages:
            text_pages.append(_dedupe_doubled_letters(page.extract_text() or ""))
            try:
                tables = page.extract_tables() or []
            except Exception as exc:  # pdfplumber occasionally crashes on layout edge cases
                logger.warning("table extraction failed on a page: %s", exc)
                tables = []
            table_pages.append(tables)
        page_count = len(pdf.pages)
    return RawStatement(
        page_count=page_count,
        text_by_page=text_pages,
        tables_by_page=table_pages,
    )


# ---------------------------------------------------------------------------
# Typed parser
# ---------------------------------------------------------------------------

# Numbers in the statement are comma-grouped with 2-decimal precision and
# may carry a leading +/- (the "Change in HKD" column shows explicit + on
# positive deltas, and Cash Balance often starts with -).
_NUM = r"[-+]?[\d,]+\.\d+"

# Month names exactly as printed (Futu HK uses US English month abbreviations).
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


def _to_decimal(s: str) -> Decimal:
    """Parse a comma-grouped number string into Decimal. Empty/None → 0."""
    if s is None:
        return Decimal("0")
    cleaned = s.strip().replace(",", "").replace("+", "")
    if not cleaned or cleaned == "-":
        return Decimal("0")
    return Decimal(cleaned)


def _parse_statement_date(s: str) -> date:
    """Parse 'May 29,2026' or 'Jun 02,2026' into a date.

    Note Futu prints these without a space after the comma — be tolerant.
    """
    m = re.match(r"\s*([A-Z][a-z]{2})\s+(\d{1,2})\s*,\s*(\d{4})\s*$", s)
    if not m:
        raise StatementParseError(f"unrecognized date format: {s!r}")
    month_name, day, year = m.group(1), int(m.group(2)), int(m.group(3))
    month = _MONTHS.get(month_name)
    if month is None:
        raise StatementParseError(f"unrecognized month: {month_name!r}")
    return date(year, month, day)


def _parse_portfolio_summary_row(text: str, label: str) -> tuple[Decimal, Decimal]:
    """Find 'label <start> <end> <change>' on page 1 and return (start, end).

    The 'Portfolio' / 'Stocks and Stock' header sits on page 1. Two layout
    variants observed:

      * 2026+ statements: 'Stocks and Stock' on the row, 'Options' wrapped
        to the next line. Matcher: 'Stocks and Stock <NUM> <NUM> <NUM>'.
      * 2024-2025 statements + Universal transition: 'Stocks and Stock
        Options' all on one line. Matcher accepts optional trailing word(s)
        between the label and the first number.

    For Funds/Cash Balance/Net Asset Value the label is on the same line
    as the numbers in both layouts.
    """
    # Allow up to two optional words (e.g. "Options" or "Options ") between
    # the label and the first number — covers both layouts without false
    # positives on other rows whose labels are unique.
    pattern = rf"^\s*{re.escape(label)}(?:\s+[A-Za-z]+){{0,2}}\s+({_NUM})\s+({_NUM})\s+{_NUM}\s*$"
    for line in text.splitlines():
        m = re.match(pattern, line)
        if m:
            return _to_decimal(m.group(1)), _to_decimal(m.group(2))
    raise StatementParseError(f"missing portfolio summary row: {label!r}")


_NAV_ANY_COLS = re.compile(rf"^\s*Net Asset Value((?:\s+{_NUM})+)\s*$", re.MULTILINE)
_CCY_HEADER_RE = re.compile(r"Assets in ([A-Z]{3})")


def _parse_currency_nav_row(text: str) -> dict[str, Decimal]:
    """Parse the 'Net Asset Value' row from a Starting/Ending Assets Overview page.

    Format per statement variant:
        Net Asset Value <total_HKD> <HKD> <USD> <CNH> <JPY> <SGD> [<KRW>]

    The 7th column (KRW) was added in newer statements; older statements
    omit it. We discover the currency list from the page's header
    ("Assets in HKD Assets in USD ...") and trim the captured numbers
    accordingly so a 6-column row maps correctly.

    The total-in-HKD column is intentionally NOT in the dict — callers use
    starting_nav_base / ending_nav_base for that.
    """
    currencies = _CCY_HEADER_RE.findall(text)
    if not currencies:
        raise StatementParseError("no 'Assets in <CCY>' header found on this page")

    for m in _NAV_ANY_COLS.finditer(text):
        numbers = re.findall(_NUM, m.group(1))
        # numbers[0] is the total in HKD; numbers[1:] are per-currency cells.
        per_ccy = numbers[1:]
        if len(per_ccy) < len(currencies):
            # Layout drift: header mentions more currencies than the row has
            # cells. Truncate the currency list to match.
            currencies_use = currencies[: len(per_ccy)]
        else:
            currencies_use = currencies
            per_ccy = per_ccy[: len(currencies)]
        return {ccy: _to_decimal(per_ccy[i]) for i, ccy in enumerate(currencies_use)}
    raise StatementParseError("missing 'Net Asset Value' currency breakdown row")


def _parse_exchange_rates(text: str) -> dict[str, Decimal]:
    """Pull every 'CCY/HKD=X.YYYYYY' on the Reference Exchange Rate line."""
    rates: dict[str, Decimal] = {}
    for ccy, value in re.findall(r"(\w+)/HKD=([\d.]+)", text):
        rates[f"{ccy}/HKD"] = _to_decimal(value)
    if not rates:
        raise StatementParseError("no exchange rates found")
    return rates


def _find_starting_overview_page(pages: list[str]) -> Optional[str]:
    """Return the text of the page that contains 'Starting Assets Overview'
    AND a 'Net Asset Value' row with the multi-currency breakdown.

    2024-2026 5668 statements put this on page 2. Universal-transition
    statements (Aug-Dec 2024) put it on the same page as the Ending
    overview (typically page 4). Locate by header so both work.
    """
    nav_pattern = re.compile(
        rf"^\s*Net Asset Value((?:\s+{_NUM}){{6,}})\s*$",
        re.MULTILINE,
    )
    for txt in pages:
        if "Starting Assets Overview" not in txt:
            continue
        if nav_pattern.search(txt):
            return txt
    return None


def _find_ending_overview_page(pages: list[str]) -> Optional[str]:
    """Return the text of the first page that contains 'Ending Assets Overview'
    AND a 'Net Asset Value' row with the 7-column currency breakdown.

    The 'Ending Assets Overview' header repeats across continuation pages
    (positions list spills over). Only the first occurrence carries the
    summary row we want — subsequent pages just list more contracts.
    """
    # NAV row has at least 6 numbers (older statements) and up to 7 (newer + KRW).
    nav_pattern = re.compile(
        rf"^\s*Net Asset Value((?:\s+{_NUM}){{6,}})\s*$",
        re.MULTILINE,
    )
    for txt in pages:
        if "Ending Assets Overview" not in txt:
            continue
        if nav_pattern.search(txt):
            return txt
    return None


def _parse_financing(pages: list[str]) -> dict:
    """Pull Financing Overview + Securities Lending rows from the 'Financing
    Overview' page (typically page 9 of newer statements).

    Returns:
        {
          "margin_interest": [
            {"date": "2026/05/29", "currency": "USD",
             "principal": Decimal, "rate": Decimal,
             "interest": Decimal, "cumulative": Decimal}
          ],
          "securities_lending": [
            {"date": "2026/05/28", "symbol": "LITX", "exchange": "US",
             "currency": "USD", "type": "Allocation",
             "quantity": Decimal, "interest_amount": Decimal,
             "collateral_amount": Decimal, "rate": Decimal,
             "interest": Decimal, "cumulative": Decimal,
             "month": "2026/05"}
          ]
        }
    Missing sections come back as empty lists.
    """
    margin_rows: list[dict] = []
    lending_rows: list[dict] = []
    # Margin: "YYYY/MM/DD CCY <principal> <rate>% <interest> <cumulative>"
    margin_re = re.compile(
        rf"^(\d{{4}}/\d{{2}}/\d{{2}})\s+([A-Z]{{3}})\s+({_NUM})\s+({_NUM})%\s+({_NUM})\s+({_NUM})\s*$"
    )
    # Securities lending: "YYYY/MM/DD <symbol_text> <exchange> <ccy> <type> <qty> <iamt> <camt> <rate>% <int> <cum> <YYYY/MM>"
    # Layout is busy; capture loosely and post-validate.
    lending_re = re.compile(
        rf"^(\d{{4}}/\d{{2}}/\d{{2}})\s+(\S+).*?\s+([A-Z]{{2,3}})\s+([A-Z]{{3}})\s+(\S+)\s+"
        rf"({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})%\s+({_NUM})\s+({_NUM})\s+(\d{{4}}/\d{{2}})\s*$"
    )

    for txt in pages:
        if "Financing Overview" not in txt and "Securities Lending Overview" not in txt:
            continue
        section = None
        for ln in txt.splitlines():
            stripped = ln.strip()
            if stripped.startswith("Financing Overview"):
                section = "margin"
                continue
            if stripped.startswith("Securities Lending Overview"):
                section = "lending"
                continue
            if stripped.startswith("Preparation Date"):
                continue
            if section == "margin":
                m = margin_re.match(stripped)
                if m:
                    margin_rows.append(
                        {
                            "date": m.group(1),
                            "currency": m.group(2),
                            "principal": _to_decimal(m.group(3)),
                            "rate_pct": _to_decimal(m.group(4)),
                            "interest": _to_decimal(m.group(5)),
                            "cumulative": _to_decimal(m.group(6)),
                        }
                    )
            elif section == "lending":
                m = lending_re.match(stripped)
                if m:
                    lending_rows.append(
                        {
                            "date": m.group(1),
                            "symbol": m.group(2),
                            "exchange": m.group(3),
                            "currency": m.group(4),
                            "type": m.group(5),
                            "quantity": _to_decimal(m.group(6)),
                            "interest_amount": _to_decimal(m.group(7)),
                            "collateral_amount": _to_decimal(m.group(8)),
                            "rate_pct": _to_decimal(m.group(9)),
                            "interest": _to_decimal(m.group(10)),
                            "cumulative": _to_decimal(m.group(11)),
                            "month": m.group(12),
                        }
                    )
    return {"margin_interest": margin_rows, "securities_lending": lending_rows}


def _parse_transaction_totals(pages: list[str]) -> dict:
    """Page 8 prints a 'Total <label>: HKD: ... USD: ... CNH: ... etc.' block.

    Parse those into:
        {
          "USD": {"transaction_amount": Decimal, "commission": Decimal,
                  "platform_fees": Decimal, "change_in_amount": Decimal, ...},
          "HKD": {...}, ...
        }
    """
    totals: dict[str, dict[str, Decimal]] = {}
    # Match e.g. "Total Commission: HKD: 0.00 USD: 24.40 CNH: 0.00 JPY: 0.00 SGD: 0.00 KRW: 0.00"
    line_re = re.compile(rf"^Total\s+([A-Za-z][A-Za-z ]+):\s+((?:[A-Z]{{3}}:\s*{_NUM}\s*)+)$")
    cell_re = re.compile(rf"([A-Z]{{3}}):\s*({_NUM})")

    for txt in pages:
        for ln in txt.splitlines():
            m = line_re.match(ln.strip())
            if not m:
                continue
            label = m.group(1).strip().lower().replace(" ", "_")  # "Commission" -> "commission"
            for ccy, value in cell_re.findall(m.group(2)):
                if ccy not in totals:
                    totals[ccy] = {}
                totals[ccy][label] = _to_decimal(value)
    return totals


_SUFFIX_RE = re.compile(r"Margin Universal Account\s*\((\d+)\)")
_ACCOUNT_NUMBER_RE = re.compile(r"Account Number[: ]+(\d+)")
_CLIENT_NAME_RE = re.compile(r"Client Name[: ]+([A-Z][A-Z ]+?)\s+Account Number")
_PREP_DATE_RE = re.compile(r"Preparation Date[: ]+([A-Z][a-z]{2}\s+\d{1,2}\s*,\s*\d{4})")


def parse(pdf_bytes: bytes, password: Optional[str] = None) -> FutuDailyStatement:
    """Decrypt + parse a Futu daily statement PDF.

    Raises StatementDecryptError if the password is wrong, or
    StatementParseError if a required field cannot be extracted.
    """
    raw = inspect(pdf_bytes, password=password)
    if raw.page_count < 5:
        raise StatementParseError(f"statement has only {raw.page_count} pages; expected ≥5")

    page1 = raw.text_by_page[0]
    starting_page = _find_starting_overview_page(raw.text_by_page)
    if starting_page is None:
        # Fallback: 5668 statements always put it on page 2.
        starting_page = raw.text_by_page[1] if len(raw.text_by_page) > 1 else ""
    ending_page = _find_ending_overview_page(raw.text_by_page)
    if ending_page is None:
        raise StatementParseError("could not locate 'Ending Assets Overview' page")

    # statement_date is the second non-empty line of page 1
    lines = [ln.strip() for ln in page1.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise StatementParseError("page 1 too short to contain a statement date")
    statement_date = _parse_statement_date(lines[1])

    # account_suffix from "Margin Universal Account (5668)" — present in
    # current-format subjects but absent in the transition-era cover.
    # Fall back to the trailing 4 digits of the full account number.
    m = _SUFFIX_RE.search(page1)
    account_suffix = m.group(1) if m else None

    # preparation_date + client_name + account_number from the footer line.
    # Transition-era statements may omit the prepared-on line; default to the
    # statement_date in that case (the value still flows into the typed row
    # but loses the "actually generated at" nuance).
    m = _PREP_DATE_RE.search(page1)
    preparation_date = _parse_statement_date(m.group(1)) if m else None

    m = _CLIENT_NAME_RE.search(page1)
    client_name = m.group(1).strip() if m else "UNKNOWN"

    m = _ACCOUNT_NUMBER_RE.search(page1)
    if not m:
        raise StatementParseError("account number not found")
    account_number = m.group(1)
    if account_suffix is None and len(account_number) >= 4:
        # Transition-era cover lacks the "(NNNN)" decoration; derive from
        # the trailing 4 digits of the full account number.
        account_suffix = account_number[-4:]

    # base currency: the "Base Currency" column on the account info row
    # Same-line constraint via [^\S\n] — under the doubled-letter rendering
    # the 'Base Currency' header sits ALONE on its line with the actual code
    # ('HKD') wrapped to the data row below. A `\s+` here would consume the
    # newline and grab the first 3-upper letters of the next line (e.g.
    # 'LLI' from 'LLII CHEN XXII ...'). All known modern statements are HKD.
    m = re.search(r"Base Currency[^\S\n]+([A-Z]{3})", page1)
    base_currency = m.group(1) if m else "HKD"

    # Portfolio summary rows. Funds is absent when the account holds no
    # money-market fund — treat as zero rather than failing.
    try:
        start_funds, end_funds = _parse_portfolio_summary_row(page1, "Funds")
    except StatementParseError:
        start_funds = end_funds = Decimal("0")
    start_cash, end_cash = _parse_portfolio_summary_row(page1, "Cash Balance")
    start_nav, end_nav = _parse_portfolio_summary_row(page1, "Net Asset Value")
    start_port, end_port = _parse_portfolio_summary_row(page1, "Stocks and Stock")

    # Per-currency NAV: from located Starting / Ending pages.
    starting_by_ccy = _parse_currency_nav_row(starting_page)
    ending_by_ccy = _parse_currency_nav_row(ending_page)

    # Reference exchange rates from page 1
    rates = _parse_exchange_rates(page1)

    # Best-effort richer extraction. None of these should fail the parse.
    try:
        financing = _parse_financing(raw.text_by_page)
    except Exception as exc:
        logger.warning("financing parse failed: %s", exc)
        financing = {"margin_interest": [], "securities_lending": []}
    try:
        transaction_totals = _parse_transaction_totals(raw.text_by_page)
    except Exception as exc:
        logger.warning("transaction totals parse failed: %s", exc)
        transaction_totals = {}

    return FutuDailyStatement(
        statement_date=statement_date,
        preparation_date=preparation_date,
        account_number=account_number,
        account_suffix=account_suffix,
        client_name=client_name,
        base_currency=base_currency,
        starting_portfolio_base=start_port,
        ending_portfolio_base=end_port,
        starting_funds_base=start_funds,
        ending_funds_base=end_funds,
        starting_cash_base=start_cash,
        ending_cash_base=end_cash,
        starting_nav_base=start_nav,
        ending_nav_base=end_nav,
        starting_nav_by_currency=starting_by_ccy,
        ending_nav_by_currency=ending_by_ccy,
        exchange_rates=rates,
        page_text=raw.text_by_page,
        financing=financing,
        transaction_totals=transaction_totals,
    )


# ---------------------------------------------------------------------------
# Legacy parser — covers pre-Aug-2024 statements (Universal account didn't
# exist yet). Three pre-consolidation accounts: 6337 US Stocks Margin,
# 6415 HK Stocks Margin, 5270 US Fund. Each was a separate brokerage
# account with its own statement format. Subjects + bodies show in either
# English or Traditional Chinese — same data, different field labels.
#
# Layout: 3-4 pages, single-currency. No "Assets Overview" pages, no
# exchange-rate table. NAV / Portfolio Value / Cash Balance are flat
# fields on page 1; fund movements (cash flow) on page 1; closing fields
# usually on page 2 (English) or still page 1 (Chinese).
# ---------------------------------------------------------------------------

# Currency label → ISO code. The Chinese variants spell currency in the
# native script (港幣 = HKD, 美元 = USD, 人民幣 = CNY/CNH).
_CCY_NAME_TO_ISO = {
    "港幣": "HKD",
    "美元": "USD",
    "人民幣": "CNY",
    "人民币": "CNY",
    "港币": "HKD",
}


def _parse_legacy_date(s: str) -> date:
    """Accept the multiple legacy date formats:
    'Oct 04, 2022' (en-us-stocks)
    '2023/08/29'   (en-us-fund)
    '2020年08月25日' (zh-trad)
    """
    s = s.strip().replace(",", "")
    # YYYY年MM月DD日
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    # YYYY/MM/DD
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    # Mon DD YYYY (after comma strip)
    try:
        return datetime.strptime(s, "%b %d %Y").date()
    except ValueError:
        pass
    raise StatementParseError(f"legacy date format not recognised: {s!r}")


def _detect_legacy_format(page1: str) -> str:
    """Classify a legacy statement by its title line. Returns one of:
    'en-us-stocks', 'en-hk-stocks', 'en-us-fund',
    'zh-hk-margin', 'zh-us-margin', 'zh-us-fund'.
    """
    head = page1.lstrip().splitlines()[0] if page1.strip() else ""
    if "Daily Statement of US Stock" in head:
        return "en-us-stocks"
    if "Daily Statement of HK Stocks" in head:
        return "en-hk-stocks"
    if "Daily Statement of US Fund" in head:
        return "en-us-fund"
    if "港股保證金" in head or "港股保证金" in head:
        return "zh-hk-margin"
    if "美股保證金" in head or "美股保证金" in head or "美股孖展" in head:
        return "zh-us-margin"
    if "美元基金" in head:
        return "zh-us-fund"
    raise StatementParseError(f"legacy format not recognised: head={head[:80]!r}")


def _find_amount(text: str, *patterns: str) -> Optional[Decimal]:
    """Find the first NUM after any of the labels and return it as Decimal.
    Patterns are tried in order; first match wins. Returns None if none match.
    """
    for pat in patterns:
        m = re.search(rf"{pat}\s+({_NUM})", text)
        if m:
            return _to_decimal(m.group(1))
    return None


def _parse_legacy_fund_movements(text: str, fmt: str) -> list[dict]:
    """Extract cash-flow rows under 'Fund Movement' / '資金進出'.
    Each row: Direction Amount Date OrderNumber [Remarks]
    """
    movements: list[dict] = []
    is_chinese = fmt.startswith("zh-")
    # Section regexes intentionally NOT MULTILINE — under MULTILINE `$` in
    # the alternation matches end-of-line, collapsing the non-greedy
    # [\s\S]*? capture to empty. \Z (string-end) is the safe anchor.
    #
    # Row regexes use [^\S\n] for inter-field whitespace so the optional
    # remarks field can't reach across newlines and gobble the next row.
    if is_chinese:
        sect_re = re.compile(r"資金進出([\s\S]*?)(?:融資總覽|盤後概況|盤後證券市值|\Z)")
        row_re = re.compile(
            r"^([一-鿿]+)[^\S\n]+([+\-]?[\d,]+\.\d+)[^\S\n]+(\d{4}/\d{1,2}/\d{1,2})[^\S\n]+(\d+)(?:[^\S\n]+([^\n]+))?",
            re.MULTILINE,
        )
    else:
        sect_re = re.compile(r"Fund Movement([\s\S]*?)(?:Financing|Ending Overview|Ending Portfolio|Maintenance|\Z)")
        row_re = re.compile(
            r"^(In|Out)[^\S\n]+([+\-]?[\d,]+\.\d+)[^\S\n]+(\d{1,2}/\d{1,2}/\d{4}|\d{4}/\d{1,2}/\d{1,2})[^\S\n]+(\d+)(?:[^\S\n]+([^\n]+))?",
            re.MULTILINE,
        )
    sect_m = sect_re.search(text)
    if not sect_m:
        return movements
    for m in row_re.finditer(sect_m.group(1)):
        movements.append(
            {
                "direction": m.group(1),
                "amount": str(_to_decimal(m.group(2))),
                "date": m.group(3),
                "order_number": m.group(4),
                "remarks": (m.group(5) or "").strip() if m.lastindex and m.lastindex >= 5 else "",
            }
        )
    return movements


def parse_legacy(pdf_bytes: bytes, password: Optional[str] = None) -> "FutuDailyStatement":
    """Parse a legacy (pre-Aug-2024) Futu daily statement.

    Returns a FutuDailyStatement just like the modern parser — the legacy
    rows fit the same table with the trade-off that:
      * starting_nav_by_currency / ending_nav_by_currency hold a single
        currency entry (e.g. {"USD": <amount>}).
      * exchange_rates is empty (single-currency statements need none).
      * financing / transaction_totals capture fund movements + financing
        overview when present.
    """
    raw = inspect(pdf_bytes, password=password)
    if raw.page_count < 1:
        raise StatementParseError("statement has 0 pages")
    page1 = raw.text_by_page[0]
    full = "\n".join(raw.text_by_page)
    fmt = _detect_legacy_format(page1)
    is_chinese = fmt.startswith("zh-")

    # Date (line 2 of page 1 in every legacy variant).
    lines = [ln.strip() for ln in page1.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise StatementParseError("page 1 too short for date")
    statement_date = _parse_legacy_date(lines[1])

    # Account number — appears as "Account Number 100..." (en) or
    # "賬戶號碼 100..." (zh). 16 digits.
    m = re.search(r"(?:Account Number|賬戶號碼|账户号码)[:：\s]+(\d{14,})", page1)
    if not m:
        raise StatementParseError("legacy account number not found")
    account_number = m.group(1)
    account_suffix = account_number[-4:]

    # Client name — first non-empty line after the title that is letters or
    # CJK only (no digits, no punctuation).
    client_name = lines[2] if len(lines) > 2 else "UNKNOWN"

    # Base currency.
    if is_chinese:
        m = re.search(r"賬戶幣種[ :]+([港人美]\S{1,2})", page1)
        base_currency = _CCY_NAME_TO_ISO.get(m.group(1), "HKD") if m else "HKD"
    else:
        m = re.search(r"Currency[: ]+([A-Z]{3})", page1)
        base_currency = m.group(1) if m else "USD"

    # NAV / Portfolio / Cash — labels differ by language.
    if is_chinese:
        # Starting values on page 1 (raw labels) and Ending values appear
        # under 盤後 prefix when section is present.
        start_nav = _find_amount(full, "資產淨值") or Decimal("0")
        start_port = _find_amount(full, "證券市值") or Decimal("0")
        start_cash = _find_amount(full, "現金結餘") or Decimal("0")
        end_nav = _find_amount(full, "盤後資產淨值[:：]", "盤後資產總淨值[:：]") or start_nav
        end_port = _find_amount(full, "盤後證券市值[:：]") or start_port
        end_cash = _find_amount(full, "盤後現金結餘[:：]") or start_cash
    else:
        # English legacy uses "Portfolio Value", "Cash Balance", "NAV" for
        # ending values (on page 1) and "Starting <X>" / "Ending <X>:" for
        # explicit period markers. US Fund uses "Beginning" / "Ending Assets".
        start_nav = _find_amount(full, "Starting NAV", r"Beginning Assets[:：]") or Decimal("0")
        start_port = _find_amount(full, "Starting Portfolio Value", r"Beginning Fund Value[:：]") or Decimal("0")
        start_cash = _find_amount(full, "Starting Cash Balance", r"Beginning Cash Balance[:：]") or Decimal("0")
        end_nav = (
            _find_amount(full, "Ending NAV[:：]", "Ending Assets[:：]") or _find_amount(page1, r"^NAV\b") or start_nav
        )
        end_port = (
            _find_amount(full, "Ending Portfolio Value[:：]", "Ending Fund Value[:：]")
            or _find_amount(page1, r"^Portfolio Value\b")
            or start_port
        )
        end_cash = (
            _find_amount(full, "Ending Cash Balance[:：]") or _find_amount(page1, r"^Cash Balance\b") or start_cash
        )

    # Cash flows under "Fund Movement" / "資金進出".
    fund_movements = _parse_legacy_fund_movements(full, fmt)

    return FutuDailyStatement(
        statement_date=statement_date,
        preparation_date=None,
        account_number=account_number,
        account_suffix=account_suffix,
        client_name=client_name,
        base_currency=base_currency,
        starting_portfolio_base=start_port,
        ending_portfolio_base=end_port,
        starting_funds_base=Decimal("0"),
        ending_funds_base=Decimal("0"),
        starting_cash_base=start_cash,
        ending_cash_base=end_cash,
        starting_nav_base=start_nav,
        ending_nav_base=end_nav,
        starting_nav_by_currency={base_currency: start_nav},
        ending_nav_by_currency={base_currency: end_nav},
        exchange_rates={},
        page_text=raw.text_by_page,
        financing={"format": fmt, "fund_movements": fund_movements},
        transaction_totals={},
    )


def parse_any(pdf_bytes: bytes, password: Optional[str] = None) -> "FutuDailyStatement":
    """Auto-detect modern vs legacy and dispatch.

    Modern (Universal / 5668) layouts get the 6+ page parser with the
    multi-currency NAV table. Legacy (US Stocks / HK Stocks / US Fund,
    English or Traditional Chinese) get the compact 3-4 page parser.
    """
    raw = inspect(pdf_bytes, password=password)
    page1 = raw.text_by_page[0] if raw.text_by_page else ""
    head = page1.lstrip().splitlines()[0] if page1.strip() else ""
    is_legacy = (
        "US Stock" in head
        or "HK Stock" in head
        or "US Fund" in head
        or "港股" in head
        or "美股" in head
        or "美元基金" in head
    )
    if is_legacy:
        return parse_legacy(pdf_bytes, password=password)
    return parse(pdf_bytes, password=password)
