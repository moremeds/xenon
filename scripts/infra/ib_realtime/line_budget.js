/**
 * IB market-data line budget — tier-aware LRU selection helpers.
 *
 * IB caps simultaneous market-data lines at ~100 per ACCOUNT (shared across all
 * API clients). The relay's subscribed universe (portfolio + open option chains,
 * where one chain = ~62 lines for C+P × ~31 visible strikes) routinely exceeds
 * that, producing error 101 "Max number of tickers has been reached" and silent
 * starvation. These helpers let the relay hold only the hottest N lines live and
 * evict the idlest, re-admitting evicted-but-still-wanted symbols when a slot
 * frees. Kept pure + separate so the LRU logic is unit-tested without a live IB.
 *
 * Each `entry` is { key, tickerId, lastAccessAt, tier } where:
 *   - tickerId != null  → line is live
 *   - tickerId == null  → symbol wanted but not currently holding a line
 *   - lastAccessAt      → ms of last subscribe or last real tick (heat signal)
 *   - tier              → "held" (portfolio/order demand) or "view" (chain
 *                         browsing); missing tier defaults to "held", matching
 *                         the relay's untagged-subscriber default.
 *
 * Tier overrides recency at the decision boundary (D13): held keys are
 * admitted before view keys and view lines are evicted before held lines,
 * so chain subscriptions can never starve a held position of its line.
 * Recency still orders keys WITHIN a tier.
 */

/**
 * Pick the live line to evict when over budget: the idlest VIEW line first
 * (oldest lastAccessAt), and only when no view line remains the idlest held
 * line — but never for an incoming held key, which waits rather than evicting
 * another held key for held-first reasons. `incomingTier` is the tier of the
 * key being admitted; an incoming view key may still take the idlest held line
 * as a last resort (pre-tier LRU fallback), which is bounded by the
 * pending-held reclaim in admitPendingLine. Returns the key to evict, or null
 * when nothing is evictable for this incoming tier.
 */
export function pickEvictable(entries, exceptKey = null, incomingTier = "held") {
  let oldestViewKey = null;
  let oldestViewAt = Infinity;
  let oldestHeldKey = null;
  let oldestHeldAt = Infinity;
  for (const e of entries) {
    if (e.tickerId == null) continue; // not live — nothing to free
    if (e.key === exceptKey) continue; // never evict the incoming symbol
    const at = e.lastAccessAt ?? 0;
    if (e.tier === "view") {
      if (at < oldestViewAt) {
        oldestViewAt = at;
        oldestViewKey = e.key;
      }
    } else if (at < oldestHeldAt) {
      oldestHeldAt = at;
      oldestHeldKey = e.key;
    }
  }
  if (oldestViewKey != null) return oldestViewKey;
  if (incomingTier === "held") return null;
  return oldestHeldKey;
}

/**
 * Pick the evicted-but-wanted symbol to re-admit when a line frees: held keys
 * first, then the NEWEST lastAccessAt (hottest) within the tier, so a pending
 * held position wins the freed slot over any amount of chain demand.
 * Returns the key to admit, or null when nothing is waiting.
 */
export function pickAdmittable(entries) {
  let bestKey = null;
  let bestAt = -Infinity;
  let bestHeld = false;
  for (const e of entries) {
    if (e.tickerId != null) continue; // already live
    const held = e.tier !== "view"; // untiered → held default
    const at = e.lastAccessAt ?? 0;
    if (
      bestKey == null ||
      (held && !bestHeld) ||
      (held === bestHeld && at > bestAt)
    ) {
      bestKey = e.key;
      bestAt = at;
      bestHeld = held;
    }
  }
  return bestKey;
}
