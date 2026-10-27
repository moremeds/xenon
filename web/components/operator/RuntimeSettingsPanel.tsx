"use client";

import { useCallback, useEffect, useState } from "react";

import type { RuntimeSetting, SettingCategory } from "@/lib/runtimeSettingsTypes";
import { settingsErrorMessage } from "@/lib/settingsReasonCodes";
import type { ToastType } from "@/lib/useToast";

// Runtime settings (DB overrides of env knobs). Renders only from the API
// metadata (GET /admin/settings via /api/admin/settings) — no client copy of
// the key list or the narrow-only rules. A change that needs a typed confirm
// is first sent without one; the API's 428 SETTING_CONFIRM_REQUIRED opens the
// confirm step, and only that step sends `confirm`.

const POLL_MS = 15_000;
const PAUSE_KEY = "orders_paused";
const GROUPS: { id: SettingCategory; label: string }[] = [
  { id: "safety", label: "Safety" },
  { id: "toggle", label: "Toggles" },
  { id: "tuning", label: "Tuning" },
];

type Toast = (type: ToastType, message: string) => void;
type PendingConfirm = { key: string; op: "put" | "delete"; value?: unknown; note: string; typed: string };

function fmt(value: unknown): string {
  if (value === null || value === undefined) return "---";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "(none)";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

const ownerEnv = (s: RuntimeSetting) => `env: set in ${s.owner}`;

function envText(s: RuntimeSetting): string {
  return s.env_known ? fmt(s.env_value) : ownerEnv(s);
}

function effectiveText(s: RuntimeSetting): string {
  if (s.env_known) return fmt(s.effective);
  return s.db_value != null ? fmt(s.db_value) : ownerEnv(s);
}

function currentValue(s: RuntimeSetting): unknown {
  if (s.env_known) return s.effective;
  return s.db_value ?? s.default;
}

function setOptions(s: RuntimeSetting): string[] {
  if (s.narrow_only && s.env_known) return Array.isArray(s.env_value) ? (s.env_value as string[]) : [];
  return s.choices;
}

function initialDraft(s: RuntimeSetting): string | string[] {
  const v = currentValue(s);
  if (s.type === "set") return Array.isArray(v) ? (v as string[]) : [];
  if (s.type === "object") return v == null ? "{}" : JSON.stringify(v);
  return v == null ? "" : String(v);
}

/** Draft → JSON value, or an error message. */
function parseDraft(s: RuntimeSetting, draft: string | string[]): { value?: unknown; error?: string } {
  if (s.type === "set") return { value: draft as string[] };
  const text = String(draft).trim();
  if (s.type === "bool") return { value: text === "true" };
  if (s.type === "enum") return { value: text };
  if (s.type === "int" || s.type === "number") {
    if (text === "" || !Number.isFinite(Number(text))) return { error: `${s.key}: enter a number` };
    return { value: Number(text) };
  }
  try {
    return { value: JSON.parse(text) };
  } catch {
    return { error: `${s.key}: not valid JSON` };
  }
}

function boolOptions(s: RuntimeSetting): string[] {
  // `and` rule (env AND DB): the DB may only turn it off while the env is off.
  if (s.rule === "and" && s.env_known && !s.env_value) return ["false"];
  return ["true", "false"];
}

async function send(url: string, init: RequestInit): Promise<{ ok: boolean; status: number; body: unknown }> {
  const res = await fetch(url, { cache: "no-store", ...init });
  const body = await res.json().catch(() => null);
  return { ok: res.ok, status: res.status, body };
}

export default function RuntimeSettingsPanel({ onToast }: { onToast: Toast }) {
  const [settings, setSettings] = useState<RuntimeSetting[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string | string[]>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<PendingConfirm | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await send("/api/admin/settings", { method: "GET" });
      if (!r.ok) {
        setLoadError(settingsErrorMessage(r.body, `HTTP ${r.status}`));
        return;
      }
      setSettings(((r.body as { settings?: RuntimeSetting[] })?.settings ?? []) as RuntimeSetting[]);
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "settings unreachable");
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [load]);

  const write = useCallback(
    async (key: string, op: "put" | "delete", value: unknown, note: string, confirm?: string) => {
      setBusy(key);
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
          r = await send(qs.toString() ? `${url}?${qs}` : url, { method: "DELETE" });
        }
        if (r.status === 428 && !confirm) {
          setPending({ key, op, value, note, typed: "" });
          return;
        }
        if (!r.ok) {
          onToast("error", settingsErrorMessage(r.body, `HTTP ${r.status}`));
          return;
        }
        setPending(null);
        setDrafts((d) => {
          const next = { ...d };
          delete next[key];
          return next;
        });
        onToast("success", op === "put" ? `${key} saved` : `${key} reverted to env`);
        await load();
      } catch (e) {
        onToast("error", e instanceof Error ? e.message : "settings request failed");
      } finally {
        setBusy(null);
      }
    },
    [load, onToast],
  );

  if (!settings) {
    return (
      <section className="runtime-settings runtime-settings--loading" data-testid="runtime-settings">
        {loadError ? `Settings — ${loadError}` : "Settings — loading…"}
      </section>
    );
  }

  const pause = settings.find((s) => s.key === PAUSE_KEY);
  const paused = pause?.effective === true;

  return (
    <section className="runtime-settings" data-testid="runtime-settings">
      <header className="runtime-settings__head">
        <span className="runtime-settings__title">Runtime settings</span>
        <span className="runtime-settings__hint">DB overrides of env knobs · safety keys are narrow-only</span>
      </header>
      {loadError ? <div className="runtime-settings__error">{loadError}</div> : null}
      {pause ? (
        <div
          className={`runtime-settings__pause ${paused ? "runtime-settings__pause--on" : ""}`}
          data-testid="orders-paused-banner"
        >
          <span className="runtime-settings__pause-state">{paused ? "Orders paused" : "Orders active"}</span>
          <span className="runtime-settings__pause-sub">
            {paused ? "place and modify refused (ORDERS_PAUSED); cancel works" : "place, modify and cancel allowed"}
          </span>
          <button
            type="button"
            className="runtime-settings__btn runtime-settings__btn--strong"
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
          <div key={g.id} className="runtime-settings__group" data-testid={`settings-group-${g.id}`}>
            <div className="runtime-settings__group-label">{g.label}</div>
            <div className="runtime-settings__table">
              <div className="runtime-settings__row runtime-settings__row--head">
                <span>Key</span>
                <span>Effective</span>
                <span>Env default</span>
                <span>DB override</span>
                <span>Last change</span>
                <span>Change</span>
              </div>
              {rows.map((s) => (
                <SettingRow
                  key={s.key}
                  s={s}
                  draft={drafts[s.key] ?? initialDraft(s)}
                  note={notes[s.key] ?? ""}
                  busy={busy === s.key}
                  pending={pending?.key === s.key ? pending : null}
                  onDraft={(v) => setDrafts((d) => ({ ...d, [s.key]: v }))}
                  onNote={(v) => setNotes((n) => ({ ...n, [s.key]: v }))}
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
            </div>
          </div>
        );
      })}
    </section>
  );
}

