import { NextResponse } from "next/server";
import { xenonFetch, XenonApiError } from "@/lib/xenonApi";

export const runtime = "nodejs";

// FastAPI owns the Flex token expiry (`flex_token` on GET /health, see
// src/xenon/api/services/flex_token.py). It reports expiry only; the static
// renewal pointers below stay here because they are not part of that contract
// and the auth-exempt /health must not carry them.
const RENEWAL_URL =
  "https://portal.interactivebrokers.com/AccountManagement/AmAuthentication?action=FlexQueries";
const BREADCRUMB =
  "Performance & Reports → Flex Queries → Flex Queries Delivery → Flex Queries → Configure Flex Web Service";

interface HealthFlexToken {
  configured?: boolean;
  expires_at?: string;
  days_remaining?: number;
  reminder_days?: number[];
  active_threshold?: number | null;
  should_warn?: boolean;
  expired?: boolean;
  error?: string;
}

export async function GET(): Promise<Response> {
  try {
    const health = await xenonFetch<{ flex_token?: HealthFlexToken }>("/health", {
      method: "GET",
      timeout: 5_000,
    });
    const ft = health.flex_token;
    if (!ft?.configured) {
      // Not configured is not an error — the banner just has nothing to show.
      return NextResponse.json({
        days_remaining: null,
        should_warn: false,
        expired: false,
        error: ft?.error ?? "flex token not configured",
      });
    }
    return NextResponse.json({
      days_remaining: ft.days_remaining ?? null,
      expires_at: ft.expires_at,
      renewal_url: RENEWAL_URL,
      breadcrumb: BREADCRUMB,
      should_warn: ft.should_warn ?? false,
      expired: ft.expired ?? false,
      active_threshold: ft.active_threshold ?? null,
      reminder_days: ft.reminder_days ?? [],
    });
  } catch (err) {
    if (err instanceof XenonApiError) {
      return NextResponse.json({ error: err.detail }, { status: err.status });
    }
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Unknown error" },
      { status: 502 },
    );
  }
}
