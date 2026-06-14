// In-memory registry of identified WS subscribers, keyed by `id`.
// Pure (no socket/IB deps) so it is unit-testable. All times are epoch ms
// passed in by the caller — the module never reads the clock itself.

export function createSubscriberRegistry({ ttlMs = 900_000 } = {}) {
  // id -> { id, connectedAt, lastSeenAt, wsCount, connectedNow, disconnectedAt }
  const byId = new Map();

  function onConnect(id, nowMs) {
    let e = byId.get(id);
    if (!e) {
      e = {
        id,
        connectedAt: nowMs,
        lastSeenAt: nowMs,
        wsCount: 0,
        connectedNow: false,
        disconnectedAt: null,
      };
      byId.set(id, e);
    }
    e.wsCount += 1;
    e.connectedNow = true;
    e.lastSeenAt = nowMs;
    e.disconnectedAt = null;
  }

  function onPong(id, nowMs) {
    const e = byId.get(id);
    if (e) e.lastSeenAt = nowMs;
  }

  function onDisconnect(id, nowMs) {
    const e = byId.get(id);
    if (!e) return;
    e.wsCount = Math.max(0, e.wsCount - 1);
    e.lastSeenAt = nowMs;
    if (e.wsCount === 0) {
      e.connectedNow = false;
      e.disconnectedAt = nowMs;
    }
  }

  function snapshot(nowMs) {
    const subs = [];
    for (const e of [...byId.values()]) {
      if (nowMs - e.lastSeenAt > ttlMs) {
        byId.delete(e.id);
        continue;
      }
      if (e.connectedNow) {
        subs.push({
          id: e.id,
          connected: true,
          connected_at_ms: e.connectedAt,
          last_pong_ms_ago: nowMs - e.lastSeenAt,
        });
      } else {
        subs.push({
          id: e.id,
          connected: false,
          last_seen_ms_ago: nowMs - e.lastSeenAt,
          offline_for_ms:
            e.disconnectedAt == null ? null : nowMs - e.disconnectedAt,
        });
      }
    }
    subs.sort((a, b) => a.id.localeCompare(b.id));
    return subs;
  }

  return { onConnect, onPong, onDisconnect, snapshot };
}
