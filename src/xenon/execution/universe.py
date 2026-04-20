"""V1 trading universe registry.

Single source of truth for which tickers are tradeable and their
option contract metadata. Frontend mirror at `web/lib/universe.ts`
is generated from this module by
`scripts/infra/dev/generate_universe_ts.py` — do not hand-edit the TS.

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §2
"""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class UniverseEntry:
    ticker: str
    type: str  # "INDEX" | "ETF"
    is_index: bool
    cash_settled: bool
    multiplier: int
    k1: bool  # K-1 tax treatment (USO)


def _make_entry(
    ticker: str,
    *,
    asset_type: str,
    is_index: bool,
    cash_settled: bool,
    multiplier: int = 100,
    k1: bool = False,
) -> UniverseEntry:
    return UniverseEntry(
        ticker=ticker,
        type=asset_type,
        is_index=is_index,
        cash_settled=cash_settled,
        multiplier=multiplier,
        k1=k1,
    )


_RAW: dict[str, UniverseEntry] = {
    "SPX": _make_entry("SPX", asset_type="INDEX", is_index=True, cash_settled=True),
    "NDX": _make_entry("NDX", asset_type="INDEX", is_index=True, cash_settled=True),
    "RUT": _make_entry("RUT", asset_type="INDEX", is_index=True, cash_settled=True),
    "SPY": _make_entry("SPY", asset_type="ETF", is_index=False, cash_settled=False),
    "QQQ": _make_entry("QQQ", asset_type="ETF", is_index=False, cash_settled=False),
    "IWM": _make_entry("IWM", asset_type="ETF", is_index=False, cash_settled=False),
    "GLD": _make_entry("GLD", asset_type="ETF", is_index=False, cash_settled=False),
    "USO": _make_entry("USO", asset_type="ETF", is_index=False, cash_settled=False, k1=True),
    "SIL": _make_entry("SIL", asset_type="ETF", is_index=False, cash_settled=False),
}

# Read-only public view.
UNIVERSE: MappingProxyType[str, UniverseEntry] = MappingProxyType(_RAW)

INDEX_UNIVERSE: frozenset[str] = frozenset(t for t, e in UNIVERSE.items() if e.is_index)


def is_known(ticker: str) -> bool:
    """True if ticker is in the V1 universe."""
    return ticker in UNIVERSE


def is_index(ticker: str) -> bool:
    """True if ticker is an index (cash-settled, no stock leg).

    Raises KeyError if ticker not in universe.
    """
    return UNIVERSE[ticker].is_index


def get_multiplier(ticker: str) -> int:
    """Option contract multiplier for the ticker.

    Raises KeyError if ticker not in universe.
    """
    return UNIVERSE[ticker].multiplier
