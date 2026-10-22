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
        from xenon.jobs.ib_nav_flex import fetch_ib_nav_series

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
        from xenon.jobs.ib_nav_flex import fetch_ib_nav_series

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


def test_fetch_ib_nav_series_warns_when_no_cash_section(monkeypatch, pg_test_engine, caplog):
    """A CSV statement with no CashTransactions section must log a WARNING.

    Otherwise a saved query that silently lost the section (or never had
    it) leaves ``xenon.ib_cash_flow`` empty and /performance counts
    deposits as gains — undiagnosable from the outside (ib_cash_flow
    investigation, Task 2).
    """
    import logging

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

    with patch("urllib.request.urlopen", _fake_urlopen_factory(nav_only)), patch("time.sleep"), caplog.at_level(
        logging.WARNING, logger="xenon.jobs.ib_nav_flex"
    ):
        from xenon.jobs.ib_nav_flex import fetch_ib_nav_series

        entries = fetch_ib_nav_series()

    assert entries is not None and len(entries) == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("CashTransactions" in m for m in warnings), warnings


def test_fetch_ib_nav_series_warns_when_cash_section_missing_columns(monkeypatch, pg_test_engine, caplog):
    """A CashTransactions section (TransactionID + Amount selected) that is
    missing a required column must warn — the parser drops its rows."""
    import logging

    nav_plus_broken_cash = (
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
        # CashTransactions (TransactionID + Amount selected) but no
        # Date/Time column.
        '"ClientAccountID","Type","Description","Amount",'
        '"CurrencyPrimary","Symbol","AssetClass","TransactionID"\n'
        '"DUQ999999","Deposits/Withdrawals",'
        '"CASH RECEIPTS","10000","HKD","","","4179119714"\n'
    )

    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1529248")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_PAPER_ACCOUNT", "DUQ999999")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DUQ999999")

    with patch("urllib.request.urlopen", _fake_urlopen_factory(nav_plus_broken_cash)), patch("time.sleep"), caplog.at_level(
        logging.WARNING, logger="xenon.jobs.ib_nav_flex"
    ):
        from xenon.jobs.ib_nav_flex import fetch_ib_nav_series

        entries = fetch_ib_nav_series()

    assert entries is not None and len(entries) == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("CashTransactions" in m and "Date/Time" in m for m in warnings), warnings


def test_fetch_ib_nav_series_warns_when_cash_section_missing_amount(monkeypatch, pg_test_engine, caplog):
    """An export with Amount deselected is NOT a CashTransactions section
    under the identity contract — the run treats it as absent and emits
    the "no CashTransactions section" warning. NAV rows still return."""
    import logging

    nav_plus_no_amount = (
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
        # TransactionID present but Amount deselected → not a cash section.
        '"ClientAccountID","Date/Time","Type","Description",'
        '"CurrencyPrimary","Symbol","AssetClass","TransactionID"\n'
        '"DUQ999999","20251026;225011","Deposits/Withdrawals",'
        '"CASH RECEIPTS","USD","","","4179119714"\n'
    )

    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1529248")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_PAPER_ACCOUNT", "DUQ999999")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DUQ999999")

    with patch("urllib.request.urlopen", _fake_urlopen_factory(nav_plus_no_amount)), patch("time.sleep"), caplog.at_level(
        logging.WARNING, logger="xenon.jobs.ib_nav_flex"
    ):
        from xenon.jobs.ib_nav_flex import fetch_ib_nav_series

        entries = fetch_ib_nav_series()

    assert entries is not None and len(entries) == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("has no CashTransactions section" in m for m in warnings), warnings


