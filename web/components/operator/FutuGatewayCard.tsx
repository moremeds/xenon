import {
  FUTU_HELD_KINDS,
  type FutuHeldKind,
  type FutuInfo,
  type FutuQuotesInfo,
  type FutuScope,
} from "@/lib/operatorTypes";

function fmtAgeShort(secs: number | null | undefined): string {
  if (secs == null) return "—";
  if (secs < 90) return `${Math.round(secs)}s`;
  if (secs < 5400) return `${Math.round(secs / 60)}m`;
  if (secs < 172800) return `${Math.round(secs / 3600)}h`;
  return `${Math.round(secs / 86400)}d`;
}

const HELD_LABEL: Record<FutuHeldKind, string> = {
  FUTU_FILL_UNPRICED: "unpriced fill",
  FUTU_FILL_CONFLICT: "fill conflict",
  FUTU_NOT_OBSERVED: "not observed",
};

const MAX_IDS_SHOWN = 3;

function heldEntries(held: FutuScope["held_rows"]) {
  return FUTU_HELD_KINDS.flatMap((kind) => {
    const row = held?.[kind];
    return row && row.count > 0
      ? [{ kind, count: row.count, ids: row.order_ids ?? [] }]
      : [];
  });
}

/**
 * Per-env scopes come from the observation loops; none run under
 * XENON_READ_ONLY=1 (`dev.sh live`), so fall back to the top-level block.
 */
function scopesOf(futu: FutuInfo): FutuScope[] {
  if (futu.scopes && futu.scopes.length > 0) return futu.scopes;
  if (!futu.configured) return [];
  return [
    {
      account_env: futu.scope_env ?? "paper",
      last_sync_age_s: futu.last_sync_age_s,
      observation: futu.observation,
      held_rows: futu.held_rows,
    },
  ];
}

function quotesLine(q: FutuQuotesInfo | null | undefined): string {
  if (!q) return "---";
  const quota = q.quota ? `${q.quota.own_used}/${q.quota.ceiling}` : "---";
  return `OpenD ${q.opend_connected ? "up" : "down"} · futu ${q.routes?.futu ?? "---"} / ib ${q.routes?.ib ?? "---"} · quota ${quota}`;
}

function ScopeRow({
  scope,
  trading,
}: {
  scope: FutuScope;
  trading: boolean | undefined;
}) {
  const held = heldEntries(scope.held_rows);
  const obs = scope.observation;
  return (
    <div className="operator-scope" data-scope={scope.account_env}>
      <div className="operator-scope__main">
        <span className="operator-scope__env">{scope.account_env}</span>
        {scope.configured === false ? (
          // A rotated-out account kept for its unresolved orders: the env's
          // trading state belongs to the configured account, not this one.
          <span className="operator-pill operator-pill--neutral">stored</span>
        ) : (
          <span
            className={`operator-pill operator-pill--${trading ? "core" : "neutral"}`}
          >
            {trading == null ? "---" : trading ? "orders on" : "orders off"}
          </span>
        )}
        <span title="last sync">{fmtAgeShort(scope.last_sync_age_s)}</span>
        <span
          className={
            obs && !obs.complete ? "operator-scope__incomplete" : undefined
          }
          title={obs?.as_of ? `observed as of ${obs.as_of}` : undefined}
        >
          {obs ? (obs.complete ? "observed" : "incomplete") : "obs ---"}
        </span>
        {scope.held_rows == null ? (
          <span>held ---</span>
        ) : held.length === 0 ? (
          <span>held 0</span>
        ) : (
          held.map((h) => (
            <span
              key={h.kind}
              className="operator-pill operator-pill--warn"
              title={`${h.kind}: ${h.ids.join(", ") || "no order ids"}`}
            >
              {`${h.count} ${HELD_LABEL[h.kind]}`}
            </span>
          ))
        )}
      </div>
      {held.map((h) => (
        <p key={h.kind} className="operator-scope__ids">
          {HELD_LABEL[h.kind]}: {h.ids.slice(0, MAX_IDS_SHOWN).join(", ")}
          {h.ids.length > MAX_IDS_SHOWN
            ? ` +${h.ids.length - MAX_IDS_SHOWN}`
            : ""}
        </p>
      ))}
    </div>
  );
}

function capsSummary(caps: FutuInfo["capabilities"]) {
  if (!caps) return null;
  const envs = Object.entries(caps);
  if (envs.length === 0) return null;
  return {
    text: envs.map(([env, c]) => `${env} ${c.tuples.length}`).join(" · "),
    title: envs
      .map(
        ([env, c]) =>
          `${env}: ${c.tuples.join(", ")} (modify ${c.modify.join(", ") || "none"})`,
      )
      .join("\n"),
  };
}

/**
 * Futu OpenD broker section — mirrors IbGatewayCard so the two brokers read the
 * same way inside the shared BrokersCard. Futu places, cancels and modifies
 * single-leg orders per env (`trading[env]`), syncs positions per scope, and
 * can serve live quotes (`quotes`, from the realtime relay). A nonzero held-row
 * count means a fill the console cannot price or observe: it turns the header
 * warn and lists the order ids.
 */
export function FutuGatewayCard({
  futu,
  quotes,
}: {
  futu: FutuInfo;
  quotes?: FutuQuotesInfo | null;
}) {
  const scopes = scopesOf(futu);
  const anyHeld = scopes.some((s) => heldEntries(s.held_rows).length > 0);
  const verdict = !futu.configured
    ? { label: "off", tone: "neutral" }
    : futu.connected
      ? { label: "connected", tone: "core" }
      : { label: "idle", tone: "neutral" };
  const tone = futu.configured && anyHeld ? "warn" : verdict.tone;
  const caps = capsSummary(futu.capabilities);

  return (
    <div className="operator-broker">
      <header className="snapshot-card__header">
        <p className="panel-eyebrow">Futu</p>
        <h3 className="panel-title">OpenD</h3>
        <span className={`operator-pill operator-pill--${tone}`}>
          {verdict.label}
        </span>
      </header>
      <dl className="operator-kv">
        <div>
          <dt>Last sync</dt>
          <dd>{fmtAgeShort(futu.last_sync_age_s)}</dd>
        </div>
        <div className="operator-kv__wide">
          <dt>Quotes</dt>
          <dd>{quotesLine(quotes)}</dd>
        </div>
      </dl>
      {scopes.length > 0 ? (
        <div className="operator-scopes">
          {scopes.map((s, i) => (
            <ScopeRow
              key={`${s.account_env}:${i}`}
              scope={s}
              trading={futu.trading?.[s.account_env]}
            />
          ))}
        </div>
      ) : null}
      {caps ? (
        <p className="operator-caps" title={caps.title}>
          {`order types ${caps.text}`}
        </p>
      ) : null}
      <div className="operator-roles">
        <span
          className="operator-role"
          title={futu.connected ? "connected" : "disconnected"}
        >
          <span
            className={`operator-role__dot operator-role__dot--${
              futu.connected ? "ok" : "down"
            }`}
            aria-hidden
          />
          sync
        </span>
      </div>
    </div>
  );
}
