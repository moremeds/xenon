import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";
import { placeOutcomeUnknownBody } from "@/lib/order/placeOutcome";
import { passThroughXenonError } from "@/lib/passThroughXenonError";
import {
  getRequestId,
  jsonApiError,
  setNoStoreResponseHeaders,
} from "@/lib/apiContracts";
import {
  firstPlaceOrderSchemaErrorMessage,
  normalizeOptionRight,
} from "@/lib/placeOrderBodySchema";
import { buildFastApiPlaceOrderPayload } from "@/lib/order/placeOrderContract";
import { forwardRequestQuery } from "@/lib/forwardRequestQuery";
import {
  FUTU_ONLY_PLACE_FIELDS,
  futuOrderTermsError,
  type FutuOrderType,
} from "@/lib/futuOrderTypes";

export const runtime = "nodejs";

function isFutuScope(request: Request): boolean {
  const broker = new URL(request.url).searchParams.get("broker");
  return broker?.toUpperCase() === "FUTU";
}

function invalidOrderBody(message: string, requestId: string): Response {
  return setNoStoreResponseHeaders(
    NextResponse.json(
      { error: message, reason_code: "INVALID_ORDER_BODY", requestId },
      { status: 400 },
    ),
    requestId,
  );
}

const ORDER_PLACE_TIMEOUT_MS = 60_000;

type ComboLeg = {
  expiry: string;
  strike: number;
  right: "C" | "P";
  action: "BUY" | "SELL";
  ratio: number;
  limitPrice?: number;
};

type PlaceBody = {
  type: "stock" | "option" | "combo";
  symbol: string;
  action: "BUY" | "SELL";
  quantity: number;
  limitPrice?: number;
  tif?: "DAY" | "GTC";
  // FUTU only (contract § Order types); refused on an IB body.
  orderType?: FutuOrderType;
  auxPrice?: number;
  trailType?: "RATIO" | "AMOUNT";
  trailValue?: number;
  trailSpread?: number;
  session?: "RTH" | "ETH" | "ALL" | "OVERNIGHT";
  expiry?: string;
  strike?: number;
  right?: "C" | "P";
  legs?: ComboLeg[];
  client_attempt_id: string;
  quote_token?: string;
  con_id?: number;
  acknowledge_limit_override?: boolean;
  // Foreign cash-equity venue/currency (Japan/Korea). Forwarded verbatim by
  // buildFastApiPlaceOrderPayload to FastAPI → ib_place_order.
  exchange?: string;
  currency?: string;
};

