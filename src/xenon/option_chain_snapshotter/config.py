"""Static constants for the option chain snapshotter daemon."""

from __future__ import annotations

# Index → qualifying exchange (verified live 2026-06-02; see spike comments).
INDEX_EXCHANGE: dict[str, str] = {
    "SPX": "CBOE",
    "NDX": "NASDAQ",
    "RUT": "RUSSELL",
    "VIX": "CBOE",
}

TICKERS: tuple[str, ...] = ("SPX", "NDX", "RUT", "VIX")

# Universe scoping — controls the contract enumeration step.
MAX_EXPIRATIONS: int = 6  # front-N expirations per tradingClass
MIN_DTE: int = 1  # skip same-day expiries (0DTE) by default
STRIKE_PCT_RANGE: float = 0.20  # keep strikes within ±20 % of spot

# Snapshot batching — reqMktData(snapshot=True) pacing.
BATCH_SIZE: int = 50  # concurrent market-data lines requested (leaves ≥50 for relay+API)
BATCH_TIMEOUT: float = 8.0  # seconds per batch before moving on

# IB connection
DEFAULT_IB_CLIENT_ID_A: int = 901  # primary (registered in xenon.clients.ib_client)
DEFAULT_IB_CLIENT_ID_B: int = 902  # fallback

# Reconnect
RECONNECT_DELAY_S: float = 30.0  # pause before each reconnect attempt
MAX_RECONNECT_ATTEMPTS: int = 5  # failures before daemon exits (Docker restarts it)

# Default cadence (seconds between full cycles, read from DB at runtime).
# When the cycle takes longer than this, the next cycle starts immediately.
DEFAULT_CADENCE_S: int = 600

# Extended session window: start X min before RTH open, stop Y min after close.
SESSION_PRE_OPEN_MIN: int = 5
SESSION_POST_CLOSE_MIN: int = 5

# Volume sentinel that IB sends when there is no volume data yet.
IB_NO_VOLUME: float = -1.0
