import { Type, type Static } from "@sinclair/typebox";
import { Value } from "@sinclair/typebox/value";
import {
  FUTU_ORDER_TYPES,
  FUTU_SESSIONS,
  FUTU_TRAIL_TYPES,
} from "@/lib/futuOrderTypes";

/** IB + chain UI use C/P; OrderBuilder sends CALL/PUT for HTTP clarity — both allowed here. */
export const OptionRightSchema = Type.Union([
  Type.Literal("C"),
  Type.Literal("P"),
  Type.Literal("CALL"),
  Type.Literal("PUT"),
]);

/** Combo leg as submitted to POST /api/orders/place */
export const PlaceOrderComboLegSchema = Type.Object({
  expiry: Type.String(),
  strike: Type.Number(),
  right: OptionRightSchema,
  action: Type.Union([Type.Literal("BUY"), Type.Literal("SELL")]),
  ratio: Type.Number(),
  limitPrice: Type.Optional(Type.Number()),
  /** Echoed from chain builder — ignored by IB bridge after normalization */
  symbol: Type.Optional(Type.String()),
  secType: Type.Optional(Type.String()),
});

const placeOrderBodyProperties = {
  type: Type.Optional(
    Type.Union([
      Type.Literal("stock"),
      Type.Literal("option"),
      Type.Literal("combo"),
    ]),
  ),
  symbol: Type.String({ minLength: 1 }),
  action: Type.Union([Type.Literal("BUY"), Type.Literal("SELL")]),
  quantity: Type.Number(),
  limitPrice: Type.Number(),
  tif: Type.Optional(Type.Union([Type.Literal("DAY"), Type.Literal("GTC")])),
  // FUTU only (contract § Order types); the route refuses them on an IB body.
  orderType: Type.Optional(
    Type.Union(FUTU_ORDER_TYPES.map((t) => Type.Literal(t))),
  ),
  auxPrice: Type.Optional(Type.Number()),
  trailType: Type.Optional(
    Type.Union(FUTU_TRAIL_TYPES.map((t) => Type.Literal(t))),
  ),
  trailValue: Type.Optional(Type.Number()),
  trailSpread: Type.Optional(Type.Number()),
  session: Type.Optional(Type.Union(FUTU_SESSIONS.map((t) => Type.Literal(t)))),
  expiry: Type.Optional(Type.String()),
  strike: Type.Optional(Type.Number()),
  right: Type.Optional(OptionRightSchema),
  legs: Type.Optional(Type.Array(PlaceOrderComboLegSchema)),
  client_attempt_id: Type.String({ minLength: 1 }),
  quote_token: Type.Optional(Type.String()),
  con_id: Type.Optional(Type.Number()),
  acknowledge_limit_override: Type.Optional(Type.Boolean()),
  // Multi-currency (Japan/Korea): a foreign cash-equity order carries its native
  // venue + currency so the place/quote path routes to TSEJ/KRX, not SMART/USD.
  exchange: Type.Optional(Type.String()),
  currency: Type.Optional(Type.String()),
};

/** Top-level place-order body (structural validation only — business rules stay in the route). */
export const PlaceOrderBodySchema = Type.Object(placeOrderBodyProperties);

/** FUTU body: `limitPrice` is absent for types without a fixed limit; the route checks the field table. */
export const FutuPlaceOrderBodySchema = Type.Object({
  ...placeOrderBodyProperties,
  limitPrice: Type.Optional(Type.Number()),
});

export type PlaceOrderBodyValidated = Static<typeof FutuPlaceOrderBodySchema>;

/** Normalize to IB `C` / `P` for guards and `ib_place_order.py`. */
export function normalizeOptionRight(r: string): "C" | "P" {
  const u = String(r).toUpperCase();
  if (u === "C" || u === "CALL") return "C";
  if (u === "P" || u === "PUT") return "P";
  throw new Error(`Invalid option right: ${r}`);
}

export function firstPlaceOrderSchemaErrorMessage(
  raw: unknown,
  options?: { futu?: boolean },
): string | null {
  const schema = options?.futu ? FutuPlaceOrderBodySchema : PlaceOrderBodySchema;
  if (!Value.Check(schema, raw)) {
    const first = [...Value.Errors(schema, raw)][0];
    if (!first) return "Invalid request body";
    const segment = first.path.replace(/^\//, "").split("/")[0];
    if (segment) return `${segment}: ${first.message}`;
    return first.message;
  }
  return null;
}
