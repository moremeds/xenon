import type { Dispatch, SetStateAction } from "react";
import type {
  ApiMessage,
  AssistantResponse,
  Message,
  WorkspaceSection,
} from "./types";
import { PI_COMMAND_ALIASES, PI_COMMAND_SET } from "./data";
import { createTimestamp, formatAssistantPayload, sleep } from "./utils";

export function isPiCommandInput(raw: string) {
  const normalized = raw.trim().toLowerCase();
  const first = normalized.replace(/^\//, "").split(/\s+/)[0];
  return first ? PI_COMMAND_SET.has(first) : false;
}

export function normalizeCommandInput(raw: string) {
  const trimmed = raw.trim();
  return trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
}

export function routeToPiPrompt(raw: string): string | null {
  const normalized = raw.trim();
  if (!normalized) {
    return null;
  }

  if (isPiCommandInput(normalized)) {
    return normalizeCommandInput(normalized);
  }

  const lower = normalized.toLowerCase();
  const alias = PI_COMMAND_ALIASES[lower];
  if (alias) {
    return alias;
  }

  if (/\bportfolio\b/.test(lower) || /\bpositions?\b/.test(lower)) {
    return "/portfolio";
  }

  if (/\bjournal\b/.test(lower)) {
    return "/journal";
  }

  return null;
}

export function fallbackReply(input: string) {
  const query = input.trim().toLowerCase();

  if (!query) {
    return "I can summarize your open positions, recent fills, and Greeks exposure. Ask about a specific ticker or panel.";
  }

  if (query.includes("portfolio") || query.includes("positions")) {
    return "Open the Portfolio page for the live positions table — it covers IB and Futu accounts with structure grouping and per-leg P&L.";
  }

  if (query.includes("journal")) {
    return "Trade decisions and reasoning live on the Journal page. Filter by date or ticker; auto-imported from order fills.";
  }

  if (query.includes("orders")) {
    return "Open orders and recent fills are on the Orders page. Use the Order tab to place / modify / cancel via IB Gateway.";
  }

  return "I can answer questions about your portfolio, orders, or recent fills. For market views, use your own sources — Xenon does not generate trade ideas.";
}

export async function requestAssistantReply(
  history: ApiMessage[],
  latestMessage: string,
): Promise<string> {
  const response = await fetch("/api/assistant", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      messages: [...history, { role: "user", content: latestMessage }],
    }),
  });

  const payload = (await response.json()) as AssistantResponse;

  if (!response.ok) {
    if (payload.error) {
      return `Error: ${payload.error}`;
    }
    return "Assistant service returned an error.";
  }

  if (typeof payload.content === "string" && payload.content.trim()) {
    return formatAssistantPayload(payload.content);
  }

  return fallbackReply(latestMessage);
}

export async function streamMessage(
  messageId: string,
  fullText: string,
  setMessages: Dispatch<SetStateAction<Message[]>>,
) {
  const chunk = 120;
  let rendered = "";
  const source = fullText.length ? fullText : "No output returned.";
  const parts = source.match(new RegExp(`.{1,${chunk}}`, "gs"));

  if (!parts) {
    setMessages((current) =>
      current.map((message) =>
        message.id === messageId ? { ...message, content: source } : message,
      ),
    );
    return;
  }

  for (const piece of parts) {
    rendered += piece;
    setMessages((current) =>
      current.map((message) =>
        message.id === messageId ? { ...message, content: rendered } : message,
      ),
    );
    await sleep(8);
  }
}

export function resolveSectionFromPath(
  pathname: string | null,
  fallback: WorkspaceSection,
): WorkspaceSection {
  if (!pathname) {
    return fallback;
  }

  if (pathname === "/" || pathname === "/dashboard") {
    return "dashboard";
  }

  if (pathname.startsWith("/portfolio")) {
    return "portfolio";
  }

  if (pathname.startsWith("/performance")) {
    return "performance";
  }

  if (pathname.startsWith("/orders")) {
    return "orders";
  }

  if (pathname.startsWith("/journal")) {
    return "journal";
  }

  // Dynamic ticker route: /AAPL, /GOOG, etc.
  if (/^\/[A-Za-z]{1,5}$/.test(pathname)) {
    return "ticker-detail";
  }

  return fallback;
}

// Backwards-compat re-exports for surfaces that still import these.
// They were the chat command runner — now no-op since /api/pi is gone.
export const formatPiPayload = (
  _command: string,
  _normalized: string,
): string => "No output returned.";
export const normalizeTextLines = (s: string): string => s;
export const requestPiReply = async (_command: string): Promise<string> => {
  return "PI commands were removed in the pure-portfolio pivot.";
};
