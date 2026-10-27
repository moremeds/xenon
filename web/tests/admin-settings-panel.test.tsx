/** @vitest-environment jsdom */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import RuntimeSettingsPanel from "@/components/operator/RuntimeSettingsPanel";
import { settingsErrorMessage } from "@/lib/settingsReasonCodes";
import { settingsFixture } from "./fixtures/runtimeSettings";

type Call = { url: string; method: string; body: unknown };

function mockFetch(responder: (call: Call) => { status: number; body: unknown } | undefined) {
  const calls: Call[] = [];
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const call = {
      url: String(url),
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    };
    calls.push(call);
    const custom = responder(call);
    if (custom) return new Response(JSON.stringify(custom.body), { status: custom.status });
    if (call.method === "GET") return new Response(JSON.stringify({ settings: settingsFixture() }), { status: 200 });
    return new Response(JSON.stringify({ changed: true }), { status: 200 });
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

const row = (key: string) => screen.getByTestId(`setting-${key}`);

beforeEach(() => {
  vi.useRealTimers();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("RuntimeSettingsPanel", () => {
  it("groups by category and shows effective / env / override / last change", async () => {
    mockFetch(() => undefined);
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByTestId("setting-XENON_FUTU_MKT_NOTIONAL_CAP");
    const groups = screen.getAllByTestId(/^settings-group-/).map((g) => g.dataset.testid);
    expect(groups).toEqual(["settings-group-safety", "settings-group-toggle", "settings-group-tuning"]);
    const cap = row("XENON_FUTU_MKT_NOTIONAL_CAP");
    expect(within(cap).getByTestId("effective").textContent).toContain("10000");
    expect(within(cap).getByTestId("env").textContent).toContain("25000");
    expect(within(cap).getByTestId("override").textContent).toContain("10000");
    expect(within(cap).getByTestId("last-change").textContent).toContain("internal-ui");
    expect(within(cap).getByTestId("last-change").textContent).toContain("fixture tighten");
    expect(within(cap).getByText("narrow-only")).toBeTruthy();
  });

  it("shows owner env for relay / futu-quotes keys instead of a guess", async () => {
    mockFetch(() => undefined);
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    const quotes = await screen.findByTestId("setting-XENON_QUOTES_DEFAULT");
    expect(within(quotes).getByTestId("env").textContent).toContain("env: set in relay");
    expect(within(quotes).getByTestId("effective").textContent).toContain("env: set in relay");
    const quota = row("FUTU_QUOTE_QUOTA");
    expect(within(quota).getByTestId("env").textContent).toContain("env: set in futu-quotes");
    expect(within(quota).getByTestId("effective").textContent).toContain("600");
  });

  it("orders_paused toggle is prominent; pause = PUT true, resume = DELETE", async () => {
    let paused = false;
    const calls = mockFetch((c) => {
      if (c.method === "GET") {
        const rows = settingsFixture().map((r) =>
          r.key === "orders_paused" ? { ...r, db_value: paused || null, effective: paused } : r,
        );
        return { status: 200, body: { settings: rows } };
      }
      if (c.method === "PUT") paused = true;
      if (c.method === "DELETE") paused = false;
      return undefined;
    });
    const toast = vi.fn();
    render(<RuntimeSettingsPanel onToast={toast} />);
    const banner = await screen.findByTestId("orders-paused-banner");
    expect(banner.textContent).toContain("Orders active");
    fireEvent.click(within(banner).getByRole("button", { name: "Pause orders" }));
    await waitFor(() => expect(screen.getByTestId("orders-paused-banner").textContent).toContain("Orders paused"));
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.url).toBe("/api/admin/settings/orders_paused");
    expect(put.body).toEqual({ value: true });
    fireEvent.click(within(screen.getByTestId("orders-paused-banner")).getByRole("button", { name: "Resume orders" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE")).toBe(true));
    expect(calls.find((c) => c.method === "DELETE")!.url).toBe("/api/admin/settings/orders_paused");
  });

  it("sends confirm only after the typed confirm step (428 → step → resend)", async () => {
    const calls = mockFetch((c) => {
      if (c.method === "PUT" && !(c.body as { confirm?: string }).confirm) {
        return { status: 428, body: { reason_code: "SETTING_CONFIRM_REQUIRED", detail: "needs confirm" } };
      }
      return undefined;
    });
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    const audit = await screen.findByTestId("setting-XENON_AUDIT_CANCEL");
    fireEvent.change(within(audit).getByLabelText("New value"), { target: { value: "true" } });
    fireEvent.click(within(audit).getByRole("button", { name: "Save" }));
    const step = await within(audit).findByTestId("confirm-step");
    const go = within(step).getByRole("button", { name: "Confirm change" });
    expect((go as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(step).getByLabelText("Type XENON_AUDIT_CANCEL to confirm"), {
      target: { value: "XENON_AUDIT" },
    });
    expect((go as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(step).getByLabelText("Type XENON_AUDIT_CANCEL to confirm"), {
      target: { value: "XENON_AUDIT_CANCEL" },
    });
    fireEvent.click(go);
    await waitFor(() => expect(calls.filter((c) => c.method === "PUT")).toHaveLength(2));
    const [first, second] = calls.filter((c) => c.method === "PUT");
    expect(first.body).toEqual({ value: true });
    expect(second.body).toEqual({ value: true, confirm: "XENON_AUDIT_CANCEL" });
  });

  it("narrow-only set offers only the env values", async () => {
    const calls = mockFetch(() => undefined);
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    const trading = await screen.findByTestId("setting-XENON_FUTU_TRADING");
    expect(within(trading).getByLabelText("simulate")).toBeTruthy();
    expect(within(trading).queryByLabelText("real")).toBeNull();
    fireEvent.click(within(trading).getByLabelText("simulate"));
    fireEvent.click(within(trading).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    expect(calls.find((c) => c.method === "PUT")!.body).toEqual({ value: [] });
  });

  it("revert to env sends DELETE; errors surface their reason_code copy", async () => {
    const calls = mockFetch((c) =>
      c.method === "DELETE" ? { status: 403, body: { reason_code: "READ_ONLY_MODE", detail: "x" } } : undefined,
    );
    const toast = vi.fn();
    render(<RuntimeSettingsPanel onToast={toast} />);
    const cap = await screen.findByTestId("setting-XENON_FUTU_MKT_NOTIONAL_CAP");
    fireEvent.click(within(cap).getByRole("button", { name: "Revert to env" }));
    await waitFor(() => expect(toast).toHaveBeenCalled());
    expect(calls.find((c) => c.method === "DELETE")!.url).toBe("/api/admin/settings/XENON_FUTU_MKT_NOTIONAL_CAP");
    expect(toast).toHaveBeenCalledWith("error", settingsErrorMessage({ reason_code: "READ_ONLY_MODE" }, ""));
  });

  it("an invalid number is a 400 INVALID_VALUE toast", async () => {
    mockFetch((c) =>
      c.method === "PUT"
        ? { status: 400, body: { reason_code: "SETTING_INVALID_VALUE", detail: "FUTU_QUOTE_QUOTA: must be an integer in [1, 100000]" } }
        : undefined,
    );
    const toast = vi.fn();
    render(<RuntimeSettingsPanel onToast={toast} />);
    const quota = await screen.findByTestId("setting-FUTU_QUOTE_QUOTA");
    // in bounds but not an integer: only the API rejects it
    fireEvent.change(within(quota).getByLabelText("New value"), { target: { value: "1.5" } });
    fireEvent.click(within(quota).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toast).toHaveBeenCalled());
    expect(toast.mock.calls[0][1]).toContain("must be an integer in [1, 100000]");
  });
});

describe("settingsErrorMessage", () => {
  it("maps settings and auth reason codes, keeps validator detail", () => {
    expect(settingsErrorMessage({ reason_code: "READ_ONLY_MODE" }, "f")).toMatch(/read-only/i);
    expect(settingsErrorMessage({ reason_code: "API_KEY_FORBIDDEN" }, "f")).toMatch(/internal token/i);
    expect(settingsErrorMessage({ reason_code: "SETTING_WIDENS_ENV", detail: "D" }, "f")).toMatch(/D$/);
    expect(settingsErrorMessage({ detail: { reason_code: "SETTING_UNKNOWN" } }, "f")).toMatch(/unknown/i);
    expect(settingsErrorMessage(null, "fallback")).toBe("fallback");
  });
});

describe("ORDERS_PAUSED order toast", () => {
  it("order paths render a pause message, not the unknown fallback", async () => {
    const { getReasonToast } = await import("@/lib/orderReasonCodes");
    expect(getReasonToast("ORDERS_PAUSED").copy).toMatch(/paused/i);
  });
});

// ─── R2 review regressions ────────────────────────────────────────────────

const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const pausedRows = (paused: boolean) =>
  settingsFixture().map((s) => (s.key === "orders_paused" ? { ...s, effective: paused, db_value: paused ? true : null } : s));

describe("RuntimeSettingsPanel (R2 regressions)", () => {
  it("Resume and a confirmed Revert pass the real /api middleware", async () => {
    const { NextRequest } = await import("next/server");
    const { default: middleware } = await import("../middleware");
    const passed: { method: string; url: string }[] = [];
    const blocked: unknown[] = [];
    let deletes = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (!init || !init.method || init.method === "GET") return reply({ settings: pausedRows(true) });
        const headers = new Headers(init.headers);
        headers.set("host", "localhost:3200");
        headers.set("origin", "http://localhost:3200");
        const res = middleware(new NextRequest(`http://localhost:3200${url}`, { ...init, headers }));
        if (!res.headers.get("x-middleware-next")) {
          blocked.push(await res.clone().json());
          return res;
        }
        passed.push({ method: init.method, url });
        deletes += 1;
        // the cap revert needs a confirm the first time (handler-level 428)
        if (url.includes("MKT_NOTIONAL_CAP") && !url.includes("confirm=")) {
          return reply({ reason_code: "SETTING_CONFIRM_REQUIRED" }, 428);
        }
        return reply({ changed: true });
      }),
    );
    const toast = vi.fn();
    render(<RuntimeSettingsPanel onToast={toast} />);
    fireEvent.click(await screen.findByRole("button", { name: "Resume orders" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("success", "orders_paused reverted to env"));

    const cap = row("XENON_FUTU_MKT_NOTIONAL_CAP");
    fireEvent.click(within(cap).getByRole("button", { name: "Revert to env" }));
    const step = await within(cap).findByTestId("confirm-step");
    fireEvent.change(within(step).getByLabelText("Type XENON_FUTU_MKT_NOTIONAL_CAP to confirm"), {
      target: { value: "XENON_FUTU_MKT_NOTIONAL_CAP" },
    });
    fireEvent.click(within(step).getByRole("button", { name: "Confirm change" }));
    await waitFor(() => expect(deletes).toBe(3));
    expect(blocked).toEqual([]);
    expect(passed.map((p) => p.method)).toEqual(["DELETE", "DELETE", "DELETE"]);
    expect(passed[2].url).toContain("confirm=XENON_FUTU_MKT_NOTIONAL_CAP");
  });

  it("editing the value after a 428 drops the pending confirm; a fresh unconfirmed request follows", async () => {
    const sent: { value: boolean; confirm?: string }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        if (!init || !init.method || init.method === "GET") return reply({ settings: pausedRows(false) });
        const body = JSON.parse(String(init.body));
        sent.push(body);
        return body.confirm ? reply({ changed: true }) : reply({ reason_code: "SETTING_CONFIRM_REQUIRED" }, 428);
      }),
    );
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    const r = await screen.findByTestId("setting-XENON_AUDIT_CANCEL");
    const editor = within(r).getByLabelText("New value") as HTMLSelectElement;
    fireEvent.change(editor, { target: { value: "true" } });
    fireEvent.click(within(r).getByRole("button", { name: "Save" }));
    await within(r).findByTestId("confirm-step");
    fireEvent.change(editor, { target: { value: "false" } });
    expect(within(r).queryByTestId("confirm-step")).toBeNull();
    // a note change also drops it
    fireEvent.change(editor, { target: { value: "true" } });
    fireEvent.click(within(r).getByRole("button", { name: "Save" }));
    await within(r).findByTestId("confirm-step");
    fireEvent.change(within(r).getByLabelText("Note"), { target: { value: "why" } });
    expect(within(r).queryByTestId("confirm-step")).toBeNull();
    expect(sent).toEqual([{ value: true }, { value: true }]);
  });

  it("a delayed pre-write poll cannot overwrite the post-write state", async () => {
    let poll!: () => Promise<void>;
    const realInterval = globalThis.setInterval;
    vi.spyOn(globalThis, "setInterval").mockImplementation(((fn: () => Promise<void>, delay: number) => {
      if (delay === 15000) {
        poll = fn;
        return 999;
      }
      return realInterval(fn, delay);
    }) as typeof setInterval);
    let gets = 0;
    let resolveOld!: (value: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        if (init?.method === "PUT") return reply({ changed: true });
        gets += 1;
        if (gets === 2) return new Promise<Response>((resolve) => (resolveOld = resolve));
        return reply({ settings: pausedRows(gets >= 3) });
      }),
    );
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByRole("button", { name: "Pause orders" });
    let pendingPoll!: Promise<void>;
    act(() => {
      pendingPoll = poll();
    });
    fireEvent.click(screen.getByRole("button", { name: "Pause orders" }));
    await screen.findByRole("button", { name: "Resume orders" });
    await act(async () => {
      resolveOld(reply({ settings: pausedRows(false) }));
      await pendingPoll;
    });
    expect(screen.getByTestId("orders-paused-banner").textContent).toContain("Orders paused");
    expect(screen.getByRole("button", { name: "Resume orders" })).toBeTruthy();
  });

  it("a value above the env cap is refused before sending", async () => {
    const sent: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        if (!init || !init.method || init.method === "GET") return reply({ settings: pausedRows(false) });
        sent.push(JSON.parse(String(init.body)));
        return reply({ changed: true });
      }),
    );
    const toast = vi.fn();
    render(<RuntimeSettingsPanel onToast={toast} />);
    const r = await screen.findByTestId("setting-XENON_FUTU_MKT_NOTIONAL_CAP");
    fireEvent.change(within(r).getByLabelText("New value"), { target: { value: "25001" } });
    fireEvent.click(within(r).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("error", expect.stringContaining("25000")));
    expect(sent).toEqual([]);
    fireEvent.change(within(r).getByLabelText("New value"), { target: { value: "25000" } });
    fireEvent.click(within(r).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(sent).toEqual([{ value: 25000 }]));
  });
});
