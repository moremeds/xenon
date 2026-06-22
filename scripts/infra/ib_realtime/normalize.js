// Pure message-normalizers for the realtime relay's subscribe protocol.
//
// Extracted into their own module (not ib_realtime_server.js) on purpose: the
// server module calls httpServer.listen() at top level with no main-guard, so
// importing it from a test would boot the relay and hang the test runner. These
// are side-effect-free and unit-testable in isolation.

// Forex pairs for live FX conversion. Accepts [{base, quote}] and returns
// [{base, quote, key}] uppercased, dropping anything missing base or quote.
// key = "<BASE>.<QUOTE>" (e.g. "USD.JPY") — matches the relay's symbol keying.
export function normalizeForex(raw) {
  if (!Array.isArray(raw)) return [];
  return raw
    .map((c) => {
      if (typeof c !== "object" || c === null) return null;
      const base =
        typeof c.base === "string" ? c.base.trim().toUpperCase() : null;
      const quote =
        typeof c.quote === "string" ? c.quote.trim().toUpperCase() : null;
      if (!base || !quote) return null;
      return { base, quote, key: `${base}.${quote}` };
    })
    .filter(Boolean);
}

// Foreign stocks with an explicit native venue + currency. Accepts
// [{symbol, exchange, currency}] and returns the same uppercased, dropping any
// row missing a field (a foreign stock MUST carry all three — unlike the
// backward-compatible SMART/USD `symbols` list which carries bare strings).
export function normalizeStocksMeta(raw) {
  if (!Array.isArray(raw)) return [];
  return raw
    .map((c) => {
      if (typeof c !== "object" || c === null) return null;
      const symbol =
        typeof c.symbol === "string" ? c.symbol.trim().toUpperCase() : null;
      const exchange =
        typeof c.exchange === "string" ? c.exchange.trim().toUpperCase() : null;
      const currency =
        typeof c.currency === "string" ? c.currency.trim().toUpperCase() : null;
      if (!symbol || !exchange || !currency) return null;
      return { symbol, exchange, currency };
    })
    .filter(Boolean);
}
