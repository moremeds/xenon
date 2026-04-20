# Ops Reference

## Log Rotation

Two layers prevent log bloat in `logs/`:

| Layer  | Mechanism                                                  | Config                                             |
| ------ | ---------------------------------------------------------- | -------------------------------------------------- |
| Python | `RotatingFileHandler` in `src/xenon/monitor_daemon/run.py` | 10MB max, 2 compressed backups                     |
| System | `newsyslog` via `/etc/newsyslog.d/xenon.conf`              | 10MB max, 2 bzip2 backups, covers all `logs/*.log` |
