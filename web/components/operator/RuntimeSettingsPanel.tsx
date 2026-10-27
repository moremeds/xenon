"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { RuntimeSetting, SettingCategory } from "@/lib/runtimeSettingsTypes";
import { settingsErrorMessage } from "@/lib/settingsReasonCodes";
import type { ToastType } from "@/lib/useToast";

// Runtime settings (DB overrides of env knobs). Renders only from the API
// metadata (GET /admin/settings via /api/admin/settings): labels, units, types,
// bounds and narrow-only / confirm rules come from the Python registry. Each
// row's control applies on change (switch, segment, chip: one click; number:
// Enter or blur). The control shows only the server value: while a request
// runs the row is disabled, and a refused change returns to the server value.
// A change that needs a typed confirm is first sent without one; the API's 428
// opens the confirm step for the proposal still on screen, and only that step
// sends `confirm`.

const POLL_MS = 15_000;
const PAUSE_KEY = "orders_paused";
// Per row (rule subset / min / and): not every Safety key is narrow-only
// (audit-cancel is a free override), so there is no group-wide claim.
const NARROW_NOTE =
  "Narrow-only: an override can only tighten what the environment allows. Revert to env restores the environment value.";
const GROUPS: { id: SettingCategory; label: string }[] = [
  { id: "safety", label: "Safety" },
  { id: "toggle", label: "Quote routing" },
  { id: "tuning", label: "Tuning" },
];

const TYPED_CONFIRM_NOTE = "Some changes to this setting need a typed confirmation: type the key to apply them.";
// env is the quiet default; an override is the accent.
const SOURCE_CHIP: Record<string, string> = { env: "muted", override: "override", "live (relay)": "plain" };

type Toast = (type: ToastType, message: string) => void;
type PendingConfirm = { key: string; op: "put" | "delete"; value?: unknown; typed: string };
type Write = (op: "put" | "delete", value?: unknown) => void;

// ─── value wording ────────────────────────────────────────────────────────

const SEC: Record<string, string> = { STK: "Stock", OPT: "Option" };
const ORDER_TYPE: Record<string, string> = {
  LMT: "Limit",
  MKT: "Market",
  STP: "Stop",
  "STP LMT": "Stop limit",
  MIT: "Market if touched",
  LIT: "Limit if touched",
  TRAIL: "Trailing stop",
  "TRAIL LIMIT": "Trailing stop limit",
};
const TIF: Record<string, string> = { DAY: "Day", GTC: "GTC" };
const SESSION: Record<string, string> = { RTH: "RTH", ETH: "Extended hours", ALL: "All sessions", OVERNIGHT: "Overnight" };
const SCOPE: Record<string, string> = { simulate: "Paper (simulate)", real: "Live (real)" };

/** One set member in words; the raw value stays available as a title. */
function itemLabel(v: string): string {
  const parts = v.split(":");
  if (parts.length === 4 && SEC[parts[0]]) {
    const [sec, type, tif, session] = parts;
    return [SEC[sec], ORDER_TYPE[type] ?? type, TIF[tif] ?? tif, SESSION[session] ?? session].join(" · ");
  }
  return SCOPE[v] ?? ORDER_TYPE[v] ?? v;
}

function fmtNumber(n: number, unit: string | null): string {
  let text = n.toLocaleString("en-US", { maximumFractionDigits: 6 });
  // never show a nonzero value as 0 (0 is the "off" sentinel for some keys)
  if (n !== 0 && Number(text.replace(/,/g, "")) === 0) text = String(n);
  return unit ? `${text} ${unit}` : text;
}

