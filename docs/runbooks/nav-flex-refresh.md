# nav-flex-refresh — Daily IB NAV Auto-Refresh

Polls IB Flex `EquitySummaryByReportDateInBase` daily at 17:30 ET on the macmini and upserts post-close NAV rows into `xenon.nav_history` with `source='close'`. The `/performance` page reads these rows.

**Architecture:** the saved Flex query is a rolling ~2-week reconciliation window — a missed day is absorbed by tomorrow's run. Historical backfill is a separate one-shot path (manual CSV download + `scripts/migrations/_2026_06_02_backfill_nav_from_ib_flex.py`); never widen the saved daily query.

## One-time install (macmini)

### Prerequisites

- macmini has the xenon checkout at `~/projects/xenon`.
- `.env` in that checkout includes: `IB_FLEX_TOKEN`, `IB_FLEX_NAV_QUERY_ID`, `XENON_LIVE_ACCOUNT`, `DATABASE_URL` pointing at `core_dev` via the `xenon_prod` role.
- **Pass-2 T7 — explicit `XENON_TRADING_MODE`.** The macmini `.env` MUST set `XENON_TRADING_MODE=live` explicitly. The wrapper fails fast (exit 2) with `FATAL: XENON_TRADING_MODE not set` if unset — there is intentionally no silent default. Operator paper-mode testing requires the same explicit set in the relevant shell.
- IB Flex query saved with:
  - **Format: XML _or_ CSV** — both work. `fetch_ib_nav_series` sniffs the response body (CSV header `"ClientAccountID"` → CSV branch, presence of `<FlexStatements>` → XML branch).
  - **Period: Last 2 Weeks** (or `Last 7 Days` / `Last 30 Days` — anything rolling). Do NOT use `Custom Date Range` with a fixed endpoint that goes stale.
- `/var/log/xenon/` exists and is writable: `sudo mkdir -p /var/log/xenon && sudo chown $(whoami) /var/log/xenon`.

### Pass-3 A2 — schema migration must apply BEFORE the LaunchAgent fires

The 2026-06-03 schema migration (`2026_06_03_nav_src_pk`) changes `nav_history`'s PK to include `source`. If new application code runs against the old schema, the UPSERT raises `there is no unique or exclusion constraint matching the ON CONFLICT specification`. Apply `alembic upgrade head` before bootstrapping the LaunchAgent for the first time, then verify:

```bash
# Confirm the new PK is in place.
psql -h 100.66.147.98 -U xenon_prod core_dev -c "
  SELECT conname FROM pg_constraint
   WHERE conrelid = 'xenon.nav_history'::regclass AND contype = 'p';"
# Expect: nav_history_pkey

# Confirm columns include 'source'.
psql -h 100.66.147.98 -U xenon_prod core_dev -c "
  SELECT a.attname FROM pg_constraint c
    JOIN pg_attribute a ON a.attnum = ANY(c.conkey) AND a.attrelid = c.conrelid
   WHERE c.conrelid = 'xenon.nav_history'::regclass AND c.contype = 'p'
   ORDER BY array_position(c.conkey, a.attnum);"
# Expect: broker, account_env, broker_account, date, source
```

### Install steps

```bash
PLIST_SRC=~/projects/xenon/scripts/infra/launchd/com.xenon.nav-flex-refresh.plist
PLIST_DST=~/Library/LaunchAgents/com.xenon.nav-flex-refresh.plist

# 1. Substitute placeholders.
sed -e "s|__XENON_ROOT__|$HOME/projects/xenon|g" \
    -e "s|__HOMEBREW_PATH__|/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin|g" \
    "$PLIST_SRC" > "$PLIST_DST"

# 2. Validate.
plutil -lint "$PLIST_DST"

# 3. Load (modern launchctl syntax).
launchctl bootstrap gui/$(id -u) "$PLIST_DST"

# 4. Verify.
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

| Symptom                                                                             | Check                                                                                                            |
| ----------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Job never fires                                                                     | `launchctl list \| grep nav-flex` — if absent, re-bootstrap                                                      |
| `FATAL: XENON_TRADING_MODE not set` (exit 2)                                        | **Pass-2 T7** — `.env` missing the explicit mode. Set `XENON_TRADING_MODE=live`.                                 |
| `FATAL: invalid XENON_TRADING_MODE=...` (exit 2)                                    | `.env` set to a non-canonical value. Must be `live` or `paper`.                                                  |
| `FLEX_NOT_CONFIGURED` (exit 2)                                                      | `.env` missing `IB_FLEX_TOKEN` or `IB_FLEX_NAV_QUERY_ID`.                                                        |
| `READ_ONLY: XENON_READ_ONLY=1 — refusing` (exit 3)                                  | `XENON_READ_ONLY=1` exported in the launchd context. Should not happen on prod — unset it.                       |
| `fetch returned None`                                                               | IB Flex throttle (rate code 1018) — wait 30+ min, then `kickstart -k`.                                           |
| `there is no unique or exclusion constraint matching the ON CONFLICT specification` | **Pass-3 A1** — schema migration didn't run before code deployed. Apply `alembic upgrade head`, then re-fire.    |
| Rows tagged `intraday` (not `close`)                                                | Regression of `fetch_ib_nav_series` source kwarg — surface via `xenon-nav-reconcile`.                            |
| Both intraday + close present, NAVs diverge                                         | Run `xenon-nav-reconcile --since YYYY-MM-DD --until YYYY-MM-DD` — non-zero exit means discrepancies > tolerance. |
| Wrong account                                                                       | `XENON_TRADING_MODE` or `XENON_LIVE_ACCOUNT` not set in `.env`.                                                  |
| Job runs but no PG rows                                                             | Wrapper rc=0 but CLI exit=1 — check stderr.log for `returned 0 rows`.                                            |

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

- **Pass-1 callout:** tail `/var/log/xenon/nav-flex-refresh.log` daily for the first week after install.
- IB Flex tokens expire after 1 year. Update `IB_FLEX_TOKEN` in `~/projects/xenon/.env` on the macmini — no LaunchAgent reload needed (the wrapper re-sources on each fire).
- If the saved Flex query is rebuilt with a different ID, update `IB_FLEX_NAV_QUERY_ID` in `.env` only.
- The schedule lives only in the plist; to change the time, edit `StartCalendarInterval` and `launchctl bootout` + `bootstrap` again.
- **Pass-3 A5 — cache flush.** After applying any `nav_history` schema migration, restart `xenon-api` so the in-process `perf_cache` repopulates against the new schema (next cache TTL would rebuild automatically; the restart shortens the staleness window to zero).

## Scope

- **IB-only.** FUTU reconciliation is deferred — Futu OpenD has no programmatic post-close NAV endpoint, and synthesizing one from positions + cash flows reconciles against itself (theater). A future plan adds a daily-statement PDF parser. FUTU cash-flow audit today is via `xenon.futu_cash_flow` row-level ingestion (PR #120).
- Use `xenon-nav-reconcile` (this PR's Task 13) to compare `source='intraday'` vs `source='close'` per date for the configured scope — non-zero exit indicates a discrepancy beyond tolerance.
