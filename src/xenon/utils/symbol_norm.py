"""Cross-broker symbol identity normalization (Futu ↔ IB).

Pure functions only — no IB or Futu SDK imports. Safe to run anywhere,
including tests that have neither library installed.

## Why this exists

The same instrument has different identifiers in each broker:

    Instrument        IB                          Futu
    ─────────         ──                          ────
    NVDA stock        NVDA / SMART / USD          US.NVDA
    Berkshire B       BRK B (space)               US.BRK.B (dot)
    SPX index         SPX / CBOE / IND            US.SPX  (v2 only)
    AAPL Jan26 $190C  Contract(...) + tradingClass  US.AAPL260116C190000

Silent mismatches route the wrong live quote to the wrong row or fail
to subscribe. This module is the single choke point.

## Contract

`futu_to_ib(futu_symbol)` always returns a dict. It never returns None
and never raises on unrecognized input — instead it returns
`{"kind": "UNKNOWN", "raw": ..., "reason": ...}`. Callers render UNKNOWN
rows with a "symbol not normalized" badge rather than silently dropping
positions.

`kind` is one of:
  - STK   → stock, goes in the `symbols` quote bucket
  - OPT   → option, goes in the `contracts` bucket. `live_data` is False
            in v1 because IB option subscriptions need a `qualifyContracts()`
            round-trip to resolve `tradingClass`, which is not inferrable
            from the OCC code alone.
  - UNKNOWN → render with badge, no quote subscription

HK/CN markets parse to UNKNOWN in v1 per scope decision.

## Semantic round-trip

`futu_to_ib(ib_to_futu(futu_to_ib(x)))` MUST yield the same normalized
dict as `futu_to_ib(x)` on the quoting fields (`symbol, expiry, strike,
right`). String-perfect round-trip is NOT required — exchange
canonicalization, expiry format, and tradingClass are all lossy.
"""

from __future__ import annotations

import re
from typing import Optional, TypedDict, Union


# ---------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------


class StockResult(TypedDict):
    kind: str  # "STK"
    symbol: str
    exchange: str
    currency: str
    live_data: bool


class OptionResult(TypedDict):
    kind: str  # "OPT"
    symbol: str  # Underlying (IB-style, e.g. "BRK B" not "BRK.B")
    expiry: str  # YYYYMMDD
    strike: float
    right: str  # "C" or "P"
    exchange: str
    currency: str
    trading_class: Optional[str]  # None in v1 (defer qualification)
    live_data: bool  # False in v1


class UnknownResult(TypedDict):
    kind: str  # "UNKNOWN"
    raw: str
    reason: str
    live_data: bool


NormalizedSymbol = Union[StockResult, OptionResult, UnknownResult]


# ---------------------------------------------------------------------
# Parsing — Futu → normalized
# ---------------------------------------------------------------------

# Futu option format: `US.AAPL240119C00190000` — underlying + YYMMDD +
# C|P + strike (integer, ×1000). The strike digit count is not fixed
# (Futu uses 6 in some samples, 8 in others), so allow 4-9.
_OPTION_RE = re.compile(r"^([A-Z][A-Z0-9]*)(\d{6})([CP])(\d{4,9})$")


def futu_to_ib(futu_symbol: str) -> NormalizedSymbol:
    """Parse a Futu code into an IB-facing normalized dict.

    Never raises. Unrecognized inputs return `{"kind": "UNKNOWN", ...}`
    so the caller can render them with a badge.
    """
    if not isinstance(futu_symbol, str) or not futu_symbol:
        return _unknown(str(futu_symbol), "empty or non-string input")

    raw = futu_symbol.strip()

    # Split market prefix. Futu uses `US.`, `HK.`, `SH.`, `SZ.`.
    if "." not in raw:
        return _unknown(raw, "missing market prefix (expected e.g. 'US.')")

    market, rest = raw.split(".", 1)
    market = market.upper()

    if market != "US":
        return _unknown(raw, f"market '{market}' not supported in v1 (US only)")

    if not rest:
        return _unknown(raw, "empty ticker after market prefix")

    # Option vs stock: try the OCC-packed pattern first.
    opt_match = _OPTION_RE.match(rest)
    if opt_match:
        return _parse_option(raw, opt_match)

    return _parse_stock(raw, rest)


def _parse_stock(raw: str, rest: str) -> StockResult:
    # Futu writes BRK.B as `BRK.B` *after* the market prefix, so the
    # market split above already consumed the first dot. We get `BRK.B`
    # as `rest` only if the market prefix is absent, which we've
    # rejected. So `rest` here is either `AAPL`, or `BRK.B` from `US.BRK.B`
    # (where `rsplit("US.", 1)` leaves `BRK.B`). Normalize dots to spaces
    # for IB convention.
    ib_symbol = rest.replace(".", " ")
    return {
        "kind": "STK",
        "symbol": ib_symbol,
        "exchange": "SMART",
        "currency": "USD",
        "live_data": True,
    }


def _parse_option(raw: str, match: re.Match) -> OptionResult:
    underlying = match.group(1)
    yymmdd = match.group(2)
    right = match.group(3)
    strike_int = int(match.group(4))

    yy = int(yymmdd[:2])
    year = 2000 + yy if yy < 80 else 1900 + yy
    expiry = f"{year:04d}{yymmdd[2:4]}{yymmdd[4:6]}"

    # Futu packs strike as int(price * 1000). Scale back.
    strike = strike_int / 1000.0

    return {
        "kind": "OPT",
        "symbol": underlying,
        "expiry": expiry,
        "strike": strike,
        "right": right,
        "exchange": "SMART",
        "currency": "USD",
        "trading_class": None,
        "live_data": False,  # defer IB qualification to v2
    }


def _unknown(raw: str, reason: str) -> UnknownResult:
    return {"kind": "UNKNOWN", "raw": raw, "reason": reason, "live_data": False}


# ---------------------------------------------------------------------
# IB → Futu (inverse) — used by the semantic round-trip test and by
# callers that need to build an OpenD query from an IB-side contract.
# ---------------------------------------------------------------------


def ib_to_futu(info: NormalizedSymbol) -> str:
    """Serialize a normalized dict back into a Futu code.

    Used for round-trip verification and for tooling that needs to
    ask OpenD about a specific IB instrument. Not every normalized
    value round-trips string-identically (dots → spaces is lossy),
    but `futu_to_ib(ib_to_futu(x))` MUST equal `x` on the quoting
    fields.

    Raises ValueError on UNKNOWN — there is no canonical Futu code
    for an unparseable input.
    """
    kind = info.get("kind")
    if kind == "STK":
        sym = info["symbol"].replace(" ", ".")
        return f"US.{sym}"
    if kind == "OPT":
        underlying = info["symbol"]
        expiry = info["expiry"]  # YYYYMMDD
        yymmdd = expiry[2:]
        strike_int = int(round(info["strike"] * 1000))
        # Pad to 8 to match the longer Futu samples; the parser
        # regex accepts 4-9 so either width round-trips fine.
        return f"US.{underlying}{yymmdd}{info['right']}{strike_int:08d}"
    raise ValueError(f"ib_to_futu() cannot serialize kind={kind!r}")
