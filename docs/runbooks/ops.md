# Ops Reference

## Log Rotation

Two layers prevent log bloat in `logs/`:

| Layer  | Mechanism                                                  | Config                                             |
| ------ | ---------------------------------------------------------- | -------------------------------------------------- |
| System | `newsyslog` via `/etc/newsyslog.d/xenon.conf`              | 10MB max, 2 bzip2 backups, covers all `logs/*.log` |

## Change a runtime setting

Runtime settings are DB overrides of some env knobs (registry: `src/xenon/runtime_settings.py`; rules: `src/xenon/api/CLAUDE.md` § Runtime settings). They apply without a restart, within about 5 s on the api.

1. Read the current state: `GET /admin/settings` (needs `X-Internal-Token`). Each key shows `env_value`, `db_value`, `effective`, `narrow_only` and `last_change`.
2. Set an override: `PUT /admin/settings/<key>` with `{"value": <json>, "note": "<why>"}`. Safety keys accept only values inside the env value (403 `SETTING_WIDENS_ENV` otherwise). Turning `XENON_AUDIT_CANCEL` on, changing the `real` token of `XENON_FUTU_TRADING`, or changing `XENON_FUTU_TUPLES_LIVE` also needs `"confirm": "<key>"`.
3. Revert to the env value: `DELETE /admin/settings/<key>?note=<why>` (same confirm rule).
4. Emergency stop for new orders: `PUT /admin/settings/orders_paused` `{"value": true}`. Place and modify return 403 `ORDERS_PAUSED`; cancel still works. Resume with `DELETE /admin/settings/orders_paused`.
5. History: `SELECT * FROM xenon.runtime_settings_audit WHERE key = '<key>' ORDER BY id;` (append-only).

Notes: the api refuses writes under `XENON_READ_ONLY=1`. The nightly `core_dev -> core_test` refresh copies the overrides too; safety keys can only restrict, but a copied `orders_paused=true` pauses the dev stack until it is deleted there.

## Nightly Flex divergence (V.4)

Runs once per day after 18:00 ET on weekdays. Compares yesterday's PG blotter
rows against IB Flex same-day output, writes a row to
`xenon.flex_divergence_runs`, and surfaces the result on
`GET /health.flex_divergence`.

```cron
0 23 * * 1-5  cd /opt/xenon && XENON_TRADING_MODE=live XENON_BROKER_ACCOUNT=$LIVE_ACCT \
              uv run python -m xenon.jobs.flex_divergence_check --apply
```

Skipped silently when `IB_FLEX_TOKEN` / `IB_FLEX_QUERY_ID` are unset.
