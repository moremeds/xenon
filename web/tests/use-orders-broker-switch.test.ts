/**
 * @vitest-environment jsdom
 *
 * Account-tab switching must not let one broker's open orders linger on, or
 * flicker back over, the other broker's. Regression for the IB↔FUTU open-order
 * flicker: a slow/in-flight response for the previous broker (or a mid-switch
 * POST sync) must never overwrite the current broker's orders.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { renderHook, waitFor, cleanup, act } from "@testing-library/react";
import { useOrders } from "@/lib/useOrders";

const originalFetch = global.fetch;

afterEach(() => {
  cleanup();
  global.fetch = originalFetch;
  vi.restoreAllMocks();
});

function ordersBody(broker: "IB" | "FUTU") {
  // `last_sync` doubles as a marker so the test can tell whose data is shown.
  return { open_orders: [], executed_orders: [], last_sync: `${broker}-sync` };
}

function jsonRes(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("useOrders — account-tab (broker) switching", () => {
  it("loads the new broker's orders after switching tabs", async () => {
    global.fetch = vi.fn((url: string | URL) =>
      Promise.resolve(
        jsonRes(
          ordersBody(String(url).includes("broker=FUTU") ? "FUTU" : "IB"),
        ),
      ),
    ) as typeof fetch;

    const { result, rerender } = renderHook(({ b }) => useOrders(false, b), {
      initialProps: { b: "IB" as "IB" | "FUTU" },
    });

    await waitFor(() => expect(result.current.data?.last_sync).toBe("IB-sync"));

    rerender({ b: "FUTU" });
    await waitFor(() =>
      expect(result.current.data?.last_sync).toBe("FUTU-sync"),
    );
  });

  it("ignores a stale slow response from the previous broker after switching", async () => {
    let resolveIB!: (r: Response) => void;
    const ibPending = new Promise<Response>((r) => {
      resolveIB = r;
    });

    global.fetch = vi.fn(
      (url: string | URL) =>
        String(url).includes("broker=FUTU")
          ? Promise.resolve(jsonRes(ordersBody("FUTU")))
          : ibPending, // IB GET hangs until we resolve it below
    ) as typeof fetch;

    const { result, rerender } = renderHook(({ b }) => useOrders(false, b), {
      initialProps: { b: "IB" as "IB" | "FUTU" },
    });

    // Switch to FUTU while the IB request is still in flight.
    rerender({ b: "FUTU" });
    await waitFor(() =>
      expect(result.current.data?.last_sync).toBe("FUTU-sync"),
    );

    // The stale IB response finally arrives — it must NOT clobber FUTU.
    await act(async () => {
      resolveIB(jsonRes(ordersBody("IB")));
      await Promise.resolve();
    });

    expect(result.current.data?.last_sync).toBe("FUTU-sync");
  });
});
