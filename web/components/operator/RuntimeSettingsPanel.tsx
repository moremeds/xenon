"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { RuntimeSetting, SettingCategory } from "@/lib/runtimeSettingsTypes";
import { settingsErrorMessage } from "@/lib/settingsReasonCodes";
import type { ToastType } from "@/lib/useToast";

// Runtime settings (DB overrides of env knobs). Renders only from the API
// metadata (GET /admin/settings via /api/admin/settings): labels, units, types,
// bounds and narrow-only / confirm rules come from the Python registry. Rows
// are read-only until "Edit" opens one inline editor. A change that needs a
// typed confirm is first sent without one; the API's 428 opens the confirm
// step for the proposal still on screen, and only that step sends `confirm`.

const POLL_MS = 15_000;
const PAUSE_KEY = "orders_paused";
const NARROW_NOTE =
  "Overrides here can only tighten what the environment allows. Revert to env restores the environment value.";
const GROUPS: { id: SettingCategory; label: string; note?: string }[] = [
  { id: "safety", label: "Safety", note: NARROW_NOTE },
  { id: "toggle", label: "Quote routing" },
  { id: "tuning", label: "Tuning" },
];

type Toast = (type: ToastType, message: string) => void;
type PendingConfirm = { key: string; op: "put" | "delete"; value?: unknown; note: string; typed: string };
type Draft = string | string[] | boolean;

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
  const text = n.toLocaleString("en-US", { maximumFractionDigits: 6 });
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

function ValueView({ s, v }: { s: RuntimeSetting; v: Shown }) {
  if (v.text) return <span className="rs-value rs-value--muted" data-testid="effective">{v.text}</span>;
  if (Array.isArray(v.value)) {
    if (!v.value.length) return <span className="rs-value rs-value--muted" data-testid="effective">None allowed</span>;
    return (
      <span className="rs-value rs-tags" data-testid="effective">
        {v.value.map((item) => (
          <span key={String(item)} className="rs-tag" title={String(item)}>
            {itemLabel(String(item))}
          </span>
        ))}
      </span>
    );
  }
  return <span className="rs-value" data-testid="effective">{fmt(s, v.value)}</span>;
}

function changeLine(s: RuntimeSetting): string | null {
  const c = s.last_change;
  if (!c) return null;
  const when = (c.changed_at ?? "").slice(0, 16).replace("T", " ");
  return `Changed ${when}${c.changed_by ? ` by ${c.changed_by}` : ""}${c.note ? ` · ${c.note}` : ""}`;
}

// ─── editing ──────────────────────────────────────────────────────────────

function setOptions(s: RuntimeSetting): string[] {
  if (s.narrow_only && s.env_known) return Array.isArray(s.env_value) ? (s.env_value as string[]) : [];
  return s.choices;
}

function initialDraft(s: RuntimeSetting): Draft {
  const v = s.env_known ? s.effective : s.db_value ?? s.live_value ?? s.default;
  if (s.type === "set") return Array.isArray(v) ? (v as string[]) : [];
  if (s.type === "bool") return v === true;
  if (s.type === "object") return v == null ? "{}" : JSON.stringify(v);
  return v == null ? "" : String(v);
}

/** Number limits from the API metadata: registry bounds, and for a `min`
 * (narrow-only) key the env value as the ceiling. The API checks again. */
function numberLimits(s: RuntimeSetting): { min?: number; max?: number; envCap: boolean } {
  const envCap = s.rule === "min" && s.env_known && typeof s.env_value === "number";
  return { min: s.bounds?.[0], max: envCap ? (s.env_value as number) : s.bounds?.[1], envCap };
}

