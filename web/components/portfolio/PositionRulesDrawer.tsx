"use client";

import { useEffect, useState } from "react";

import { cancelRule, fetchPositionRules, type PositionRule } from "@/lib/api/positionRules";

interface DrawerProps {
  positionKey: string;
  onClose: () => void;
}

const CANCELABLE_STATES = new Set<PositionRule["state"]>(["PENDING_ARM", "ARMED", "TRIGGERED"]);

function filterRules(rules: PositionRule[], positionKey: string): PositionRule[] {
  return rules.filter((rule) => rule.position_key === positionKey);
}

export function PositionRulesDrawer({ positionKey, onClose }: DrawerProps) {
  const [rules, setRules] = useState<PositionRule[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setRules(null);
    setError(null);

    fetchPositionRules()
      .then((all) => {
        if (active) setRules(filterRules(all, positionKey));
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : String(err));
      });

    return () => {
      active = false;
    };
  }, [positionKey]);

  async function onCancel(id: number) {
    setError(null);
    try {
      await cancelRule(id);
      const all = await fetchPositionRules();
      setRules(filterRules(all, positionKey));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <aside
      className="fixed right-0 top-0 z-50 h-full w-full max-w-96 overflow-y-auto border-l border-[var(--border-dim)] bg-[var(--bg-panel)] p-4 text-[var(--text-primary)]"
      role="dialog"
      aria-label="Position rules"
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-mono text-[10px] uppercase text-[var(--text-secondary)]">Position rules</div>
          <h2 className="mt-1 break-all font-mono text-sm">{positionKey}</h2>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="h-7 w-7 rounded-full border border-[var(--border-dim)] font-mono text-xs text-[var(--text-secondary)]"
        >
          X
        </button>
      </div>

      {error ? (
        <div
          role="alert"
          className="mt-4 border border-[var(--fault)] bg-[var(--bg-panel-raised)] p-2 text-xs text-[var(--fault)]"
        >
          {error}
        </div>
      ) : null}

      {rules === null ? <div className="mt-4 text-xs text-[var(--text-secondary)]">Measuring rules...</div> : null}
      {rules?.length === 0 ? (
        <div className="mt-4 text-xs text-[var(--text-secondary)]">No position rules match this position key.</div>
      ) : null}

      <ul className="mt-4 space-y-3">
        {rules?.map((rule) => (
          <li key={rule.protection_id} className="border border-[var(--border-dim)] bg-[var(--bg-panel-raised)] p-3">
            <div className="flex items-center justify-between gap-3">
              <span className="font-mono text-xs">{rule.rule_kind}</span>
              <span className="font-mono text-[10px] text-[var(--text-secondary)]">{rule.state}</span>
            </div>
            <pre className="mt-3 max-h-44 overflow-auto whitespace-pre-wrap break-words font-mono text-[10px] text-[var(--text-secondary)]">
              {JSON.stringify(rule.config, null, 2)}
            </pre>
            {CANCELABLE_STATES.has(rule.state) ? (
              <button
                type="button"
                onClick={() => void onCancel(rule.protection_id)}
                className="mt-3 rounded-full bg-[var(--fault)] px-3 py-1 font-mono text-[11px] text-[var(--bg-base)]"
              >
                Cancel rule
              </button>
            ) : null}
          </li>
        ))}
      </ul>
    </aside>
  );
}