/** A value as plain text (confirm prompt, env hints). */
function fmt(s: RuntimeSetting, value: unknown): string {
  if (value === null || value === undefined) return "Not set";
  if (typeof value === "boolean") return value ? "On" : "Off";
  if (typeof value === "number") return fmtNumber(value, s.unit);
  if (Array.isArray(value)) return value.length ? value.map((v) => itemLabel(String(v))).join(", ") : "None";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

type Shown = { value: unknown; text?: string; source: string | null };

/** What the row shows as its current value, and where that value comes from. */
function shown(s: RuntimeSetting): Shown {
  if (s.env_known) return { value: s.effective, source: s.db_value != null ? "override" : "env" };
  if (s.owner === "relay" && s.live_value !== undefined) return { value: s.live_value, source: "live (relay)" };
  if (s.db_value != null) return { value: s.db_value, source: "override" };
  return s.owner === "relay"
    ? { value: null, text: "Not reported by relay", source: null }
    : { value: null, text: "Service default", source: null };
}

function changeLine(s: RuntimeSetting): string | null {
  const c = s.last_change;
  if (!c) return null;
  const when = (c.changed_at ?? "").slice(0, 16).replace("T", " ");
  return `Changed ${when}${c.changed_by ? ` by ${c.changed_by}` : ""}${c.note ? ` · ${c.note}` : ""}`;
}

// ─── controls ─────────────────────────────────────────────────────────────

function setOptions(s: RuntimeSetting): string[] {
  if (s.narrow_only && s.env_known) return Array.isArray(s.env_value) ? (s.env_value as string[]) : [];
  return s.choices;
}

/** Number limits from the API metadata: registry bounds, and for a `min`
 * (narrow-only) key the env value as the ceiling. The API checks again. */
function numberLimits(s: RuntimeSetting): { min?: number; max?: number; envCap: boolean } {
  const envCap = s.rule === "min" && s.env_known && typeof s.env_value === "number";
  return { min: s.bounds?.[0], max: envCap ? (s.env_value as number) : s.bounds?.[1], envCap };
}

/** Field text → JSON value (number / object keys), or an error message. */
function parseText(s: RuntimeSetting, raw: string): { value?: unknown; error?: string } {
  const text = raw.trim();
  if (s.type === "int" || s.type === "number") {
    const n = Number(text);
    if (text === "" || !Number.isFinite(n)) return { error: `${s.label}: enter a number` };
    const { min, max, envCap } = numberLimits(s);
    if (max != null && n > max)
      return { error: `${s.label}: ${n} is above the ${envCap ? "env cap" : "maximum"} ${fmtNumber(max, s.unit)}` };
    if (min != null && n < min) return { error: `${s.label}: ${n} is below the minimum ${fmtNumber(min, s.unit)}` };
    return { value: n };
  }
  try {
    return { value: JSON.parse(text) };
  } catch {
    return { error: `${s.label}: not valid JSON` };
  }
}

const fieldText = (s: RuntimeSetting, v: unknown) =>
  v == null ? "" : s.type === "object" ? JSON.stringify(v) : String(v);

/** `and` rule (env AND DB): with the env off, the DB may only keep it off. */
const switchLocked = (s: RuntimeSetting) => s.rule === "and" && s.env_known && !s.env_value;

async function send(url: string, init: RequestInit): Promise<{ ok: boolean; status: number; body: unknown }> {
  const res = await fetch(url, { cache: "no-store", ...init });
  const body = await res.json().catch(() => null);
  return { ok: res.ok, status: res.status, body };
}

export default function RuntimeSettingsPanel({ onToast }: { onToast: Toast }) {
  const [settings, setSettings] = useState<RuntimeSetting[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingConfirm | null>(null);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  // Per-row reset counter: a field remounts with the server value after a
  // write settles or a confirm is abandoned (no unaccepted value stays on screen).
  const [resets, setResets] = useState<Record<string, number>>({});
  const bumpReset = (key: string) => setResets((r) => ({ ...r, [key]: (r[key] ?? 0) + 1 }));
  // Bumped by every load and write: a load whose response arrives after a
  // newer load or write started is obsolete and is dropped.
  const seq = useRef(0);
  // Per-row proposal token: every new proposal (click, field edit, Escape)
  // bumps it. A response for an older proposal never opens a confirm.
  const proposals = useRef<Record<string, number>>({});
  const bumpProposal = useCallback((key: string) => {
    proposals.current[key] = (proposals.current[key] ?? 0) + 1;
    setPending((p) => (p?.key === key ? null : p));
  }, []);

  const load = useCallback(async () => {
    const mine = ++seq.current;
    try {
      const r = await send("/api/admin/settings", { method: "GET" });
      if (mine !== seq.current) return;
      if (!r.ok) {
        setLoadError(settingsErrorMessage(r.body, `HTTP ${r.status}`));
        return;
      }
      setSettings(((r.body as { settings?: RuntimeSetting[] })?.settings ?? []) as RuntimeSetting[]);
      setLoadError(null);
    } catch (e) {
      if (mine === seq.current) setLoadError(e instanceof Error ? e.message : "Settings unreachable");
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [load]);

  const write = useCallback(
    async (key: string, op: "put" | "delete", value: unknown, confirm?: string) => {
      setBusy((b) => ({ ...b, [key]: true }));
      seq.current += 1; // a load already in flight is now obsolete
      const proposal = proposals.current[key] ?? 0;
      let awaitingConfirm = false;
      try {
        const url = `/api/admin/settings/${key}`;
        // JSON content type on every write: web/middleware.ts refuses any other non-GET /api request.
        const headers = { "Content-Type": "application/json" };
        const r =
          op === "put"
            ? await send(url, { method: "PUT", headers, body: JSON.stringify(confirm ? { value, confirm } : { value }) })
            : await send(confirm ? `${url}?${new URLSearchParams({ confirm })}` : url, { method: "DELETE", headers });
        if (r.status === 428 && !confirm) {
          // only for the proposal still on screen
          if (proposal === (proposals.current[key] ?? 0)) {
            setPending({ key, op, value, typed: "" });
            awaitingConfirm = true;
          }
          return;
        }
        if (!r.ok) {
          onToast("error", settingsErrorMessage(r.body, `HTTP ${r.status}`));
          return;
        }
        setPending((p) => (p?.key === key ? null : p));
        onToast("success", op === "put" ? `${key} saved` : `${key} reverted to env`);
        await load();
      } catch (e) {
        onToast("error", e instanceof Error ? e.message : "Settings request failed");
      } finally {
        setBusy((b) => ({ ...b, [key]: false }));
        if (!awaitingConfirm) bumpReset(key);
      }
    },
    [load, onToast],
  );

  if (!settings) {
    return (
      <section className="rs rs--loading" data-testid="runtime-settings">
        {loadError ? `Runtime settings unavailable: ${loadError}` : "Loading runtime settings"}
      </section>
    );
  }

  const pause = settings.find((s) => s.key === PAUSE_KEY);
  const paused = pause?.effective === true;

  return (
    <section className="rs" data-testid="runtime-settings">
      <header className="rs__head">
        <span className="rs__title">Runtime settings</span>
        <span className="rs__sub">Stored overrides. A setting without an override uses its environment value.</span>
      </header>
      {loadError ? <div className="rs__error">Last refresh failed: {loadError}</div> : null}
      {pause ? (
        <div className={`rs-status ${paused ? "rs-status--paused" : ""}`} data-testid="orders-paused-banner">
          <span className="rs-status__dot" aria-hidden="true" />
          <span className="rs-status__text">
            <span className="rs-status__state">{paused ? "Orders paused" : "Orders active"}</span>
            <span className="rs-status__detail">
              {paused
                ? "Place and modify are refused (ORDERS_PAUSED). Cancel still works."
                : "Place, modify and cancel are allowed."}
            </span>
          </span>
          <button
            type="button"
            className={`rs-btn ${paused ? "rs-btn--primary" : "rs-btn--danger"}`}
            disabled={busy[PAUSE_KEY]}
            onClick={() => (paused ? write(PAUSE_KEY, "delete", undefined) : write(PAUSE_KEY, "put", true))}
          >
            {paused ? "Resume orders" : "Pause orders"}
          </button>
        </div>
      ) : null}
      {GROUPS.map((g) => {
        const rows = settings.filter((s) => s.category === g.id && s.key !== PAUSE_KEY);
        if (!rows.length) return null;
        return (
          <section key={g.id} className="rs-group" data-testid={`settings-group-${g.id}`}>
            <header className="rs-group__head">
              <span className="rs-group__label">{g.label}</span>
            </header>
            {rows.map((s) => (
              <SettingRow
                key={s.key}
                s={s}
                busy={busy[s.key] === true}
                reset={resets[s.key] ?? 0}
                pending={pending?.key === s.key ? pending : null}
                onWrite={(op, value) => {
                  bumpProposal(s.key); // a new proposal replaces any pending confirm
                  write(s.key, op, value);
                }}
                onPropose={() => bumpProposal(s.key)}
                onTyped={(typed) => setPending((p) => (p ? { ...p, typed } : p))}
                onConfirm={() => pending && write(pending.key, pending.op, pending.value, pending.key)}
                onCancelConfirm={() => {
                  bumpProposal(s.key);
                  bumpReset(s.key);
                }}
              />
            ))}
          </section>
        );
      })}
    </section>
  );
}

function SettingRow({
  s,
  busy,
  reset,
  pending,
  onWrite,
  onPropose,
  onTyped,
  onConfirm,
  onCancelConfirm,
}: {
  s: RuntimeSetting;
  busy: boolean;
  reset: number;
  pending: PendingConfirm | null;
  onWrite: Write;
  onPropose: () => void;
  onTyped: (v: string) => void;
  onConfirm: () => void;
  onCancelConfirm: () => void;
}) {
  const v = shown(s);
  const overridden = s.db_value != null || s.db_value_invalid;
  const change = changeLine(s);
  return (
    <div
      className={`rs-row ${overridden ? "rs-row--override" : ""} ${busy ? "rs-row--busy" : ""}`}
      data-testid={`setting-${s.key}`}
      data-overridden={String(overridden)}
      aria-busy={busy}
    >
      <div className="rs-row__main">
        <div className="rs-row__info">
          <span className="rs-row__label" data-testid="label">
            {s.label}
          </span>
          <span className="rs-row__sub">
            <span className="rs-row__key" data-testid="key">
              {s.key}
            </span>
            <span className="rs-row__desc">{s.description}</span>
          </span>
          {change ? (
            <span className="rs-row__change" data-testid="last-change">
              {change}
            </span>
          ) : null}
          {s.db_value_invalid ? <span className="rs-row__invalid">Stored override is invalid and ignored.</span> : null}
        </div>
        <div className="rs-row__state">
          <Control key={`${reset}:${fieldText(s, v.value)}`} s={s} v={v} busy={busy} onWrite={onWrite} onPropose={onPropose} />
          {busy ? <span className="rs-pending">Applying</span> : null}
          {v.source ? (
            <span className={`rs-chip rs-chip--${SOURCE_CHIP[v.source] ?? "plain"}`} data-testid="source">
              {v.source}
            </span>
          ) : null}
          {s.narrow_only ? (
            <span className="rs-chip rs-chip--plain" title={NARROW_NOTE}>
              narrow-only
            </span>
          ) : null}
          {s.confirm_rule ? (
            <span className="rs-chip rs-chip--plain" title={TYPED_CONFIRM_NOTE}>
              typed confirm
            </span>
          ) : null}
          {overridden ? (
            <button
              type="button"
              className="rs-link"
              disabled={busy}
              title={s.env_known ? `Environment value: ${fmt(s, s.env_value)}` : undefined}
              onClick={() => onWrite("delete")}
            >
              Revert to env
            </button>
          ) : null}
        </div>
      </div>
      {pending ? (
        <div className="rs-confirm" data-testid="confirm-step">
          <span className="rs-confirm__proposal" data-testid="confirm-proposal">
            {pending.op === "delete" ? `Revert ${s.label} to env` : `Set ${s.label} to ${fmt(s, pending.value)}`}
          </span>
          <span className="rs-row__key" data-testid="confirm-key">
            {s.key}
          </span>
          <label className="rs-confirm__label">
            Type {s.key} to confirm
            <input
              className="rs-input rs-input--mono"
              aria-label={`Type ${s.key} to confirm`}
              value={pending.typed}
              onChange={(e) => onTyped(e.target.value)}
            />
          </label>
          <div className="rs-confirm__actions">
            <button
              type="button"
              className="rs-btn rs-btn--primary"
              disabled={busy || pending.typed !== s.key}
              onClick={onConfirm}
            >
              Confirm change
            </button>
            <button type="button" className="rs-btn" onClick={onCancelConfirm}>
              Back
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** The row's value as its control. Shows only the server value; a change goes
 * straight to `onWrite`. */
function Control({
  s,
  v,
  busy,
  onWrite,
  onPropose,
}: {
  s: RuntimeSetting;
  v: Shown;
  busy: boolean;
  onWrite: Write;
  onPropose: () => void;
}) {
  if (s.type === "bool") {
    const on = v.value === true;
    const locked = switchLocked(s) && !on;
    return (
      <span className="rs-switch-wrap" title={locked ? "Off in the environment; an override can only keep it off." : undefined}>
        <button
          type="button"
          role="switch"
          aria-checked={on}
          aria-label={s.label}
          className="rs-switch"
          disabled={busy || locked}
          onClick={() => onWrite("put", !on)}
        >
          <span className="rs-switch__knob" />
        </button>
        <span className={`rs-value ${v.text ? "rs-value--muted" : ""}`} data-testid="effective">
          {v.text ?? (on ? "On" : "Off")}
        </span>
      </span>
    );
  }
  if (s.type === "enum") {
    return (
      <span className="rs-seg-wrap">
        <span className="rs-seg" role="group" aria-label={s.label}>
          {s.choices.map((o) => (
            <button
              key={o}
              type="button"
              className="rs-seg__btn"
              aria-pressed={v.value === o}
              disabled={busy}
              onClick={() => v.value !== o && onWrite("put", o)}
            >
              {o}
            </button>
          ))}
        </span>
        {v.text ? (
          <span className="rs-value rs-value--muted" data-testid="effective">
            {v.text}
          </span>
        ) : null}
      </span>
    );
  }
  if (s.type === "set") {
    const options = setOptions(s);
    if (!options.length) return <span className="rs-hint">The environment allows nothing here.</span>;
    const chosen = new Set(Array.isArray(v.value) ? (v.value as string[]) : []);
    return (
      <span className="rs-chips-edit" role="group" aria-label={s.label}>
        {options.map((opt) => (
          <button
            key={opt}
            type="button"
            className="rs-toggle"
            aria-pressed={chosen.has(opt)}
            title={opt}
            disabled={busy}
            onClick={() => onWrite("put", options.filter((o) => (o === opt ? !chosen.has(o) : chosen.has(o))))}
          >
            {itemLabel(opt)}
          </button>
        ))}
      </span>
    );
  }
  return <FieldControl s={s} v={v} busy={busy} onWrite={onWrite} onPropose={onPropose} />;
}

/** Number / object field: applies on Enter or blur when the value changed and
 * is valid; Escape restores the server value. Remounted (parent key) whenever
 * the server value changes or a write settles. */
function FieldControl({
  s,
  v,
  busy,
  onWrite,
  onPropose,
}: {
  s: RuntimeSetting;
  v: Shown;
  busy: boolean;
  onWrite: Write;
  onPropose: () => void;
}) {
  const server = fieldText(s, v.value);
  const [text, setText] = useState(server);
  const [error, setError] = useState<string | null>(null);
  const sent = useRef<string | null>(null); // the text already sent for this mount

  const apply = () => {
    if (busy || text.trim() === server || text === sent.current) return;
    const parsed = parseText(s, text);
    if (parsed.error) {
      setError(parsed.error);
      return;
    }
    setError(null);
    sent.current = text;
    onWrite("put", parsed.value);
  };
  const { min, max, envCap } = numberLimits(s);
  const isNumber = s.type !== "object";
  return (
    <span className="rs-number">
      <input
        className={`rs-input rs-input--mono ${isNumber ? "rs-input--number" : "rs-input--json"} ${v.text ? "rs-input--unset" : ""}`}
        type="text"
        inputMode={isNumber ? "decimal" : undefined}
        aria-label={s.label}
        aria-invalid={error ? true : undefined}
        placeholder={v.text ?? undefined}
        value={text}
        disabled={busy}
        onChange={(e) => {
          setText(e.target.value);
          setError(null);
          sent.current = null;
          onPropose(); // a pending confirm was for the old text
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") apply();
          if (e.key === "Escape") {
            setText(server);
            setError(null);
            onPropose();
          }
        }}
        onBlur={apply}
      />
      {s.unit ? <span className="rs-number__unit">{s.unit}</span> : null}
      {envCap && max != null ? <span className="rs-hint">Env cap {fmtNumber(max, s.unit)}</span> : null}
      {!envCap && isNumber && min != null && max != null ? (
        <span className="rs-hint">
          Range {fmtNumber(min, null)}–{fmtNumber(max, s.unit)}
        </span>
      ) : null}
      {error ? (
        <span className="rs-field-error" role="alert">
          {error}
        </span>
      ) : null}
    </span>
  );
}