function SettingRow({
  s,
  draft,
  note,
  busy,
  pending,
  onDraft,
  onNote,
  onSave,
  onRevert,
  onTyped,
  onConfirm,
  onCancelConfirm,
}: {
  s: RuntimeSetting;
  draft: string | string[];
  note: string;
  busy: boolean;
  pending: PendingConfirm | null;
  onDraft: (v: string | string[]) => void;
  onNote: (v: string) => void;
  onSave: () => void;
  onRevert: () => void;
  onTyped: (v: string) => void;
  onConfirm: () => void;
  onCancelConfirm: () => void;
}) {
  const change = s.last_change;
  return (
    <div className="runtime-settings__row" data-testid={`setting-${s.key}`}>
      <span className="runtime-settings__key">
        <span className="runtime-settings__mono">{s.key}</span>
        <span className="runtime-settings__badges">
          {s.narrow_only ? <span className="runtime-settings__badge runtime-settings__badge--safety">narrow-only</span> : null}
          {s.confirm_rule ? <span className="runtime-settings__badge">typed confirm</span> : null}
          <span className="runtime-settings__badge">{s.apply === "live" ? "applies live" : "needs restart"}</span>
          {s.owner !== "api" ? <span className="runtime-settings__badge">{s.owner}</span> : null}
        </span>
        <span className="runtime-settings__desc">{s.description}</span>
      </span>
      <span className="runtime-settings__mono runtime-settings__value--effective" data-testid="effective">
        {effectiveText(s)}
      </span>
      <span className="runtime-settings__mono" data-testid="env">
        {envText(s)}
      </span>
      <span className="runtime-settings__mono" data-testid="override">
        {s.db_value != null ? fmt(s.db_value) : "---"}
        {s.db_value_invalid ? <span className="runtime-settings__invalid"> invalid row (ignored)</span> : null}
      </span>
      <span className="runtime-settings__mono runtime-settings__change" data-testid="last-change">
        {change
          ? `${(change.changed_at ?? "").slice(0, 19).replace("T", " ")} · ${change.changed_by ?? "---"}${
              change.note ? ` · ${change.note}` : ""
            }`
          : "---"}
      </span>
      <span className="runtime-settings__edit">
        <Editor s={s} draft={draft} onDraft={onDraft} />
        <input
          className="runtime-settings__input"
          aria-label="Note"
          placeholder="note"
          value={note}
          onChange={(e) => onNote(e.target.value)}
        />
        <span className="runtime-settings__actions">
          <button type="button" className="runtime-settings__btn" disabled={busy} onClick={onSave}>
            Save
          </button>
          {s.db_value != null || s.db_value_invalid ? (
            <button type="button" className="runtime-settings__btn" disabled={busy} onClick={onRevert}>
              Revert to env
            </button>
          ) : null}
        </span>
        {pending ? (
          <span className="runtime-settings__confirm" data-testid="confirm-step">
            <label className="runtime-settings__confirm-label">
              Type {s.key} to confirm
              <input
                className="runtime-settings__input runtime-settings__mono"
                aria-label={`Type ${s.key} to confirm`}
                value={pending.typed}
                onChange={(e) => onTyped(e.target.value)}
              />
            </label>
            <button
              type="button"
              className="runtime-settings__btn runtime-settings__btn--strong"
              disabled={busy || pending.typed !== s.key}
              onClick={onConfirm}
            >
              Confirm change
            </button>
            <button type="button" className="runtime-settings__btn" onClick={onCancelConfirm}>
              Cancel
            </button>
          </span>
        ) : null}
      </span>
    </div>
  );
}

