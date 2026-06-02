# nav-flex-refresh — Daily IB NAV Auto-Refresh

Polls IB Flex `EquitySummaryByReportDateInBase` daily at 17:30 ET on the macmini and upserts post-close NAV rows into `xenon.nav_history` with `source='close'`. The `/performance` page reads these rows.

**Architecture:** the saved Flex query is a rolling ~2-week reconciliation window — a missed day is absorbed by tomorrow's run. Historical backfill is a separate one-shot path (manual CSV download + `scripts/migrations/_2026_06_02_backfill_nav_from_ib_flex.py`), never widen the saved daily query.

## One-time install (macmini)

Prerequisites:

- macmini has the xenon checkout at `~/projects/xenon`.
- `.env` in that checkout includes: `IB_FLEX_TOKEN`, `IB_FLEX_NAV_QUERY_ID`, `XENON_LIVE_ACCOUNT`, `DATABASE_URL` pointing at `core_dev` via the `xenon_prod` role.
- IB Flex query saved with:
  - **Format: XML _or_ CSV** — both work. `fetch_ib_nav_series` hits the `ndcdyn/AccountManagement/FlexWebService/*` endpoint and sniffs the response body (CSV header `"ClientAccountID"` → CSV branch, presence of `<FlexStatements>` → XML branch). The current saved query `1529248` is CSV; future queries can pick either format.
  - **Period: Last 2 Weeks** (or `Last 7 Days` / `Last 30 Days` — anything rolling). Do NOT use `Custom Date Range` with a fixed endpoint that goes stale.
  - Sections to include: at minimum `EquitySummaryInBase`. Additional sections (CashTransactions, etc.) are ignored by `fetch_ib_nav_series` — the parser stops at the next-section header row.
  - Update in IB Account Management → Reports → Flex Queries → Edit.
- `/var/log/xenon/` exists and is writable: `sudo mkdir -p /var/log/xenon && sudo chown $(whoami) /var/log/xenon`.

Install steps:

```bash
PLIST_SRC=~/projects/xenon/scripts/infra/launchd/com.xenon.nav-flex-refresh.plist
PLIST_DST=~/Library/LaunchAgents/com.xenon.nav-flex-refresh.plist

# 1. Substitute placeholders
sed -e "s|__XENON_ROOT__|$HOME/projects/xenon|g" \
    -e "s|__HOMEBREW_PATH__|/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin|g" \
    "$PLIST_SRC" > "$PLIST_DST"

# 2. Validate
plutil -lint "$PLIST_DST"

# 3. Load (modern launchctl syntax)
launchctl bootstrap gui/$(id -u) "$PLIST_DST"

# 4. Verify
launchctl list | grep com.xenon.nav-flex-refresh
launchctl print gui/$(id -u)/com.xenon.nav-flex-refresh | grep -A5 calendar
```

## Smoke test (immediate trigger)

```bash
launchctl kickstart -k gui/$(id -u)/com.xenon.nav-flex-refresh
tail -f /var/log/xenon/nav-flex-refresh.log
```

Or run the wrapper directly (no launchd):

```bash
cd ~/projects/xenon
./scripts/infra/nav-flex-refresh.sh
```

## Diagnostics

| Symptom                 | Check                                                                 |
| ----------------------- | --------------------------------------------------------------------- |
| Job never fires         | `launchctl list \| grep nav-flex` — if absent, re-bootstrap           |
| `FLEX_NOT_CONFIGURED`   | `.env` missing `IB_FLEX_TOKEN` or `IB_FLEX_NAV_QUERY_ID`              |
| `fetch returned None`   | IB Flex throttle (rate code 1001) — wait 30+ min, then `kickstart -k` |
| Rows tagged `intraday`  | `fetch_ib_nav_series` regressed (PR-1 Task 2 fix missing)             |
| Wrong account           | `XENON_TRADING_MODE` or `XENON_LIVE_ACCOUNT` not set in `.env`        |
| Job runs but no PG rows | wrapper rc=0 but CLI exit=1 — check stderr.log for `returned 0 rows`  |

Cross-check the PG side:

```bash
psql -h 100.66.147.98 -U xenon_prod core_dev -c "
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

- IB Flex tokens expire after 1 year. Update `IB_FLEX_TOKEN` in `~/projects/xenon/.env` on the macmini — no LaunchAgent reload needed (the wrapper re-sources on each fire).
- If the saved Flex query is rebuilt with a different ID, update `IB_FLEX_NAV_QUERY_ID` in `.env` only.
- The schedule lives only in the plist; to change the time, edit `StartCalendarInterval` and `launchctl bootout` + `bootstrap` again.
- **Do not widen the saved query's period to chase a historical event.** That's the one-shot backfill's job — see `scripts/migrations/_2026_06_02_backfill_nav_from_ib_flex.py`.
