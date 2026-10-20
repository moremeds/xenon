// Relay clientId allocation (PR-10 / A5). Mirrors xenon.clients.ib_client:
// each environment owns the block XENON_IB_CLIENT_ID_BASE .. base+99 (base
// 0-800, step 100; prod 0, dev.sh 100). The relay lives in base+10..19 and
// never opens a pool id (base+0..9) or a Python id (base+20..99). This is an
// operational contract, not fencing.

export const CLIENT_ID_BASE_ENV = "XENON_IB_CLIENT_ID_BASE";
const BASE_STEP = 100;
const BASE_MAX = 800;
const RELAY_LO = 10;
const RELAY_HI = 19;
// The relay rotates across this many consecutive ids on IB error 326.
export const RELAY_ROTATION_SIZE = 3;

export function relayClientIdBase(env = process.env) {
  const raw = String(env[CLIENT_ID_BASE_ENV] ?? "").trim();
  if (raw === "") return 0;
  if (!/^\d+$/.test(raw)) {
    throw new Error(`${CLIENT_ID_BASE_ENV}=${JSON.stringify(raw)} is not an integer`);
  }
  const base = Number.parseInt(raw, 10);
  if (base > BASE_MAX || base % BASE_STEP !== 0) {
    throw new Error(
      `${CLIENT_ID_BASE_ENV}=${base} is invalid — use 0-${BASE_MAX} in steps of ${BASE_STEP}`,
    );
  }
  return base;
}

/**
 * Return the relay's rotation ids. Default first id is base+10; an explicit
 * --client-id must keep the whole rotation inside base+10..base+19.
 */
export function resolveRelayClientIds({ env = process.env, cliClientId = null } = {}) {
  const base = relayClientIdBase(env);
  const first = cliClientId ?? base + RELAY_LO;
  const last = first + RELAY_ROTATION_SIZE - 1;
  if (!Number.isInteger(first) || first < base + RELAY_LO || last > base + RELAY_HI) {
    throw new Error(
      `relay clientId ${first} (rotation ${first}-${last}) is outside the relay zone ` +
        `${base + RELAY_LO}-${base + RELAY_HI} for ${CLIENT_ID_BASE_ENV}=${base}`,
    );
  }
  return Array.from({ length: RELAY_ROTATION_SIZE }, (_, i) => first + i);
}
