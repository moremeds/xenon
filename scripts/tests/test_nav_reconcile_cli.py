"""xenon-nav-reconcile CLI behavior (Pass-1 addition).

Pass-2 E1(a): under the 5-col PK (broker, account_env, broker_account, date,
source), the two ``upsert_nav_sync`` calls in each test produce TWO coexisting
rows, exactly what the per-date intraday-vs-close comparison reads.
"""

from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal

from xenon.execution.account_scope import AccountScope
from xenon.utils.portfolio_loader import upsert_nav_sync

SCOPE = AccountScope(broker="IB", account_env="live", broker_account="U_RECONCILE1")


def _reimport():
    import xenon.jobs.nav_reconcile as m

    importlib.reload(m)
    return m


def _seed_env(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_LIVE_ACCOUNT", SCOPE.broker_account)
    monkeypatch.setenv("XENON_BROKER", SCOPE.broker)
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", SCOPE.broker_account)
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)


def test_main_no_rows_exits_0(monkeypatch, pg_test_engine, capsys):
    _seed_env(monkeypatch)
    m = _reimport()
    rc = m.main(["--since", "2099-01-01", "--until", "2099-06-01"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no rows reconcilable" in out.lower()


def test_main_within_tolerance_exits_0(monkeypatch, pg_test_engine, capsys):
    _seed_env(monkeypatch)
    upsert_nav_sync(scope=SCOPE, day=date(2026, 1, 15), nav=Decimal("100000.00"), source="intraday")
    upsert_nav_sync(scope=SCOPE, day=date(2026, 1, 15), nav=Decimal("100000.05"), source="close")
    m = _reimport()
    # Default tolerance 10 bps = 0.1%. Diff is 0.005% → within tolerance.
    rc = m.main(["--since", "2026-01-01", "--until", "2026-02-01"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK:" in out


def test_main_exceeds_tolerance_exits_4(monkeypatch, pg_test_engine, capsys):
    _seed_env(monkeypatch)
    upsert_nav_sync(scope=SCOPE, day=date(2026, 2, 15), nav=Decimal("100000.00"), source="intraday")
    # 5% discrepancy — well beyond default 10 bps tolerance.
    upsert_nav_sync(scope=SCOPE, day=date(2026, 2, 15), nav=Decimal("105000.00"), source="close")
    m = _reimport()
    rc = m.main(["--since", "2026-01-01", "--until", "2026-03-01"])
    assert rc == 4
    out = capsys.readouterr().out
    assert "2026-02-15" in out
    assert "flagged date" in out


def test_main_bad_date_exits_1(monkeypatch, pg_test_engine, capsys):
    _seed_env(monkeypatch)
    m = _reimport()
    rc = m.main(["--since", "not-a-date", "--until", "2026-01-01"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "FATAL" in err