def test_fetch_ib_nav_series_transfers_section_does_not_warn(monkeypatch, pg_test_engine, caplog):
    """A Transfers section — with TransactionID but no Amount — is not
    cash-shaped: with a valid CashTransactions section alongside it, the
    run emits no CashTransactions warning at all."""
    import logging

    nav_cash_transfers = (
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
        # Transfers — TransactionID but no Amount column.
        '"ClientAccountID","Date/Time","Type","Direction",'
        '"TransferCompany","CurrencyPrimary","TransactionID"\n'
        '"DUQ999999","20260301","ACATS","IN","CONTRA BROKER","USD","555001"\n'
        '"ClientAccountID","Date/Time","Type","Description","Amount",'
        '"CurrencyPrimary","Symbol","AssetClass","TransactionID"\n'
        '"DUQ999999","20260107","Deposits/Withdrawals",'
        '"CASH RECEIPTS","35000","USD","","","4326167414"\n'
    )

    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1529248")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_PAPER_ACCOUNT", "DUQ999999")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DUQ999999")

    with patch("urllib.request.urlopen", _fake_urlopen_factory(nav_cash_transfers)), patch("time.sleep"), caplog.at_level(
        logging.WARNING, logger="xenon.jobs.ib_nav_flex"
    ):
        from xenon.jobs.ib_nav_flex import fetch_ib_nav_series

        entries = fetch_ib_nav_series()

    assert entries is not None and len(entries) == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("CashTransactions" in m for m in warnings), warnings


def test_parse_cash_transactions_rejects_blank_amount():
    """A row with a missing/blank Amount is skipped — never emitted as '0'
    (a '0' upsert would overwrite a stored amount on transaction_id replay)."""
    from xenon.jobs.ib_nav_flex import _parse_ib_cash_transactions_section

    section = [
        '"ClientAccountID","Date/Time","Type","Description","Amount","CurrencyPrimary","Symbol","AssetClass","TransactionID"',
        '"A","20260107","Deposits/Withdrawals","WIRE IN","","USD","","","T1"',
        '"A","20260108","Deposits/Withdrawals","WIRE IN","100","USD","","","T2"',
    ]
    rows = _parse_ib_cash_transactions_section(section)
    assert [r["transaction_id"] for r in rows] == ["T2"]
    assert rows[0]["amount_native"] == "100"


def test_parse_cash_transactions_rejects_nonfinite_amount():
    """NaN / sNaN / Infinity / non-numeric Amounts are skipped — a stored
    flow must never be overwritten by a non-finite replay (Astra R2 P2).
    Finite 0 and negative withdrawals stay valid."""
    from xenon.jobs.ib_nav_flex import _parse_ib_cash_transactions_section

    section = [
        '"ClientAccountID","Date/Time","Type","Description","Amount","CurrencyPrimary","Symbol","AssetClass","TransactionID"',
        '"A","20260107","Deposits/Withdrawals","W1","NaN","USD","","","T1"',
        '"A","20260107","Deposits/Withdrawals","W2","sNaN","USD","","","T2"',
        '"A","20260107","Deposits/Withdrawals","W3","Infinity","USD","","","T3"',
        '"A","20260107","Deposits/Withdrawals","W4","abc","USD","","","T4"',
        '"A","20260108","Deposits/Withdrawals","IN","0","USD","","","T5"',
        '"A","20260109","Deposits/Withdrawals","OUT","-500","USD","","","T6"',
    ]
    rows = _parse_ib_cash_transactions_section(section)
    assert [r["transaction_id"] for r in rows] == ["T5", "T6"]
    assert [r["amount_native"] for r in rows] == ["0", "-500"]


def test_fetch_ib_nav_series_warns_on_xml_response(monkeypatch, pg_test_engine, caplog):
    """XML-format saved queries never ingest CashTransactions — the XML
    branch has no cash parser, so it must warn that ib_cash_flow gets
    nothing from the run."""
    import logging

    xml_body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<FlexStatements count="1"><FlexStatement>'
        '<EquitySummaryByReportDateInBase accountId="DUQ999999" '
        'reportDate="20260601" total="100" cash="50" stock="40" options="10"/>'
        "</FlexStatement></FlexStatements>"
    )

    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1529248")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_PAPER_ACCOUNT", "DUQ999999")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DUQ999999")

    with patch("urllib.request.urlopen", _fake_urlopen_factory(xml_body)), patch("time.sleep"), caplog.at_level(
        logging.WARNING, logger="xenon.jobs.ib_nav_flex"
    ):
        from xenon.jobs.ib_nav_flex import fetch_ib_nav_series

        entries = fetch_ib_nav_series()

    assert entries is not None and len(entries) == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("CashTransactions" in m for m in warnings), warnings


