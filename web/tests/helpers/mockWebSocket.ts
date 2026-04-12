/**
 * Reusable MockWebSocket for Vitest.
 *
 * Usage:
 *   const { install, instances } = createMockWebSocket();
 *   install(); // replaces globalThis.WebSocket
 *   // ... trigger hook that creates WS ...
 *   instances[0].simulateMessage({ type: "price", data: {...} });
 */
import { vi } from "vitest";

export type MockWSInstance = {
  url: string;
  readyState: number;
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onclose: ((ev: CloseEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  send: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  simulateOpen: () => void;
  simulateMessage: (data: unknown) => void;
  simulateClose: (code?: number) => void;
  simulateError: () => void;
};

export function createMockWebSocket() {
  const instances: MockWSInstance[] = [];
  const original = globalThis.WebSocket;

  function MockWS(url: string) {
    const instance: MockWSInstance = {
      url,
      readyState: 0, // CONNECTING
      onopen: null,
      onmessage: null,
      onclose: null,
      onerror: null,
      send: vi.fn(),
      close: vi.fn(() => {
        instance.readyState = 3;
      }),
      simulateOpen() {
        instance.readyState = 1;
        instance.onopen?.({ type: "open" } as Event);
      },
      simulateMessage(data: unknown) {
        instance.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
      },
      simulateClose(code = 1000) {
        instance.readyState = 3;
        instance.onclose?.({ code, reason: "" } as CloseEvent);
      },
      simulateError() {
        instance.onerror?.({ type: "error" } as Event);
      },
    };
    instances.push(instance);
    return instance;
  }

  return {
    instances,
    install: () => {
      globalThis.WebSocket = MockWS as unknown as typeof WebSocket;
    },
    restore: () => {
      globalThis.WebSocket = original;
    },
  };
}
