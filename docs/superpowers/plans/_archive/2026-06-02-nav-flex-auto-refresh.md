# NAV Auto-Refresh (PR-1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land daily auto-refresh of IB NAV close prices into Postgres so the `/performance` page stays current without manual CSV downloads.

**Architecture:** Three-piece wiring — (1) extend `upsert_nav_sync` to accept an optional `source` arg, (2) fix `fetch_ib_nav_series` end-to-end (it is currently broken — wrong URL → IB returns code 1001; wrong format assumption → XML parser cannot read the CSV body IB actually returns; missing `source='close'` arg on the upsert — all three fix together), (3) ship a new `xenon-nav-flex-refresh` CLI invoked daily at 17:30 ET on the macmini via a macOS LaunchAgent. The shell wrapper sources the macmini's `.env` so DB creds + Flex token come from the operator's existing config, not the plist (matches the pattern in `scripts/infra/refresh-core-test.sh`).

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 (`_pg_insert.on_conflict_do_update`), pytest, macOS launchd (`StartCalendarInterval`), IB Flex Web Service (`EquitySummaryByReportDateInBase`).

---

## Context

PR #119 (merged 2026-06-02) imported 262 close-price NAV rows from a manually-downloaded IB Flex CSV so the `/performance` page could render the YTD curve back to inception. The PR description called out four deferred items; this plan covers the first and highest-leverage one — wiring the same Flex query to run on a daily schedule so manual downloads stop.

Live verification on 2026-06-02 surfaced that `fetch_ib_nav_series` (the function the scheduler will invoke) is **fully broken today** — PR #119 used the CSV-file import path, never the Web Service path, so the breakage went unnoticed:

