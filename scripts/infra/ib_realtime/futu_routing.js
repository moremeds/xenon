// Futu provider routing — pure state, no sockets/timers. Callers pass `now`.
//
// A relay client opts into Futu-first quotes via `?quotes=futu-first` on the
// upgrade URL (honoured only for X-API-Key auth — carried in by the server).
// A symbol may be live on Futu and IB at once; each client receives frames
// only from the source routed for it.
//
// Routing rules (docs/architecture/futu-quotes.md):
//   - a symbol goes to Futu only when it is in `ok` of the latest `assign`
//     gen, the link is up, and the ok membership has held for >= holdDownMs;
//   - failover (symbol leaves ok, opend_connected:false, socket lost, health
//     deadline) puts the symbol back on the IB line budget immediately;
//   - indexes, options, forex and non-US stocks are pinned to IB and are never
//     sent in `want`;
//   - IB demand = IB-preference clients' symbols + IB-pinned + symbols not yet
//     Futu-live.
//
// Per-source state: futuStates holds the latest Futu-sourced PriceData per
// symbol (hydration); it and ok/okSince are cleared on every down event.

export const FUTU_HOLD_DOWN_MS = 60_000;

export function createFutuRouting({ holdDownMs = FUTU_HOLD_DOWN_MS } = {}) {
  const futuFirst = new Set(); // clients that opted into Futu-first quotes
  const ibPinned = new Set(); // symbol keys that never route to Futu
  let okSet = new Set(); // symbols in `ok` of the latest accepted assign
  const okSince = new Map(); // symbol -> ms it entered ok (current up-epoch)
  let socketOpen = false;
  let up = false; // health-driven: fresh health + opend_connected
  let lastHealth = null; // { opend_connected, quota }
  const futuStates = new Map(); // symbol -> latest Futu PriceData

  function setPref(client, pref) {
    if (pref === "futu-first") futuFirst.add(client);
    else futuFirst.delete(client);
  }

  function removeClient(client) {
    futuFirst.delete(client);
  }

  function isFutuFirst(client) {
    return futuFirst.has(client);
  }

  function pinIbOnly(symbol) {
    ibPinned.add(symbol);
  }

  function isIbPinned(symbol) {
    return ibPinned.has(symbol);
  }

  function clearUp() {
    up = false;
    okSet = new Set();
    okSince.clear();
    futuStates.clear();
  }

  function noteSocket(open) {
    socketOpen = Boolean(open);
    if (!socketOpen) clearUp();
  }

  function noteHealth(health) {
    lastHealth = health;
    if (health?.opend_connected === false) {
      clearUp();
    } else if (socketOpen) {
      up = true;
    }
  }

  function noteDown(_reason) {
    clearUp();
  }

  function isUp() {
    return up;
  }

  // An assign is valid only for the latest want gen sent on the current
  // socket (passed in by the caller, which owns the counter). The service
  // sends repeated assigns for one want gen as partitions move — pending→ok,
  // ok→pending/session — and every one of them applies.
  function applyAssign(assign, now, wantGen) {
    const gen = Number(assign?.gen);
    if (!Number.isFinite(gen) || gen !== wantGen) {
      return { entered: [], left: [], ignored: true };
    }
    const ok = Array.isArray(assign.ok) ? assign.ok : [];
    const next = new Set(ok);
    const entered = [];
    const left = [];
    for (const s of next) {
      if (!okSet.has(s)) entered.push(s);
    }
    for (const s of okSet) {
      if (!next.has(s)) left.push(s);
    }
    for (const s of left) {
      okSince.delete(s);
      futuStates.delete(s);
    }
    for (const s of entered) {
      okSince.set(s, now);
    }
    okSet = next;
    return { entered, left, ignored: false };
  }

  // Futu serves this symbol now: link up + not IB-pinned + in ok +
  // hold-down elapsed.
  function futuLive(symbol, now) {
    if (!up) return false;
    if (ibPinned.has(symbol)) return false;
    if (!okSet.has(symbol)) return false;
    const since = okSince.get(symbol);
    return since != null && now - since >= holdDownMs;
  }

  function routeFor(client, symbol, now) {
    return futuFirst.has(client) && futuLive(symbol, now) ? "futu" : "ib";
  }

  // Does this symbol need an IB line? (subscribers = Set<client>)
  function ibDemand(symbol, subscribers, now) {
    if (!subscribers || subscribers.size === 0) return false;
    for (const client of subscribers) {
      if (!futuFirst.has(client)) return true; // an IB-preference client wants it
    }
    // Only Futu-first subscribers: IB while Futu is not live for this symbol.
    return !futuLive(symbol, now);
  }

  // Complete desired set for `want` — futu-first clients' symbols minus
  // IB-pinned keys (indexes/options/forex/non-US are never Futu-served).
  function desiredWant(clientSymbols) {
    const want = new Set();
    for (const client of futuFirst) {
      const symbols = clientSymbols.get(client);
      if (!symbols) continue;
      for (const symbol of symbols) {
        if (!ibPinned.has(symbol)) want.add(symbol);
      }
    }
    return [...want].sort();
  }

  function recordTick(symbol, data) {
    futuStates.set(symbol, data);
  }

  function futuData(symbol) {
    return futuStates.get(symbol);
  }

  function status() {
    return {
      connected: socketOpen,
      opend_connected: up && lastHealth?.opend_connected === true,
      quota: up ? (lastHealth?.quota ?? null) : null,
    };
  }

  return {
    setPref,
    removeClient,
    isFutuFirst,
    pinIbOnly,
    isIbPinned,
    noteSocket,
    noteHealth,
    noteDown,
    isUp,
    applyAssign,
    futuLive,
    routeFor,
    ibDemand,
    desiredWant,
    recordTick,
    futuData,
    status,
  };
}
