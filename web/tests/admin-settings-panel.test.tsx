/** @vitest-environment jsdom */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import RuntimeSettingsPanel from "@/components/operator/RuntimeSettingsPanel";
import { settingsErrorMessage } from "@/lib/settingsReasonCodes";
import { settingsFixture } from "./fixtures/runtimeSettings";

type Call = { url: string; method: string; body: unknown; contentType: string | null };

function mockFetch(
  responder: (call: Call) => { status: number; body: unknown } | Promise<Response> | undefined,
) {
  const calls: Call[] = [];
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const call = {
      url: String(url),
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      contentType: new Headers(init?.headers).get("content-type"),
    };
    calls.push(call);
    const custom = responder(call);
    if (custom instanceof Promise) return custom;
    if (custom) return new Response(JSON.stringify(custom.body), { status: custom.status });
    if (call.method === "GET") return new Response(JSON.stringify({ settings: settingsFixture() }), { status: 200 });
    return new Response(JSON.stringify({ changed: true }), { status: 200 });
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const pausedRows = (paused: boolean) =>
  settingsFixture().map((s) => (s.key === "orders_paused" ? { ...s, effective: paused, db_value: paused ? true : null } : s));

const row = (key: string) => screen.getByTestId(`setting-${key}`);
const writes = (calls: Call[]) => calls.filter((c) => c.method !== "GET");
const need428 = { status: 428, body: { reason_code: "SETTING_CONFIRM_REQUIRED", detail: "needs confirm" } };
const field = (key: string, label: string) => within(row(key)).getByLabelText(label) as HTMLInputElement;

beforeEach(() => {
  vi.useRealTimers();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("RuntimeSettingsPanel", () => {
  it("groups as sections with human labels, the key as secondary text, and source chips", async () => {
    mockFetch(() => undefined);
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByTestId("setting-XENON_FUTU_MKT_NOTIONAL_CAP");
    const groups = screen.getAllByTestId(/^settings-group-/).map((g) => g.dataset.testid);
    expect(groups).toEqual(["settings-group-safety", "settings-group-toggle", "settings-group-tuning"]);
    expect(screen.getByTestId("settings-group-toggle").textContent).toContain("Quote routing");
    const cap = row("XENON_FUTU_MKT_NOTIONAL_CAP");
    expect(within(cap).getByTestId("label").textContent).toBe("Futu market-order cap");
    expect(within(cap).getByTestId("key").textContent).toBe("XENON_FUTU_MKT_NOTIONAL_CAP");
    expect(field("XENON_FUTU_MKT_NOTIONAL_CAP", "Futu market-order cap").value).toBe("10000");
    expect(cap.textContent).toContain("USD");
    expect(cap.textContent).toContain("Env cap 25,000 USD");
    expect(within(cap).getByTestId("source").textContent).toBe("override");
    expect(cap.dataset.overridden).toBe("true");
    expect(within(cap).getByTestId("last-change").textContent).toContain("internal-ui");
    expect(within(cap).getByTestId("last-change").textContent).toContain("tighten before first live order");
    const audit = row("XENON_AUDIT_CANCEL");
    const sw = within(audit).getByRole("switch", { name: "Naked-short audit: cancel violations" });
    expect(sw.getAttribute("aria-checked")).toBe("false");
    expect(within(audit).getByTestId("effective").textContent).toBe("Off");
    expect(within(audit).getByTestId("source").textContent).toBe("env");
    expect(audit.dataset.overridden).toBe("false");
    expect(within(audit).queryByTestId("last-change")).toBeNull(); // only when present
    expect(within(audit).queryByRole("button", { name: "Revert to env" })).toBeNull();
    const typed = within(audit).getByText("typed confirm");
    expect(typed.getAttribute("title")).toMatch(/type the key/i);
    expect(within(row("XENON_GATE4_NETTING")).queryByText("typed confirm")).toBeNull();
  });

  it("has no Edit step: no Edit, Save, Cancel or note field anywhere", async () => {
    mockFetch(() => undefined);
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    const panel = await screen.findByTestId("runtime-settings");
    await screen.findByTestId("setting-XENON_AUDIT_CANCEL");
    for (const name of ["Edit", "Save", "Cancel"]) expect(within(panel).queryAllByRole("button", { name })).toHaveLength(0);
    expect(within(panel).queryAllByLabelText(/note/i)).toHaveLength(0);
    expect(screen.queryAllByTestId("editor")).toHaveLength(0);
  });

  it("marks narrow-only per row (not as a blanket group rule), never uses --- placeholders", async () => {
    mockFetch(() => undefined);
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByTestId("setting-XENON_FUTU_MKT_NOTIONAL_CAP");
    const panel = screen.getByTestId("runtime-settings");
    expect(panel.textContent).not.toMatch(/Overrides here can only tighten/);
    for (const key of ["XENON_FUTU_TRADING", "XENON_FUTU_MKT_NOTIONAL_CAP", "XENON_GATE4_NETTING"]) {
      const chip = within(row(key)).getByText("narrow-only");
      expect(chip.getAttribute("title")).toMatch(/can only tighten/);
    }
    expect(within(row("XENON_AUDIT_CANCEL")).queryByText("narrow-only")).toBeNull();
    // `and` rule with the env off: the switch can only stay off
    expect((within(row("XENON_GATE4_NETTING")).getByRole("switch") as HTMLButtonElement).disabled).toBe(true);
    expect(panel.textContent).not.toContain("---");
    expect(panel.textContent).not.toContain("applies live");
  });

  it("a nonzero value never displays as 0", async () => {
    mockFetch((c) =>
      c.method === "GET"
        ? {
            status: 200,
            body: {
              settings: settingsFixture().map((r) =>
                r.key === "FUTU_BOOK_STALE_S" ? { ...r, db_value: 1e-7, effective: 1e-7 } : r,
              ),
            },
          }
        : undefined,
    );
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByTestId("setting-FUTU_BOOK_STALE_S");
    expect(field("FUTU_BOOK_STALE_S", "Order-book staleness cutoff").value).toBe("1e-7");
    expect(field("IB_REALTIME_STALE_TICK_MS", "Stale tick threshold").value).toBe("60000");
  });

  it("relay keys show the relay's live value; futu-quotes keys without override show service default", async () => {
    mockFetch((c) =>
      c.method === "GET"
        ? {
            status: 200,
            body: {
              settings: settingsFixture().map((r) =>
                r.key === "IB_REALTIME_MAX_LINES" ? { ...r, live_value: undefined } : r,
              ),
            },
          }
        : undefined,
    );
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    const quotes = await screen.findByTestId("setting-XENON_QUOTES_DEFAULT");
    const seg = within(quotes).getByRole("group", { name: "Default quote source" });
    expect(within(seg).getByRole("button", { name: "ib" }).getAttribute("aria-pressed")).toBe("true");
    expect(within(seg).getByRole("button", { name: "futu-first" }).getAttribute("aria-pressed")).toBe("false");
    expect(within(quotes).getByTestId("source").textContent).toBe("live (relay)");
    const lines = field("IB_REALTIME_MAX_LINES", "IB line budget");
    expect(lines.value).toBe("");
    expect(lines.placeholder).toBe("Not reported by relay");
    expect(field("FUTU_BOOK_STALE_S", "Order-book staleness cutoff").placeholder).toBe("Service default");
    expect(field("IB_REALTIME_STALE_TICK_MS", "Stale tick threshold").value).toBe("60000");
    expect(within(row("FUTU_QUOTE_QUOTA")).queryByTestId("source")).toBeNull();
    expect(screen.getByTestId("runtime-settings").textContent).not.toContain("env: set in");
  });

  it("tuples read as words with the raw tuple as title", async () => {
    mockFetch(() => undefined);
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    const tuples = await screen.findByTestId("setting-XENON_FUTU_TUPLES_LIVE");
    const stock = within(tuples).getByRole("button", { name: "Stock · Limit · Day · RTH" });
    expect(stock.getAttribute("title")).toBe("STK:LMT:DAY:RTH");
    expect(stock.getAttribute("aria-pressed")).toBe("true");
    expect(within(tuples).getByRole("button", { name: "Option · Limit · GTC · RTH" })).toBeTruthy();
  });

  it("orders status strip: pause = PUT true, resume = DELETE", async () => {
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
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
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
    expect(screen.queryByTestId("setting-orders_paused")).toBeNull(); // only in the strip
  });

  it("a switch click sends one PUT with the new boolean", async () => {
    const calls = mockFetch(() => undefined);
    const toast = vi.fn();
    render(<RuntimeSettingsPanel onToast={toast} />);
    await screen.findByTestId("setting-XENON_FUTU_OPTIONS");
    fireEvent.click(within(row("XENON_FUTU_OPTIONS")).getByRole("switch", { name: "Futu option quotes" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("success", "XENON_FUTU_OPTIONS saved"));
    expect(writes(calls)).toEqual([
      expect.objectContaining({ method: "PUT", url: "/api/admin/settings/XENON_FUTU_OPTIONS", body: { value: true } }),
    ]);
  });

  it("a switch works from the keyboard (Space and Enter reach the native button)", async () => {
    mockFetch(() => undefined);
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByTestId("setting-XENON_FUTU_OPTIONS");
    const sw = within(row("XENON_FUTU_OPTIONS")).getByRole("switch", { name: "Futu option quotes" });
    expect(sw.tagName).toBe("BUTTON");
    expect(sw.getAttribute("type")).toBe("button");
  });

  it("a typed-confirm switch: first PUT without confirm, the 428 step sends confirm", async () => {
    const calls = mockFetch((c) => (c.method === "PUT" && !(c.body as { confirm?: string }).confirm ? need428 : undefined));
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByTestId("setting-XENON_AUDIT_CANCEL");
    const audit = row("XENON_AUDIT_CANCEL");
    fireEvent.click(within(audit).getByRole("switch", { name: "Naked-short audit: cancel violations" }));
    const step = await within(audit).findByTestId("confirm-step");
    // the control never shows the unconfirmed value
    expect(within(audit).getByRole("switch").getAttribute("aria-checked")).toBe("false");
    expect(within(step).getByTestId("confirm-proposal").textContent).toBe("Set Naked-short audit: cancel violations to On");
    expect(within(step).getByTestId("confirm-key").textContent).toBe("XENON_AUDIT_CANCEL");
    const go = within(step).getByRole("button", { name: "Confirm change" });
    expect((go as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(step).getByLabelText("Type XENON_AUDIT_CANCEL to confirm"), {
      target: { value: "XENON_AUDIT_CANCEL" },
    });
    fireEvent.click(go);
    await waitFor(() => expect(calls.filter((c) => c.method === "PUT")).toHaveLength(2));
    const [first, second] = calls.filter((c) => c.method === "PUT");
    expect(first.body).toEqual({ value: true });
    expect(second.body).toEqual({ value: true, confirm: "XENON_AUDIT_CANCEL" });
    await waitFor(() => expect(within(audit).queryByTestId("confirm-step")).toBeNull());
  });

  it("a chip click sends the set with that item toggled; chips stay limited to the env values", async () => {
    const calls = mockFetch(() => undefined);
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByTestId("setting-XENON_FUTU_TUPLES_PAPER");
    fireEvent.click(within(row("XENON_FUTU_TUPLES_PAPER")).getByRole("button", { name: "Stock · Market · Day · RTH" }));
    await waitFor(() => expect(writes(calls)).toHaveLength(1));
    expect(writes(calls)[0].body).toEqual({ value: ["OPT:LMT:DAY:RTH", "STK:LMT:DAY:RTH"] });
    const trading = row("XENON_FUTU_TRADING");
    expect(within(trading).getByRole("button", { name: "Paper (simulate)" }).getAttribute("aria-pressed")).toBe("true");
    expect(within(trading).queryByRole("button", { name: "Live (real)" })).toBeNull();
  });

  it("a segment click sends the enum; the current segment sends nothing", async () => {
    const calls = mockFetch(() => undefined);
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByTestId("setting-XENON_QUOTES_DEFAULT");
    const seg = within(row("XENON_QUOTES_DEFAULT")).getByRole("group", { name: "Default quote source" });
    fireEvent.click(within(seg).getByRole("button", { name: "ib" }));
    expect(writes(calls)).toHaveLength(0);
    fireEvent.click(within(seg).getByRole("button", { name: "futu-first" }));
    await waitFor(() => expect(writes(calls)).toHaveLength(1));
    expect(writes(calls)[0]).toEqual(expect.objectContaining({ method: "PUT", body: { value: "futu-first" } }));
  });

  it("a number applies on Enter; an invalid value shows inline and sends nothing; Escape restores", async () => {
    const calls = mockFetch(() => undefined);
    const toast = vi.fn();
    render(<RuntimeSettingsPanel onToast={toast} />);
    await screen.findByTestId("setting-XENON_FUTU_MKT_NOTIONAL_CAP");
    const label = "Futu market-order cap";
    // above the env cap: refused in the browser, inline
    fireEvent.change(field("XENON_FUTU_MKT_NOTIONAL_CAP", label), { target: { value: "25001" } });
    fireEvent.keyDown(field("XENON_FUTU_MKT_NOTIONAL_CAP", label), { key: "Enter" });
    expect(within(row("XENON_FUTU_MKT_NOTIONAL_CAP")).getByRole("alert").textContent).toBe(
      "Futu market-order cap: 25001 is above the env cap 25,000 USD",
    );
    fireEvent.change(field("XENON_FUTU_MKT_NOTIONAL_CAP", label), { target: { value: "abc" } });
    fireEvent.blur(field("XENON_FUTU_MKT_NOTIONAL_CAP", label));
    expect(within(row("XENON_FUTU_MKT_NOTIONAL_CAP")).getByRole("alert").textContent).toMatch(/enter a number/);
    // Escape restores the server value and clears the error
    fireEvent.keyDown(field("XENON_FUTU_MKT_NOTIONAL_CAP", label), { key: "Escape" });
    expect(field("XENON_FUTU_MKT_NOTIONAL_CAP", label).value).toBe("10000");
    expect(within(row("XENON_FUTU_MKT_NOTIONAL_CAP")).queryByRole("alert")).toBeNull();
    // unchanged: blur sends nothing
    fireEvent.blur(field("XENON_FUTU_MKT_NOTIONAL_CAP", label));
    expect(writes(calls)).toHaveLength(0);
    fireEvent.change(field("XENON_FUTU_MKT_NOTIONAL_CAP", label), { target: { value: "25000" } });
    fireEvent.keyDown(field("XENON_FUTU_MKT_NOTIONAL_CAP", label), { key: "Enter" });
    await waitFor(() => expect(writes(calls)).toHaveLength(1));
    expect(writes(calls)[0].body).toEqual({ value: 25000 });
    expect(toast).not.toHaveBeenCalledWith("error", expect.anything());
  });

  it("a number applies on blur when it changed", async () => {
    const calls = mockFetch(() => undefined);
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByTestId("setting-IB_REALTIME_STALE_TICK_MS");
    const f = field("IB_REALTIME_STALE_TICK_MS", "Stale tick threshold");
    fireEvent.change(f, { target: { value: "45000" } });
    fireEvent.blur(f);
    await waitFor(() => expect(writes(calls)).toHaveLength(1));
    expect(writes(calls)[0].body).toEqual({ value: 45000 });
  });

  it("an integer the API rejects is a 400 INVALID_VALUE toast", async () => {
    mockFetch((c) =>
      c.method === "PUT"
        ? { status: 400, body: { reason_code: "SETTING_INVALID_VALUE", detail: "FUTU_QUOTE_QUOTA: must be an integer in [1, 100000]" } }
        : undefined,
    );
    const toast = vi.fn();
    render(<RuntimeSettingsPanel onToast={toast} />);
    await screen.findByTestId("setting-FUTU_QUOTE_QUOTA");
    const f = field("FUTU_QUOTE_QUOTA", "Futu quote quota");
    fireEvent.change(f, { target: { value: "1.5" } }); // in bounds, not an integer: only the API rejects it
    fireEvent.keyDown(f, { key: "Enter" });
    await waitFor(() => expect(toast).toHaveBeenCalled());
    expect(toast.mock.calls[0][1]).toContain("must be an integer in [1, 100000]");
    await waitFor(() => expect(field("FUTU_QUOTE_QUOTA", "Futu quote quota").value).toBe(""));
  });

  it("a failed PUT rolls the control back to the server value", async () => {
    mockFetch((c) => (c.method === "PUT" ? { status: 403, body: { reason_code: "READ_ONLY_MODE", detail: "x" } } : undefined));
    const toast = vi.fn();
    render(<RuntimeSettingsPanel onToast={toast} />);
    await screen.findByTestId("setting-XENON_FUTU_MKT_NOTIONAL_CAP");
    const label = "Futu market-order cap";
    fireEvent.change(field("XENON_FUTU_MKT_NOTIONAL_CAP", label), { target: { value: "12000" } });
    fireEvent.keyDown(field("XENON_FUTU_MKT_NOTIONAL_CAP", label), { key: "Enter" });
    await waitFor(() => expect(toast).toHaveBeenCalledWith("error", settingsErrorMessage({ reason_code: "READ_ONLY_MODE" }, "")));
    await waitFor(() => expect(field("XENON_FUTU_MKT_NOTIONAL_CAP", label).value).toBe("10000"));
    fireEvent.click(within(row("XENON_FUTU_OPTIONS")).getByRole("switch"));
    await waitFor(() => expect(toast).toHaveBeenCalledTimes(2));
    expect(within(row("XENON_FUTU_OPTIONS")).getByRole("switch").getAttribute("aria-checked")).toBe("false");
  });

  it("controls are disabled with a pending state while a request runs; no optimistic value", async () => {
    let release!: () => void;
    mockFetch((c) =>
      c.method === "PUT" ? new Promise<Response>((resolve) => (release = () => resolve(reply({ changed: true })))) : undefined,
    );
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByTestId("setting-XENON_FUTU_OPTIONS");
    const r = row("XENON_FUTU_OPTIONS");
    const sw = within(r).getByRole("switch") as HTMLButtonElement;
    fireEvent.click(sw);
    await waitFor(() => expect(sw.disabled).toBe(true));
    expect(r.getAttribute("aria-busy")).toBe("true");
    expect(r.textContent).toContain("Applying");
    expect(sw.getAttribute("aria-checked")).toBe("false");
    // other rows stay usable
    expect((within(row("XENON_AUDIT_CANCEL")).getByRole("switch") as HTMLButtonElement).disabled).toBe(false);
    await act(async () => release());
    await waitFor(() => expect((within(row("XENON_FUTU_OPTIONS")).getByRole("switch") as HTMLButtonElement).disabled).toBe(false));
    expect(row("XENON_FUTU_OPTIONS").getAttribute("aria-busy")).toBe("false");
  });

  it("two quick clicks: only the last proposal can confirm", async () => {
    let hold!: () => void;
    let n = 0;
    const calls = mockFetch((c) => {
      if (c.method !== "PUT") return undefined;
      if ((c.body as { confirm?: string }).confirm) return undefined;
      n += 1;
      if (n === 1) return new Promise<Response>((resolve) => (hold = () => resolve(reply(need428.body, 428))));
      return need428;
    });
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByTestId("setting-XENON_FUTU_TUPLES_LIVE");
    const r = row("XENON_FUTU_TUPLES_LIVE");
    const a = "Stock · Limit · GTC · RTH";
    const b = "Option · Limit · GTC · RTH";
    fireEvent.click(within(r).getByRole("button", { name: a }));
    fireEvent.click(within(r).getByRole("button", { name: b })); // row busy: ignored
    await act(async () => hold());
    expect(writes(calls)).toHaveLength(1);
    let step = await within(r).findByTestId("confirm-step");
    expect(within(step).getByTestId("confirm-proposal").textContent).toBe(
      "Set Futu live order types to Option · Limit · Day · RTH, Option · Limit · GTC · RTH, Stock · Limit · Day · RTH",
    );
    // a later click replaces the pending proposal
    fireEvent.click(within(r).getByRole("button", { name: b }));
    expect(within(r).queryByTestId("confirm-step")).toBeNull();
    step = await within(r).findByTestId("confirm-step");
    expect(within(step).getByTestId("confirm-proposal").textContent).toBe(
      "Set Futu live order types to Option · Limit · Day · RTH, Stock · Limit · Day · RTH, Stock · Limit · GTC · RTH",
    );
    fireEvent.change(within(step).getByLabelText("Type XENON_FUTU_TUPLES_LIVE to confirm"), {
      target: { value: "XENON_FUTU_TUPLES_LIVE" },
    });
    fireEvent.click(within(step).getByRole("button", { name: "Confirm change" }));
    await waitFor(() => expect(writes(calls)).toHaveLength(3));
    expect(writes(calls).map((c) => c.body)).toEqual([
      { value: ["OPT:LMT:DAY:RTH", "OPT:LMT:GTC:RTH", "STK:LMT:DAY:RTH"] },
      { value: ["OPT:LMT:DAY:RTH", "STK:LMT:DAY:RTH", "STK:LMT:GTC:RTH"] },
      { value: ["OPT:LMT:DAY:RTH", "STK:LMT:DAY:RTH", "STK:LMT:GTC:RTH"], confirm: "XENON_FUTU_TUPLES_LIVE" },
    ]);
  });

  it("revert to env sends DELETE with a JSON content type and no note; errors toast", async () => {
    const calls = mockFetch((c) =>
      c.method === "DELETE" ? { status: 403, body: { reason_code: "READ_ONLY_MODE", detail: "x" } } : undefined,
    );
    const toast = vi.fn();
    render(<RuntimeSettingsPanel onToast={toast} />);
    await screen.findByTestId("setting-XENON_FUTU_MKT_NOTIONAL_CAP");
    fireEvent.click(within(row("XENON_FUTU_MKT_NOTIONAL_CAP")).getByRole("button", { name: "Revert to env" }));
    await waitFor(() => expect(toast).toHaveBeenCalled());
    const del = calls.find((c) => c.method === "DELETE")!;
    expect(del.url).toBe("/api/admin/settings/XENON_FUTU_MKT_NOTIONAL_CAP");
    expect(del.contentType).toBe("application/json");
    expect(toast).toHaveBeenCalledWith("error", settingsErrorMessage({ reason_code: "READ_ONLY_MODE" }, ""));
  });
});

describe("RuntimeSettingsPanel (toggles review T5)", () => {
  it("T5-1 pointer: mousedown on Revert keeps the draft from applying; only DELETE is sent", async () => {
    const calls = mockFetch(() => undefined);
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByTestId("setting-XENON_FUTU_MKT_NOTIONAL_CAP");
    const f = field("XENON_FUTU_MKT_NOTIONAL_CAP", "Futu market-order cap");
    f.focus();
    fireEvent.change(f, { target: { value: "12000" } });
    const revert = within(row("XENON_FUTU_MKT_NOTIONAL_CAP")).getByRole("button", { name: "Revert to env" });
    // the browser's default mousedown action (focus move -> field blur) is prevented
    expect(fireEvent.mouseDown(revert)).toBe(false);
    // a browser that still moves focus to the link blurs the field toward Revert
    fireEvent.blur(f, { relatedTarget: revert });
    fireEvent.click(revert);
    await waitFor(() => expect(writes(calls)).toHaveLength(1));
    expect(writes(calls)[0]).toEqual(
      expect.objectContaining({ method: "DELETE", url: "/api/admin/settings/XENON_FUTU_MKT_NOTIONAL_CAP" }),
    );
  });

  it("T5-1 keyboard: tabbing from a dirty field to Revert discards the draft; Revert sends only DELETE", async () => {
    const calls = mockFetch(() => undefined);
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByTestId("setting-XENON_FUTU_MKT_NOTIONAL_CAP");
    const f = field("XENON_FUTU_MKT_NOTIONAL_CAP", "Futu market-order cap");
    f.focus();
    fireEvent.change(f, { target: { value: "12000" } });
    const revert = within(row("XENON_FUTU_MKT_NOTIONAL_CAP")).getByRole("button", { name: "Revert to env" });
    fireEvent.blur(f, { relatedTarget: revert }); // Tab moves focus to Revert
    revert.focus();
    expect(field("XENON_FUTU_MKT_NOTIONAL_CAP", "Futu market-order cap").value).toBe("10000");
    expect(writes(calls)).toHaveLength(0);
    fireEvent.click(revert); // Enter / Space on the focused button
    await waitFor(() => expect(writes(calls)).toHaveLength(1));
    expect(writes(calls)[0].method).toBe("DELETE");
  });

  it("T5-2 a formatting-only number edit sends nothing and normalizes the field", async () => {
    const calls = mockFetch(() => undefined);
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByTestId("setting-XENON_FUTU_MKT_NOTIONAL_CAP");
    const cap = field("XENON_FUTU_MKT_NOTIONAL_CAP", "Futu market-order cap");
    fireEvent.change(cap, { target: { value: "10000.0" } });
    fireEvent.blur(cap);
    expect(field("XENON_FUTU_MKT_NOTIONAL_CAP", "Futu market-order cap").value).toBe("10000");
    // relay key without an override: the live value counts as current
    const lines = field("IB_REALTIME_MAX_LINES", "IB line budget");
    fireEvent.change(lines, { target: { value: "85.0" } });
    fireEvent.keyDown(lines, { key: "Enter" });
    expect(field("IB_REALTIME_MAX_LINES", "IB line budget").value).toBe("85");
    expect(writes(calls)).toHaveLength(0);
  });

  it("T5-2 a JSON reformat or key reorder sends nothing; a real change does", async () => {
    const calls = mockFetch((c) =>
      c.method === "GET"
        ? {
            status: 200,
            body: {
              settings: settingsFixture().map((r) =>
                r.key === "FUTU_GREEK_FACTORS" ? { ...r, db_value: { iv: 0.01, delta: 1 } } : r,
              ),
            },
          }
        : undefined,
    );
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByTestId("setting-FUTU_GREEK_FACTORS");
    const label = "Futu greek factors";
    expect(field("FUTU_GREEK_FACTORS", label).value).toBe('{"iv":0.01,"delta":1}');
    fireEvent.change(field("FUTU_GREEK_FACTORS", label), { target: { value: '{ "delta" : 1.0, "iv" : 0.01 }' } });
    fireEvent.blur(field("FUTU_GREEK_FACTORS", label));
    expect(field("FUTU_GREEK_FACTORS", label).value).toBe('{"iv":0.01,"delta":1}');
    expect(writes(calls)).toHaveLength(0);
    fireEvent.change(field("FUTU_GREEK_FACTORS", label), { target: { value: '{"iv":0.01,"delta":2}' } });
    fireEvent.keyDown(field("FUTU_GREEK_FACTORS", label), { key: "Enter" });
    await waitFor(() => expect(writes(calls)).toHaveLength(1));
    expect(writes(calls)[0].body).toEqual({ value: { iv: 0.01, delta: 2 } });
  });

  it("T5-3 the confirm input and Back are disabled while the confirmed write runs", async () => {
    let release!: () => void;
    mockFetch((c) => {
      if (c.method !== "PUT") return undefined;
      if (!(c.body as { confirm?: string }).confirm) return need428;
      return new Promise<Response>((resolve) => (release = () => resolve(reply({ changed: true }))));
    });
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    await screen.findByTestId("setting-XENON_AUDIT_CANCEL");
    const audit = row("XENON_AUDIT_CANCEL");
    fireEvent.click(within(audit).getByRole("switch"));
    const step = await within(audit).findByTestId("confirm-step");
    const typed = within(step).getByLabelText("Type XENON_AUDIT_CANCEL to confirm") as HTMLInputElement;
    fireEvent.change(typed, { target: { value: "XENON_AUDIT_CANCEL" } });
    fireEvent.click(within(step).getByRole("button", { name: "Confirm change" }));
    await waitFor(() => expect(audit.getAttribute("aria-busy")).toBe("true"));
    expect(typed.disabled).toBe(true);
    expect((within(step).getByRole("button", { name: "Back" }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => release());
    await waitFor(() => expect(within(row("XENON_AUDIT_CANCEL")).queryByTestId("confirm-step")).toBeNull());
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
    expect(within(step).getByTestId("confirm-proposal").textContent).toBe("Revert Futu market-order cap to env");
    fireEvent.change(within(step).getByLabelText("Type XENON_FUTU_MKT_NOTIONAL_CAP to confirm"), {
      target: { value: "XENON_FUTU_MKT_NOTIONAL_CAP" },
    });
    fireEvent.click(within(step).getByRole("button", { name: "Confirm change" }));
    await waitFor(() => expect(deletes).toBe(3));
    expect(blocked).toEqual([]);
    expect(passed.map((p) => p.method)).toEqual(["DELETE", "DELETE", "DELETE"]);
    expect(passed[1].url).toBe("/api/admin/settings/XENON_FUTU_MKT_NOTIONAL_CAP");
    expect(passed[2].url).toBe("/api/admin/settings/XENON_FUTU_MKT_NOTIONAL_CAP?confirm=XENON_FUTU_MKT_NOTIONAL_CAP");
  });

  it("editing the value after a 428 drops the pending confirm; Back restores the server value", async () => {
    const sent: { value: unknown; confirm?: string }[] = [];
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
    const r = await screen.findByTestId("setting-XENON_FUTU_MKT_NOTIONAL_CAP");
    const label = "Futu market-order cap";
    fireEvent.change(field("XENON_FUTU_MKT_NOTIONAL_CAP", label), { target: { value: "12000" } });
    fireEvent.keyDown(field("XENON_FUTU_MKT_NOTIONAL_CAP", label), { key: "Enter" });
    const step = await within(r).findByTestId("confirm-step");
    expect(within(step).getByTestId("confirm-proposal").textContent).toBe("Set Futu market-order cap to 12,000 USD");
    fireEvent.change(field("XENON_FUTU_MKT_NOTIONAL_CAP", label), { target: { value: "12001" } });
    expect(within(r).queryByTestId("confirm-step")).toBeNull();
    fireEvent.keyDown(field("XENON_FUTU_MKT_NOTIONAL_CAP", label), { key: "Enter" });
    fireEvent.click(within(await within(r).findByTestId("confirm-step")).getByRole("button", { name: "Back" }));
    expect(within(r).queryByTestId("confirm-step")).toBeNull();
    expect(field("XENON_FUTU_MKT_NOTIONAL_CAP", label).value).toBe("10000");
    expect(sent).toEqual([{ value: 12000 }, { value: 12001 }]);
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
});

describe("RuntimeSettingsPanel (R3 regression)", () => {
  it("a 428 that returns after the proposal changed opens no confirm for the old value", async () => {
    const sent: { value: unknown; confirm?: string }[] = [];
    let release428!: () => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        if (!init || !init.method || init.method === "GET") return reply({ settings: pausedRows(false) });
        const body = JSON.parse(String(init.body));
        sent.push(body);
        if (body.confirm) return reply({ changed: true });
        return new Promise<Response>((resolve) => {
          release428 = () => resolve(reply({ reason_code: "SETTING_CONFIRM_REQUIRED" }, 428));
        });
      }),
    );
    render(<RuntimeSettingsPanel onToast={vi.fn()} />);
    const r = await screen.findByTestId("setting-XENON_FUTU_MKT_NOTIONAL_CAP");
    const f = field("XENON_FUTU_MKT_NOTIONAL_CAP", "Futu market-order cap");
    fireEvent.change(f, { target: { value: "12000" } });
    fireEvent.keyDown(f, { key: "Enter" });
    await waitFor(() => expect(sent).toHaveLength(1)); // PUT 12000 in flight
    // defense in depth: the field is disabled while the request runs, but a
    // proposal change by any path (here Escape) still voids the old 428
    fireEvent.keyDown(f, { key: "Escape" });
    await act(async () => {
      release428();
    });
    await waitFor(() => expect(row("XENON_FUTU_MKT_NOTIONAL_CAP").getAttribute("aria-busy")).toBe("false"));
    expect(within(r).queryByTestId("confirm-step")).toBeNull();
    expect(sent).toEqual([{ value: 12000 }]);
  });
});
