from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from functools import reduce
from math import gcd

from .combo_quotes import compute_combo_quote
from .models import ComboLegQuote, ComboLegSpec, ComboPlan


def _same_expiry(legs: list[ComboLegSpec]) -> bool:
    return len({leg.expiry for leg in legs}) == 1


def _detect_vertical(legs: list[ComboLegSpec]) -> str | None:
    if len(legs) != 2 or not _same_expiry(legs):
        return None

    buys = [leg for leg in legs if leg.action == "BUY"]
    sells = [leg for leg in legs if leg.action == "SELL"]
    if len(buys) != 1 or len(sells) != 1:
        return None

    buy_leg = buys[0]
    sell_leg = sells[0]
    if buy_leg.right != sell_leg.right:
        return None
    if buy_leg.quantity != sell_leg.quantity:
        return None

    if buy_leg.right == "C":
        return "Bull Call Spread" if buy_leg.strike < sell_leg.strike else "Bear Call Spread"
    return "Bear Put Spread" if buy_leg.strike > sell_leg.strike else "Bull Put Spread"


def _detect_iron_structure(legs: list[ComboLegSpec]) -> str | None:
    if len(legs) != 4 or not _same_expiry(legs):
        return None

    puts = sorted([leg for leg in legs if leg.right == "P"], key=lambda leg: leg.strike)
    calls = sorted([leg for leg in legs if leg.right == "C"], key=lambda leg: leg.strike)
    if len(puts) != 2 or len(calls) != 2:
        return None
    if len({leg.quantity for leg in legs}) != 1:
        return None

    if (
        puts[0].action == "BUY"
        and puts[1].action == "SELL"
        and calls[0].action == "SELL"
        and calls[1].action == "BUY"
    ):
        return "Iron Butterfly" if puts[1].strike == calls[0].strike else "Long Iron Condor"
    return None


def _detect_butterfly(legs: list[ComboLegSpec]) -> str | None:
    if not legs or not _same_expiry(legs):
        return None
    if {leg.right for leg in legs} not in ({"C"}, {"P"}):
        return None

    aggregated: dict[Decimal, dict[str, int]] = defaultdict(lambda: {"BUY": 0, "SELL": 0})
    for leg in legs:
        aggregated[leg.strike][leg.action] += leg.quantity

    counts = [count for sides in aggregated.values() for count in sides.values() if count > 0]
    scale = reduce(gcd, counts) if counts else 1
    normalized = {
        strike: {
            "BUY": sides["BUY"] // scale,
            "SELL": sides["SELL"] // scale,
        }
        for strike, sides in aggregated.items()
    }

    strikes = sorted(normalized)
    if len(strikes) != 3:
        return None

    low, mid, high = strikes
    is_long_butterfly = (
        normalized[low]["BUY"] == 1
        and normalized[low]["SELL"] == 0
        and normalized[mid]["BUY"] == 0
        and normalized[mid]["SELL"] == 2
        and normalized[high]["BUY"] == 1
        and normalized[high]["SELL"] == 0
    )
    if not is_long_butterfly:
        return None

    right = next(iter({leg.right for leg in legs}))
    return "Long Call Butterfly" if right == "C" else "Long Put Butterfly"


def detect_supported_structure(legs: list[ComboLegSpec]) -> str:
    structure = _detect_vertical(legs) or _detect_iron_structure(legs) or _detect_butterfly(legs)
    if structure is None:
        raise ValueError("Unsupported combo wizard structure")
    return structure


def _ladder_step(ticker: str, quote_width: Decimal) -> Decimal:
    index_symbols = {"SPX", "SPXW", "XSP", "RUT", "NDX"}
    if ticker.upper() in index_symbols:
        return Decimal("0.10") if quote_width >= Decimal("0.10") else Decimal("0.05")
    return Decimal("0.05") if quote_width >= Decimal("0.10") else Decimal("0.02")


def _price_polarity(signed_natural_price: Decimal) -> str:
    if signed_natural_price < 0:
        return "CREDIT"
    if signed_natural_price > 0:
        return "DEBIT"
    return "FLAT"


def build_plan(
    *,
    ticker: str,
    legs: list[ComboLegSpec],
    quotes: dict[str, ComboLegQuote],
) -> ComboPlan:
    structure_name = detect_supported_structure(legs)
    quote = compute_combo_quote(legs, quotes)
    return ComboPlan(
        structure_name=structure_name,
        natural_price=quote.ask,
        mid_price=quote.mid,
        signed_natural_price=quote.net_ask,
        signed_mid_price=quote.signed_mid,
        price_polarity=_price_polarity(quote.net_ask),
        ladder_step=_ladder_step(ticker, quote.ask - quote.bid),
        quote=quote,
    )
