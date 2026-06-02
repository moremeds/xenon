"""fetch_ib_nav_series must persist source='close' (PR-1 NAV auto-refresh).

EquitySummaryByReportDateInBase rows are post-close. Until PR-1 the call
site omitted the source kwarg so every Flex import silently landed as
the server default 'intraday'. Locking down the tag prevents a daily
xenon-nav-flex-refresh run from being clobbered by a same-day intraday
ib_sync write.
"""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import text

from xenon.db.engine import get_sync_engine

_SAMPLE_FLEX_XML = """<?xml version="1.0"?>
<FlexQueryResponse>
  <FlexStatements count="1">
    <FlexStatement accountId="DUQ999999">
      <EquitySummaryInBase>
        <EquitySummaryByReportDateInBase reportDate="20260601"
          total="100" cash="50" stock="40" options="10"/>
      </EquitySummaryInBase>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""


def test_fetch_ib_nav_series_writes_source_close(monkeypatch, pg_test_engine):
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1234567")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_PAPER_ACCOUNT", "DUQ999999")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DUQ999999")

    def fake_urlopen(url, timeout=30):
        class _R:
            def __init__(self, body: str) -> None:
                self._body = body

            def read(self) -> bytes:
                return self._body.encode()

        if "SendRequest" in url:
            return _R(
                '<?xml version="1.0"?><FlexStatementResponse>'
                "<Status>Success</Status><ReferenceCode>REF123</ReferenceCode>"
                "</FlexStatementResponse>"
            )
        return _R(_SAMPLE_FLEX_XML)

    # urlopen is imported inside fetch_ib_nav_series via
    #   `from urllib.request import urlopen`
    # so the binding read at call time is urllib.request.urlopen — patch the
    # module attr, not the local re-bind.
    with patch("urllib.request.urlopen", fake_urlopen), patch("time.sleep"):
        from xenon.reports.portfolio_performance import fetch_ib_nav_series

        entries = fetch_ib_nav_series()

    assert entries is not None
    assert len(entries) == 1

    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT source FROM xenon.nav_history "
                "WHERE broker='IB' AND account_env='paper' "
                "AND broker_account='DUQ999999' AND date='2026-06-01'"
            )
        ).first()
    assert row is not None, "fetch_ib_nav_series did not persist to PG"
    assert row.source == "close", f"expected 'close', got {row.source!r}"
