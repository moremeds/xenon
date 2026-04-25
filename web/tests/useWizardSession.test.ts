// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

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

  it("refreshes the one-shot wizard stream on demand", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        mockSseResponse(
          buildSse([
            {
              type: "session",
              data: {
                session_id: "wiz-1",
                state: "planned",
                structure_name: "Bull Call Spread",
              },
            },
          ]),
        ),
      )
      .mockResolvedValueOnce(
        mockSseResponse(
          buildSse([
            {
              type: "session",
              data: {
                session_id: "wiz-1",
                state: "working",
                structure_name: "Bull Call Spread",
                current_attempt_id: "attempt-1",
              },
            },
          ]),
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useWizardSession("wiz-1"));
    await waitFor(() => {
      expect(result.current.session?.state).toBe("planned");
    });

    act(() => result.current.refresh());

    await waitFor(() => {
      expect(result.current.session?.state).toBe("working");
    });
    expect(result.current.session?.current_attempt_id).toBe("attempt-1");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