export async function POST(request: Request): Promise<Response> {
  const scopeQs = forwardRequestQuery(request);
  const requestId = getRequestId();
  // Set right before the FastAPI call: any failure after it may have placed
  // the order at IB (OP-1).
  let dispatched = false;
  try {
    let parsed: unknown;
    try {
      parsed = await request.json();
    } catch {
      return setNoStoreResponseHeaders(
        jsonApiError({
          message: "Invalid JSON body",
          status: 400,
          code: "BAD_REQUEST",
          requestId,
        }),
        requestId,
      );
    }

    if (
      parsed === null ||
      typeof parsed !== "object" ||
      Array.isArray(parsed)
    ) {
      return setNoStoreResponseHeaders(
        jsonApiError({
          message: "Request body must be a JSON object",
          status: 400,
          code: "BAD_REQUEST",
          requestId,
        }),
        requestId,
      );
    }

    const futu = isFutuScope(request);
    // An IB body carrying a Futu-only key (any value, even null) is refused,
    // never stripped (contract § Order types).
    if (!futu) {
      const futuField = FUTU_ONLY_PLACE_FIELDS.find((key) => key in parsed);
      if (futuField)
        return invalidOrderBody(
          `${futuField} is only valid for a FUTU order`,
          requestId,
        );
    }
    const schemaErr = firstPlaceOrderSchemaErrorMessage(parsed, { futu });
    if (schemaErr) {
      return setNoStoreResponseHeaders(
        jsonApiError({
          message: schemaErr,
          status: 400,
          code: "VALIDATION_ERROR",
          requestId,
        }),
        requestId,
      );
    }

    const body = parsed as PlaceBody;
    body.type = body.type ?? "stock";

    // Chain UI sends CALL/PUT; IB + naked-short guard expect C/P
    if (body.type === "option" && body.right != null) {
      body.right = normalizeOptionRight(body.right as unknown as string);
    }
    if (body.type === "combo" && body.legs) {
      body.legs = body.legs.map((leg) => ({
        ...leg,
        right: normalizeOptionRight(leg.right as unknown as string),
      }));
    }

    // Required fields (schema ensures presence; trim rejects whitespace-only symbol)
    if (!body.symbol?.trim() || !body.action) {
      return setNoStoreResponseHeaders(
        jsonApiError({
          message: "Required: symbol, action, quantity, limitPrice",
          status: 400,
          code: "BAD_REQUEST",
          requestId,
        }),
        requestId,
      );
    }

    // Validate quantity: must be positive integer
    if (
      body.quantity == null ||
      body.quantity <= 0 ||
      !Number.isFinite(body.quantity)
    ) {
      return setNoStoreResponseHeaders(
        jsonApiError({
          message: "quantity must be a positive number",
          status: 400,
          code: "BAD_REQUEST",
          requestId,
        }),
        requestId,
      );
    }

    // FUTU single-leg: the fields of the order type, every other term absent.
    const futuSingleLeg = futu && body.type !== "combo";
    if (futuSingleLeg) {
      const termsErr = futuOrderTermsError(body);
      if (termsErr) return invalidOrderBody(termsErr, requestId);
    }

    // Signed combo prices are valid: IB combo pricing preserves credit/debit sign.
    // Single-leg stock/option orders must remain strictly positive.
    const comboSignedPrice = body.type === "combo";
    const limitPriceInvalid = comboSignedPrice
      ? body.limitPrice == null ||
        body.limitPrice === 0 ||
        !Number.isFinite(body.limitPrice)
      : body.limitPrice == null ||
        body.limitPrice <= 0 ||
        !Number.isFinite(body.limitPrice);
    if (!futuSingleLeg && limitPriceInvalid) {
      return setNoStoreResponseHeaders(
        jsonApiError({
          message: comboSignedPrice
            ? "combo limitPrice must be a non-zero number"
            : "limitPrice must be a positive number",
          status: 400,
          code: "BAD_REQUEST",
          requestId,
        }),
        requestId,
      );
    }

    if (
      body.type === "option" &&
      (!body.expiry || !body.strike || !body.right)
    ) {
      return setNoStoreResponseHeaders(
        jsonApiError({
          message: "Options require: expiry, strike, right",
          status: 400,
          code: "BAD_REQUEST",
          requestId,
        }),
        requestId,
      );
    }

    if (body.type === "combo" && (!body.legs || body.legs.length < 2)) {
      return setNoStoreResponseHeaders(
        jsonApiError({
          message: "Combo orders require 'legs' array with 2+ entries",
          status: 400,
          code: "BAD_REQUEST",
          requestId,
        }),
        requestId,
      );
    }

    const orderPayload = buildFastApiPlaceOrderPayload(body, { futu });

    dispatched = true;
    const orderResult = await xenonFetch<Record<string, unknown>>(
      `/orders/place${scopeQs}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(orderPayload),
        timeout: ORDER_PLACE_TIMEOUT_MS,
      },
    );

    // FastAPI owns the place verdict (ordered Trade.log): a 200 means IB
    // accepted or may hold the order. Never relabel it "rejected" from the
    // final status — that invited a resend that duplicates the order.

    // Refresh orders after placement
    let refreshedOrders: Record<string, unknown> | null = null;
    try {
      refreshedOrders = await xenonFetch<Record<string, unknown>>(
        `/orders/refresh${scopeQs}`,
        { method: "POST", timeout: 10_000 },
      );
    } catch {
      // Non-fatal — order was placed, refresh failed
    }

    const response = NextResponse.json({
      status: "ok",
      orderId: orderResult.orderId,
      permId: orderResult.permId,
      tif: orderResult.tif ?? orderPayload.tif,
      initialStatus: orderResult.initialStatus,
      message: orderResult.message,
      submission_id: orderResult.submission_id,
      // IB accepted but the DB write failed; reconciliation repairs it.
      ...(orderResult.persist_warning
        ? { persist_warning: orderResult.persist_warning }
        : {}),
      orders: refreshedOrders,
      requestId,
    });
    return setNoStoreResponseHeaders(response, requestId);
  } catch (error) {
    // After dispatch, only a proven rejection is terminal; a timeout, socket
    // reset, unreadable body or unexplained 5xx is "status unknown".
    const uncertain = dispatched ? placeOutcomeUnknownBody(error) : null;
    if (uncertain) {
      const res = NextResponse.json(uncertain, { status: 502 });
      res.headers.set("X-Request-Id", requestId);
      return setNoStoreResponseHeaders(res, requestId);
    }
    return setNoStoreResponseHeaders(
      passThroughXenonError(error, requestId),
      requestId,
    );
  }
}
