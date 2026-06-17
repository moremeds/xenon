// @vitest-environment jsdom
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import Sidebar from "../components/Sidebar";

// Unmount after each test so next/link's mount-time prefetch (IntersectionObserver
// / idle callback) is torn down before the jsdom env is destroyed. Without this
// the leaked callbacks fire after teardown as "window is not defined" unhandled
// errors, which vitest 4 reports as a non-zero exit even though all tests pass.
afterEach(cleanup);

const base = { activeSection: "portfolio" as const, actionTone: "#fff" };

describe("Sidebar subscribers", () => {
  it("renders a live subscriber row with its id and age", () => {
    render(
      <Sidebar
        {...base}
        subscribersReachable
        subscribers={[
          { id: "hedge-bot", connected: true, lastPongMsAgo: 3000 },
        ]}
        anonymousCount={0}
      />,
    );
    expect(screen.getByText("hedge-bot")).toBeTruthy();
    expect(screen.getByText("3s")).toBeTruthy();
  });

  it("renders an offline subscriber row", () => {
    render(
      <Sidebar
        {...base}
        subscribersReachable
        subscribers={[
          { id: "scalper", connected: false, offlineForMs: 120_000 },
        ]}
        anonymousCount={0}
      />,
    );
    expect(screen.getByText("offline 2m")).toBeTruthy();
  });

  it("shows stream offline when unreachable", () => {
    render(
      <Sidebar
        {...base}
        subscribersReachable={false}
        subscribers={[]}
        anonymousCount={0}
      />,
    );
    expect(screen.getByText("stream offline")).toBeTruthy();
  });

  it("shows none when reachable with no subscribers", () => {
    render(
      <Sidebar
        {...base}
        subscribersReachable
        subscribers={[]}
        anonymousCount={0}
      />,
    );
    expect(screen.getByText("none")).toBeTruthy();
  });

  it("shows the anonymous app-client count", () => {
    render(
      <Sidebar
        {...base}
        subscribersReachable
        subscribers={[]}
        anonymousCount={2}
      />,
    );
    expect(screen.getByText("+2 app clients")).toBeTruthy();
  });
});
