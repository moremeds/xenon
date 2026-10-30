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
MAX_EXPIRATIONS: int = 999  # front-N expirations per tradingClass (999 = full chain)
MIN_DTE: int = 0  # include 0DTE for full chain
STRIKE_PCT_RANGE: float = 9.99  # keep strikes within ±999 % of spot (= all strikes)

# Exchange filter: skip venues that are routing aliases for the same underlying
# contracts. SMART and IBUSOPT return the same conIds as the listed exchange
# (CBOE for SPX/SPXW) but consume extra enumeration time and inflate contract
# counts ~3×. Filtering to listed-only keeps the canonical conId set.
EXCLUDE_VENUES: frozenset[str] = frozenset({"SMART", "IBUSOPT"})

# Snapshot batching — reqMktData(snapshot=True) pacing.
BATCH_SIZE: int = 50  # concurrent market-data lines requested (leaves ≥50 for relay+API)
BATCH_TIMEOUT: float = 8.0  # seconds per batch before moving on

# IB connection
DEFAULT_IB_CLIENT_ID_A: int = 901  # primary (registered in xenon.clients.ib_client)
DEFAULT_IB_CLIENT_ID_B: int = 902  # fallback

# Reconnect
RECONNECT_DELAY_S: float = 30.0  # pause before each reconnect attempt
MAX_RECONNECT_ATTEMPTS: int = 5  # consecutive failures before a long cooldown
# After the reconnect budget is exhausted we do NOT exit the task (it runs
# in-process under the FastAPI lifespan — nothing would restart it). Instead we
# back off for this long, reset the counter, and keep trying so the snapshotter
# self-heals across IB Gateway nightly restarts / 2FA re-auth windows.
RECONNECT_COOLDOWN_S: float = 300.0

# Hard ceiling on a single ticker's snapshot cycle. Guards against a hung IB
# call (qualify / reqSecDefOptParams / reqContractDetails have no internal
# timeout) freezing the whole daemon. Generous: ~10 batches × BATCH_TIMEOUT +
# enumeration overhead.
CYCLE_TIMEOUT_S: float = 7200.0  # 2 h ceiling — allows full SPX chain (~53 min measured)

# Default cadence (seconds between full cycles, read from DB at runtime).
# When the cycle takes longer than this, the next cycle starts immediately.
DEFAULT_CADENCE_S: int = 600

# Extended session window: start X min before RTH open, stop Y min after close.
SESSION_PRE_OPEN_MIN: int = 5
SESSION_POST_CLOSE_MIN: int = 5

# Volume sentinel that IB sends when there is no volume data yet.
IB_NO_VOLUME: float = -1.0
