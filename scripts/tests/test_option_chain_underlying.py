"""xenon-ib-option-chain must qualify indices as Index, not Stock.

Root cause of the CHAIN-tab 502 'Could not qualify SPX' (2026-06-13):
the CLI hardcoded Stock(symbol, "SMART", "USD") and secType "STK" for
every symbol. SPX/NDX/RUT are cash-settled indices — Stock can never
qualify, and reqSecDefOptParams needs underlyingSecType="IND".
"""

from __future__ import annotations

from ib_async import Index, Stock

from xenon.execution.ib_option_chain import underlying_contract


def test_spx_is_an_index_on_cboe() -> None:
    contract, sec_type = underlying_contract("SPX")
    assert isinstance(contract, Index)
    assert contract.exchange == "CBOE"
    assert sec_type == "IND"


def test_ndx_routes_to_nasdaq() -> None:
    contract, sec_type = underlying_contract("NDX")
    assert isinstance(contract, Index)
    assert contract.exchange == "NASDAQ"
    assert sec_type == "IND"


def test_etf_stays_a_smart_stock() -> None:
    contract, sec_type = underlying_contract("QQQ")
    assert isinstance(contract, Stock)
    assert contract.exchange == "SMART"
    assert sec_type == "STK"


def test_unknown_ticker_defaults_to_stock() -> None:
    contract, sec_type = underlying_contract("AAPL")  # not in the V1 universe
    assert isinstance(contract, Stock)
    assert sec_type == "STK"
