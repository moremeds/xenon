/**
 * Futu order types (contract `docs/architecture/futu-orders.md` § Order types).
 *
 * The allowed (security, type, TIF, session) tuples come only from
 * `/api/health` → `futu.capabilities[env]`; this module holds the vocabulary
 * and the per-type field table, never a copy of the allowed matrix.
 */

export const FUTU_ORDER_TYPES = [
  "LMT",
  "MKT",
  "STP",
  "STP LMT",
  "MIT",
  "LIT",
  "TRAIL",
  "TRAIL LIMIT",
] as const;
export type FutuOrderType = (typeof FUTU_ORDER_TYPES)[number];

export const FUTU_TIFS = ["DAY", "GTC"] as const;
export type FutuTif = (typeof FUTU_TIFS)[number];

export const FUTU_SESSIONS = ["RTH", "ETH", "ALL", "OVERNIGHT"] as const;
export type FutuSession = (typeof FUTU_SESSIONS)[number];

export const FUTU_TRAIL_TYPES = ["RATIO", "AMOUNT"] as const;
export type FutuTrailType = (typeof FUTU_TRAIL_TYPES)[number];

export type FutuSecType = "STK" | "OPT";

export type FutuTuple = {
  sec: FutuSecType;
  orderType: FutuOrderType;
  tif: FutuTif;
  session: FutuSession;
};

export type FutuEnvCapabilities = {
  tuples: FutuTuple[];
  modify: FutuOrderType[];
};

export type FutuCapabilities = Partial<Record<string, FutuEnvCapabilities>>;

type TypeFields = {
  limit: boolean;
  aux: boolean;
  trail: boolean;
  spread: boolean;
};

/** Which body fields each type has; every other field must be absent. */
export const FUTU_ORDER_TYPE_FIELDS: Record<FutuOrderType, TypeFields> = {
  LMT: { limit: true, aux: false, trail: false, spread: false },
  MKT: { limit: false, aux: false, trail: false, spread: false },
  STP: { limit: false, aux: true, trail: false, spread: false },
  "STP LMT": { limit: true, aux: true, trail: false, spread: false },
  MIT: { limit: false, aux: true, trail: false, spread: false },
  LIT: { limit: true, aux: true, trail: false, spread: false },
  TRAIL: { limit: false, aux: false, trail: true, spread: false },
  "TRAIL LIMIT": { limit: false, aux: false, trail: true, spread: true },
};

/** Place-body keys only a FUTU request may carry (IB → 400, never stripped). */
export const FUTU_ONLY_PLACE_FIELDS = [
  "orderType",
  "auxPrice",
  "trailType",
  "trailValue",
  "trailSpread",
  "session",
] as const;

function isOneOf<T extends string>(list: readonly T[], v: unknown): v is T {
  return typeof v === "string" && (list as readonly string[]).includes(v);
}

export function isFutuOrderType(v: unknown): v is FutuOrderType {
  return isOneOf(FUTU_ORDER_TYPES, v);
}

/** `"STK:STP LMT:DAY:RTH"` → tuple; malformed or unknown parts → null. */
export function parseFutuTuple(raw: unknown): FutuTuple | null {
  if (typeof raw !== "string") return null;
  const parts = raw.split(":");
  if (parts.length !== 4) return null;
  const [sec, orderType, tif, session] = parts;
  if (sec !== "STK" && sec !== "OPT") return null;
  if (!isFutuOrderType(orderType)) return null;
  if (!isOneOf(FUTU_TIFS, tif) || !isOneOf(FUTU_SESSIONS, session)) return null;
  return { sec, orderType, tif, session };
}

/** `futu.capabilities` → per-env tuples + modify types (unknown entries dropped). */
export function parseFutuCapabilities(raw: unknown): FutuCapabilities | null {
  if (!raw || typeof raw !== "object") return null;
  const out: FutuCapabilities = {};
  for (const [env, value] of Object.entries(raw as Record<string, unknown>)) {
    if (!value || typeof value !== "object") continue;
    const { tuples, modify } = value as { tuples?: unknown; modify?: unknown };
    out[env] = {
      tuples: Array.isArray(tuples)
        ? tuples.map(parseFutuTuple).filter((t): t is FutuTuple => t !== null)
        : [],
      modify: Array.isArray(modify) ? modify.filter(isFutuOrderType) : [],
    };
  }
  return out;
}

