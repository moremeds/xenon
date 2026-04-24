from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .models import ComboLegQuote, ComboLegSpec, ComboQuote

_CENT = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _resolve_leg_prices(
    leg: ComboLegSpec,
    quotes: dict[str, ComboLegQuote],
) -> tuple[Decimal, Decimal]:
    quote = quotes.get(leg.contract_id)
    use_live_quote = quote is not None and not leg.price_manually_set
    bid = quote.bid if use_live_quote else leg.limit_price
    ask = quote.ask if use_live_quote else leg.limit_price
    if bid is None or ask is None:
        raise ValueError(f"Missing bid/ask for combo leg {leg.contract_id}")
    return Decimal(bid), Decimal(ask)


def compute_combo_quote(
    legs: list[ComboLegSpec],
    quotes: dict[str, ComboLegQuote],
) -> ComboQuote:
    if not legs:
        raise ValueError("At least one combo leg is required")

    net_ask = Decimal("0")
    net_bid = Decimal("0")
    for leg in legs:
        bid, ask = _resolve_leg_prices(leg, quotes)
        quantity = Decimal(leg.quantity)
        if leg.action == "BUY":
            net_ask += ask * quantity
            net_bid += bid * quantity
        else:
            net_ask -= bid * quantity
            net_bid -= ask * quantity

    normalized_bid = min(abs(net_bid), abs(net_ask))
    normalized_ask = max(abs(net_bid), abs(net_ask))
    mid = (normalized_bid + normalized_ask) / Decimal("2")
    signed_mid = (net_bid + net_ask) / Decimal("2")

    return ComboQuote(
        net_bid=_q(net_bid),
        net_ask=_q(net_ask),
        signed_mid=_q(signed_mid),
        bid=_q(normalized_bid),
        ask=_q(normalized_ask),
        mid=_q(mid),
    )