1. **Wrong URL.** Code calls `https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest` (legacy/deprecated). IB returns `<ErrorCode>1001</ErrorCode>` ("Statement could not be generated at this time") — a generic error that masks the real problem. The documented current URL is `https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest` with a `User-Agent: Java` header, and that endpoint succeeded instantly under live test. (IB's success response even returns the matching `GetStatement` URL inline.)
2. **Wrong format assumption.** After `SendRequest` succeeds, `GetStatement` returns **CSV**, not XML — saved query `1529248` is configured at the IB side for CSV output (same multi-section CSV shape the PR #119 backfill script already parses via `ingest_nav_csv`). The current parser does `ET.fromstring(...)`, which throws `ParseError`, gets swallowed by the bare `except Exception: return None`, and the whole function silently returns `None`.
3. **Missing `source='close'`.** Even if the function worked, the upsert call omits `source`, so rows would silently land as `source='intraday'` (server default) and a scheduled run would clobber the just-imported close rows.

All three fixes land together in this PR — splitting them would mean shipping a broken function alone or a scheduler on top of a function that returns `None`.

The `_extract_cash_flows` function (`portfolio_performance.py:491`) and `trade_blotter/flex_query.py::FlexQueryFetcher` (used by `xenon-blotter-history` + `/blotter`) have the **same URL + format bug** but are out of scope here — they will be fixed in the next plan along with the historical-fills port.

## Scope

In scope:

- `upsert_nav_sync` accepts optional `source` arg
- `fetch_ib_nav_series` end-to-end fix: correct URL + `User-Agent: Java`, CSV parsing (replaces XML parser), and passes `source='close'` to the upsert
- `xenon-nav-flex-refresh` CLI entry point
- Shell wrapper + LaunchAgent plist + install runbook

Out of scope (separate plans listed at the bottom):

- Same URL/format fix in `_extract_cash_flows` and `trade_blotter/flex_query.py` — bundled into the next plan with the trade-side persistence work
- `/performance` page period selector (frontend)
- Honest `total_return` headline % (subtract deposits/transfers from gain)
- Trade-side PG persistence (Flex trades CSV → new `historical_fills` table — operator decision locked in 2026-06-02)
- Matching IB's exact server-side TWR — `docs/architecture/performance-reconstruction.md` concluded this is irreducible without IB's proprietary algorithm

## File Structure

| File                                                     | Responsibility                      | Action                                                                                                 |
| -------------------------------------------------------- | ----------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `src/xenon/utils/portfolio_loader.py`                    | NAV upsert helper                   | Modify `upsert_nav_sync` (lines 125-178) — add `source: str \| None = None`                            |
| `src/xenon/reports/portfolio_performance.py`             | IB Flex NAV fetcher                 | Rewrite `fetch_ib_nav_series` (lines 312-411): fix URL, swap XML parser for CSV, pass `source='close'` |
| `src/xenon/jobs/__init__.py`                             | New package dir                     | Create — empty                                                                                         |
| `src/xenon/jobs/nav_flex_refresh.py`                     | CLI entry point                     | Create — `main()` returns int exit code                                                                |
| `pyproject.toml`                                         | Entry-point registration            | Modify `[project.scripts]` (after line 60) — add `xenon-nav-flex-refresh`                              |
| `scripts/infra/nav-flex-refresh.sh`                      | Shell wrapper                       | Create — sources macmini `.env`, then `uv run xenon-nav-flex-refresh`                                  |
| `scripts/infra/launchd/com.xenon.nav-flex-refresh.plist` | LaunchAgent template                | Create — daily 17:30 ET, modeled on `com.xenon.refresh-core-test.plist`                                |
| `scripts/tests/test_upsert_nav_source.py`                | Test source-arg behavior            | Create                                                                                                 |
| `scripts/tests/test_fetch_ib_nav_series_csv.py`          | E2E: URL, CSV parse, source persist | Create                                                                                                 |
| `scripts/tests/test_nav_flex_refresh_cli.py`             | Test CLI module                     | Create                                                                                                 |
| `docs/runbooks/nav-flex-refresh.md`                      | Install + ops runbook               | Create                                                                                                 |

**Reused utilities (don't reinvent):**

- `xenon.execution.account_scope.AccountScope` — scope dataclass for the test fixtures
- `xenon.execution.account_scope.resolve_from_env` — already used inside `fetch_ib_nav_series`
- `xenon.utils.portfolio_loader.upsert_nav_sync` — the helper being extended; one call site exists for now (the Flex importer)
- `scripts/tests/conftest.py::pg_test_engine` fixture — gates PG tests on a reachable database, otherwise skips
- `scripts/infra/refresh-core-test.sh` + `scripts/infra/launchd/com.xenon.refresh-core-test.plist` — the structural template for the new wrapper + plist

---

## Task 1: Extend `upsert_nav_sync` with `source` parameter

**Files:**

- Modify: `src/xenon/utils/portfolio_loader.py:125-178`
- Test: `scripts/tests/test_upsert_nav_source.py` (create)

**Background:** The `nav_history` schema (`src/xenon/db/schema.py:174-201`) declares `source TEXT NOT NULL DEFAULT 'intraday'` with `CHECK (source IN ('close','intraday'))`. Currently no caller passes the param, so every row lands as `intraday`. The change must be backward-compatible: omitting `source` keeps the existing DB value on conflict (so a daily `close` write is not later clobbered by an intraday ib_sync NAV snapshot writing the same date with no source). When `source` is provided, it's INSERTed and updated on conflict.

- [ ] **Step 1: Write the failing tests**

Create `scripts/tests/test_upsert_nav_source.py`:

```python
"""upsert_nav_sync source-arg behavior (PR-1 NAV auto-refresh)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from xenon.utils.portfolio_loader import upsert_nav_sync

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DUQ999999")


def _read_back(scope: AccountScope, day: date) -> dict:
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT nav, source FROM xenon.nav_history "
                "WHERE broker=:b AND account_env=:e AND broker_account=:a AND date=:d"
            ),
            {"b": scope.broker, "e": scope.account_env, "a": scope.broker_account, "d": day},
        ).first()
    return {"nav": row.nav, "source": row.source} if row else {}


def test_omitting_source_writes_server_default_intraday(pg_test_engine):
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100.00"))
    assert _read_back(SCOPE, date(2026, 6, 1))["source"] == "intraday"


def test_source_close_writes_close(pg_test_engine):
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100.00"), source="close")
    assert _read_back(SCOPE, date(2026, 6, 1))["source"] == "close"


def test_source_close_overwrites_existing_intraday(pg_test_engine):
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100.00"))
    assert _read_back(SCOPE, date(2026, 6, 1))["source"] == "intraday"
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("110.00"), source="close")
    row = _read_back(SCOPE, date(2026, 6, 1))
    assert row["source"] == "close"
    assert row["nav"] == Decimal("110.00")


def test_omitting_source_preserves_existing_close(pg_test_engine):
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("100.00"), source="close")
    upsert_nav_sync(scope=SCOPE, day=date(2026, 6, 1), nav=Decimal("105.00"))
    row = _read_back(SCOPE, date(2026, 6, 1))
    assert row["source"] == "close"  # preserved on conflict
    assert row["nav"] == Decimal("105.00")  # nav still updates
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest scripts/tests/test_upsert_nav_source.py -xvs
```

Expected: All four fail with `TypeError: upsert_nav_sync() got an unexpected keyword argument 'source'`.

- [ ] **Step 3: Implement the change**

Edit `src/xenon/utils/portfolio_loader.py:125-178`. Replace the entire `upsert_nav_sync` function body with:

```python
def upsert_nav_sync(
    *,
    scope: AccountScope,
    day: _date,
    nav: _Decimal | float | int,
    daily_pnl: _Decimal | float | int | None = None,
    total: _Decimal | float | int | None = None,
    cash: _Decimal | float | int | None = None,
    stock_value: _Decimal | float | int | None = None,
    options_value: _Decimal | float | int | None = None,
    source: str | None = None,
) -> None:
    """Sync mirror of `xenon.db.queries.portfolio.upsert_nav`.

    NULL-safe on every nullable column: when a caller passes ``None`` for a
    breakdown field (or daily_pnl), the existing PG value is preserved rather
    than overwritten. ib_sync only knows ``nav``; the IB Flex importer
    (`fetch_ib_nav_series`) supplies the full breakdown — both can write to
    the same row without erasing each other's contributions.

    ``source`` distinguishes post-close (IB Flex EquitySummaryByReportDateInBase)
    rows from intraday (ib_sync) snapshots. Omitting it keeps the existing
    value on conflict so a daily 'close' row from xenon-nav-flex-refresh is
    not clobbered by an intraday ib_sync that fires later.
    """
    values: dict[str, object] = {
        "broker": scope.broker,
        "account_env": scope.account_env,
        "broker_account": scope.broker_account,
        "date": day,
        "nav": nav,
        "daily_pnl": daily_pnl,
        "total": total,
        "cash": cash,
        "stock_value": stock_value,
        "options_value": options_value,
    }
    if source is not None:
        values["source"] = source
    stmt = _pg_insert(nav_history).values(**values)
    set_columns: dict[str, object] = {"nav": stmt.excluded.nav}
    if daily_pnl is not None:
        set_columns["daily_pnl"] = stmt.excluded.daily_pnl
    if total is not None:
        set_columns["total"] = stmt.excluded.total
    if cash is not None:
        set_columns["cash"] = stmt.excluded.cash
    if stock_value is not None:
        set_columns["stock_value"] = stmt.excluded.stock_value
    if options_value is not None:
        set_columns["options_value"] = stmt.excluded.options_value
    if source is not None:
        set_columns["source"] = stmt.excluded.source
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            nav_history.c.broker,
            nav_history.c.account_env,
            nav_history.c.broker_account,
            nav_history.c.date,
        ],
        set_=set_columns,
    )
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(stmt)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest scripts/tests/test_upsert_nav_source.py -xvs
```

Expected: 4 passed.

- [ ] **Step 5: Regression sweep on existing NAV writers**

```bash
uv run python scripts/infra/dev/run_pytest_affected.py
```

Expected: no failures in `test_ib_sync_*`, `test_portfolio_loader*`, `test_read_only_mode*`.

- [ ] **Step 6: Commit**

```bash
git checkout -b feat/nav-flex-auto-refresh
git add src/xenon/utils/portfolio_loader.py scripts/tests/test_upsert_nav_source.py
git commit -m "feat(nav): upsert_nav_sync accepts source param

Default behavior unchanged (server default 'intraday'). When source
is supplied it INSERTs and UPDATEs on conflict, so a daily 'close'
write from xenon-nav-flex-refresh wins over stale 'intraday' rows.

Prereq for xenon-nav-flex-refresh CLI."
```

---

## Task 2: Fix `fetch_ib_nav_series` end-to-end (URL + CSV format + source)

**Files:**

- Modify: `src/xenon/reports/portfolio_performance.py:312-411` (the whole function body)
- Test: `scripts/tests/test_fetch_ib_nav_series_csv.py` (create)

**Background:** Live verification on 2026-06-02 found three stacked bugs in `fetch_ib_nav_series` — all three must land in one diff or the function stays broken. The CSV format of the response was confirmed against IB live for query `1529248`: a multi-section CSV starting with the `EquitySummaryByReportDateInBase` header `"ClientAccountID","ReportDate","Total","TotalLong","TotalShort","Cash",…` followed (when present) by a `"ClientAccountID","Date/Time","Type",…` Cash-Transactions section header — same shape `scripts/migrations/_2026_06_02_backfill_nav_from_ib_flex.py::ingest_nav_csv` already parses correctly (the second-section header is detected and the loop breaks).

Bug 1 — **URL.** Replace the legacy `gdcdyn.../Universal/servlet/FlexStatementService.*` endpoints with the current `ndcdyn.../AccountManagement/FlexWebService/SendRequest` (send) and `gdcdyn.../AccountManagement/FlexWebService/GetStatement` (poll). Both calls must include `User-Agent: Java` header (per the canonical `csingley/ibflex` client; raw `urlopen` without it can be rejected).

Bug 2 — **Format.** The response is CSV, not XML. Replace the `ET.fromstring(...)` + `findall(".//EquitySummaryByReportDateInBase")` block with `csv.DictReader` over the response body. Apply the same multi-section break used in `ingest_nav_csv`: stop when a row's `ClientAccountID` literally equals the string `"ClientAccountID"` (that is the next section's header bleeding through). Filter rows to `scope.broker_account`.

Bug 3 — **Source.** Pass `source="close"` to `upsert_nav_sync`.

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_fetch_ib_nav_series_csv.py`:

```python
"""fetch_ib_nav_series — URL, CSV parsing, source='close' (PR-1).

Three regressions in one test: the function must call the documented
ndcdyn SendRequest URL, parse the CSV body IB actually returns, and
persist source='close' for each NAV row.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine

# Matches the live CSV shape captured 2026-06-02 from query 1529248:
# - First section: EquitySummaryByReportDateInBase header + 2 rows
# - Second section header for CashTransactions (zero data rows here)
_LIVE_CSV = (
    '"ClientAccountID","ReportDate","Total","TotalLong","TotalShort","Cash",'
    '"CashLong","CashShort","Stock","StockLong","StockShort","Options",'
    '"OptionsLong","OptionsShort","Bonds","BondsLong","BondsShort"\n'
    '"DUQ999999","20260529","100.00","100","0","50.00","50","0","40.00","40","0","10.00","10","0","0","0","0"\n'
    '"DUQ999999","20260601","110.00","110","0","55.00","55","0","44.00","44","0","11.00","11","0","0","0","0"\n'
    '"ClientAccountID","Date/Time","Type","Description","Amount","CurrencyPrimary","Symbol","AssetClass","TransactionID"\n'
)

_SEND_OK = (
    "<FlexStatementResponse>"
    "<Status>Success</Status>"
    "<ReferenceCode>REF123</ReferenceCode>"
    "<Url>https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url>"
    "</FlexStatementResponse>"
)


def test_fetch_ib_nav_series_csv_end_to_end(monkeypatch, pg_test_engine):
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1234567")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_PAPER_ACCOUNT", "DUQ999999")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DUQ999999")

    seen_urls: list[str] = []

    def fake_urlopen(req_or_url, timeout=30):
        # urlopen accepts either a str or a Request object — handle both so
        # the test does not couple to whether the prod code passes raw URLs
        # or Request(url, headers={"User-Agent": "Java"}).
        url = req_or_url if isinstance(req_or_url, str) else req_or_url.full_url
        seen_urls.append(url)

        class _R:
            def __init__(self, body):
                self._body = body
            def read(self):
                return self._body.encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        if "SendRequest" in url:
            return _R(_SEND_OK)
        return _R(_LIVE_CSV)

    with patch("urllib.request.urlopen", fake_urlopen), patch("time.sleep"):
        from xenon.reports.portfolio_performance import fetch_ib_nav_series
        entries = fetch_ib_nav_series()

    # Two NAV rows parsed, Cash-Transactions header correctly skipped.
    assert entries is not None and len(entries) == 2
    assert {e["date"] for e in entries} == {"2026-05-29", "2026-06-01"}

    # Correct (current, documented) SendRequest endpoint was used.
    assert any(
        u.startswith("https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest")
        for u in seen_urls
    ), f"SendRequest URL wrong; saw {seen_urls!r}"

    # Both rows persisted with source='close'.
    engine = get_sync_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT date, source FROM xenon.nav_history "
                "WHERE broker='IB' AND account_env='paper' "
                "AND broker_account='DUQ999999' ORDER BY date"
            )
        ).fetchall()
    assert [r.source for r in rows] == ["close", "close"]


def test_fetch_ib_nav_series_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("IB_FLEX_TOKEN", raising=False)
    monkeypatch.delenv("IB_FLEX_NAV_QUERY_ID", raising=False)
    from xenon.reports.portfolio_performance import fetch_ib_nav_series
    assert fetch_ib_nav_series() is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest scripts/tests/test_fetch_ib_nav_series_csv.py -xvs
```

Expected: `test_fetch_ib_nav_series_csv_end_to_end` fails — either no rows parsed (XML parser barfs on CSV → bare `except` returns `None`), or wrong SendRequest URL recorded.

- [ ] **Step 3: Implement the fix**

Replace the body of `fetch_ib_nav_series` in `src/xenon/reports/portfolio_performance.py` (lines 312-411) with:

```python
def fetch_ib_nav_series() -> Optional[List[Dict[str, Any]]]:
    """Fetch daily NAV from IB Flex Query (EquitySummaryInBase) and persist to PG.

    Calls the documented Flex Web Service endpoints
    (ndcdyn .../AccountManagement/FlexWebService/SendRequest +
    gdcdyn .../AccountManagement/FlexWebService/GetStatement) with a
    `User-Agent: Java` header. The saved query at IB returns CSV (not XML);
    we parse with csv.DictReader and break on the next-section header so a
    concatenated Cash-Transactions section does not pollute the NAV rows.

    Each entry is upserted into ``xenon.nav_history`` with ``source='close'``
    (EquitySummaryByReportDateInBase rows are post-close by definition).
    Returns list of ``{date, total, cash, stock, options}`` or None on failure.
    """
    token = os.environ.get("IB_FLEX_TOKEN")
    nav_query_id = os.environ.get("IB_FLEX_NAV_QUERY_ID")
    if not token or not nav_query_id:
        return None

    import csv
    import io
    import time
    import xml.etree.ElementTree as ET
    from datetime import date as _date
    from decimal import Decimal as _Decimal
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen

    SEND_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
    GET_URL = "https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"

    def _get(url: str, params: dict) -> str:
        req = Request(f"{url}?{urlencode(params)}", headers={"User-Agent": "Java"})
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")

    try:
        send_xml = _get(SEND_URL, {"t": token, "q": nav_query_id, "v": "3"})
        root = ET.fromstring(send_xml)
        if (root.find(".//Status") is not None
                and (root.find(".//Status").text or "") != "Success"):
            return None
        ref_node = root.find(".//ReferenceCode")
        if ref_node is None or not ref_node.text:
            return None
        ref_code = ref_node.text

        # Poll GetStatement until the body stops being an in-progress
        # FlexStatementResponse and becomes the CSV statement body.
        body = ""
        for _ in range(30):
            time.sleep(3)
            body = _get(GET_URL, {"t": token, "q": ref_code, "v": "3"})
            if body.lstrip().startswith("<FlexStatementResponse"):
                continue
            break
        else:
            return None

        # CSV header always starts with quoted ClientAccountID — sanity check.
        if not body.lstrip().startswith('"ClientAccountID"'):
            return None

        reader = csv.DictReader(io.StringIO(body))
        entries: List[Dict[str, Any]] = []
        for row in reader:
            account = (row.get("ClientAccountID") or "").strip()
            # Multi-section CSV: a second section header row repeats the
            # column name 'ClientAccountID' as its value. Stop here so we
            # do not mis-parse CashTransaction rows as NAV.
            if account == "ClientAccountID":
                break
            dt_raw = (row.get("ReportDate") or "").strip()
            if len(dt_raw) != 8 or not dt_raw.isdigit():
                continue
            entries.append(
                {
                    "date": f"{dt_raw[:4]}-{dt_raw[4:6]}-{dt_raw[6:8]}",
                    "total": safe_float(row.get("Total")),
                    "cash": safe_float(row.get("Cash")),
                    "stock": safe_float(row.get("Stock")),
                    "options": safe_float(row.get("Options")),
                    "_account": account,
                }
            )

        try:
            from xenon.execution.account_scope import resolve_from_env
            from xenon.utils.portfolio_loader import upsert_nav_sync

            scope = resolve_from_env()
            for e in entries:
                if e["_account"] != scope.broker_account:
                    continue
                try:
                    day = _date.fromisoformat(e["date"])
                except (TypeError, ValueError):
                    continue
                total_v = _Decimal(str(e["total"])) if e.get("total") is not None else None
                cash_v = _Decimal(str(e["cash"])) if e.get("cash") is not None else None
                stock_v = _Decimal(str(e["stock"])) if e.get("stock") is not None else None
                opt_v = _Decimal(str(e["options"])) if e.get("options") is not None else None
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
            logger.warning(
                "fetch_ib_nav_series PG persist failed (%s) — returning fresh entries; cache will miss next run",
                exc,
            )

        # Strip the internal _account marker before returning so callers see
        # the same shape they did before the CSV rewrite.
        for e in entries:
            e.pop("_account", None)
        return entries
    except Exception:
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest scripts/tests/test_fetch_ib_nav_series_csv.py -xvs
```

Expected: both tests pass.

- [ ] **Step 5: Regression sweep**

```bash
uv run python scripts/infra/dev/run_pytest_affected.py
```

Expected: no failures in `test_portfolio_performance*`, `test_nav_history*`, or any test that imports `fetch_ib_nav_series`.

- [ ] **Step 6: Live smoke (optional, requires IB Flex to be responsive)**

From the worktree, with the live token in `.env`:

```bash
uv run python -c "
from xenon.reports.portfolio_performance import fetch_ib_nav_series
import os
os.environ['XENON_BROKER_ACCOUNT'] = os.environ.get('XENON_LIVE_ACCOUNT','')
print(fetch_ib_nav_series())
"
```

Expected: a non-empty list of `{date, total, cash, stock, options}` dicts. Skip this step if `1001`/`1009` returned — re-test mid-day ET.

- [ ] **Step 7: Commit**

```bash
git add src/xenon/reports/portfolio_performance.py scripts/tests/test_fetch_ib_nav_series_csv.py
git commit -m "fix(nav): fetch_ib_nav_series — correct URL, CSV parsing, source='close'

Three stacked bugs all blocking the daily auto-refresh:
1. Legacy 'Universal/servlet/FlexStatementService' URL returns code
   1001 unconditionally. Use the documented ndcdyn .../AccountManagement/
   FlexWebService/SendRequest + Java User-Agent.
2. Saved IB query returns CSV, not XML — swap ET.fromstring for
   csv.DictReader, break on the multi-section header.
3. Upsert now passes source='close' so post-close NAV rows are
   tagged correctly and survive a same-day intraday ib_sync.

Surfaced via live verification 2026-06-02. PR #119 missed it because
the backfill went through the --from-csv path, not fetch_ib_nav_series."
```

---

## Task 3: Build `xenon-nav-flex-refresh` CLI

**Files:**

- Create: `src/xenon/jobs/__init__.py` (empty)
- Create: `src/xenon/jobs/nav_flex_refresh.py`
- Modify: `pyproject.toml:60`
- Test: `scripts/tests/test_nav_flex_refresh_cli.py` (create)

**Background:** New CLI entry point invoked daily by launchd. Mirrors the env-derivation pattern from `scripts/infra/dev.sh:174` and `scripts/migrations/_2026_06_02_backfill_nav_from_ib_flex.py:37-43` (derive `XENON_BROKER_ACCOUNT` from `XENON_LIVE_ACCOUNT` / `XENON_PAPER_ACCOUNT` based on `XENON_TRADING_MODE`). Calls `fetch_ib_nav_series` and reports a structured result. Exit codes: 0=ok, 1=fetch failed/empty, 2=FLEX_NOT_CONFIGURED. New `xenon.jobs` package keeps this module out of `xenon.reports.*` (where it would be one of many CLIs).

- [ ] **Step 1: Write the failing tests**

Create `scripts/tests/test_nav_flex_refresh_cli.py`:

```python
"""xenon-nav-flex-refresh CLI behavior (PR-1)."""

from __future__ import annotations

import importlib
import os
from unittest.mock import patch

import pytest


def _reimport_module():
    """Force re-import so module-level state (env reads) refreshes per test."""
    import xenon.jobs.nav_flex_refresh as m
    importlib.reload(m)
    return m


def test_main_exits_2_when_token_missing(monkeypatch, capsys):
    monkeypatch.delenv("IB_FLEX_TOKEN", raising=False)
    monkeypatch.delenv("IB_FLEX_NAV_QUERY_ID", raising=False)
    m = _reimport_module()
    rc = m.main()
    assert rc == 2
    err = capsys.readouterr().err
    assert "FLEX_NOT_CONFIGURED" in err


def test_main_exits_1_when_fetch_returns_none(monkeypatch):
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1234567")
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_LIVE_ACCOUNT", "U18007831")
    m = _reimport_module()
    with patch.object(m, "fetch_ib_nav_series", return_value=None):
        rc = m.main()
    assert rc == 1


def test_main_exits_1_when_fetch_returns_empty(monkeypatch):
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1234567")
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "U18007831")
    m = _reimport_module()
    with patch.object(m, "fetch_ib_nav_series", return_value=[]):
        rc = m.main()
    assert rc == 1


def test_main_derives_broker_account_from_live(monkeypatch, capsys):
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1234567")
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_LIVE_ACCOUNT", "U18007831")
    monkeypatch.delenv("XENON_BROKER_ACCOUNT", raising=False)
    m = _reimport_module()
    sample = [{"date": "2026-06-01", "total": 100.0, "cash": 50.0, "stock": 40.0, "options": 10.0}]
    with patch.object(m, "fetch_ib_nav_series", return_value=sample):
        rc = m.main()
    assert rc == 0
    assert os.environ["XENON_BROKER_ACCOUNT"] == "U18007831"
    out = capsys.readouterr().out
    assert "fetched 1 NAV row" in out


def test_main_derives_broker_account_from_paper(monkeypatch):
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1234567")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_PAPER_ACCOUNT", "DUQ378889")
    monkeypatch.delenv("XENON_BROKER_ACCOUNT", raising=False)
    m = _reimport_module()
    with patch.object(m, "fetch_ib_nav_series", return_value=[{"date": "2026-06-01"}]):
        rc = m.main()
    assert rc == 0
    assert os.environ["XENON_BROKER_ACCOUNT"] == "DUQ378889"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest scripts/tests/test_nav_flex_refresh_cli.py -xvs
```

Expected: All fail with `ModuleNotFoundError: No module named 'xenon.jobs'`.

- [ ] **Step 3: Create the package + module**

Create `src/xenon/jobs/__init__.py` as an empty file (content: a single newline).

Create `src/xenon/jobs/nav_flex_refresh.py`:

```python
"""Daily IB Flex NAV refresh — invoked by launchd at 17:30 ET.

Polls IB Flex Web Service for EquitySummaryByReportDateInBase rows and
upserts them into ``xenon.nav_history`` with ``source='close'``. The
underlying ``fetch_ib_nav_series`` handles the two-step SendRequest +
GetStatement polling and the upsert.

Exit codes:
  0 — fetched and persisted N>0 rows
  1 — fetch returned None or empty (token rejected, poll timeout, no rows)
  2 — FLEX_NOT_CONFIGURED (missing IB_FLEX_TOKEN or IB_FLEX_NAV_QUERY_ID)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    except ImportError:
        pass


def _ensure_broker_account_env() -> None:
    """Mirror scripts/infra/dev.sh:174 — derive XENON_BROKER_ACCOUNT from mode."""
    if os.environ.get("XENON_BROKER_ACCOUNT"):
        return
    mode = os.environ.get("XENON_TRADING_MODE", "").strip().lower()
    env_key = {"live": "XENON_LIVE_ACCOUNT", "paper": "XENON_PAPER_ACCOUNT"}.get(mode)
    if env_key and os.environ.get(env_key):
        os.environ["XENON_BROKER_ACCOUNT"] = os.environ[env_key]


# Re-export so tests can monkeypatch via the local module namespace.
from xenon.reports.portfolio_performance import fetch_ib_nav_series  # noqa: E402


def main() -> int:
    _load_env()
    _ensure_broker_account_env()

    if not os.environ.get("IB_FLEX_TOKEN") or not os.environ.get("IB_FLEX_NAV_QUERY_ID"):
        print(
            "FLEX_NOT_CONFIGURED: set IB_FLEX_TOKEN and IB_FLEX_NAV_QUERY_ID",
            file=sys.stderr,
        )
        return 2

    print(
        f"xenon-nav-flex-refresh: mode={os.environ.get('XENON_TRADING_MODE')} "
        f"account={os.environ.get('XENON_BROKER_ACCOUNT')}"
    )
    print("polling IB Flex Web Service (last-N-days query, ~30-90s)...")

    entries = fetch_ib_nav_series()
    if entries is None:
        print(
            "fetch_ib_nav_series returned None — token rejected, "
            "poll timeout, or no rows",
            file=sys.stderr,
        )
        return 1
    if not entries:
        print("fetch_ib_nav_series returned 0 rows", file=sys.stderr)
        return 1

    plural = "s" if len(entries) != 1 else ""
    print(
        f"fetched {len(entries)} NAV row{plural} "
        "(source='close' persisted via upsert_nav_sync)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Register the entry point**

Edit `pyproject.toml`. Find the `[project.scripts]` block (line ~40-60) and add a new line after `xenon-perf-explainer`:

```toml
xenon-perf-explainer     = "xenon.reports.performance_explainer_report:main"
xenon-nav-flex-refresh   = "xenon.jobs.nav_flex_refresh:main"
```

- [ ] **Step 5: Reinstall the package + verify entry-point resolves**

```bash
uv sync --extra test
uv run python -c "from xenon.jobs.nav_flex_refresh import main; print('import ok')"
```

Expected: `import ok` printed; no `ModuleNotFoundError`.

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run pytest scripts/tests/test_nav_flex_refresh_cli.py -xvs
```

Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add src/xenon/jobs/__init__.py src/xenon/jobs/nav_flex_refresh.py pyproject.toml scripts/tests/test_nav_flex_refresh_cli.py
git commit -m "feat(nav): xenon-nav-flex-refresh CLI

Daily entry point that polls IB Flex Web Service and upserts NAV
rows with source='close'. Wired up for the LaunchAgent in Task 4.

Exit codes: 0=ok, 1=fetch failed, 2=FLEX_NOT_CONFIGURED."
```

---

## Task 4: Shell wrapper + LaunchAgent plist

**Files:**

- Create: `scripts/infra/nav-flex-refresh.sh`
- Create: `scripts/infra/launchd/com.xenon.nav-flex-refresh.plist`

**Background:** Modeled on `scripts/infra/refresh-core-test.sh` + `scripts/infra/launchd/com.xenon.refresh-core-test.plist`. Same placeholder substitution scheme (the literal tokens `__XENON_ROOT__` and `__HOMEBREW_PATH__` are sed-replaced at install time), same `StandardOutPath`/`StandardErrorPath` → `/var/log/xenon/`, same `StartCalendarInterval` shape. The wrapper sources `.env` on the macmini so DB creds + Flex token come from the operator's existing file rather than being duplicated into the plist (which is checked in and cannot carry secrets). The plist sets `TZ=America/New_York` defensively so `StartCalendarInterval Hour=17 Minute=30` always means 17:30 ET.

> Note on the placeholder tokens: they are literal double-underscore strings (`__XENON_ROOT__`, `__HOMEBREW_PATH__`). Some Markdown renderers display them with bolded sides; the executing agent should treat them as plain text in the plist file.

- [ ] **Step 1: Write the shell wrapper**

Create `scripts/infra/nav-flex-refresh.sh`:

```bash
#!/usr/bin/env bash
# nav-flex-refresh.sh — Daily IB Flex NAV refresh wrapper.
#
# Runs on the macmini at 17:30 ET via the LaunchAgent at
# scripts/infra/launchd/com.xenon.nav-flex-refresh.plist.
#
# Why this exists: the plist passes only PATH + XENON_ROOT. This script
# sources .env from the checkout so DB creds + IB_FLEX_TOKEN come from
# the operator's already-configured file rather than the plist (which
# is checked in and cannot carry secrets). Same pattern as
# refresh-core-test.sh.
#
# Usage:
#   ./scripts/infra/nav-flex-refresh.sh           # invoke the CLI
#   ./scripts/infra/nav-flex-refresh.sh --dry     # source .env, do not run

set -euo pipefail

LOG_DIR="${XENON_NAV_REFRESH_LOG_DIR:-/var/log/xenon}"
LOG_FILE="$LOG_DIR/nav-flex-refresh.log"

XENON_ROOT="${XENON_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
ENV_FILE="${XENON_ENV_FILE:-$XENON_ROOT/.env}"

DRY=0
for arg in "$@"; do
  case "$arg" in
    --dry|-n) DRY=1 ;;
    -h|--help)
      sed -n '2,18p' "$0"
      exit 0
      ;;
    *)
      echo "FATAL: unknown argument '$arg'" >&2
      exit 2
      ;;
  esac
done

mkdir -p "$LOG_DIR"

log() {
  printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG_FILE"
}

log "=== nav-flex-refresh start: XENON_ROOT=$XENON_ROOT env=$ENV_FILE dry=$DRY ==="

if [[ ! -f "$ENV_FILE" ]]; then
  log "FATAL: env file not found: $ENV_FILE"
  exit 2
fi

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

# Default to live for the prod scheduled run; operators can override
# XENON_TRADING_MODE in .env for paper.
export XENON_TRADING_MODE="${XENON_TRADING_MODE:-live}"

if [[ "$DRY" == "1" ]]; then
  log "DRY: env sourced, mode=$XENON_TRADING_MODE — exiting before CLI invocation."
  exit 0
fi

cd "$XENON_ROOT"
if uv run xenon-nav-flex-refresh 2>&1 | tee -a "$LOG_FILE"; then
  log "=== nav-flex-refresh ok ==="
  exit 0
else
  rc=$?
  log "=== nav-flex-refresh FAILED rc=$rc — inspect $LOG_FILE ==="
  exit $rc
fi
```

- [ ] **Step 2: Make it executable and validate**

```bash
chmod +x scripts/infra/nav-flex-refresh.sh
bash -n scripts/infra/nav-flex-refresh.sh
```

Expected: no syntax errors.

- [ ] **Step 3: Dry-run the wrapper locally (paper mode)**

```bash
XENON_TRADING_MODE=paper XENON_NAV_REFRESH_LOG_DIR=/tmp/xenon-test ./scripts/infra/nav-flex-refresh.sh --dry
```

Expected: prints `DRY: env sourced, mode=paper — exiting before CLI invocation.` and exits 0. Log file at `/tmp/xenon-test/nav-flex-refresh.log` exists with the start + dry lines.

- [ ] **Step 4: Write the LaunchAgent plist template**

Create `scripts/infra/launchd/com.xenon.nav-flex-refresh.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!--
  com.xenon.nav-flex-refresh.plist — Daily IB Flex NAV refresh LaunchAgent.

  Install steps and full ops procedure: docs/runbooks/nav-flex-refresh.md.
  Logs:
    /var/log/xenon/nav-flex-refresh.stdout.log
    /var/log/xenon/nav-flex-refresh.stderr.log
    /var/log/xenon/nav-flex-refresh.log (wrapper output)
-->
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.xenon.nav-flex-refresh</string>

  <key>ProgramArguments</key>
  <array>
    <string>__XENON_ROOT__/scripts/infra/nav-flex-refresh.sh</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>__HOMEBREW_PATH__</string>
    <key>XENON_ROOT</key>
    <string>__XENON_ROOT__</string>
    <key>TZ</key>
    <string>America/New_York</string>
  </dict>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>17</integer>
    <key>Minute</key>
    <integer>30</integer>
  </dict>

  <key>StandardOutPath</key>
  <string>/var/log/xenon/nav-flex-refresh.stdout.log</string>

  <key>StandardErrorPath</key>
  <string>/var/log/xenon/nav-flex-refresh.stderr.log</string>

  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
```

(The `__XENON_ROOT__` and `__HOMEBREW_PATH__` strings above are literal placeholders the install step replaces.)

- [ ] **Step 5: Validate the plist**

```bash
plutil -lint scripts/infra/launchd/com.xenon.nav-flex-refresh.plist
```

Expected: `scripts/infra/launchd/com.xenon.nav-flex-refresh.plist: OK`.

- [ ] **Step 6: Commit**

```bash
git add scripts/infra/nav-flex-refresh.sh scripts/infra/launchd/com.xenon.nav-flex-refresh.plist
git commit -m "feat(infra): nav-flex-refresh LaunchAgent + wrapper

Daily 17:30 ET schedule on the macmini. Wrapper sources .env from
the checkout so plist stays secret-free (matches refresh-core-test
pattern). Install runbook in docs/runbooks/nav-flex-refresh.md."
```

---

## Task 5: Install + Operations Runbook

**Files:**

- Create: `docs/runbooks/nav-flex-refresh.md`

**Background:** The macmini operator follows this runbook to install, smoke-test, diagnose, and uninstall the LaunchAgent. Match the section structure of existing runbooks under `docs/runbooks/` (look at one neighbour for the house style before writing).

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/nav-flex-refresh.md` with these sections:

1. **Title + one-line summary.** "Polls IB Flex `EquitySummaryByReportDateInBase` daily at 17:30 ET on the macmini and upserts post-close NAV rows into `xenon.nav_history` with `source='close'`."

2. **Prerequisites.**
   - macmini checkout at `~/projects/xenon`.
   - `.env` contains `IB_FLEX_TOKEN`, `IB_FLEX_NAV_QUERY_ID`, `XENON_LIVE_ACCOUNT`, and a `DATABASE_URL` pointing at `core_dev` via the `xenon_prod` role.
   - Saved IB Flex query (`IB_FLEX_NAV_QUERY_ID`) is configured with a period that includes the prior trading day at 17:30 ET. "Last 7 Days" or "Last 30 Days" both work. Avoid "Custom Date Range" with a static endpoint — it goes stale.
   - The saved query may be either CSV or XML format. Our parser auto-detects CSV (current default for query `1529248` as of 2026-06-02). XML support is intentionally not included in the parser — if you change the format at IB, also change the parser.
   - `/var/log/xenon/` exists and is writable by the operator: `sudo mkdir -p /var/log/xenon && sudo chown $(whoami) /var/log/xenon`.

3. **Install steps (bash block).**
   - `PLIST_SRC=~/projects/xenon/scripts/infra/launchd/com.xenon.nav-flex-refresh.plist`
   - `PLIST_DST=~/Library/LaunchAgents/com.xenon.nav-flex-refresh.plist`
   - `sed` two substitutions replacing the literal `__XENON_ROOT__` token with `$HOME/projects/xenon` and `__HOMEBREW_PATH__` with `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`, redirect to `$PLIST_DST`.
   - `plutil -lint "$PLIST_DST"` (expect `OK`).
   - `launchctl bootstrap gui/$(id -u) "$PLIST_DST"`.
   - Verify: `launchctl list | grep com.xenon.nav-flex-refresh` and `launchctl print gui/$(id -u)/com.xenon.nav-flex-refresh | grep -A5 calendar`.

4. **Smoke test.**
   - `launchctl kickstart -k gui/$(id -u)/com.xenon.nav-flex-refresh` then `tail -f /var/log/xenon/nav-flex-refresh.log`.
   - Direct invocation alternative: `cd ~/projects/xenon && ./scripts/infra/nav-flex-refresh.sh`.

5. **Diagnostics table** (Symptom | Check). IB Flex error codes per [official docs](https://www.ibkrguides.com/orgportal/performanceandstatements/flex3error.htm):
   - Job never fires | `launchctl list \| grep nav-flex` — if absent, re-bootstrap
   - FLEX_NOT_CONFIGURED | `.env` missing `IB_FLEX_TOKEN` or `IB_FLEX_NAV_QUERY_ID`
   - `fetch returned None` and ErrorCode=1001 in IB response | Generic "try again shortly"; commonly seen during IB overnight batch (~1–5 AM ET). Just rerun with `kickstart -k` later in the day.
   - `fetch returned None` and ErrorCode=1009 | Server under heavy load — wait + rerun
   - `fetch returned None` and ErrorCode=1018 | Actual rate limit (1 req/s, 10 req/min per token) — wait 1 min
   - `fetch returned None` with no obvious error | Most likely a parser/format drift: the saved query's format changed (CSV ↔ XML) or columns were renamed. Curl the endpoint manually (see § Manual smoke) and inspect first line.
   - Rows tagged `intraday` instead of `close` | `fetch_ib_nav_series` regressed (Task 2 fix missing in branch)
   - Wrong account | `XENON_TRADING_MODE` or `XENON_LIVE_ACCOUNT` not set in `.env`
   - Job runs but no PG rows | wrapper rc=0 but CLI exit=1 — check stderr.log for `returned 0 rows`

   **Manual smoke** (when the CLI is returning `None` and you want to see the raw response):

   ```bash
   set -a; source ~/projects/xenon/.env; set +a
   curl -s -A 'Java' "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest?t=${IB_FLEX_TOKEN}&q=${IB_FLEX_NAV_QUERY_ID}&v=3"
   ```

   Expect `<Status>Success</Status>` + a `<ReferenceCode>`. If you see `<ErrorCode>` instead, that's the real problem code.

6. **PG cross-check** — `psql -h 100.66.147.98 -U xenon_prod core_dev -c "SELECT date, total, source FROM xenon.nav_history WHERE broker='IB' AND account_env='live' AND broker_account='U18007831' ORDER BY date DESC LIMIT 7;"`. Expected: yesterday's row appears with `source='close'`.

7. **Uninstall.** `launchctl bootout gui/$(id -u)/com.xenon.nav-flex-refresh && rm ~/Library/LaunchAgents/com.xenon.nav-flex-refresh.plist`.

8. **Maintenance.**
   - IB Flex tokens expire after 1 year. Update `IB_FLEX_TOKEN` in `~/projects/xenon/.env` on the macmini — no LaunchAgent reload needed (the wrapper re-sources on each fire).
   - If the saved Flex query is rebuilt with a different ID, update `IB_FLEX_NAV_QUERY_ID` in `.env` only.
   - The schedule lives only in the plist; to change the time, edit `StartCalendarInterval` and `launchctl bootout` + `bootstrap` again.

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/nav-flex-refresh.md
git commit -m "docs(runbook): nav-flex-refresh install + ops procedure"
```

---

## Task 6: End-to-end verification + PR

**Background:** All unit tests pass locally. Now exercise the full path: CLI from MacBook against paper mode, then open the PR, then deploy on macmini and smoke-test the schedule.

- [ ] **Step 1: Local CLI smoke test (paper mode, MacBook)**

```bash
cd ~/projects/xenon
XENON_TRADING_MODE=paper uv run xenon-nav-flex-refresh
```

Expected: prints scope (mode=paper, account=DUQ378889), polls Flex Web Service, prints `fetched N NAV rows (source='close' persisted via upsert_nav_sync)`, exits 0.

Verify rows in `core_test`:

```bash
psql -h 100.66.147.98 -U xenon_dev core_test -c "
  SELECT date, total, source FROM xenon.nav_history
   WHERE broker_account='DUQ378889'
   ORDER BY date DESC LIMIT 5;"
```

Expected: rows present with `source='close'`.

> **Note:** If IB returns `<ErrorCode>1001</ErrorCode>` ("Statement could not be generated at this time") the CLI will print `returned None` and exit 1. 1001 is generic — the most common cause is IB's overnight batch window (~1–5 AM ET). Just retry mid-morning. Actual rate-limiting is code 1018 (1 req/s, 10 req/min per token); see Task 5 runbook diagnostics for the full error-code table.

- [ ] **Step 2: Push branch + open PR**

```bash
git push -u origin feat/nav-flex-auto-refresh
gh pr create --title "feat: NAV auto-refresh — Flex CLI + LaunchAgent" \
  --body "$(cat <<'EOF'
## Summary
- Extend `upsert_nav_sync` with optional `source` arg (default behavior unchanged).
- Fix `fetch_ib_nav_series` end-to-end — three stacked bugs surfaced by live verification on 2026-06-02:
  - Wrong URL (legacy `gdcdyn/Universal/servlet/FlexStatementService.SendRequest` → IB returned code 1001 unconditionally). Now uses the documented `ndcdyn/AccountManagement/FlexWebService/SendRequest` + `User-Agent: Java`.
  - Wrong format (saved query at IB returns CSV, parser expected XML → silent failure via `except Exception: return None`). Now `csv.DictReader` with multi-section break.
  - Missing `source='close'` on the upsert.
- New `xenon-nav-flex-refresh` CLI.
- LaunchAgent + shell wrapper for daily 17:30 ET schedule on the macmini.
- Install + ops runbook.

Closes the auto-refresh deferred item from #119. PR #119 used the `--from-csv` path so the broken Web Service path wasn't exercised.

## Test plan
- [ ] `uv run pytest scripts/tests/test_upsert_nav_source.py scripts/tests/test_fetch_ib_nav_series_csv.py scripts/tests/test_nav_flex_refresh_cli.py -v`
- [ ] Local CLI smoke test against paper mode: `XENON_TRADING_MODE=paper uv run xenon-nav-flex-refresh`
- [ ] Live manual curl of the corrected URL succeeds (see Task 5 runbook § Manual smoke).
- [ ] Post-merge: install LaunchAgent on macmini per `docs/runbooks/nav-flex-refresh.md`
- [ ] Confirm `launchctl kickstart -k` triggers a successful run and `xenon.nav_history` has a new `source='close'` row for today
EOF
)"
```

- [ ] **Step 3: Wait for CI, merge**

```bash
gh pr checks --watch
gh pr merge --squash --delete-branch
```

- [ ] **Step 4: Macmini deploy (operator)**

User SSHes to the macmini and follows `docs/runbooks/nav-flex-refresh.md` install steps. Operator-side prereqs:

1. Saved IB Flex NAV query (`1529248`) is configured for a rolling period such as `Last 7 Days` — not `Custom Date Range` with a fixed endpoint that goes stale.
2. Saved query stays on **CSV** format (current setting). The new parser expects the CSV body that starts with `"ClientAccountID","ReportDate",…`. If you switch the saved query to XML the parser will return `None` until the parser is updated to match.

Then:

```bash
ssh macmini
cd ~/projects/xenon
git pull
# Follow runbook install steps...
launchctl kickstart -k gui/$(id -u)/com.xenon.nav-flex-refresh
tail -50 /var/log/xenon/nav-flex-refresh.log
```

Confirm PG side picked up today's row with `source='close'`:

```bash
psql -h 100.66.147.98 -U xenon_prod core_dev -c "
  SELECT date, total, source FROM xenon.nav_history
   WHERE broker='IB' AND account_env='live' AND broker_account='U18007831'
   ORDER BY date DESC LIMIT 3;"
```

Plan complete.

---

## Future work (separate plans)

These were grouped with this work in #119's description but each is its own subsystem and warrants a separate plan:

- **NEXT PLAN — Full historical IB-data port to `core_dev`.** Bundle of trades + cash transactions + transfers backfill. Operator confirmed 2026-06-02 that IB Flex is authoritative, so writing directly to `core_dev` is acceptable.
  - **Trades:** new `historical_fills` table (operator decision locked in 2026-06-02 — separate audit-overlay from `order_fills` to avoid mixing live execution-layer events with backfill data; joinable to `order_fills` on `tradeID` for reconciliation). Parser source: `data/Xenon_Trades.csv` (271 rows already exported).
  - **Cash transactions:** new `cash_flows` table — schema-design open. Source: `CashTransaction` section of the existing IB Flex NAV query `1529248` (already returns it; the current PR-1 backfill ignores section 2). Two transactions visible on live U18007831 — `2025-10-26: +$10,000`, `2026-01-07: +$35,000`.
  - **Transfers / position contributions:** new `transfers` table — captures security-side contributions like ACAT-in events that don't move cash. Two ingest paths, following the [[flex-is-reconciliation-not-history]] rule:
    - _One-shot historical backfill_ (this plan's responsibility) — covers the 2025-11-11 ACAT (stock_value 0→$13,994 with ~zero cash delta — inter-broker transfer, IB buckets it in `IncomingTradeTransfers` not the bare `Transfers` section). Path: either a manual CSV export from IB Account Management with Custom Date Range + both Transfers sections, or a separate one-off Flex query. Ingest via a sibling of `scripts/migrations/_2026_06_02_backfill_nav_from_ib_flex.py`.
    - _Recurring 2-week reconciliation_ — saved query **`1529534` "Transfers"** created 2026-06-02; `.env` will gain `IB_FLEX_ACTIVITY_QUERY_ID=1529534`. Live verify 2026-06-02: SendRequest 200 OK, GetStatement returned 411 bytes (CSV header only, 0 data rows under current 2-week window — confirmed transport works, just no transfer events in the last 14 days). **Operator follow-up before this plan starts:** add the **Incoming/Outgoing Trade Transfers** section to the saved query so _future_ ACATs land via the recurring path. Do NOT widen the period — 2 weeks is intentional per the rolling-reconciliation architecture.
  - **Validation oracle (already proven from PG alone on 2026-06-02):** daily-chained TWR with manually-identified contributions matches IB's published numbers within ~0.7pp — YTD `40.36% vs 41%`, since-inception `33.35% vs 34%`. So the data port + a daily-TWR window query in `xenon.db.queries.portfolio` is sufficient to replace the headline `%` — no need to reproduce IB's exact Modified Dietz internals.
- **`/performance` period selector.** Frontend-only: add a date-range picker (1M / 3M / YTD / All) on the performance page; thread through to the API query param. Independent of the data-port work.
- **Honest `total_return` headline %.** Becomes a small frontend/backend wiring change once `cash_flows` + `transfers` exist — chain daily `(NAV_d − contributions_d) / NAV_{d−1}` over the requested window. Confirmed accurate to ~0.7pp vs IB's published figures (validation above). Deliberately stops short of matching IB's exact server-side TWR byte-for-byte (irreducible per `docs/architecture/performance-reconstruction.md`).