export type FutuTicketSelection = {
  orderType?: FutuOrderType | null;
  tif?: FutuTif | null;
  session?: FutuSession | null;
};

export type FutuTicketChoices = {
  orderTypes: FutuOrderType[];
  orderType: FutuOrderType | null;
  tifs: FutuTif[];
  tif: FutuTif | null;
  sessions: FutuSession[];
  session: FutuSession | null;
};

function inOrder<T extends string>(list: readonly T[], present: T[]): T[] {
  return list.filter((v) => present.includes(v));
}

/**
 * Ticket choices for one security type. Each level is narrowed by the
 * previous one; a selection outside the tuples falls back to the first
 * allowed value.
 */
export function futuTicketChoices(
  caps: FutuEnvCapabilities | null | undefined,
  sec: FutuSecType,
  sel: FutuTicketSelection,
): FutuTicketChoices {
  const forSec = (caps?.tuples ?? []).filter((t) => t.sec === sec);
  const orderTypes = inOrder(
    FUTU_ORDER_TYPES,
    forSec.map((t) => t.orderType),
  );
  const orderType =
    sel.orderType && orderTypes.includes(sel.orderType)
      ? sel.orderType
      : (orderTypes[0] ?? null);
  const forType = forSec.filter((t) => t.orderType === orderType);
  const tifs = inOrder(
    FUTU_TIFS,
    forType.map((t) => t.tif),
  );
  const tif = sel.tif && tifs.includes(sel.tif) ? sel.tif : (tifs[0] ?? null);
  const sessions = inOrder(
    FUTU_SESSIONS,
    forType.filter((t) => t.tif === tif).map((t) => t.session),
  );
  const session =
    sel.session && sessions.includes(sel.session)
      ? sel.session
      : (sessions[0] ?? null);
  return { orderTypes, orderType, tifs, tif, sessions, session };
}

export type FutuOrderTermsInput = {
  orderType: FutuOrderType;
  sec: FutuSecType;
  session: FutuSession | null;
  limitPrice?: number | null;
  auxPrice?: number | null;
  trailType?: FutuTrailType | null;
  trailValue?: number | null;
  trailSpread?: number | null;
};

export type FutuOrderFields = {
  orderType: FutuOrderType;
  limitPrice?: number;
  auxPrice?: number;
  trailType?: FutuTrailType;
  trailValue?: number;
  trailSpread?: number;
  session?: FutuSession;
};

const positive = (v: unknown): v is number =>
  typeof v === "number" && Number.isFinite(v) && v > 0;
const nonNegative = (v: unknown): v is number =>
  typeof v === "number" && Number.isFinite(v) && v >= 0;

/** Body fields for one ticket; only the fields the type has. */
export function buildFutuOrderTerms(
  input: FutuOrderTermsInput,
): { ok: true; fields: FutuOrderFields } | { ok: false; message: string } {
  const f = FUTU_ORDER_TYPE_FIELDS[input.orderType];
  const fields: FutuOrderFields = { orderType: input.orderType };
  if (f.limit) {
    if (!positive(input.limitPrice))
      return { ok: false, message: "Enter a positive limit price." };
    fields.limitPrice = input.limitPrice;
  }
  if (f.aux) {
    if (!positive(input.auxPrice))
      return { ok: false, message: "Enter a positive trigger price." };
    fields.auxPrice = input.auxPrice;
  }
  if (f.trail) {
    if (!isOneOf(FUTU_TRAIL_TYPES, input.trailType))
      return { ok: false, message: "Choose a trail type." };
    if (!positive(input.trailValue))
      return { ok: false, message: "Enter a positive trail value." };
    fields.trailType = input.trailType;
    fields.trailValue = input.trailValue;
  }
  if (f.spread) {
    if (!nonNegative(input.trailSpread))
      return { ok: false, message: "Enter a trail spread of 0 or more." };
    fields.trailSpread = input.trailSpread;
  }
  // `session` applies to US stocks only; option orders stay on the RTH default.
  if (input.sec === "STK" && input.session) fields.session = input.session;
  return { ok: true, fields };
}

/**
 * Route-side check of a FUTU single-leg body against the field table:
 * required fields present and valid, every other term absent.
 * `orderType` absent means LMT. Returns the first problem or null.
 */
