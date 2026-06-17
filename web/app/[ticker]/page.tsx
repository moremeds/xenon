import { notFound, redirect } from "next/navigation";
import WorkspaceShell from "@/components/WorkspaceShell";

// Static routes that Next.js already handles — defense-in-depth guard
const RESERVED = new Set([
  "api",
  "dashboard",
  "flow-analysis",
  "portfolio",
  "performance",
  "orders",
  "scanner",
  "discover",
  "journal",
  "regime",
  "cta",
  "kit",
  "internals",
  "_next",
  "favicon",
]);

const TICKER_RE = /^[A-Za-z]{1,5}$/;

type Props = {
  params: Promise<{ ticker: string }>;
  searchParams: Promise<{ tab?: string; posId?: string; leg?: string }>;
};

export default async function TickerPage({ params, searchParams }: Props) {
  const { ticker: raw } = await params;
  const sp = await searchParams;

  // Guard reserved paths (static routes already win, but be explicit)
  if (RESERVED.has(raw.toLowerCase())) return notFound();

  // Format validation: 1-5 alpha chars only
  if (!TICKER_RE.test(raw)) return notFound();

  // Canonical URL is uppercase — redirect if not, preserving the full query
  // string (tab / posId / leg) so a shared lowercase link keeps its selection.
  const upper = raw.toUpperCase();
  if (raw !== upper) {
    const params = new URLSearchParams();
    if (sp.tab) params.set("tab", sp.tab);
    if (sp.posId) params.set("posId", sp.posId);
    if (sp.leg) params.set("leg", sp.leg);
    const qs = params.toString();
    redirect(`/${upper}${qs ? `?${qs}` : ""}`);
  }

  return <WorkspaceShell section="ticker-detail" tickerParam={upper} />;
}
