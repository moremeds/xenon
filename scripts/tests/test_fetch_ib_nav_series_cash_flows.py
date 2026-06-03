"""fetch_ib_nav_series ingests CashTransactions section (Task 37).

When the saved Flex query includes both EquitySummaryInBase AND
CashTransactions sections (the current xenon configuration on
1529248), ``fetch_ib_nav_series`` must:

* Parse the NAV section as before — return value contract unchanged.
* Detect the second section header and parse Deposits/Withdrawals rows.
* Upsert each into ``xenon.ib_cash_flow`` with USD-equivalent amounts.
* Skip non-deposit/withdrawal rows (dividends, fees) and unsupported
  currencies, without failing the NAV ingest.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import text

from xenon.db.engine import get_sync_engine

# Multi-section CSV — NAV (2 rows) + CashTransactions (Deposits/Withdrawals: HKD + USD).
# Includes a Dividend row to verify the type filter (must be skipped — only
# Deposits/Withdrawals go into ib_cash_flow).
_MULTI_SECTION_CSV = (
    # Section 1 header (EquitySummaryInBase)
    '"ClientAccountID","ReportDate","Total","TotalLong","TotalShort","Cash",'
    '"CashLong","CashShort","Stock","StockLong","StockShort","Options",'
    '"OptionsLong","OptionsShort","Bonds","BondsLong","BondsShort",'
    '"Commodities","CommoditiesLong","CommoditiesShort","Funds","FundsLong",'
    '"FundsShort","DividendAccruals","DividendAccrualsLong",'
    '"DividendAccrualsShort","InterestAccruals","InterestAccrualsLong",'
    '"InterestAccrualsShort"\n'
    '"DUQ999999","20260601","100","100","0","50","50","0","40","40","0",'
    '"10","10","0","0","0","0","0","0","0","0","0","0","0","0","0","0",'
    '"0","0"\n'
    '"DUQ999999","20260602","135000","135000","0","85000","85000","0","45","45","0",'
    '"10","10","0","0","0","0","0","0","0","0","0","0","0","0","0","0",'
    '"0","0"\n'
    # Section 2 header (CashTransactions)
    '"ClientAccountID","Date/Time","Type","Description","Amount",'
    '"CurrencyPrimary","Symbol","AssetClass","TransactionID"\n'
    '"DUQ999999","20251026;225011","Deposits/Withdrawals",'
    '"CASH RECEIPTS / ELECTRONIC FUND TRANSFERS","10000","HKD","","",'
    '"4179119714"\n'
    '"DUQ999999","20260107","Deposits/Withdrawals",'
    '"CASH RECEIPTS / ELECTRONIC FUND TRANSFERS","35000","USD","","",'
    '"4326167414"\n'
    # Non-deposit row (dividend) — must NOT be persisted to ib_cash_flow
    '"DUQ999999","20260201","Dividends","SPY DIVIDEND","42.50","USD","SPY","STK",'
    '"4400000001"\n'
)


def _fake_urlopen_factory(body: str):
    def fake_urlopen(url, timeout=30):
        class _R:
            def __init__(self, b: str) -> None:
                self._body = b

            def read(self) -> bytes:
                return self._body.encode()

        if "SendRequest" in url:
            return _R(
                '<?xml version="1.0"?><FlexStatementResponse>'
                "<Status>Success</Status><ReferenceCode>REF789</ReferenceCode>"
                "</FlexStatementResponse>"
            )
        return _R(body)

    return fake_urlopen


def test_fetch_ib_nav_series_ingests_cash_transactions(monkeypatch, pg_test_engine):
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1529248")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_PAPER_ACCOUNT", "DUQ999999")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DUQ999999")

    with patch("urllib.request.urlopen", _fake_urlopen_factory(_MULTI_SECTION_CSV)), patch("time.sleep"):
        from xenon.reports.portfolio_performance import fetch_ib_nav_series

        entries = fetch_ib_nav_series()

    # NAV return value contract: 2 entries, no third row from section break.
    assert entries is not None
    assert len(entries) == 2, f"expected 2 NAV entries, got {len(entries)}"

    engine = get_sync_engine()
    with engine.begin() as conn:
        cash_rows = conn.execute(
            text(
                "SELECT transaction_id, txn_type, currency, amount_native, "
                "amount_usd, fx_rate "
                "FROM xenon.ib_cash_flow "
                "WHERE broker='IB' AND account_env='paper' "
                "AND broker_account='DUQ999999' "
                "ORDER BY transaction_id"
            )
        ).fetchall()

    # Two Deposits/Withdrawals rows persisted; the Dividend row filtered out.
    assert len(cash_rows) == 2, [dict(r._mapping) for r in cash_rows]

    by_id = {r.transaction_id: r for r in cash_rows}
    assert "4179119714" in by_id
    assert "4326167414" in by_id
    assert "4400000001" not in by_id, "Dividend row must not enter ib_cash_flow"

    # HKD deposit — 10000 HKD * 0.128205 = 1282.05
    hkd = by_id["4179119714"]
    assert hkd.currency == "HKD"
    assert hkd.amount_native == Decimal("10000.0000")
    assert hkd.amount_usd == Decimal("1282.0500")
    assert hkd.fx_rate == Decimal("0.128205")

    # USD deposit — passthrough
    usd = by_id["4326167414"]
    assert usd.currency == "USD"
    assert usd.amount_native == Decimal("35000.0000")
    assert usd.amount_usd == Decimal("35000.0000")
    assert usd.fx_rate == Decimal("1.000000")


def test_fetch_ib_nav_series_no_cash_section_still_works(monkeypatch, pg_test_engine):
    """Saved queries without a CashTransactions section must keep working —
    NAV-only response is the historical baseline.
    """
    nav_only = (
        '"ClientAccountID","ReportDate","Total","TotalLong","TotalShort","Cash",'
        '"CashLong","CashShort","Stock","StockLong","StockShort","Options",'
        '"OptionsLong","OptionsShort","Bonds","BondsLong","BondsShort",'
        '"Commodities","CommoditiesLong","CommoditiesShort","Funds","FundsLong",'
        '"FundsShort","DividendAccruals","DividendAccrualsLong",'
        '"DividendAccrualsShort","InterestAccruals","InterestAccrualsLong",'
        '"InterestAccrualsShort"\n'
        '"DUQ999999","20260601","100","100","0","50","50","0","40","40","0",'
        '"10","10","0","0","0","0","0","0","0","0","0","0","0","0","0","0",'
        '"0","0"\n'
    )

    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1529248")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_PAPER_ACCOUNT", "DUQ999999")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DUQ999999")

    with patch("urllib.request.urlopen", _fake_urlopen_factory(nav_only)), patch("time.sleep"):
        from xenon.reports.portfolio_performance import fetch_ib_nav_series

        entries = fetch_ib_nav_series()

    assert entries is not None and len(entries) == 1

    # Cash-flow table empty for this scope — no section means nothing to ingest.
    engine = get_sync_engine()
    with engine.begin() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM xenon.ib_cash_flow "
                "WHERE broker='IB' AND account_env='paper' "
                "AND broker_account='DUQ999999'"
            )
        ).scalar()
    assert count == 0


def test_split_ib_flex_csv_sections_handles_unquoted_headers():
    """The splitter must match both quoted and unquoted ClientAccountID headers."""
    from xenon.reports.portfolio_performance import _split_ib_flex_csv_sections

    body = (
        "ClientAccountID,ReportDate,Total\n"
        "ACCT,20260101,100\n"
        '"ClientAccountID","Date/Time","Type"\n'
        '"ACCT","20260102","X"\n'
    )
    sections = _split_ib_flex_csv_sections(body)
    assert len(sections) == 2
    assert "ReportDate" in sections[0][0]
    assert "Date/Time" in sections[1][0]


def test_parse_cash_transactions_filters_non_deposit_rows():
    """Only Deposits/Withdrawals rows should round-trip; Dividends/Fees/etc must be filtered."""
    from xenon.reports.portfolio_performance import _parse_ib_cash_transactions_section

    section = [
        '"ClientAccountID","Date/Time","Type","Description","Amount","CurrencyPrimary","Symbol","AssetClass","TransactionID"',
        '"A","20260107","Deposits/Withdrawals","WIRE IN","100","USD","","","T1"',
        '"A","20260108","Dividends","SPY DIV","5","USD","SPY","STK","T2"',
        '"A","20260109","Withholding Tax","TAX","-1","USD","","","T3"',
        '"A","20260110","Deposits/Withdrawals","WIRE OUT","-50","USD","","","T4"',
    ]
    rows = _parse_ib_cash_transactions_section(section)
    assert len(rows) == 2
    assert {r["transaction_id"] for r in rows} == {"T1", "T4"}
