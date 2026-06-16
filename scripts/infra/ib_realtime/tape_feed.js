// Pure time-and-sales tape ring buffer — extracted from radon's stateful
// applyTrade (scripts/ib_realtime_server.js:1180-1193). radon pushes onto a
// per-symbol array then splices it back to size; this port keeps just the
// bounded-append core as a PURE function so it is unit-testable without IB
// state. The relay (ib_realtime_server.js) holds each per-key ring in
// symbolDepthStates and reassigns it: `state.trades = appendTrade(state.trades, t)`.
//
// ORDERING CONVENTION: newest-last — index 0 is the oldest retained print, the
// final element is the most recent. classifyTicks (web/lib/book/depthDerivations)
// reads the array forward applying the prior-tick test, so chronological /
// newest-last matches its semantics.

export const TAPE_RING_SIZE = 50;

/**
 * Append one trade print to a bounded ring and return a NEW array (immutable).
 * Trade shape: `{ price, size, exchange|null, time(ISO) }`.
 */
export function appendTrade(ring, trade) {
  const next = ring.length >= TAPE_RING_SIZE ? ring.slice(1) : ring.slice();
  next.push(trade);
  return next;
}
