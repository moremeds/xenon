# Ops Reference

## Log Rotation

Two layers prevent log bloat in `logs/`:

| Layer  | Mechanism                                                  | Config                                             |
| ------ | ---------------------------------------------------------- | -------------------------------------------------- |
| System | `newsyslog` via `/etc/newsyslog.d/xenon.conf`              | 10MB max, 2 bzip2 backups, covers all `logs/*.log` |

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
