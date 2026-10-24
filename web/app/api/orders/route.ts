import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonApi";
import { forwardRequestQuery } from "@/lib/forwardRequestQuery";

export const runtime = "nodejs";

type OrdersPayload = Record<string, unknown>;

// Per-broker (and per Futu account_env) singleflight so refreshes never collapse.
const syncInFlight: Record<string, Promise<void> | null> = {};

function ordersSyncKey(req?: Request): string {
  if (!req?.url) return "ib";
  const sp = new URL(req.url).searchParams;
  const broker = sp.get("broker");
  if (broker?.toUpperCase() === "FUTU") {
    const env = sp.get("account_env");
    return `futu:${env ?? ""}`;
  }
  return "ib";
}

async function fetchOrders(qs: string): Promise<OrdersPayload> {
  return await xenonFetch<OrdersPayload>(`/orders${qs}`, {
    method: "GET",
    timeout: 10_000,
  });
}

export async function GET(req?: Request): Promise<Response> {
  const qs = forwardRequestQuery(req);
  try {
    return NextResponse.json(await fetchOrders(qs));
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Failed to read orders";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}

export async function POST(req?: Request): Promise<Response> {
  const qs = forwardRequestQuery(req);
  const key = ordersSyncKey(req);
  try {
    if (!syncInFlight[key]) {
      syncInFlight[key] = xenonFetch(`/orders/refresh${qs}`, {
        method: "POST",
        timeout: 35_000,
      })
        .then(() => {})
        .finally(() => {
          syncInFlight[key] = null;
        });
    }
    await syncInFlight[key];

    return NextResponse.json(await fetchOrders(qs));
  } catch (error) {
    const message = error instanceof Error ? error.message : "Sync failed";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
