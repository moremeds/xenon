# Ops Reference

## Log Rotation

Two layers prevent log bloat in `logs/`:

| Layer  | Mechanism                                                  | Config                                             |
| ------ | ---------------------------------------------------------- | -------------------------------------------------- |
| Python | `RotatingFileHandler` in `src/xenon/monitor_daemon/run.py` | 10MB max, 2 compressed backups                     |
| System | `newsyslog` via `/etc/newsyslog.d/xenon.conf`              | 10MB max, 2 bzip2 backups, covers all `logs/*.log` |

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
