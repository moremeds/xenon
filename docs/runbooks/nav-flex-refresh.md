# nav-flex-refresh — Daily IB NAV Auto-Refresh

Polls IB Flex `EquitySummaryByReportDateInBase` daily at 17:30 ET on the macmini and upserts post-close NAV rows into `xenon.nav_history` with `source='close'`. The `/performance` page reads these rows.

**Architecture:** the saved Flex query is a rolling ~2-week reconciliation window — a missed day is absorbed by tomorrow's run. Historical backfill is a separate one-shot path (manual CSV download + `scripts/migrations/_2026_06_02_backfill_nav_from_ib_flex.py`, see "Historical backfill" below); never widen the saved daily query.

## One-time install (macmini)

### Prerequisites

- The job runs from the api image (no source checkout on the macmini). The api `env_file` (`/opt/xenon/.env`, `web.env`) includes: `IB_FLEX_TOKEN`, `IB_FLEX_NAV_QUERY_ID`, `XENON_LIVE_ACCOUNT`, `DATABASE_URL` pointing at `core_dev` via the `xenon_app` role.
- **Pass-2 T7 — explicit `XENON_TRADING_MODE`.** The api `env_file` MUST set `XENON_TRADING_MODE=live` explicitly. There is intentionally no silent default; the account is derived from the mode (`src/xenon/jobs/nav_flex_refresh.py`). Operator paper-mode testing requires the same explicit set in the relevant shell.
- IB Flex query saved with:
  - **Format: XML _or_ CSV** — both work. `fetch_ib_nav_series` sniffs the response body (CSV header `"ClientAccountID"` → CSV branch, presence of `<FlexStatements>` → XML branch).
  - **Period: Last 2 Weeks** (or `Last 7 Days` / `Last 30 Days` — anything rolling). Do NOT use `Custom Date Range` with a fixed endpoint that goes stale.
- `/var/log/xenon/` exists and is writable: `sudo mkdir -p /var/log/xenon && sudo chown $(whoami) /var/log/xenon`.

### Pass-3 A2 — schema migration must apply BEFORE the LaunchAgent fires

The 2026-06-03 schema migration (`2026_06_03_nav_src_pk`) changes `nav_history`'s PK to include `source`. If new application code runs against the old schema, the UPSERT raises `there is no unique or exclusion constraint matching the ON CONFLICT specification`. Apply `alembic upgrade head` before bootstrapping the LaunchAgent for the first time, then verify:

```bash
# Confirm the new PK is in place.
psql -h 100.66.147.98 -U xenon_app core_dev -c "
  SELECT conname FROM pg_constraint
   WHERE conrelid = 'xenon.nav_history'::regclass AND contype = 'p';"
# Expect: nav_history_pkey

# Confirm columns include 'source'.
psql -h 100.66.147.98 -U xenon_app core_dev -c "
  SELECT a.attname FROM pg_constraint c
    JOIN pg_attribute a ON a.attnum = ANY(c.conkey) AND a.attrelid = c.conrelid
   WHERE c.conrelid = 'xenon.nav_history'::regclass AND c.contype = 'p'
   ORDER BY array_position(c.conkey, a.attnum);"
# Expect: broker, account_env, broker_account, date, source
```

### Install steps

The scheduled entry lives in `deploy/jobs/com.xenon.nav-flex-refresh.plist` (launchd) or `deploy/jobs/crontab.example`. Follow `deploy/README.md` (Jobs table and operator rollout checklist). It runs `xenon-nav-flex-refresh` inside the api image through `docker-compose run --rm`.

## Smoke test (manual run and evidence)

`launchctl kickstart` is only a schedule-path check, NOT a job test: outside 17:00-17:59 America/New_York the plist guard exits 0 before Docker starts and nothing runs. Do not use it as evidence that the job works.

To run the job now (REAL WRITES), use the canonical unguarded command from `deploy/README.md` section F, on the macmini with `export PATH=/opt/homebrew/bin:$PATH`. Read section E first. The fixed container name is the overlap guard, so keep `--name`. `docker rm` has no `-f`, so a running job is never touched and still blocks a second run:

```bash
cd /opt/xenon && docker rm xenon-job-nav-flex-refresh 2>/dev/null; docker-compose -f /opt/xenon/compose.yml run --rm -T --no-deps --name xenon-job-nav-flex-refresh --label com.centurylinklabs.watchtower.enable=false api xenon-nav-flex-refresh
```

Evidence: output lines `xenon-nav-flex-refresh: mode=live account=...` and `fetched N NAV rows`, exit code 0, and no `PG persist failed` line. The CLI prints "fetched N NAV rows" even when the DB write failed, so also check the persisted rows (see "Cross-check the PG side" below and `deploy/README.md` section F). An empty result for the scope is a failure even if the CLI exited 0. If the name conflict error appears, follow `deploy/README.md` section I.

## Diagnostics

