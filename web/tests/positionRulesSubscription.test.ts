import { afterEach, describe, expect, it, vi } from "vitest";

import { subscribePositionRulesRealtime } from "@/lib/realtime/positionRulesSubscription";

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  onmessage: (() => void) | null = null;
  closed = false;

  constructor(public readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  close() {
    this.closed = true;
  }
}

afterEach(() => {
  FakeEventSource.instances = [];
  vi.unstubAllGlobals();
});

describe("subscribePositionRulesRealtime", () => {
  it("subscribes to position_rule.transition and closes cleanly", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const onEvent = vi.fn();

    const unsubscribe = subscribePositionRulesRealtime(onEvent);

    expect(FakeEventSource.instances[0].url).toBe(
      "/api/realtime?channel=position_rule.transition",
    );
    FakeEventSource.instances[0].onmessage?.();
    expect(onEvent).toHaveBeenCalledOnce();

    unsubscribe();
    expect(FakeEventSource.instances[0].closed).toBe(true);
  });
});
