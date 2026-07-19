"""Option-chain snapshotter — periodic SPX/VIX chain captures into archive DB.

Scope (v1): SPX + VIX ATM-call snapshots every 10 min during NYSE RTH.
Persists into the `option_chain` Postgres DB (TimescaleDB-backed).
Promoted from `scripts/spike/option_chain_minimal.py` (PR #125) with:
  - TICKERS restricted to (SPX, VIX) — was 4 in the spike
  - NYSE RTH gating via exchange-calendars (skips cleanly off-hours)
  - Default host = 127.0.0.1 (host-native IB Gateway)

Future scope (PR 4-10 of the IMPL plan): full chain enumeration, IB line
budget pacing, OHLCV worker, daily universe refresh. Not built here.

Entry point: `xenon-option-chain-snapshot` (registered in pyproject.toml).
Launchd: scripts/infra/launchd/com.xenon.option-chain-snapshotter.plist.
"""