export function futuOrderTermsError(
  body: Record<string, unknown>,
): string | null {
  const orderType = body.orderType ?? "LMT";
  if (!isFutuOrderType(orderType)) return "orderType: unsupported order type";
  const f = FUTU_ORDER_TYPE_FIELDS[orderType];
  const rules: Array<[string, boolean, (v: unknown) => boolean]> = [
    ["limitPrice", f.limit, positive],
    ["auxPrice", f.aux, positive],
    ["trailType", f.trail, (v) => isOneOf(FUTU_TRAIL_TYPES, v)],
    ["trailValue", f.trail, positive],
    ["trailSpread", f.spread, nonNegative],
  ];
  for (const [key, required, valid] of rules) {
    const present = body[key] !== undefined && body[key] !== null;
    if (required && (!present || !valid(body[key])))
      return `${key} is required and must be valid for ${orderType}`;
    if (!required && present) return `${key} must be absent for ${orderType}`;
  }
  if (body.session != null && !isOneOf(FUTU_SESSIONS, body.session))
    return "session: unsupported session";
  return null;
}

/** At least 2 dp, never hides a finer digit (sub-$1 stock, 3 dp spread). */
function fmt2(v: number): string {
  const dp = (String(v).split(".")[1] ?? "").length;
  return v.toFixed(Math.min(Math.max(2, dp), 6));
}

/** One-line ticket summary naming type, trigger, limit, trail, TIF, session. */
export function futuOrderConfirmText(args: {
  action: string;
  quantity: number;
  symbol: string;
  tif: string;
  fields: FutuOrderFields;
}): string {
  const { fields } = args;
  const parts = [
    `${args.action} ${args.quantity} ${args.symbol}`,
    fields.orderType,
  ];
  if (fields.orderType === "MKT") parts.push("market");
  if (fields.auxPrice != null) parts.push(`trigger ${fmt2(fields.auxPrice)}`);
  if (fields.limitPrice != null) parts.push(`limit ${fmt2(fields.limitPrice)}`);
  if (fields.trailValue != null)
    parts.push(
      fields.trailType === "RATIO"
        ? `trail ${fields.trailValue}%`
        : `trail ${fmt2(fields.trailValue)}`,
    );
  if (fields.trailSpread != null)
    parts.push(`spread ${fmt2(fields.trailSpread)}`);
  parts.push(args.tif);
  if (fields.session) parts.push(fields.session);
  return parts.join(" · ");
}

/* ── Modify (contract § Modify) ─────────────────────────────────────── */

/** Modify-body keys only a FUTU request may carry (IB → 400, never stripped). */
export const FUTU_ONLY_MODIFY_FIELDS = [
  "newAuxPrice",
  "newTrailValue",
  "newTrailSpread",
] as const;

/** Route-side shape check of the FUTU-only modify fields; first problem or null. */
export function futuModifyFieldsError(
  body: Record<string, unknown>,
): string | null {
  const rules: Array<[string, (v: unknown) => boolean, string]> = [
    ["newAuxPrice", positive, "a positive number"],
    ["newTrailValue", positive, "a positive number"],
    ["newTrailSpread", nonNegative, "a number of 0 or more"],
  ];
  for (const [key, valid, what] of rules) {
    if (body[key] != null && !valid(body[key])) return `${key} must be ${what}`;
  }
  return null;
}

/** The current terms of a working Futu order the modify dialog edits. */
export type FutuModifyOrderTerms = {
  orderType: string;
  totalQuantity: number;
  limitPrice: number | null;
  auxPrice: number | null;
  trailType?: string | null;
  trailValue?: number | null;
  trailSpread?: number | null;
};

/** Dialog inputs, parsed; NaN = blank or not a number. */
export type FutuModifyInput = {
  quantity: number;
  limitPrice: number;
  auxPrice: number;
  trailValue: number;
  trailSpread: number;
};

export type FutuModifyChange = {
  field: "qty" | "limit" | "trigger" | "trail" | "spread";
  from: number | null;
  to: number;
};

export type FutuModifyRequestFields = {
  newPrice?: number;
  newQuantity?: number;
  newAuxPrice?: number;
  newTrailValue?: number;
  newTrailSpread?: number;
};

export const FUTU_MODIFY_NO_CHANGE = "No change to send.";

/** Fields the dialog shows for an order type; null = type not known here. */
export function futuModifyFieldsFor(orderType: string): TypeFields | null {
  return isFutuOrderType(orderType) ? FUTU_ORDER_TYPE_FIELDS[orderType] : null;
}

const sameNumber = (a: number, b: number | null | undefined) =>
  b != null && Math.abs(a - b) < 1e-9;

