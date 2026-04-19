"""Shared formatting utilities for trade blotter output."""
from decimal import Decimal


def format_currency(value: Decimal) -> str:
    """Format decimal as currency."""
    if value >= 0:
        return f"${value:,.2f}"
    return f"-${abs(value):,.2f}"


def format_pnl(value: Decimal) -> str:
    """Format P&L with color indicators."""
    formatted = format_currency(value)
    if value > 0:
        return f"\u2705 {formatted}"
    elif value < 0:
        return f"\u274c {formatted}"
    return f"\u2b1c {formatted}"
