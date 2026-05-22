import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

/**
 * Advanced chat function tests with mocked fetch.
 *
 * Tests requestAssistantReply, requestPiReply, and streamMessage
 * by mocking the global fetch to simulate various API responses.
 */

// ---------------------------------------------------------------------------
// Mock global fetch
// ---------------------------------------------------------------------------

const originalFetch = global.fetch;
const mockFetch = vi.fn();

beforeEach(() => {
  global.fetch = mockFetch;
  mockFetch.mockReset();
});

afterEach(() => {
  global.fetch = originalFetch;
});

// ---------------------------------------------------------------------------
// Import after mock setup — these are pure functions that use fetch internally
// ---------------------------------------------------------------------------

import { requestAssistantReply, streamMessage } from "../lib/chat";

// =============================================================================
// requestAssistantReply
// =============================================================================

describe("requestAssistantReply", () => {
  it("returns formatted content on success", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ content: "The market looks strong today." }),
    });

    const result = await requestAssistantReply([], "How is the market?");

    expect(result).toBe("The market looks strong today.");
    expect(mockFetch).toHaveBeenCalledOnce();

    // Verify the request was properly formed
    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/assistant");
    expect(options.method).toBe("POST");
    const body = JSON.parse(options.body);
    expect(body.messages).toHaveLength(1);
    expect(body.messages[0].role).toBe("user");
    expect(body.messages[0].content).toBe("How is the market?");
  });

  it("returns error message when fetch fails", async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      json: async () => ({ error: "API rate limit exceeded" }),
    });

    const result = await requestAssistantReply([], "test question");

    expect(result).toBe("Error: API rate limit exceeded");
  });

  it("returns generic error when HTTP error has no error field", async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      json: async () => ({}),
    });

    const result = await requestAssistantReply([], "test question");

    expect(result).toBe("Assistant service returned an error.");
  });

  it("returns fallback reply when content is empty", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ content: "" }),
    });

    const result = await requestAssistantReply([], "portfolio");

    // fallbackReply for "portfolio" returns a portfolio-related string
    expect(result).toContain("Portfolio");
  });

  it("returns fallback reply when content is whitespace-only", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ content: "   " }),
    });

    const result = await requestAssistantReply([], "unknown question");

    // Portfolio-pivot default fallbackReply
    expect(result).toContain("portfolio");
  });

  it("passes conversation history to the API", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ content: "Follow-up response" }),
    });

    const history = [
      { role: "user" as const, content: "Hello" },
      { role: "assistant" as const, content: "Hi there" },
    ];

    await requestAssistantReply(history, "What about AAPL?");

    const [, options] = mockFetch.mock.calls[0];
    const body = JSON.parse(options.body);
    expect(body.messages).toHaveLength(3);
    expect(body.messages[0].content).toBe("Hello");
    expect(body.messages[1].content).toBe("Hi there");
    expect(body.messages[2].content).toBe("What about AAPL?");
  });

  it("formats JSON content as structured output", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        content: JSON.stringify({ ticker: "AAPL", price: 175.5 }),
      }),
    });

    const result = await requestAssistantReply([], "AAPL price");

    // formatAssistantPayload parses JSON and formats it
    expect(result).toContain("Ticker");
    expect(result).toContain("AAPL");
  });
});

// =============================================================================
// requestPiReply
// =============================================================================

// =============================================================================
// streamMessage
// =============================================================================

describe("streamMessage", () => {
  it("calls setMessages at least once for normal text", async () => {
    const setMessages = vi.fn();

    await streamMessage("msg-1", "Hello, this is a test message.", setMessages);

    expect(setMessages).toHaveBeenCalled();

    // The last call should contain the full text
    const lastCall = setMessages.mock.calls[setMessages.mock.calls.length - 1];
    const updater = lastCall[0];
    // The updater is a function; call it with a mock current state
    const current = [
      { id: "msg-1", role: "assistant", content: "", timestamp: "10:00" },
    ];
    const result = updater(current);
    expect(result[0].content).toBe("Hello, this is a test message.");
  });

  it("handles empty string by using fallback message", async () => {
    const setMessages = vi.fn();

    await streamMessage("msg-2", "", setMessages);

    expect(setMessages).toHaveBeenCalled();

    // Should use fallback "No output returned."
    const lastCall = setMessages.mock.calls[setMessages.mock.calls.length - 1];
    const updater = lastCall[0];
    const current = [
      { id: "msg-2", role: "assistant", content: "", timestamp: "10:00" },
    ];
    const result = updater(current);
    expect(result[0].content).toBe("No output returned.");
  });

  it("preserves other messages in the array", async () => {
    const setMessages = vi.fn();

    await streamMessage("msg-target", "Updated content", setMessages);

    expect(setMessages).toHaveBeenCalled();

    const lastCall = setMessages.mock.calls[setMessages.mock.calls.length - 1];
    const updater = lastCall[0];
    const current = [
      {
        id: "msg-other",
        role: "user",
        content: "User question",
        timestamp: "09:59",
      },
      { id: "msg-target", role: "assistant", content: "", timestamp: "10:00" },
    ];
    const result = updater(current);
    // Other messages should be untouched
    expect(result[0].content).toBe("User question");
    // Target message should be updated
    expect(result[1].content).toBe("Updated content");
  });

  it("streams long text in chunks", async () => {
    const setMessages = vi.fn();

    // Create a message longer than the 120-char chunk size
    const longText = "A".repeat(300);

    await streamMessage("msg-3", longText, setMessages);

    // Should be called multiple times (300 / 120 = 3 chunks)
    expect(setMessages.mock.calls.length).toBeGreaterThanOrEqual(3);

    // Final call should have the full text
    const lastCall = setMessages.mock.calls[setMessages.mock.calls.length - 1];
    const updater = lastCall[0];
    const current = [
      { id: "msg-3", role: "assistant", content: "", timestamp: "10:00" },
    ];
    const result = updater(current);
    expect(result[0].content).toBe(longText);
  });
});
