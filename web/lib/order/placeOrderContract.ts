import {
  normalizeOptionRight,
  type PlaceOrderBodyValidated,
} from "@/lib/placeOrderBodySchema";

export function buildFastApiPlaceOrderPayload(body: PlaceOrderBodyValidated) {
  const type = body.type ?? "stock";
  const payload: Record<string, unknown> = {
    type,
    symbol: body.symbol.toUpperCase(),
    action: body.action,
    quantity: body.quantity,
    limitPrice: body.limitPrice,
    tif: body.tif ?? "DAY",
  };

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
