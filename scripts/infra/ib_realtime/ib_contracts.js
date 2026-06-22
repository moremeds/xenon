// Plain-object IB contract builders for @stoqey/ib.
//
// @stoqey/ib has no `ib.contract.*` factory helpers (the old `ib@0.2.9` package
// did) — contracts are plain objects. These mirror the shapes the relay needs
// for L1 market data + (future) L2 depth subscriptions. Param order is xenon's
// own (chosen for call-site clarity); only the returned object shape matters.
import { SecType } from "@stoqey/ib";

export function stockContract(symbol, exchange = "SMART", currency = "USD") {
  return { symbol, secType: SecType.STK, exchange, currency };
}

export function optionContract(
  symbol,
  expiry,
  strike,
  right,
  exchange = "SMART",
  currency = "USD",
) {
  return {
    symbol,
    secType: SecType.OPT,
    exchange,
    currency,
    lastTradeDateOrContractMonth: expiry,
    strike,
    right,
    multiplier: "100",
  };
}

export function indexContract(symbol, exchange, currency = "USD") {
  return { symbol, secType: SecType.IND, exchange, currency };
}

// Forex (CASH) for live FX conversion. IB CASH contracts use symbol=base,
// currency=quote, exchange=IDEALPRO. Verified live (2026-06-22): USD.JPY and
// USD.KRW quote on IDEALPRO. Keyed downstream as "<base>.<quote>" (e.g.
// "USD.JPY") — the "." (not "_") keeps it out of the option-stale tick branch.
export function forexContract(base, quote, exchange = "IDEALPRO") {
  return { symbol: base, secType: SecType.CASH, currency: quote, exchange };
}

// Forward-compatible: futures depth is deferred (see plan Scope Decision), but
// the builder is kept so the deferred ladder path can wire up without a new file.
export function futureContract(symbol, expiry, exchange, currency = "USD") {
  return {
    symbol,
    secType: SecType.FUT,
    exchange,
    currency,
    lastTradeDateOrContractMonth: expiry,
  };
}