/**
 * Changed terms → modify request with only the changed fields of the
 * order's type (an omitted field keeps Futu's current value).
 */
export function buildFutuModifyRequest(
  order: FutuModifyOrderTerms,
  input: FutuModifyInput,
):
  | { ok: true; request: FutuModifyRequestFields; changes: FutuModifyChange[] }
  | { ok: false; message: string } {
  const f = futuModifyFieldsFor(order.orderType);
  if (!f)
    return {
      ok: false,
      message: `${order.orderType} cannot be modified here.`,
    };
  const request: FutuModifyRequestFields = {};
  const changes: FutuModifyChange[] = [];
  const terms: Array<{
    shown: boolean;
    value: number;
    current: number | null | undefined;
    valid: (v: number) => boolean;
    key: keyof FutuModifyRequestFields;
    field: FutuModifyChange["field"];
    message: string;
  }> = [
    {
      shown: true,
      value: input.quantity,
      current: order.totalQuantity,
      valid: (v) => Number.isInteger(v) && v > 0,
      key: "newQuantity",
      field: "qty",
      message: "Enter a whole quantity above 0.",
    },
    {
      shown: f.limit,
      value: input.limitPrice,
      current: order.limitPrice,
      valid: positive,
      key: "newPrice",
      field: "limit",
      message: "Enter a positive limit price.",
    },
    {
      shown: f.aux,
      value: input.auxPrice,
      current: order.auxPrice,
      valid: positive,
      key: "newAuxPrice",
      field: "trigger",
      message: "Enter a positive trigger price.",
    },
    {
      shown: f.trail,
      value: input.trailValue,
      current: order.trailValue,
      valid: positive,
      key: "newTrailValue",
      field: "trail",
      message: "Enter a positive trail value.",
    },
    {
      shown: f.spread,
      value: input.trailSpread,
      current: order.trailSpread,
      valid: nonNegative,
      key: "newTrailSpread",
      field: "spread",
      message: "Enter a trail spread of 0 or more.",
    },
  ];
  for (const t of terms) {
    if (!t.shown) continue;
    // A term Futu did not report and the user left blank stays omitted.
    if (Number.isNaN(t.value) && t.current == null) continue;
    if (!t.valid(t.value)) return { ok: false, message: t.message };
    if (sameNumber(t.value, t.current)) continue;
    request[t.key] = t.value;
    changes.push({ field: t.field, from: t.current ?? null, to: t.value });
  }
  if (changes.length === 0)
    return { ok: false, message: FUTU_MODIFY_NO_CHANGE };
  return { ok: true, request, changes };
}

function fmtTerm(
  field: FutuModifyChange["field"],
  v: number | null,
  trailType: string | null | undefined,
): string {
  if (v == null) return "---";
  if (field === "qty") return String(v);
  if (field === "trail" && trailType === "RATIO") return `${v}%`;
  return fmt2(v);
}

const CHANGE_LABEL: Record<FutuModifyChange["field"], string> = {
  qty: "qty",
  limit: "limit",
  trigger: "trigger",
  trail: "trail",
  spread: "spread",
};

/** `"trigger 170.00 → 168.00 · qty 100 → 50"` — dialog confirmation line. */
export function futuModifyConfirmText(
  changes: FutuModifyChange[],
  trailType?: string | null,
): string {
  return changes
    .map(
      (c) =>
        `${CHANGE_LABEL[c.field]} ${fmtTerm(c.field, c.from, trailType)} → ${fmtTerm(c.field, c.to, trailType)}`,
    )
    .join(" · ");
}

/** `"trigger 168.00, trail 2.5%"` — the new terms of a modify request (toast copy). */
export function futuModifyTermsText(
  request: FutuModifyRequestFields,
  trailType?: string | null,
): string {
  const parts: string[] = [];
  if (request.newPrice != null) parts.push(`limit ${fmt2(request.newPrice)}`);
  if (request.newAuxPrice != null)
    parts.push(`trigger ${fmt2(request.newAuxPrice)}`);
  if (request.newTrailValue != null)
    parts.push(`trail ${fmtTerm("trail", request.newTrailValue, trailType)}`);
  if (request.newTrailSpread != null)
    parts.push(`spread ${fmt2(request.newTrailSpread)}`);
  if (request.newQuantity != null) parts.push(`qty ${request.newQuantity}`);
  return parts.join(", ");
}
