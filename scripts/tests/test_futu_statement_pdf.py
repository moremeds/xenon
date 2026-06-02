"""Unit tests for xenon.clients.futu_statement_pdf.

Three tiers:
  * Tier 1 — pure-Python helpers (date parsing, number parsing, regex
    extraction from a text fixture). No PDF involved.
  * Tier 2 — full parse against a real fixture PDF, gated on env
    FUTU_STATEMENT_FIXTURE=/path/to/decrypted.pdf. Skipped when absent
    so CI doesn't depend on a private artifact.
  * Tier 3 — round-trip decrypt of a real encrypted PDF + parse.
    Gated on env FUTU_STATEMENT_ENCRYPTED_FIXTURE + FUTU_STATEMENT_PASSWORD.

We never check the operator's real statement into the repo.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from xenon.clients.futu_statement_pdf import (
    FutuDailyStatement,
    StatementDecryptError,
    StatementParseError,
    _parse_currency_nav_row,
    _parse_exchange_rates,
    _parse_portfolio_summary_row,
    _parse_statement_date,
    _to_decimal,
    decrypt,
    inspect,
    parse,
)

# ---------- Tier 1: pure helpers ----------


def test_to_decimal_basic():
    assert _to_decimal("1,234.56") == Decimal("1234.56")
    assert _to_decimal("-420,718.13") == Decimal("-420718.13")
    assert _to_decimal("+8,091.06") == Decimal("8091.06")


def test_to_decimal_empty():
    assert _to_decimal("") == Decimal("0")
    assert _to_decimal(None) == Decimal("0")
    assert _to_decimal("-") == Decimal("0")


def test_parse_statement_date():
    assert _parse_statement_date("May 29,2026") == date(2026, 5, 29)
    assert _parse_statement_date("Jun 02,2026") == date(2026, 6, 2)


def test_parse_statement_date_bad():
    with pytest.raises(StatementParseError):
        _parse_statement_date("not-a-date")


def test_parse_portfolio_summary_row_signed():
    text = """
Starting Amount in HKD Ending Amount in HKD Change in HKD
Portfolio
Stocks and Stock 2,162,394.77 2,148,590.59 -13,804.18
Options
Funds 24,789.94 24,791.22 +1.28
Cash Balance -420,718.13 -412,627.07 +8,091.06
Net Asset Value 1,766,466.58 1,760,754.74 -5,711.84
""".strip()
    assert _parse_portfolio_summary_row(text, "Funds") == (
        Decimal("24789.94"),
        Decimal("24791.22"),
    )
    assert _parse_portfolio_summary_row(text, "Cash Balance") == (
        Decimal("-420718.13"),
        Decimal("-412627.07"),
    )
    assert _parse_portfolio_summary_row(text, "Net Asset Value") == (
        Decimal("1766466.58"),
        Decimal("1760754.74"),
    )
    assert _parse_portfolio_summary_row(text, "Stocks and Stock") == (
        Decimal("2162394.77"),
        Decimal("2148590.59"),
    )


def test_parse_portfolio_summary_row_missing():
    with pytest.raises(StatementParseError):
        _parse_portfolio_summary_row("nothing here", "Funds")


def test_parse_currency_nav_row():
    text = (
        "Starting Net Asset Value Summary Total in HKD Assets in HKD Assets in USD Assets in CNH Assets in JPY Assets in SGD Assets in KRW\n"
        "Portfolio\n"
        "Stocks and Stock 2,162,394.77 0.00 276,035.89 0.00 0.00 0.00 0.00\n"
        "Net Asset Value 1,766,466.58 25,264.93 222,269.38 0.00 0.00 0.00 0.00\n"
    )
    result = _parse_currency_nav_row(text)
    assert result == {
        "HKD": Decimal("25264.93"),
        "USD": Decimal("222269.38"),
        "CNH": Decimal("0.00"),
        "JPY": Decimal("0.00"),
        "SGD": Decimal("0.00"),
        "KRW": Decimal("0.00"),
    }


def test_parse_exchange_rates():
    text = (
        "Reference Exchange Rate: USD/HKD=7.836615 CNH/HKD=1.158600 JPY/HKD=0.049203 SGD/HKD=6.139000 KRW/HKD=0.005198"
    )
    rates = _parse_exchange_rates(text)
    assert rates["USD/HKD"] == Decimal("7.836615")
    assert rates["CNH/HKD"] == Decimal("1.158600")
    assert rates["JPY/HKD"] == Decimal("0.049203")
    assert rates["SGD/HKD"] == Decimal("6.139000")
    assert rates["KRW/HKD"] == Decimal("0.005198")


def test_parse_exchange_rates_empty():
    with pytest.raises(StatementParseError):
        _parse_exchange_rates("nothing to parse")


# ---------- Tier 2 / 3: real PDF fixture ----------


@pytest.fixture
def decrypted_fixture() -> bytes:
    path = os.environ.get("FUTU_STATEMENT_FIXTURE")
    if not path or not Path(path).exists():
        pytest.skip("FUTU_STATEMENT_FIXTURE not set — provide a decrypted Futu daily-statement PDF")
    return Path(path).read_bytes()


@pytest.fixture
def encrypted_fixture_with_password() -> tuple[bytes, str]:
    path = os.environ.get("FUTU_STATEMENT_ENCRYPTED_FIXTURE")
    pwd = os.environ.get("FUTU_STATEMENT_PASSWORD")
    if not path or not Path(path).exists() or not pwd:
        pytest.skip("FUTU_STATEMENT_ENCRYPTED_FIXTURE + FUTU_STATEMENT_PASSWORD must be set")
    return Path(path).read_bytes(), pwd


def test_parse_real_statement(decrypted_fixture: bytes):
    stmt = parse(decrypted_fixture)
    assert isinstance(stmt, FutuDailyStatement)
    assert stmt.base_currency == "HKD"
    assert stmt.starting_nav_base > 0
    assert stmt.ending_nav_base > 0
    # Cross-check: per-currency NAV summed via rates should match base NAV
    hkd_total = sum(
        amt * (stmt.exchange_rates.get(f"{ccy}/HKD") or (Decimal("1") if ccy == "HKD" else Decimal("0")))
        for ccy, amt in stmt.ending_nav_by_currency.items()
    )
    # statements round at 0.01 HKD per cell; allow 1 HKD aggregate drift
    assert abs(hkd_total - stmt.ending_nav_base) < Decimal("1.0")


def test_decrypt_wrong_password(encrypted_fixture_with_password):
    enc_bytes, _ = encrypted_fixture_with_password
    with pytest.raises(StatementDecryptError):
        decrypt(enc_bytes, password="wrong-password")


def test_decrypt_correct_password(encrypted_fixture_with_password):
    enc_bytes, pwd = encrypted_fixture_with_password
    out = decrypt(enc_bytes, password=pwd)
    assert isinstance(out, bytes)
    assert out.startswith(b"%PDF-")
