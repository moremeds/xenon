/**
 * IB market-data line budget — pure LRU selection helpers.
 *
 * IB caps simultaneous market-data lines at ~100 per ACCOUNT (shared across all
 * API clients). The relay's subscribed universe (portfolio + open option chains,
 * where one chain = ~62 lines for C+P × ~31 visible strikes) routinely exceeds
 * that, producing error 101 "Max number of tickers has been reached" and silent
 * starvation. These helpers let the relay hold only the hottest N lines live and
 * evict the idlest, re-admitting evicted-but-still-wanted symbols when a slot
 * frees. Kept pure + separate so the LRU logic is unit-tested without a live IB.
 *
 * Each `entry` is { key, tickerId, lastAccessAt } where:
 *   - tickerId != null  → line is live
 *   - tickerId == null  → symbol wanted but not currently holding a line
 *   - lastAccessAt      → ms of last subscribe or last real tick (heat signal)
 */

/**
 * Pick the live line to evict when over budget: the one with the OLDEST
 * lastAccessAt (idlest), excluding `exceptKey` (the symbol we're admitting).
 * Returns the key to evict, or null when nothing is evictable.
 */
export function pickEvictable(entries, exceptKey = null) {
  let oldestKey = null;
  let oldestAt = Infinity;
  for (const e of entries) {
    if (e.tickerId == null) continue; // not live — nothing to free
    if (e.key === exceptKey) continue; // never evict the incoming symbol
    const at = e.lastAccessAt ?? 0;
    if (at < oldestAt) {
      oldestAt = at;
      oldestKey = e.key;
    }
  }
  return oldestKey;
}

/**
 * Pick the evicted-but-wanted symbol to re-admit when a line frees: the one with
 * the NEWEST lastAccessAt (hottest), so re-access wins the freed slot.
 * Returns the key to admit, or null when nothing is waiting.
 */
export function pickAdmittable(entries) {
  let hottestKey = null;
  let hottestAt = -Infinity;
  for (const e of entries) {
    if (e.tickerId != null) continue; // already live
    const at = e.lastAccessAt ?? 0;
    if (at > hottestAt) {
      hottestAt = at;
      hottestKey = e.key;
    }
  }
  return hottestKey;
}
