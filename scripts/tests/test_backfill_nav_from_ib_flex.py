"""Backfill script ``_2026_06_02_backfill_nav_from_ib_flex.py`` regression tests.

The script is the only inception-to-date path: the daily
``xenon-nav-flex-refresh`` Flex query is a rolling ~2-week window, so
historical NAV and CashTransactions rows can only enter through a manual
CSV export + this script. Locks in:

* ``--from-csv`` writes NAV rows via ``upsert_nav_sync`` with
  ``source='close'`` (the post-``2026_06_03_nav_src_pk`` 5-column PK) and
  Deposits/Withdrawals rows into ``xenon.ib_cash_flow``; re-runs are
  idempotent.
* ``--dry-run`` parses and reports counts with zero DB writes.
* No-flag invocation prints usage and exits 2 (the old Flex-API default
  path duplicated ``xenon-nav-flex-refresh`` and was removed).
* Writes refuse under ``XENON_READ_ONLY=1`` (exit 3), mirroring
  ``xenon-nav-flex-refresh``.

All fixture values are synthetic (paper-style ``DUQ999999`` account).
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from scripts.migrations._2026_06_02_backfill_nav_from_ib_flex import ingest_nav_csv, main
from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope

_SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DUQ999999")

# NAV section (2 rows) + CashTransactions section (2 deposits + 1 dividend
# that must be filtered). Fixture-labelled synthetic values.
_TWO_SECTION_CSV = (
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
    '"ClientAccountID","Date/Time","Type","Description","Amount",'
    '"CurrencyPrimary","Symbol","AssetClass","TransactionID"\n'
    '"DUQ999999","20251026;225011","Deposits/Withdrawals",'
    '"CASH RECEIPTS / ELECTRONIC FUND TRANSFERS","10000","HKD","","",'
    '"4179119714"\n'
    '"DUQ999999","20260107","Deposits/Withdrawals",'
    '"CASH RECEIPTS / ELECTRONIC FUND TRANSFERS","35000","USD","","",'
    '"4326167414"\n'
    '"DUQ999999","20260201","Dividends","SPY DIVIDEND","42.50","USD","SPY","STK",'
    '"4400000001"\n'
)


@pytest.fixture
def duq_env(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DUQ999999")
    monkeypatch.setenv("XENON_PAPER_ACCOUNT", "DUQ999999")
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)


@pytest.fixture
def csv_path(tmp_path: Path) -> Path:
    p = tmp_path / "flex_inception.csv"
    p.write_text(_TWO_SECTION_CSV)
    return p


def _nav_rows():
    engine = get_sync_engine()
    with engine.begin() as conn:
        return conn.execute(
            text(
                "SELECT date, source, total, cash, stock_value, options_value "
                "FROM xenon.nav_history "
                "WHERE broker='IB' AND account_env='paper' "
                "AND broker_account='DUQ999999' ORDER BY date"
            )
        ).fetchall()


def _cash_rows():
    engine = get_sync_engine()
    with engine.begin() as conn:
        return conn.execute(
            text(
                "SELECT transaction_id, txn_type, currency, amount_native, "
                "amount_usd, fx_rate FROM xenon.ib_cash_flow "
                "WHERE broker='IB' AND account_env='paper' "
                "AND broker_account='DUQ999999' ORDER BY transaction_id"
            )
        ).fetchall()


def test_from_csv_writes_nav_close_and_cash_rows_idempotent(csv_path, pg_test_engine, duq_env):
    result = ingest_nav_csv(path=csv_path, scope=_SCOPE)

    assert "error" not in result
    assert result["nav_rows_written"] == 2
    assert result["cash_inserted"] == 2
    assert result["earliest_date"] == "2026-06-01"
    assert result["latest_date"] == "2026-06-02"

    nav = _nav_rows()
    assert len(nav) == 2
    assert all(r.source == "close" for r in nav)
    assert nav[1].total == Decimal("135000")
    assert nav[1].cash == Decimal("85000")
    assert nav[1].stock_value == Decimal("45")
    assert nav[1].options_value == Decimal("10")

    cash = _cash_rows()
    assert len(cash) == 2, [dict(r._mapping) for r in cash]
    by_id = {r.transaction_id: r for r in cash}
    assert "4400000001" not in by_id, "Dividend row must not enter ib_cash_flow"
    assert by_id["4179119714"].amount_usd == Decimal("1282.0500")  # 10000 HKD * peg
    assert by_id["4326167414"].amount_usd == Decimal("35000.0000")

    # Re-run: same rows, counted as updates — no duplicates, no error.
    result2 = ingest_nav_csv(path=csv_path, scope=_SCOPE)
    assert "error" not in result2
    assert result2["nav_rows_written"] == 2
    assert result2["cash_inserted"] == 0
    assert result2["cash_updated"] == 2
    assert len(_nav_rows()) == 2
    assert len(_cash_rows()) == 2


def test_dry_run_writes_nothing(csv_path, pg_test_engine, duq_env):
    result = ingest_nav_csv(path=csv_path, scope=_SCOPE, dry_run=True)

    assert result["dry_run"] is True
    assert result["nav_rows"] == 2
    assert result["cash_flow_rows"] == 2
    assert result["earliest_date"] == "2026-06-01"
    assert result["latest_date"] == "2026-06-02"
    assert "nav_rows_written" not in result
    assert _nav_rows() == []
    assert _cash_rows() == []


def test_no_flag_prints_usage_and_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["backfill_nav_from_ib_flex"])
    assert main() == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_read_only_refuses_writes(csv_path, pg_test_engine, duq_env, monkeypatch, capsys):
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    monkeypatch.setattr(
        sys, "argv", ["backfill_nav_from_ib_flex", "--from-csv", str(csv_path)]
    )
    assert main() == 3
    assert "READ_ONLY" in capsys.readouterr().err
    assert _nav_rows() == []
    assert _cash_rows() == []


def test_read_only_dry_run_still_works(csv_path, pg_test_engine, duq_env, monkeypatch):
    """--dry-run is parse-only: allowed under XENON_READ_ONLY=1."""
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        ["backfill_nav_from_ib_flex", "--from-csv", str(csv_path), "--dry-run"],
    )
    assert main() == 0
    assert _nav_rows() == []
