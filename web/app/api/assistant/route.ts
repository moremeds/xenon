import { NextRequest, NextResponse } from "next/server";

type ChatRole = "user" | "assistant";

export type ChatMessage = {
  role: ChatRole;
  content: string;
};

type ClaudeContentBlock = {
  type: string;
  text?: string;
};

type ClaudeResponse = {
  model: string;
  content: ClaudeContentBlock[];
  usage?: {
    input_tokens: number;
    output_tokens: number;
  };
  stop_reason?: string;
};

export type AssistantPayload = {
  messages: ChatMessage[];
};

const DEFAULT_MODEL =
  process.env.ANTHROPIC_MODEL || "claude-sonnet-4-5-20250929";
const ANTHROPIC_API_URL =
  process.env.ANTHROPIC_API_URL || "https://api.anthropic.com/v1/messages";

const SYSTEM_PROMPT =
  "You are Xenon's portfolio operations assistant. " +
  "You help the user understand their open positions, current orders, Greeks exposure, and recent fills. " +
  "Respond in short, factual blocks. Reference the live position data the user provided rather than inventing it. " +
  "If a question depends on data you can't see, say so and tell the user which page or panel to check. " +
  "Never recommend trades, signals, or market direction — Xenon does not generate trade ideas.";

const isMockMode = () =>
  process.env.ASSISTANT_MOCK === "1" ||
  (process.env.NODE_ENV === "test" && process.env.ASSISTANT_MOCK !== "0");

const ANTHROPIC_ENV_KEYS = [
  "ANTHROPIC_API_KEY",
  "CLAUDE_CODE_API_KEY",
  "CLAUDE_API_KEY",
];
const resolveApiKey = () => {
  for (const key of ANTHROPIC_ENV_KEYS) {
    const value = process.env[key]?.trim();
    if (value) return value;
  }
  return undefined;
};

const cleanString = (value: unknown) =>
  typeof value === "string" ? value.trim() : "";

function fallbackReply(input: string) {
  const q = cleanString(input).toLowerCase();
  if (!q) {
    return "Ask about your positions, orders, or recent fills.";
  }

  if (q.includes("help")) {
    return "Ask about: open positions, working orders, P&L attribution, recent fills, Greeks exposure. I read what's on your screen — I don't recommend trades.";
  }

  if (q.includes("portfolio") || q.includes("positions")) {
    return "Open positions live on the Portfolio page (IB and Futu tabs). Per-leg P&L and Greeks are on each row.";
  }

  if (q.includes("orders")) {
    return "Working orders and recent fills are on the Orders page. Use the Order tab to place, modify, or cancel through IB Gateway.";
  }

  return "I can answer questions about your portfolio, orders, or recent fills. For market views or trade ideas, use your own sources — Xenon does not generate signals.";
}

function safeMessages(rawMessages: unknown): ChatMessage[] {
  if (!Array.isArray(rawMessages)) {
    return [];
  }

  const parsed: ChatMessage[] = [];
  for (const item of rawMessages) {
    const role = (item as { role?: unknown }).role;
    const content = (item as { content?: unknown }).content;

    if (
      (role === "user" || role === "assistant") &&
      typeof content === "string" &&
      content.trim()
    ) {
      parsed.push({ role, content: content.trim() });
    }
  }

  return parsed;
}

function mockReply(messages: ChatMessage[]) {
  const lastUserMessage = [...messages]
    .reverse()
    .find((message) => message.role === "user");
  const userText = lastUserMessage?.content || "No user message provided.";

  return {
    model: "mock",
    content: [
      {
        type: "text",
        text: `Mock Claude response: ${fallbackReply(userText)}`,
      },
    ],
    stop_reason: "end_turn",
    usage: { input_tokens: 0, output_tokens: 0 },
  };
}

async function callAnthropic(messages: ChatMessage[]) {
  const apiKey = resolveApiKey();
  if (!apiKey) {
    throw new Error(
      "Missing Anthropic API key. Set ANTHROPIC_API_KEY, CLAUDE_CODE_API_KEY, or CLAUDE_API_KEY in web/.env.",
    );
  }

  const response = await fetch(ANTHROPIC_API_URL, {
    method: "POST",
    headers: {
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
      "content-type": "application/json",
      accept: "application/json",
    },
    body: JSON.stringify({
      model: DEFAULT_MODEL,
      system: SYSTEM_PROMPT,
      max_tokens: 1200,
      messages,
    }),
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(
      `Anthropic API request failed (${response.status}): ${detail}`,
    );
  }

  const data = (await response.json()) as ClaudeResponse;
  const text = data.content.find(
    (chunk) => chunk.type === "text" && typeof chunk.text === "string",
  )?.text;

  if (!text) {
    throw new Error("Anthropic returned an empty completion.");
  }

  return {
    model: data.model,
    content: [{ type: "text", text }],
    stop_reason: data.stop_reason,
    usage: data.usage,
  } as ClaudeResponse;
}

export async function POST(request: NextRequest): Promise<Response> {
  if (isMockMode()) {
    const body = await request.json().catch(() => null);
    const messages = safeMessages(body?.messages);

    if (!messages.length) {
      return NextResponse.json(
        { error: "No messages supplied." },
        { status: 400 },
      );
    }

    const completion = mockReply(messages);
    return NextResponse.json({
      content: completion.content[0].text,
      model: completion.model,
      usage: completion.usage,
      stop_reason: completion.stop_reason,
    });
  }

  let body: AssistantPayload;

  try {
    body = (await request.json()) as AssistantPayload;
  } catch {
    return NextResponse.json(
      { error: "Invalid JSON payload." },
      { status: 400 },
    );
  }

  const messages = safeMessages(body?.messages);
  if (!messages.length) {
    return NextResponse.json(
      { error: "No messages supplied." },
      { status: 400 },
    );
  }

  const lastMessage = messages[messages.length - 1];
  if (lastMessage.role !== "user") {
    return NextResponse.json(
      { error: "The last message must be from user." },
      { status: 400 },
    );
  }

  try {
    const completion = await callAnthropic(messages);
    const text = completion.content.find(
      (chunk) => chunk.type === "text" && typeof chunk.text === "string",
    )?.text;

    return NextResponse.json({
      content: text,
      model: completion.model,
      usage: completion.usage,
      stop_reason: completion.stop_reason,
    });
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Unknown assistant error.";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
