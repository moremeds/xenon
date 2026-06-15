import type { IbPoolRole } from "@/lib/operatorTypes";

export function IbPoolRoles({ pool }: { pool: Record<string, IbPoolRole> }) {
  const roles = Object.entries(pool);
  return (
    <section className="snapshot-card">
      <span className="panel-edge-trace" aria-hidden />
      <header className="snapshot-card__header">
        <p className="panel-eyebrow">IB Pool</p>
        <h3 className="panel-title">Roles</h3>
      </header>
      <ul className="operator-pool">
        {roles.length === 0 ? (
          <li className="operator-pool__empty">no pool</li>
        ) : null}
        {roles.map(([role, info]) => (
          <li key={role} className="operator-pool__row">
            <span
              className={`operator-pool__dot operator-pool__dot--${
                info?.connected ? "ok" : "down"
              }`}
              aria-hidden
            />
            <span className="operator-pool__role">{role}</span>
            <span className="operator-pool__cid">
              {info?.client_id != null ? `#${info.client_id}` : "---"}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
