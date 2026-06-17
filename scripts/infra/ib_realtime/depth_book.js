// Pure L2 ladder accumulator — ported from radon
// scripts/ib_realtime_server.js (applyDepthDelta :1250-1270,
// serializeLadder :1272-1292, nbboPriceForOptionLadder :1296-1301,
// summarizeOptionNbbo :1305-1311).
//
// The radon originals read/write module-level state keyed by symbol and call
// the broadcast helper inline. This port inverts ownership: the caller passes
// in the ladders object, so these functions are PURE (no IB state, no
// broadcast). The relay (ib_realtime_server.js) holds one `{bid:[],ask:[]}`
// ladder per depth key, calls applyDepthDelta on every updateMktDepth(L2)
// delta, then serializeLadder on hydrate. Keeping the math here makes it
// unit-testable without a live IB connection.

/**
 * Apply one IB market-depth delta to a `{bid:[],ask:[]}` ladder, in place.
 * IB depth is positional — each side is an ordered array, best-first.
 *   operation 0 = insert  (splice a new level in at `position`)
 *   operation 1 = update  (replace level at `position`; OOB → insert defensively)
 *   operation 2 = delete  (remove level at `position`; OOB ignored)
 *   side 1 = bid, side 0 = ask
 */
export function applyDepthDelta(
  ladders,
  position,
  marketMaker,
  operation,
  side,
  price,
  size,
) {
  const ladder = side === 1 ? ladders.bid : ladders.ask;
  const level = { price, size, marketMaker: marketMaker || null };
  switch (operation) {
    case 0: // insert: shift every level at/below position down one.
      ladder.splice(position, 0, level);
      break;
    case 1: // update in place; defensive OOB update => insert.
      if (position < ladder.length) ladder[position] = level;
      else ladder.splice(position, 0, level);
      break;
    case 2: // delete: shift every level below position up one; ignore OOB.
      if (position < ladder.length) ladder.splice(position, 1);
      break;
    default:
      break;
  }
}

/**
 * Serialize one ladder side into DepthLevel[] for the wire protocol.
 *   stock:   venue/MPID exposed as `exchange`; marketMaker stays null. The web
 *            montage Market column reads `marketMaker ?? exchange`, so equities
 *            label from `exchange`.
 *   option:  per-venue top-of-book BBO. The venue populates BOTH marketMaker
 *            AND exchange so options label like stocks; rows tied at the inside
 *            price (max bid / min ask) are flagged `nbbo=true`.
 *   future:  single-venue native depth — no attribution (both null).
 */
export function serializeLadder(ladder, isFutures, kind, side) {
  const isOption = kind === "option";
  const nbboPrice = nbboPriceForOptionLadder(ladder, side, isOption);
  return ladder.map((lvl) => {
    const venue = isFutures ? null : lvl.marketMaker || null;
    const marketMaker = isOption ? venue : null;
    const exchange = venue;
    const level = { price: lvl.price, size: lvl.size, marketMaker, exchange };
    if (isOption) level.nbbo = nbboPrice != null && lvl.price === nbboPrice;
    return level;
  });
}

// Inside price across an option venue montage: max bid / min ask. Returns null
// for non-option ladders or empty rows (no flag emitted).
function nbboPriceForOptionLadder(ladder, side, isOption) {
  if (!isOption || ladder.length === 0) return null;
  const prices = ladder
    .map((lvl) => lvl.price)
    .filter((p) => typeof p === "number");
  if (prices.length === 0) return null;
  return side === "bid" ? Math.max(...prices) : Math.min(...prices);
}

// Cross-venue NBBO summary for an option montage: best bid / best ask / mid /
// total displayed size across the venue rows at the inside. Honest framing:
// this is top-of-book per venue, not stacked depth.
export function summarizeOptionNbbo(bid, ask) {
  const bestBid = bid.length ? Math.max(...bid.map((l) => l.price)) : null;
  const bestAsk = ask.length ? Math.min(...ask.map((l) => l.price)) : null;
  const mid =
    bestBid != null && bestAsk != null ? (bestBid + bestAsk) / 2 : null;
  const sumSize = (rows) =>
    rows.reduce((acc, l) => acc + (typeof l.size === "number" ? l.size : 0), 0);
  return {
    bestBid,
    bestAsk,
    mid,
    bidSize: sumSize(bid),
    askSize: sumSize(ask),
  };
}
