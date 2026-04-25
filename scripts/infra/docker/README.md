# Archived Docker / VPS runners

These scripts are the legacy launchers for two paths that are no longer the active dev story:

- `cloud.sh` — runs local services against the Hetzner VPS IB Gateway over Tailscale.
- `local.sh` — stops the VPS gateway and starts a local Docker IB Gateway.

The active dev launcher is `scripts/infra/dev.sh`, which talks to a native IB Gateway
(launchd) on the same machine and derives the port from `XENON_TRADING_MODE`.

Use the scripts in this directory only when you need the Docker or VPS fallback —
e.g. CI on a host without a native Gateway, or a temporary VPS-backed shift. They have
not been updated for the `XENON_TRADING_MODE` switch; if you use them, set
`XENON_TRADING_MODE` in `.env` to match the Gateway's `TRADING_MODE`.
