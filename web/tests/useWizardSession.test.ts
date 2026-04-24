// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

import { useWizardSession } from "@/lib/useWizardSession";

function buildSse(events: { type?: string; data: unknown }[]): string {
  return events
    .map((event) => {
      const prefix = event.type ? `event: ${event.type}\n` : "";
      return `${prefix}data: ${JSON.stringify(event.data)}\n\n`;
    })
    .join("");
}

function mockSseResponse(sseText: string): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(sseText));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

describe("useWizardSession", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("streams wizard SSE state into the hook", async () => {
    const sse = buildSse([
      {
        type: "session",
        data: {
          session_id: "wiz-1",
          state: "WORKING",
          structure_name: "Bull Call Spread",
        },
      },
    ]);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(mockSseResponse(sse)),
    );

    const { result } = renderHook(() => useWizardSession("wiz-1"));

    await waitFor(() => {
      expect(result.current.session?.state).toBe("WORKING");
    });
    expect(result.current.session?.structure_name).toBe("Bull Call Spread");
    expect(result.current.error).toBeNull();
  });
});
