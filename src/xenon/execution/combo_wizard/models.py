from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class ComboLegSpec(BaseModel):
    contract_id: str
    action: Literal["BUY", "SELL"]
    right: Literal["C", "P"]
    strike: Decimal
    expiry: str
    quantity: int = Field(default=1, gt=0)
    limit_price: Decimal | None = None
    price_manually_set: bool = False


class ComboLegQuote(BaseModel):
    bid: Decimal | None = None
    ask: Decimal | None = None


class ComboQuote(BaseModel):
    net_bid: Decimal
    net_ask: Decimal
    signed_mid: Decimal
    bid: Decimal
    ask: Decimal
    mid: Decimal


class ComboPlan(BaseModel):
    mode: Literal["COMBO"] = "COMBO"
    structure_name: str
    natural_price: Decimal
    mid_price: Decimal
    signed_natural_price: Decimal
    signed_mid_price: Decimal
    price_polarity: Literal["DEBIT", "CREDIT", "FLAT"]
    ladder_step: Decimal
    quote: ComboQuote
