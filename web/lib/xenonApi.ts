/**
 * Xenon FastAPI client — minimal fetch helper for Next.js routes.
 *
 * All POST operations go through FastAPI.
 * Attaches Clerk JWT when available for authenticated requests.
 */

const XENON_API = process.env.XENON_API_URL || "http://localhost:8321";

export class XenonApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
    public readonly body: Record<string, unknown> | null = null,
  ) {
    super(`Xenon API ${status}: ${detail}`);
    this.name = "XenonApiError";
  }
}

export async function xenonFetch<T = Record<string, unknown>>(
  path: string,
  opts?: RequestInit & { timeout?: number; token?: string },
): Promise<T> {
  const { timeout = 30_000, token, ...fetchOpts } = opts ?? {};
  const headers = new Headers(fetchOpts.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const res = await fetch(`${XENON_API}${path}`, {
    ...fetchOpts,
    headers,
    cache: fetchOpts.cache ?? "no-store",
    signal: AbortSignal.timeout(timeout),
  });
  if (!res.ok) {
    let detail: string;
    let bodyJson: Record<string, unknown> | null = null;
    try {
      const body = await res.json();
      bodyJson = body && typeof body === "object" ? body : null;
      detail = body.detail ?? body.error ?? JSON.stringify(body);
    } catch {
      detail = await res.text().catch(() => `HTTP ${res.status}`);
    }
    throw new XenonApiError(res.status, detail, bodyJson);
  }
  return res.json();
}
