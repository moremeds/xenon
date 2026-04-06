import { NextResponse } from "next/server";
import { randomUUID } from "node:crypto";

export type ErrorCode =
  | "BAD_REQUEST"
  | "NOT_FOUND"
  | "CONFIG_ERROR"
  | "UPSTREAM_ERROR"
  | "VALIDATION_ERROR"
  | "INTERNAL_ERROR";

export type CacheState = "HIT" | "MISS" | "STALE" | "BYPASS";

export type ApiErrorPayload = {
  error: string;
  code: ErrorCode;
  detail?: string;
  requestId: string;
};

export type ApiOptions = {
  requestId?: string;
  cacheState?: CacheState;
};

export function getRequestId(): string {
  try {
    return randomUUID();
  } catch {
    return `rid_${Date.now()}_${Math.random().toString(16).slice(2)}`;
  }
}

export function setNoStoreResponseHeaders(response: NextResponse, requestId: string): NextResponse {
  response.headers.set("Cache-Control", "no-store, no-cache, must-revalidate");
  response.headers.set("Pragma", "no-cache");
  response.headers.set("X-Request-Id", requestId);
  return response;
}

export function setCacheResponseHeaders(
  response: NextResponse,
  {
    maxAgeSeconds,
    staleWhileRevalidateSeconds,
    requestId,
    cacheState,
    tags,
  }: {
    maxAgeSeconds: number;
    staleWhileRevalidateSeconds?: number;
    requestId: string;
    cacheState?: CacheState;
    tags?: string[];
  },
): NextResponse {
  const staleDirective = staleWhileRevalidateSeconds && staleWhileRevalidateSeconds > 0
    ? `, stale-while-revalidate=${staleWhileRevalidateSeconds}`
    : "";
  response.headers.set(
    "Cache-Control",
    `public, max-age=${Math.max(0, Math.trunc(maxAgeSeconds))}${staleDirective}`,
  );
  response.headers.set("Vary", "Accept, Accept-Encoding");
  response.headers.set("X-Request-Id", requestId);
  response.headers.set("X-Cache-State", cacheState ?? "BYPASS");
  if (tags?.length) {
    response.headers.set("X-Cache-Tags", tags.join(","));
  }
  return response;
}

export function jsonApiError(params: {
  message: string;
  status?: number;
  code?: ErrorCode;
  detail?: string;
  requestId: string;
}): NextResponse<ApiErrorPayload> {
  const status = params.status ?? 500;
  const code: ErrorCode =
    params.code ??
    (status === 404 ? "NOT_FOUND" : status >= 500 ? "INTERNAL_ERROR" : "BAD_REQUEST");
  return NextResponse.json(
    {
      error: params.message,
      code,
      ...(params.detail ? { detail: params.detail } : {}),
      requestId: params.requestId,
    },
    { status },
  );
}

/** Alias for call sites that prefer a shorter name — same payload as `jsonApiError`. */
export const jsonError = jsonApiError;
