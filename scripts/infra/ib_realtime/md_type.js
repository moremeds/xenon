// IB marketDataType recording — pure helpers, unit-testable.
//
// The relay requests market data type 4 (Delayed-Frozen cascade) at connect;
// IB reports the effective type per request via the marketDataType callback.
// We record it per symbol and expose it as `mdType` in the per-symbol
// status/snapshot payload. It resets on reconnect or resubscribe.

// Snapshot and live request ids are tracked separately: the callback's reqId
// must match the live tickerId for live state to update, so a concurrent
// snapshot on the same symbol can't overwrite or manufacture live evidence.
export function recordMdType(
  { requestIdToSymbol, symbolStates, snapshotRequests },
  reqId,
  mdType,
) {
  const symbol = requestIdToSymbol.get(reqId);
  if (!symbol) return null;
  const live = symbolStates.get(symbol);
  if (live && live.tickerId === reqId) live.mdType = mdType;
  const snap = snapshotRequests?.get(reqId);
  if (snap?.data) snap.data.mdType = mdType;
  return symbol;
}

export function resetMdType(state) {
  if (state) state.mdType = null;
}
