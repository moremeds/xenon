// Runtime settings pull (D5): the relay reads the DB overrides of its own and
// the futu-quotes keys from the api (`GET /admin/settings`, X-Internal-Token —
// the credential it already uses for /ws-ticket/validate; never X-API-Key).
// The api is the only DB reader. Effective value = a valid DB override, else
// this process's env value. When the api is unreachable or answers badly the
// last effective values stay (process start: env values).
//
// Registry of record: src/xenon/runtime_settings.py. The relay only needs the
// keys it applies or forwards, and re-validates every value defensively (an
// invalid value is ignored, the env value stays).

export const RELAY_KEYS = [
  "XENON_QUOTES_DEFAULT",
  "XENON_FUTU_OPTIONS",
  "IB_REALTIME_MAX_LINES",
  "IB_REALTIME_STALE_TICK_MS",
];
export const FUTU_QUOTES_KEYS = [
  "FUTU_QUOTE_QUOTA",
  "FUTU_UNITS_PER_SYMBOL",
  "FUTU_BOOK_STALE_S",
  "FUTU_GREEK_FACTORS",
];
export const SETTINGS_POLL_MS = 10_000;

const isInt = (v, lo, hi) =>
  Number.isInteger(v) && typeof v === "number" && v >= lo && v <= hi;
const isNum = (v, lo, hi) =>
  typeof v === "number" && Number.isFinite(v) && v >= lo && v <= hi;
const GREEKS = new Set(["iv", "delta", "gamma", "theta", "vega"]);

const VALID = {
  XENON_QUOTES_DEFAULT: (v) => v === "ib" || v === "futu-first",
  XENON_FUTU_OPTIONS: (v) => typeof v === "boolean",
  IB_REALTIME_MAX_LINES: (v) => isInt(v, 10, 100),
  IB_REALTIME_STALE_TICK_MS: (v) => isInt(v, 1_000, 3_600_000),
  FUTU_QUOTE_QUOTA: (v) => isInt(v, 1, 100_000),
  FUTU_UNITS_PER_SYMBOL: (v) => isInt(v, 1, 10),
  FUTU_BOOK_STALE_S: (v) => isNum(v, 0, 3600),
  FUTU_GREEK_FACTORS: (v) =>
    v !== null &&
    typeof v === "object" &&
    !Array.isArray(v) &&
    Object.entries(v).every(
      ([k, f]) =>
        GREEKS.has(k) && typeof f === "number" && Number.isFinite(f) && Math.abs(f) <= 1e6,
    ),
};

/** The env values the relay reads today (unchanged parsing). */
export function relayEnvValues(env = process.env) {
  return {
    XENON_QUOTES_DEFAULT:
      env.XENON_QUOTES_DEFAULT === "futu-first" ? "futu-first" : "ib",
    XENON_FUTU_OPTIONS: env.XENON_FUTU_OPTIONS === "1",
    IB_REALTIME_MAX_LINES: Math.max(
      10,
      Number(env.IB_REALTIME_MAX_LINES) || 85,
    ),
    IB_REALTIME_STALE_TICK_MS: Number(env.IB_REALTIME_STALE_TICK_MS) || 60_000,
  };
}

/** `/admin/settings` on the same api origin as the ticket validate URL. */
export function settingsUrl(validateUrl) {
  return new URL("/admin/settings", validateUrl).toString();
}

/**
 * Valid DB overrides from a GET /admin/settings body, for the relay and
 * futu-quotes keys only: `{key: value}` (no row → key absent). Returns null
 * for a body that is not the expected shape (treated like an unreachable api).
 */
export function parseOverrides(body) {
  if (!body || !Array.isArray(body.settings)) return null;
  const out = {};
  for (const row of body.settings) {
    const key = row?.key;
    if (!(key in VALID)) continue;
    const v = row.db_value;
    if (v === null || v === undefined) continue;
    if (VALID[key](v)) out[key] = v;
  }
  return out;
}

const sameJson = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/**
 * Poller. `onRelayChange(effective, prev)` fires when an effective relay value
 * changes; `onFutuConfig(values)` fires when the futu-quotes override set
 * changes (values: every FUTU_QUOTES_KEYS key, null = use the service env).
 */
export function createRuntimeSettings({
  url,
  token,
  env = relayEnvValues(),
  fetchImpl = fetch,
  intervalMs = SETTINGS_POLL_MS,
  timeoutMs = 5_000,
  onRelayChange = () => {},
  onFutuConfig = () => {},
  log = () => {},
}) {
  let overrides = {}; // last valid override set (start: none = env)
  let known = false; // at least one successful pull
  let lastOkAt = null;
  let lastError = null;
  let timer = null;

  const effectiveFrom = (ov) => {
    const out = {};
    for (const k of RELAY_KEYS) out[k] = k in ov ? ov[k] : env[k];
    return out;
  };
  const futuConfigFrom = (ov) => {
    const out = {};
    for (const k of FUTU_QUOTES_KEYS) out[k] = k in ov ? ov[k] : null;
    return out;
  };

  function apply(next) {
    const prevEff = effectiveFrom(overrides);
    const prevFutu = futuConfigFrom(overrides);
    const wasKnown = known;
    overrides = next;
    known = true;
    const eff = effectiveFrom(next);
    if (!sameJson(prevEff, eff)) {
      log(`runtime settings: relay ${JSON.stringify(eff)}`);
      onRelayChange(eff, prevEff);
    }
    const futu = futuConfigFrom(next);
    if (!wasKnown || !sameJson(prevFutu, futu)) onFutuConfig(futu);
  }

  async function pollOnce() {
    if (!token) {
      lastError = "no_internal_token";
      return false;
    }
    try {
      const res = await fetchImpl(url, {
        headers: { "X-Internal-Token": token },
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const parsed = parseOverrides(await res.json());
      if (parsed == null) throw new Error("unexpected body");
      lastOkAt = Date.now();
      lastError = null;
      apply(parsed);
      return true;
    } catch (err) {
      const msg = String(err?.message ?? err);
      if (msg !== lastError)
        log(`runtime settings pull failed (keeping last values): ${msg}`);
      lastError = msg;
      return false;
    }
  }

  return {
    pollOnce,
    start() {
      if (timer) return;
      pollOnce();
      timer = setInterval(pollOnce, intervalMs);
      timer.unref?.();
    },
    stop() {
      if (timer) clearInterval(timer);
      timer = null;
    },
    effective: () => effectiveFrom(overrides),
    /** Current futu-quotes config, or null before the first successful pull. */
    futuConfig: () => (known ? futuConfigFrom(overrides) : null),
    status: () => ({
      known,
      last_ok_at: lastOkAt,
      last_error: lastError,
      overrides: { ...overrides },
      effective: effectiveFrom(overrides),
    }),
  };
}
