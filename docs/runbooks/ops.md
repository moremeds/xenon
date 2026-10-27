# Ops Reference

## Log Rotation

Two layers prevent log bloat in `logs/`:

| Layer  | Mechanism                                                  | Config                                             |
| ------ | ---------------------------------------------------------- | -------------------------------------------------- |
| System | `newsyslog` via `/etc/newsyslog.d/xenon.conf`              | 10MB max, 2 bzip2 backups, covers all `logs/*.log` |

## Change a runtime setting

Runtime settings are DB overrides of some env knobs (registry: `src/xenon/runtime_settings.py`; rules: `src/xenon/api/CLAUDE.md` § Runtime settings). They apply without a restart: within about 5 s on the api, about 10 s on the realtime relay and futu-quotes (the relay pulls them from the api; check relay `/status` → `runtime_settings`). If the api is down the relay keeps its last values.

1. Read the current state: `GET /admin/settings` (needs `X-Internal-Token`). Each key shows `env_value`, `db_value`, `effective`, `narrow_only` and `last_change`.
2. Set an override: `PUT /admin/settings/<key>` with `{"value": <json>, "note": "<why>"}`. Safety keys accept only values inside the env value (403 `SETTING_WIDENS_ENV` otherwise). Turning `XENON_AUDIT_CANCEL` on, changing the `real` token of `XENON_FUTU_TRADING`, or changing `XENON_FUTU_TUPLES_LIVE` also needs `"confirm": "<key>"`.
3. Revert to the env value: `DELETE /admin/settings/<key>?note=<why>` (same confirm rule).
4. Emergency stop for new orders: `PUT /admin/settings/orders_paused` `{"value": true}`. Place and modify return 403 `ORDERS_PAUSED`; cancel still works. Resume with `DELETE /admin/settings/orders_paused`.
5. History: `SELECT * FROM xenon.runtime_settings_audit WHERE key = '<key>' ORDER BY id;` (append-only).

Notes: the api refuses writes under `XENON_READ_ONLY=1`. The nightly `core_dev -> core_test` refresh copies the overrides too; safety keys can only restrict, but a copied `orders_paused=true` pauses the dev stack until it is deleted there.

## Daily Flex divergence (V.4)

Runs daily at 07:30 ET from the deployed api image (launchd job
`deploy/jobs/com.xenon.flex-divergence.plist`, cron alternative in
`deploy/jobs/crontab.example`; install and manual-run steps are in the plist
header and `deploy/README.md` § Jobs). It compares the previous New York day's
PG blotter rows against IB Flex output for the api scope
(`XENON_TRADING_MODE` + `XENON_BROKER_ACCOUNT`), writes a row to
`xenon.flex_divergence_runs`, and surfaces the result on
`GET /health.flex_divergence` and the /admin Flex Divergence tile.

Prints `{"skipped": true}` and exits 0 (no row) when `IB_FLEX_TOKEN` /
`IB_FLEX_QUERY_ID` are unset or Flex is unavailable.