def test_split_ib_flex_csv_sections_handles_unquoted_headers():
    """The splitter must match both quoted and unquoted ClientAccountID headers."""
    from xenon.jobs.ib_nav_flex import _split_ib_flex_csv_sections

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
    from xenon.jobs.ib_nav_flex import _parse_ib_cash_transactions_section

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


def test_parse_cash_transactions_account_filter_rejects_cross_scope():
    """account_filter must reject rows whose ClientAccountID doesn't match.

    Protects against the operator misconfiguring the saved Flex query to
    point at a different account than the env (e.g., paper mode but query
    pointing at live U... account) — those rows would otherwise persist
    with the wrong scope. account_env mismatch is a silent corruption
    risk that broker_account scope columns exist specifically to prevent.
    """
    from xenon.jobs.ib_nav_flex import _parse_ib_cash_transactions_section

    section = [
        '"ClientAccountID","Date/Time","Type","Description","Amount","CurrencyPrimary","Symbol","AssetClass","TransactionID"',
        '"U18007831","20260107","Deposits/Withdrawals","WIRE IN","100","USD","","","T_LIVE"',
        '"DUQ378889","20260108","Deposits/Withdrawals","WIRE IN","200","USD","","","T_PAPER"',
    ]
    # Operator running paper mode — saved query mistakenly returns live rows.
    rows = _parse_ib_cash_transactions_section(section, account_filter="DUQ378889")
    assert len(rows) == 1
    assert rows[0]["transaction_id"] == "T_PAPER"


def test_fetch_ib_nav_series_skips_cross_account_rows(monkeypatch, pg_test_engine):
    """Integration: fetch_ib_nav_series must filter NAV + CashTransactions
    rows by the env-resolved broker_account when the saved Flex query
    contains rows from another account."""
    cross_account_csv = (
        # Section 1: NAV (1 row matching env, 1 row from a different acct)
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
        '"U18007831","20260601","999999","999999","0","0","0","0","0","0","0",'
        '"0","0","0","0","0","0","0","0","0","0","0","0","0","0","0","0",'
        '"0","0"\n'
        # Section 2: CashTransactions (matching + non-matching)
        '"ClientAccountID","Date/Time","Type","Description","Amount",'
        '"CurrencyPrimary","Symbol","AssetClass","TransactionID"\n'
        '"DUQ999999","20260107","Deposits/Withdrawals","WIRE IN","100","USD","","","T_OK"\n'
        '"U18007831","20260108","Deposits/Withdrawals","WIRE OTHER","500","USD","","","T_OTHER"\n'
    )

    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1529248")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_PAPER_ACCOUNT", "DUQ999999")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DUQ999999")

    with patch("urllib.request.urlopen", _fake_urlopen_factory(cross_account_csv)), patch("time.sleep"):
        from xenon.jobs.ib_nav_flex import fetch_ib_nav_series

        entries = fetch_ib_nav_series()

    # Only the DUQ999999 NAV row, not the U18007831 row
    assert entries is not None and len(entries) == 1
    assert entries[0]["total"] == 100.0

    # Cash flow: only T_OK persisted, T_OTHER rejected
    engine = get_sync_engine()
    with engine.begin() as conn:
        txn_ids = [
            r.transaction_id
            for r in conn.execute(
                text(
                    "SELECT transaction_id FROM xenon.ib_cash_flow "
                    "WHERE broker='IB' AND account_env='paper' "
                    "AND broker_account='DUQ999999'"
                )
            ).fetchall()
        ]
    assert txn_ids == ["T_OK"]
    # T_OTHER must not appear ANYWHERE under the paper scope
    with engine.begin() as conn:
        bad = conn.execute(text("SELECT COUNT(*) FROM xenon.ib_cash_flow WHERE transaction_id='T_OTHER'")).scalar()
    assert bad == 0