function Editor({
  s,
  draft,
  onDraft,
}: {
  s: RuntimeSetting;
  draft: string | string[];
  onDraft: (v: string | string[]) => void;
}) {
  if (s.type === "set") {
    const chosen = new Set(draft as string[]);
    const options = setOptions(s);
    if (!options.length) return <span className="runtime-settings__desc">env allows nothing to narrow</span>;
    return (
      <span className="runtime-settings__set">
        {options.map((opt) => (
          <label key={opt} className="runtime-settings__check runtime-settings__mono">
            <input
              type="checkbox"
              checked={chosen.has(opt)}
              onChange={(e) => {
                const next = new Set(chosen);
                if (e.target.checked) next.add(opt);
                else next.delete(opt);
                onDraft(options.filter((o) => next.has(o)));
              }}
            />
            {opt}
          </label>
        ))}
      </span>
    );
  }
  if (s.type === "bool" || s.type === "enum") {
    const options = s.type === "bool" ? boolOptions(s) : s.choices;
    return (
      <select
        className="runtime-settings__input runtime-settings__mono"
        aria-label="New value"
        value={String(draft)}
        onChange={(e) => onDraft(e.target.value)}
      >
        {options.map((o) => (
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
        className="runtime-settings__input runtime-settings__mono"
        aria-label="New value"
        rows={2}
        value={String(draft)}
        onChange={(e) => onDraft(e.target.value)}
      />
    );
  }
  const max = s.rule === "min" && s.env_known && typeof s.env_value === "number" ? s.env_value : s.bounds?.[1];
  return (
    <input
      className="runtime-settings__input runtime-settings__mono"
      aria-label="New value"
      type="number"
      min={s.bounds?.[0]}
      max={max}
      step={s.type === "int" ? 1 : "any"}
      value={String(draft)}
      onChange={(e) => onDraft(e.target.value)}
    />
  );
}
