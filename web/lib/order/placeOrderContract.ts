import {
  normalizeOptionRight,
  type PlaceOrderBodyValidated,
} from "@/lib/placeOrderBodySchema";
import { FUTU_ONLY_PLACE_FIELDS } from "@/lib/futuOrderTypes";

export function buildFastApiPlaceOrderPayload(
  body: PlaceOrderBodyValidated,
  options?: { futu?: boolean },
) {
  const futu = options?.futu === true;
  const type = body.type ?? "stock";
  const payload: Record<string, unknown> = {
    type,
    symbol: body.symbol.toUpperCase(),
    action: body.action,
    quantity: body.quantity,
    limitPrice: body.limitPrice,
    tif: body.tif ?? "DAY",
  };
  // FUTU types without a fixed limit send no limitPrice; IB keeps its shape.
  if (futu) {
    if (body.limitPrice == null) delete payload.limitPrice;
    for (const key of FUTU_ONLY_PLACE_FIELDS) {
      if (body[key] != null) payload[key] = body[key];
    }
  }

  if (type === "option") {
    payload.expiry = body.expiry;
    payload.strike = body.strike;
    payload.right =
      body.right != null ? normalizeOptionRight(body.right) : body.right;
  }

  if (type === "combo" && body.legs) {
    payload.legs = body.legs.map((leg) => ({
      expiry: leg.expiry,
      strike: leg.strike,
      right: normalizeOptionRight(leg.right),
      action: leg.action,
      ratio: leg.ratio,
      ...(leg.limitPrice != null ? { limitPrice: leg.limitPrice } : {}),
    }));
  }

  payload.client_attempt_id = body.client_attempt_id;
  if (body.quote_token) payload.quote_token = body.quote_token;
  if (body.con_id != null) payload.con_id = body.con_id;
  // Foreign cash-equity venue/currency (omitted for US SMART/USD orders).
  if (body.exchange) payload.exchange = body.exchange;
  if (body.currency) payload.currency = body.currency;
  if (body.acknowledge_limit_override === true) {
    payload.acknowledge_limit_override = true;
  }

  return payload;
}

export const buildPlaceOrderBody = buildFastApiPlaceOrderPayload;