| Symptom                                                                             | Check                                                                                                            |
| ----------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Job never fires                                                                     | `launchctl list \| grep nav-flex` — if absent, re-bootstrap                                                      |
| `FLEX_NOT_CONFIGURED` (exit 2)                                                      | `.env` missing `IB_FLEX_TOKEN` or `IB_FLEX_NAV_QUERY_ID`.                                                        |
| `READ_ONLY: XENON_READ_ONLY=1 — refusing` (exit 3)                                  | `XENON_READ_ONLY=1` exported in the launchd context. Should not happen on prod — unset it.                       |
| `fetch returned None`                                                               | IB Flex throttle (rate code 1018) — wait 30+ min, then re-run the job.                                           |
| `there is no unique or exclusion constraint matching the ON CONFLICT specification` | **Pass-3 A1** — schema migration didn't run before code deployed. Apply `alembic upgrade head`, then re-fire.    |
| Rows tagged `intraday` (not `close`)                                                | Regression of `fetch_ib_nav_series` source kwarg — surface via `xenon-nav-reconcile`.                            |
| Both intraday + close present, NAVs diverge                                         | Run `xenon-nav-reconcile --since YYYY-MM-DD --until YYYY-MM-DD` — non-zero exit means discrepancies > tolerance. |
| Wrong account                                                                       | `XENON_TRADING_MODE` or `XENON_LIVE_ACCOUNT` not set in `.env`.                                                  |
| Job runs but no PG rows                                                             | Wrapper rc=0 but CLI exit=1 — check stderr.log for `returned 0 rows`.                                            |

Cross-check the PG side:

```bash
psql -h 100.66.147.98 -U xenon_app core_dev -c "
SELECT date, total, source
  FROM xenon.nav_history
 WHERE broker='IB' AND account_env='live' AND broker_account='U18007831'
 ORDER BY date DESC LIMIT 7;"
```

Expected: yesterday's row appears with `source='close'`.

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.xenon.nav-flex-refresh
rm ~/Library/LaunchAgents/com.xenon.nav-flex-refresh.plist
```

## Maintenance

- **Pass-1 callout:** tail `/var/log/xenon/nav-flex-refresh.stdout.log` and `.stderr.log` daily for the first week after install.
- IB Flex tokens expire after 1 year. Update `IB_FLEX_TOKEN` in `/opt/xenon/.env` on the macmini; the next `docker-compose run` reads it (no reload needed).
- If the saved Flex query is rebuilt with a different ID, update `IB_FLEX_NAV_QUERY_ID` in `/opt/xenon/.env` only.
- The schedule lives in `deploy/jobs/` (plist `StartCalendarInterval` or crontab). To change the time, edit it there, then `launchctl bootout` + `bootstrap` again (see `deploy/README.md`).
- **Pass-3 A5 — cache flush.** After applying any `nav_history` schema migration, restart `xenon-api` so the in-process `perf_cache` repopulates against the new schema (next cache TTL would rebuild automatically; the restart shortens the staleness window to zero).

## Historical backfill (one-shot, manual CSV)

The daily job only ever sees the rolling window. To load inception-to-date NAV rows (`source='close'`) **and** historical `Deposits/Withdrawals` cash flows into `xenon.ib_cash_flow` (required for flow-adjusted TWR/IRR on `/performance`), run the backfill script once:

1. **Export the CSV from the IB portal.** Account Management → Reports → Flex Queries → create (or clone) a *Custom Date Range* query covering inception-to-date that includes **both** `EquitySummaryInBase` (or `EquitySummaryByReportDateInBase`) and `CashTransactions` (with the `TransactionID`, `Type`, `Date/Time`, `Amount`, `CurrencyPrimary` columns). Run it and save the CSV.
2. **Dry-run first** (parses, prints row counts + NAV date range, writes nothing):

   ```bash
   cd /opt/xenon && docker-compose -f /opt/xenon/compose.yml run --rm -T --no-deps \
     -v /path/to/flex.csv:/tmp/flex.csv api \
     python scripts/migrations/_2026_06_02_backfill_nav_from_ib_flex.py \
       --from-csv /tmp/flex.csv --dry-run
   ```

   Expect `nav_rows` ≈ trading days since funding, `cash_flow_rows` = deposit/withdrawal count, and `earliest_date`/`latest_date` matching the export window. On a MacBook checkout: `uv run python scripts/migrations/_2026_06_02_backfill_nav_from_ib_flex.py --from-csv <path> --dry-run`.
3. **Real run — operator step, requires approval.** Drop `--dry-run` and run the same command against the intended DB (`DATABASE_URL` decides). Against prod (`core_dev`) this writes real history rows: get explicit sign-off, run on the macmini stack, and confirm `XENON_READ_ONLY` is unset (the script exits 3 when it is). Rows are upserts on `(broker, account_env, broker_account, date, source)` / `transaction_id`, so a re-run is idempotent.
4. **Verify:**

   ```bash
   psql -h 100.66.147.98 -U xenon_app core_dev -c \
     "SELECT count(*), min(date), max(date) FROM xenon.nav_history WHERE source='close';
      SELECT count(*), min(occurred_at::date), max(occurred_at::date) FROM xenon.ib_cash_flow;"
   ```

   Then re-check `/performance` — `twr_total_return` / `irr_total_return` populate once flows exist.

## Scope

- **IB-only.** FUTU reconciliation is deferred — Futu OpenD has no programmatic post-close NAV endpoint, and synthesizing one from positions + cash flows reconciles against itself (theater). A future plan adds a daily-statement PDF parser. FUTU cash-flow audit today is via `xenon.futu_cash_flow` row-level ingestion (PR #120).
- Use `xenon-nav-reconcile` (this PR's Task 13) to compare `source='intraday'` vs `source='close'` per date for the configured scope — non-zero exit indicates a discrepancy beyond tolerance.