/** Draft → JSON value, or an error message. */
function parseDraft(s: RuntimeSetting, draft: Draft): { value?: unknown; error?: string } {
  if (s.type === "set") return { value: draft as string[] };
  if (s.type === "bool") return { value: draft === true };
  const text = String(draft).trim();
  if (s.type === "enum") return { value: text };
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
  const [editing, setEditing] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<PendingConfirm | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  // Bumped by every load and write: a load whose response arrives after a
  // newer load or write started is obsolete and is dropped.
  const seq = useRef(0);
  // Per-row proposal token: every value/note edit bumps it. A response for an
  // older proposal (e.g. a 428 that arrives after the edit) never opens a confirm.
  const proposals = useRef<Record<string, number>>({});
  const bumpProposal = (key: string) => {
    proposals.current[key] = (proposals.current[key] ?? 0) + 1;
    setPending((p) => (p?.key === key ? null : p));
  };

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

  const closeEditor = (key: string) => {
    bumpProposal(key);
    setEditing((k) => (k === key ? null : k));
    setDrafts((d) => {
      const next = { ...d };
      delete next[key];
      return next;
    });
    setNotes((n) => {
      const next = { ...n };
      delete next[key];
      return next;
    });
  };

  const write = useCallback(
    async (key: string, op: "put" | "delete", value: unknown, note: string, confirm?: string) => {
      setBusy(key);
      seq.current += 1; // a load already in flight is now obsolete
      const proposal = proposals.current[key] ?? 0;
      if (!confirm) setPending(null); // a fresh request replaces any pending confirm
      try {
        const url = `/api/admin/settings/${key}`;
        let r;
        if (op === "put") {
          const body: Record<string, unknown> = { value };
          if (note) body.note = note;
          if (confirm) body.confirm = confirm;
          r = await send(url, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        } else {
          const qs = new URLSearchParams();
          if (note) qs.set("note", note);
          if (confirm) qs.set("confirm", confirm);
          // JSON content type: web/middleware.ts refuses any other non-GET /api request.
          r = await send(qs.toString() ? `${url}?${qs}` : url, {
            method: "DELETE",
            headers: { "Content-Type": "application/json" },
          });
        }
        if (r.status === 428 && !confirm) {
          // only for the proposal still on screen; an edited row needs a fresh Save
          if (proposal === (proposals.current[key] ?? 0)) setPending({ key, op, value, note, typed: "" });
          return;
        }
        if (!r.ok) {
          onToast("error", settingsErrorMessage(r.body, `HTTP ${r.status}`));
          return;
        }
        setPending(null);
        setEditing((k) => (k === key ? null : k));
        setDrafts((d) => {
          const next = { ...d };
          delete next[key];
          return next;
        });
        onToast("success", op === "put" ? `${key} saved` : `${key} reverted to env`);
        await load();
      } catch (e) {
        onToast("error", e instanceof Error ? e.message : "Settings request failed");
      } finally {
        setBusy(null);
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
            disabled={busy === PAUSE_KEY}
            onClick={() => (paused ? write(PAUSE_KEY, "delete", undefined, "") : write(PAUSE_KEY, "put", true, ""))}
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
              {g.note && rows.some((s) => s.narrow_only) ? (
                <span className="rs-group__note" title={NARROW_NOTE}>
                  {NARROW_NOTE}
                </span>
              ) : null}
            </header>
            {rows.map((s) => (
              <SettingRow
                key={s.key}
                s={s}
                open={editing === s.key}
                draft={drafts[s.key] ?? initialDraft(s)}
                note={notes[s.key] ?? ""}
                busy={busy === s.key}
                pending={pending?.key === s.key ? pending : null}
                onEdit={() => {
                  if (editing && editing !== s.key) closeEditor(editing);
                  setEditing(s.key);
                }}
                onCancel={() => closeEditor(s.key)}
                onDraft={(v) => {
                  bumpProposal(s.key); // a pending or in-flight confirm was for the old proposal
                  setDrafts((d) => ({ ...d, [s.key]: v }));
                }}
                onNote={(v) => {
                  bumpProposal(s.key);
                  setNotes((n) => ({ ...n, [s.key]: v }));
                }}
                onSave={() => {
                  const parsed = parseDraft(s, drafts[s.key] ?? initialDraft(s));
                  if (parsed.error) onToast("error", parsed.error);
                  else write(s.key, "put", parsed.value, notes[s.key] ?? "");
                }}
                onRevert={() => write(s.key, "delete", undefined, notes[s.key] ?? "")}
                onTyped={(typed) => setPending((p) => (p ? { ...p, typed } : p))}
                onConfirm={() => pending && write(pending.key, pending.op, pending.value, pending.note, pending.key)}
                onCancelConfirm={() => setPending(null)}
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
  open,
  draft,
  note,
  busy,
  pending,
  onEdit,
  onCancel,
  onDraft,
  onNote,
  onSave,
  onRevert,
  onTyped,
  onConfirm,
  onCancelConfirm,
}: {
  s: RuntimeSetting;
  open: boolean;
  draft: Draft;
  note: string;
  busy: boolean;
  pending: PendingConfirm | null;
  onEdit: () => void;
  onCancel: () => void;
  onDraft: (v: Draft) => void;
  onNote: (v: string) => void;
  onSave: () => void;
  onRevert: () => void;
  onTyped: (v: string) => void;
  onConfirm: () => void;
  onCancelConfirm: () => void;
}) {
  const v = shown(s);
  const overridden = s.db_value != null || s.db_value_invalid;
  const change = changeLine(s);
  return (
    <div className={`rs-row ${overridden ? "rs-row--override" : ""}`} data-testid={`setting-${s.key}`} data-overridden={String(overridden)}>
      <div className="rs-row__main">
        <div className="rs-row__info">
          <span className="rs-row__label" data-testid="label">
            {s.label}
          </span>
          <span className="rs-row__key" data-testid="key">
            {s.key}
          </span>
          <span className="rs-row__desc">{s.description}</span>
          {change ? (
            <span className="rs-row__change" data-testid="last-change">
              {change}
            </span>
          ) : null}
          {s.db_value_invalid ? <span className="rs-row__invalid">Stored override is invalid and ignored.</span> : null}
        </div>
        <div className="rs-row__state">
          <ValueView s={s} v={v} />
          <span className="rs-row__chips">
            {v.source ? (
              <span className={`rs-chip rs-chip--${v.source === "override" ? "override" : "plain"}`} data-testid="source">
                {v.source}
              </span>
            ) : null}
            {s.confirm_rule ? <span className="rs-chip rs-chip--plain" title="Some changes need a typed confirmation">confirm</span> : null}
          </span>
          {!open ? (
            <button type="button" className="rs-btn" onClick={onEdit}>
              Edit
            </button>
          ) : null}
        </div>
      </div>
      {open ? (
        <div className="rs-editor" data-testid="editor">
          <dl className="rs-editor__facts">
            <dt>Environment</dt>
            <dd>{s.env_known ? fmt(s, s.env_value) : `Set in the ${s.owner} service; not visible to the API`}</dd>
            <dt>Override</dt>
            <dd>{s.db_value != null ? fmt(s, s.db_value) : "No override — using env"}</dd>
            <dt>Last change</dt>
            <dd>{change ?? "Never changed"}</dd>
          </dl>
          <div className="rs-editor__control">
            <Editor s={s} draft={draft} onDraft={onDraft} />
          </div>
          <input
            className="rs-input"
            aria-label="Note"
            placeholder="Note (optional)"
            value={note}
            onChange={(e) => onNote(e.target.value)}
          />
          <div className="rs-editor__actions">
            <button type="button" className="rs-btn rs-btn--primary" disabled={busy} onClick={onSave}>
              Save
            </button>
            <button type="button" className="rs-btn" onClick={onCancel}>
              Cancel
            </button>
            {overridden ? (
              <button type="button" className="rs-btn rs-btn--quiet" disabled={busy} onClick={onRevert}>
                Revert to env
              </button>
            ) : null}
          </div>
          {pending ? (
            <div className="rs-confirm" data-testid="confirm-step">
              <span className="rs-confirm__proposal" data-testid="confirm-proposal">
                {pending.op === "delete" ? `Revert ${s.key} to env` : `Set ${s.key} to ${fmt(s, pending.value)}`}
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
              <div className="rs-editor__actions">
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
      ) : null}
    </div>
  );
}

function Editor({ s, draft, onDraft }: { s: RuntimeSetting; draft: Draft; onDraft: (v: Draft) => void }) {
  if (s.type === "set") {
    const chosen = new Set(draft as string[]);
    const options = setOptions(s);
    if (!options.length) return <span className="rs-hint">The environment allows nothing here, so there is nothing to narrow.</span>;
    return (
      <span className="rs-chips-edit" role="group" aria-label="New value">
        {options.map((opt) => (
          <button
            key={opt}
            type="button"
            className="rs-toggle"
            aria-pressed={chosen.has(opt)}
            title={opt}
            onClick={() => {
              const next = new Set(chosen);
              if (next.has(opt)) next.delete(opt);
              else next.add(opt);
              onDraft(options.filter((o) => next.has(o)));
            }}
          >
            {itemLabel(opt)}
          </button>
        ))}
      </span>
    );
  }
  if (s.type === "bool") {
    const on = draft === true;
    const locked = switchLocked(s) && !on;
    return (
      <span className="rs-switch-wrap">
        <button
          type="button"
          role="switch"
          aria-checked={on}
          aria-label="New value"
          className="rs-switch"
          disabled={locked}
          onClick={() => onDraft(!on)}
        >
          <span className="rs-switch__knob" />
        </button>
        <span className="rs-value">{on ? "On" : "Off"}</span>
        {locked ? <span className="rs-hint">Off in the environment; an override can only keep it off.</span> : null}
      </span>
    );
  }
  if (s.type === "enum") {
    return (
      <select className="rs-input rs-input--mono" aria-label="New value" value={String(draft)} onChange={(e) => onDraft(e.target.value)}>
        {s.choices.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    );
  }
  if (s.type === "object") {
    return (
      <textarea
        className="rs-input rs-input--mono"
        aria-label="New value"
        rows={2}
        value={String(draft)}
        onChange={(e) => onDraft(e.target.value)}
      />
    );
  }
  const { min, max, envCap } = numberLimits(s);
  return (
    <span className="rs-number">
      <input
        className="rs-input rs-input--mono"
        aria-label="New value"
        type="number"
        min={min}
        max={max}
        step={s.type === "int" ? 1 : "any"}
        value={String(draft)}
        onChange={(e) => onDraft(e.target.value)}
      />
      {s.unit ? <span className="rs-number__unit">{s.unit}</span> : null}
      {envCap && max != null ? <span className="rs-hint">Env cap {fmtNumber(max, s.unit)}</span> : null}
      {!envCap && min != null && max != null ? (
        <span className="rs-hint">
          Range {fmtNumber(min, null)}–{fmtNumber(max, s.unit)}
        </span>
      ) : null}
    </span>
  );
}
