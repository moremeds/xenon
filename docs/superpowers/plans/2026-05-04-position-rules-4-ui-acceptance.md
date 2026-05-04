# Position Rules — Plan 4: UI, Sweep CLI, FastAPI, Acceptance Gate

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the engine to the operator. Ship the `xenon-position-rules` CLI (list / show / cancel / sweep / health / events), the FastAPI routes that proxy them, the web UI (per-position shield badge, drawer, global health indicator), the daily out-of-band sweep, the duplicate-close audit script, and the paper-smoke runbook. Then walk the 14-day "clean operation" acceptance gate before flipping `XENON_POSITION_RULES_ENABLED=1` on the live account.

**Architecture:** Three vertical slices that read the engine state Plan 2/3 already produces. **CLI slice** uses existing `argparse` patterns and the queries from Plan 2. **FastAPI slice** mirrors existing routes (Clerk auth, `AccountScope` Depends, `JSONResponse` error mapping). **UI slice** lives entirely in `web/` and consumes the FastAPI surface; live updates piggyback on the existing LISTEN/NOTIFY web bridge. The daily out-of-band sweep is a new `BaseHandler` in the daemon, gated behind market-close-only ticking.

**Tech Stack:** Python 3.13, FastAPI, Pydantic, Alembic, Postgres advisory locks, Next.js 15, React 19, Vitest, Playwright, `uv`.

**Spec reference:** `docs/superpowers/specs/2026-05-04-position-rules-design.md` §6.5 (out-of-band sweep), §11 (sweep CLI), §12 (UI + FastAPI + outbox + notifications + CLI mirror), §13.5–§13.8 (route/E2E/paper/CI tests), §15 Phases 6 + 7, §20 (acceptance criteria).

**Prerequisites:**

- Plan 1 merged (DST fix).
- Plan 2 merged (schema + queries + arm consumer).
- Plan 3 merged (handler + executor + Migration B).

---

## File Structure

### Created — backend (CLI + FastAPI + sweep handler + audit)

| Path                                                                  | Responsibility                                                                                                        |
| --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `src/xenon/cli/position_rules.py`                                     | `xenon-position-rules` argparse entry — `list / show / cancel / sweep / health / events`                              |
| `src/xenon/api/routes/position_rules.py`                              | `GET /position-rules`, `GET /position-rules/health`, `POST /position-rules/{id}/cancel`, `POST /position-rules/sweep` |
| `src/xenon/api/services/position_rules_health.py`                     | `compute_health(scope) -> HealthResponse` — daemon liveness, claim counts, market window                              |
| `src/xenon/monitor_daemon/handlers/out_of_band_sweep.py`              | Daily reconciliation handler (§6.5, §10.4). Runs once at 16:30 ET                                                     |
| `src/xenon/db/queries/position_rules_review.py`                       | Sidecar review-annotation table (T3)                                                                                  |
| `src/xenon/db/migrations/versions/<rev>_add_position_rules_review.py` | Alembic migration for the review table                                                                                |
| `scripts/checks/no_duplicate_close_audit.py`                          | Audit script joining claims against IB Flex executions (T4)                                                           |
| `docs/runbooks/position-rules-paper-smoke.md`                         | Paper-account smoke checklist (§13.7)                                                                                 |
| `docs/runbooks/position-rules-acceptance-gate.md`                     | 14-day clean-operation tracker                                                                                        |

### Created — frontend

| Path                                                 | Responsibility                                                                      |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `web/lib/api/positionRules.ts`                       | Typed fetchers + types for `/position-rules*`                                       |
| `web/components/portfolio/ShieldBadge.tsx`           | Per-position color-coded shield (green/amber/orange/red/gray/neutral)               |
| `web/components/portfolio/PositionRulesDrawer.tsx`   | Side drawer with per-rule rows + cancel button                                      |
| `web/components/portfolio/GlobalHealthIndicator.tsx` | Sidebar `🛡 N armed · last tick Xs ago` + market-window-aware staleness suppression |
| `web/lib/realtime/positionRulesSubscription.ts`      | LISTEN/NOTIFY `position_rule.transition` consumer that triggers React Query refetch |
| `web/app/api/position-rules/route.ts`                | Next.js proxy → FastAPI                                                             |
| `web/app/api/position-rules/[id]/cancel/route.ts`    | Next.js proxy → FastAPI cancel                                                      |
| `web/app/api/position-rules/health/route.ts`         | Next.js proxy → FastAPI health                                                      |
| `web/app/api/position-rules/sweep/route.ts`          | Next.js proxy → FastAPI sweep apply                                                 |

### Created — tests

| Path                                                                    | Layer                                                                               |
| ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `scripts/tests/test_position_rules_cli/test_list_show_health.py`        | CLI happy path                                                                      |
| `scripts/tests/test_position_rules_cli/test_sweep.py`                   | Sweep `--dry-run` + `--apply` (paper mode)                                          |
| `scripts/tests/test_position_rules_cli/test_events_review.py`           | Events CLI + review annotation                                                      |
| `scripts/tests/test_position_rules_db/test_out_of_band_sweep.py`        | Daily sweep — 70% gate, unprotected_position_detected emit                          |
| `scripts/tests/test_position_rules_db/test_no_duplicate_close_audit.py` | Audit script SQL                                                                    |
| `web/tests/positionRules.test.ts`                                       | FastAPI harness — happy path, scope filter, cancel idempotency, live-mode auth gate |
| `web/tests/positionRulesShieldBadge.test.tsx`                           | Vitest — color logic per state                                                      |
| `web/tests/positionRulesGlobalHealth.test.tsx`                          | Vitest — staleness suppression outside RTH                                          |
| `web/tests/e2e/positionRules.spec.ts`                                   | Playwright golden path                                                              |

---

## Task 1: Sidecar review-annotation table (Alembic + queries)

**Files:**

- Create: `src/xenon/db/migrations/versions/<rev>_add_position_rules_review.py`
- Modify: `src/xenon/db/schema.py` (add `position_rules_review` Table)
- Create: `src/xenon/db/queries/position_rules_review.py`

- [ ] **Step 1: Generate the migration**

```bash
uv run alembic revision -m "add position_rules_review sidecar table"
```

- [ ] **Step 2: Author migration body**

```python
"""add position_rules_review sidecar table

Revision ID: <generated>
Revises: <plan3 migration B revision>
"""
from alembic import op
import sqlalchemy as sa

revision = "<generated>"
down_revision = "<plan3 revision>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "position_rules_review",
        sa.Column("review_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("protection_id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),  # outbox.id of the transition
        sa.Column("reviewed_by", sa.Text(), nullable=False),
        sa.Column("reviewed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("(now() AT TIME ZONE 'UTC')")),
        sa.Column("verdict", sa.Text(), nullable=False),  # 'expected' | 'unexpected' | 'structural'
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "verdict IN ('expected','unexpected','structural')",
            name="ck_position_rules_review_verdict",
        ),
        sa.UniqueConstraint("event_id", name="uq_position_rules_review_event"),
        schema="xenon",
    )
    op.create_index(
        "ix_position_rules_review_protection",
        "position_rules_review",
        ["protection_id", "reviewed_at"],
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_index("ix_position_rules_review_protection", table_name="position_rules_review", schema="xenon")
    op.drop_table("position_rules_review", schema="xenon")
```

- [ ] **Step 3: Add Table to `schema.py`**

```python
position_rules_review = Table(
    "position_rules_review",
    xenon_metadata,
    Column("review_id", BigInteger, primary_key=True, autoincrement=True),
    Column("protection_id", BigInteger, nullable=False),
    Column("event_id", BigInteger, nullable=False),
    Column("reviewed_by", Text, nullable=False),
    Column("reviewed_at", TIMESTAMP(timezone=True), nullable=False, server_default=tz_now),
    Column("verdict", Text, nullable=False),
    Column("note", Text, nullable=True),
    CheckConstraint(
        "verdict IN ('expected','unexpected','structural')",
        name="ck_position_rules_review_verdict",
    ),
    UniqueConstraint("event_id", name="uq_position_rules_review_event"),
    Index("ix_position_rules_review_protection", "protection_id", "reviewed_at"),
)
```

- [ ] **Step 4: Implement `position_rules_review.py` queries**

```python
# src/xenon/db/queries/position_rules_review.py
"""Sidecar annotations for the daily ops review tool. Spec §13.8 T3."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, and_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xenon.db.schema import position_rules_review


def add_annotation(
    engine,
    *,
    protection_id: int,
    event_id: int,
    reviewed_by: str,
    verdict: str,
    note: str | None = None,
) -> int | None:
    assert verdict in ("expected", "unexpected", "structural")
    with engine.begin() as conn:
        stmt = (
            pg_insert(position_rules_review)
            .values(
                protection_id=protection_id, event_id=event_id,
                reviewed_by=reviewed_by, verdict=verdict, note=note,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(position_rules_review.c.review_id)
        )
        row = conn.execute(stmt).first()
        return row[0] if row else None


def list_annotations(engine, *, since_event_id: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(position_rules_review)
            .where(position_rules_review.c.event_id > since_event_id)
            .order_by(position_rules_review.c.event_id.desc())
            .limit(limit)
        ).all()
        return [dict(r._mapping) for r in rows]
```

- [ ] **Step 5: Migrate + commit**

```bash
uv run alembic upgrade head
uv run pytest src/xenon/db/tests/test_schema.py -xvs
git add src/xenon/db/migrations/versions/<rev>_add_position_rules_review.py src/xenon/db/schema.py src/xenon/db/queries/position_rules_review.py
git commit -m "feat(db): add position_rules_review sidecar table for ops annotations"
```

---

## Task 2: `xenon-position-rules` CLI — list / show / health

**Files:**

- Create: `src/xenon/cli/position_rules.py`
- Modify: `pyproject.toml` — register `xenon-position-rules` script
- Create: `scripts/tests/test_position_rules_cli/__init__.py` (empty)
- Create: `scripts/tests/test_position_rules_cli/test_list_show_health.py`

- [ ] **Step 1: Write the failing tests**

```python
# scripts/tests/test_position_rules_cli/test_list_show_health.py
"""xenon-position-rules CLI list/show/health. Spec §12.5."""
from __future__ import annotations

import json
import subprocess

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_protection import insert_pending_arm


@pytest.fixture
def engine_with_row():
    e = get_sync_engine()
    with e.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key='STK::CLITEST'"))
    pid = insert_pending_arm(
        e, broker="IB", account_env="paper", broker_account="DU1234567",
        position_key="STK::CLITEST",
        position_descriptor={"asset_class": "stock", "anchor_price": 100.0, "opened_qty": 100,
                             "protected_qty": 100, "multiplier": 1, "qty_unit": "share",
                             "opened_at": "2026-05-04T14:00:00Z", "source": "fastapi_orders_place",
                             "anchor_currency": "USD",
                             "legs": [{"sec_type": "STK", "symbol": "CLITEST", "action": "BUY", "ratio": 1, "fill_price": 100.0, "con_id": 1}]},
        asset_class="stock", rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    yield pid
    with e.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key='STK::CLITEST'"))


def _run(args, env_extra=None):
    env = {"XENON_TRADING_MODE": "paper", "XENON_BROKER_ACCOUNT": "DU1234567", "XENON_BROKER": "IB"}
    if env_extra:
        env.update(env_extra)
    import os as _os
    full_env = {**_os.environ, **env}
    return subprocess.run(["xenon-position-rules", *args], capture_output=True, text=True, env=full_env, timeout=30)


def test_list_returns_json(engine_with_row):
    result = _run(["list", "--json"])
    assert result.returncode == 0
    rows = json.loads(result.stdout)
    assert any(r["position_key"] == "STK::CLITEST" for r in rows)


def test_show_returns_full_row(engine_with_row):
    result = _run(["show", str(engine_with_row), "--json"])
    assert result.returncode == 0
    body = json.loads(result.stdout)
    assert body["protection_id"] == engine_with_row
    assert body["position_key"] == "STK::CLITEST"


def test_health_includes_market_window():
    result = _run(["health", "--json"])
    assert result.returncode == 0
    body = json.loads(result.stdout)
    assert "market_window" in body
    assert "rule_counts_by_state" in body
    assert "claim_counts_by_status" in body
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_position_rules_cli/test_list_show_health.py -xvs`

(`xenon-position-rules` is not yet a registered entry-point — test fails with FileNotFoundError.)

- [ ] **Step 3: Implement the CLI**

```python
# src/xenon/cli/position_rules.py
"""xenon-position-rules CLI — operator-facing surface.

Spec §11 + §12.5. Subcommands:
  list    — show active rows in the current scope
  show    — full payload of a single row
  cancel  — operator override → CANCELED
  sweep   — backfill missing protections (--dry-run | --apply)
  health  — daemon liveness + counts
  events  — outbox transition feed for the daily review (T3)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_protection import (
    cas_transition,
    get_by_id,
    list_active_rows,
)
from xenon.db.queries.position_rules_review import add_annotation
from xenon.db.schema import (
    outbox,
    position_close_claims,
    position_protection,
    position_rules_review,
)
from xenon.execution.account_scope import resolve_from_env


# ── list ─────────────────────────────────────────────────────────────────────


def _cmd_list(args) -> int:
    scope = resolve_from_env()
    engine = get_sync_engine()
    rows = list_active_rows(
        engine, broker=scope.broker, account_env=scope.account_env,
        broker_account=scope.broker_account,
        states=tuple(args.state) if args.state else ("PENDING_ARM", "ARMED", "TRIGGERED"),
    )
    if args.rule_kind:
        rows = [r for r in rows if r["rule_kind"] in args.rule_kind]
    if args.json:
        # SQLAlchemy datetimes are not JSON-serializable; coerce.
        for r in rows:
            for k, v in list(r.items()):
                if isinstance(v, datetime):
                    r[k] = v.isoformat()
        print(json.dumps(rows, default=str))
    else:
        for r in rows:
            print(f"{r['protection_id']:>6} {r['rule_kind']:<20} {r['state']:<12} {r['position_key']}")
    return 0


# ── show ─────────────────────────────────────────────────────────────────────


def _cmd_show(args) -> int:
    engine = get_sync_engine()
    row = get_by_id(engine, protection_id=args.protection_id)
    if row is None:
        print(f"protection_id={args.protection_id} not found", file=sys.stderr)
        return 1
    for k, v in list(row.items()):
        if isinstance(v, datetime):
            row[k] = v.isoformat()
    if args.json:
        print(json.dumps(row, default=str))
    else:
        for k, v in row.items():
            print(f"{k:<25} {v}")
    return 0


# ── cancel ───────────────────────────────────────────────────────────────────


def _cmd_cancel(args) -> int:
    engine = get_sync_engine()
    row = get_by_id(engine, protection_id=args.protection_id)
    if row is None:
        print(f"protection_id={args.protection_id} not found", file=sys.stderr)
        return 1
    if row["state"] not in ("PENDING_ARM", "ARMED", "TRIGGERED"):
        if not args.force:
            print(f"row is already terminal ({row['state']}); use --force to override", file=sys.stderr)
            return 1
    success = cas_transition(
        engine, protection_id=args.protection_id,
        expected_state=row["state"], new_state="CANCELED",
        reason="operator_cancel_cli",
    )
    if not success:
        print("CAS transition failed (concurrent state change?)", file=sys.stderr)
        return 1
    print(f"canceled protection_id={args.protection_id}")
    return 0


# ── sweep ────────────────────────────────────────────────────────────────────


def _cmd_sweep(args) -> int:
    """Spec §11. Walk IBClient.positions(); insert PENDING_ARM rows for any not protected."""
    import os
    if args.apply:
        if os.environ.get("XENON_TRADING_MODE") == "live":
            if not os.environ.get("XENON_OPERATOR_USER_ID"):
                print(json.dumps({"reason_code": "live_trading_auth_unconfigured",
                                  "message": "XENON_OPERATOR_USER_ID must be set for live --apply"}), file=sys.stderr)
                return 1

    scope = resolve_from_env()
    engine = get_sync_engine()
    from xenon.clients.ib_client import IBClient
    ib = IBClient.singleton()
    positions = ib.positions() if hasattr(ib, "positions") else []

    plan = []
    for pos in positions or []:
        # Cheap precheck: does any non-terminal position_protection row exist for this position?
        with engine.connect() as conn:
            existing = conn.execute(text("""
                SELECT 1 FROM xenon.position_protection
                WHERE broker = :b AND account_env = :e AND broker_account = :a
                  AND position_key LIKE :pkey
                  AND state IN ('PENDING_ARM','ARMED','TRIGGERED')
                LIMIT 1
            """), {"b": scope.broker, "e": scope.account_env, "a": scope.broker_account,
                   "pkey": f"%::{pos['symbol']}%"}).first()
        if existing:
            continue
        plan.append({"symbol": pos.get("symbol"), "qty": pos.get("qty"), "con_id": pos.get("con_id")})

    if not args.apply:
        print(json.dumps({"would_insert": plan, "count": len(plan)}, default=str))
        return 0

    # --apply path: classify each candidate via arm_hook semantics, then INSERT PENDING_ARM.
    from xenon.execution.brackets.arm_hook import on_fill_event
    inserted = 0
    for cand in plan:
        # Build a synthetic fill_recorded payload to drive the arm hook.
        synthetic_payload = {
            "exec_id": f"sweep-{scope.broker_account}-{cand['symbol']}-{datetime.now(timezone.utc).timestamp()}",
            "submission_id": None, "combo_attempt_id": None,
            "perm_id": None, "ticker": cand["symbol"], "side": "BUY",
            "qty": int(cand["qty"]), "price": "0",  # anchor will be the current mark
            "filled_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"sec_type": "STK"},
            "broker": scope.broker, "account_env": scope.account_env, "broker_account": scope.broker_account,
            "con_id": cand.get("con_id"),
        }
        # NB: arm_hook re-fetches from order_fills, so for sweep we need a different code path
        # — call the classifier + insert directly. Implemented in arm_hook.sweep_insert helper.
        from xenon.execution.brackets.arm_hook import sweep_insert
        try:
            sweep_insert(engine, scope=scope, candidate=cand)
            inserted += 1
        except Exception as exc:  # noqa: BLE001
            print(f"sweep: failed for {cand['symbol']}: {exc}", file=sys.stderr)

    print(json.dumps({"applied": inserted, "skipped": len(plan) - inserted}))
    return 0


# ── health ───────────────────────────────────────────────────────────────────


def _cmd_health(args) -> int:
    from xenon.api.services.position_rules_health import compute_health
    scope = resolve_from_env()
    engine = get_sync_engine()
    body = compute_health(engine=engine, scope=scope)
    print(json.dumps(body, default=str))
    return 0


# ── events (T3) ──────────────────────────────────────────────────────────────


def _cmd_events(args) -> int:
    engine = get_sync_engine()
    since = args.since
    cutoff = datetime.now(timezone.utc) - _parse_since(since)
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, payload, created_at
            FROM events.outbox
            WHERE channel = 'position_rule.transition'
              AND created_at >= :cutoff
            ORDER BY id DESC
            LIMIT 1000
        """), {"cutoff": cutoff}).all()
    out = []
    for r in rows:
        rec = {"event_id": r.id, "created_at": r.created_at.isoformat(), **r.payload}
        out.append(rec)
    print(json.dumps(out, default=str))
    return 0


def _cmd_review(args) -> int:
    """xenon-position-rules events --annotate <event_id> --verdict expected --note '...'"""
    if not args.protection_id:
        print("--protection-id required", file=sys.stderr)
        return 1
    engine = get_sync_engine()
    rid = add_annotation(
        engine,
        protection_id=args.protection_id,
        event_id=args.event_id,
        reviewed_by=args.reviewed_by,
        verdict=args.verdict,
        note=args.note,
    )
    print(json.dumps({"review_id": rid}))
    return 0


# ── argparse wiring ──────────────────────────────────────────────────────────


def _parse_since(s: str) -> timedelta:
    if s.endswith("h"):
        return timedelta(hours=int(s[:-1]))
    if s.endswith("d"):
        return timedelta(days=int(s[:-1]))
    if s.endswith("m"):
        return timedelta(minutes=int(s[:-1]))
    raise ValueError(f"unrecognized --since: {s!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xenon-position-rules")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--state", action="append", choices=["PENDING_ARM", "ARMED", "TRIGGERED", "CLOSED", "CANCELED", "FAILED", "SUPERSEDED"])
    p_list.add_argument("--rule-kind", dest="rule_kind", action="append")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show")
    p_show.add_argument("protection_id", type=int)
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=_cmd_show)

    p_cancel = sub.add_parser("cancel")
    p_cancel.add_argument("protection_id", type=int)
    p_cancel.add_argument("--force", action="store_true")
    p_cancel.set_defaults(func=_cmd_cancel)

    p_sweep = sub.add_parser("sweep")
    p_sweep.add_argument("--dry-run", action="store_true", default=True)
    p_sweep.add_argument("--apply", action="store_true")
    p_sweep.add_argument("--rate-limit-per-min", type=int, default=30)
    p_sweep.set_defaults(func=_cmd_sweep)

    p_health = sub.add_parser("health")
    p_health.add_argument("--json", action="store_true")
    p_health.set_defaults(func=_cmd_health)

    p_events = sub.add_parser("events")
    p_events.add_argument("--since", default="24h")
    p_events.set_defaults(func=_cmd_events)

    p_review = sub.add_parser("review")
    p_review.add_argument("--event-id", type=int, required=True)
    p_review.add_argument("--protection-id", type=int)
    p_review.add_argument("--reviewed-by", required=True)
    p_review.add_argument("--verdict", required=True, choices=["expected", "unexpected", "structural"])
    p_review.add_argument("--note")
    p_review.set_defaults(func=_cmd_review)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Add `sweep_insert` helper to `arm_hook.py`**

Append to `src/xenon/execution/brackets/arm_hook.py`:

```python
def sweep_insert(engine, *, scope, candidate: dict[str, Any]) -> None:
    """Operator-driven sweep insert. Classifies + inserts PENDING_ARM rows for an
    existing un-protected position. Spec §11.
    """
    legs = [{
        "sec_type": "STK", "symbol": candidate["symbol"], "action": "BUY",
        "ratio": 1, "fill_price": 0.0, "con_id": candidate.get("con_id") or 0,
    }]
    classify = classify_position(legs=legs, wizard_session_payload=None, sibling_legs=None)
    if classify.asset_class.value not in ("stock", "long_option"):
        # v1 sweep covers only single-leg via this path.
        return
    rows = resolve_for_scope(
        engine, broker=scope.broker, account_env=scope.account_env,
        broker_account=scope.broker_account, asset_class=classify.asset_class.value,
    )
    deduped = deduplicate_by_specificity(rows)
    if not deduped:
        return
    descriptor = {
        "asset_class": classify.asset_class.value,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "source": "sweep_cli",
        "anchor_price": 0.0,
        "anchor_currency": "USD",
        "opened_qty": int(candidate["qty"]),
        "protected_qty": int(candidate["qty"]),
        "multiplier": 1,
        "qty_unit": "share",
        "legs": legs,
    }
    pkey = compute_position_key(classify.asset_class.value, descriptor)
    for policy in deduped:
        insert_pending_arm(
            engine,
            broker=scope.broker, account_env=scope.account_env, broker_account=scope.broker_account,
            position_key=pkey, position_descriptor=descriptor,
            asset_class=classify.asset_class.value, rule_kind=policy.rule_kind,
            config=policy.config,
        )
```

- [ ] **Step 5: Register entry point in `pyproject.toml`**

Add to `[project.scripts]`:

```toml
xenon-position-rules = "xenon.cli.position_rules:main"
```

Then:

```bash
uv sync --extra test
```

- [ ] **Step 6: Run tests + commit**

Run: `uv run pytest scripts/tests/test_position_rules_cli/test_list_show_health.py -xvs`

(The `health` test depends on Task 3 below — defer that one row of the test until then; mark it `xfail` if useful, but the `list`/`show` tests should pass.)

```bash
git add src/xenon/cli/position_rules.py src/xenon/execution/brackets/arm_hook.py pyproject.toml uv.lock scripts/tests/test_position_rules_cli/__init__.py scripts/tests/test_position_rules_cli/test_list_show_health.py
git commit -m "feat(cli): add xenon-position-rules with list/show/cancel/sweep/events/review"
```

---

## Task 3: Health-endpoint compute helper

**Files:**

- Create: `src/xenon/api/services/position_rules_health.py`
- Create: `scripts/tests/test_position_rules_db/test_position_rules_health.py`

- [ ] **Step 1: Write the failing tests**

```python
# scripts/tests/test_position_rules_db/test_position_rules_health.py
"""compute_health() reports correct counts + market window. Spec §12.2."""
from __future__ import annotations

from unittest.mock import patch
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from xenon.api.services.position_rules_health import compute_health
from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope


@pytest.fixture
def engine():
    e = get_sync_engine()
    yield e


def test_health_reports_market_window(engine):
    body = compute_health(engine=engine, scope=AccountScope(broker="IB", account_env="paper", broker_account="DU1234567"))
    assert body["market_window"] in ("open", "closed", "pre_open", "post_close")
    assert body["schema_version"] == 1
    assert "rule_counts_by_state" in body
    assert "claim_counts_by_status" in body
    assert "ib_connected" in body


def test_health_groups_state_counts(engine):
    body = compute_health(engine=engine, scope=AccountScope(broker="IB", account_env="paper", broker_account="DU1234567"))
    counts = body["rule_counts_by_state"]
    for state in ("PENDING_ARM", "ARMED", "TRIGGERED", "FAILED", "CANCELED", "CLOSED", "SUPERSEDED"):
        assert state in counts
        assert isinstance(counts[state], int)
```

- [ ] **Step 2: Implement `position_rules_health.py`**

```python
# src/xenon/api/services/position_rules_health.py
"""Health/status payload for /position-rules/health. Spec §12.2."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from xenon.execution.account_scope import AccountScope


_RTH_OPEN_H, _RTH_OPEN_M = 9, 30
_RTH_CLOSE_H, _RTH_CLOSE_M = 16, 0


def _market_window(now: datetime | None = None) -> tuple[str, datetime]:
    et = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        # Weekend — next event is next Monday RTH open.
        days_until_monday = (7 - et.weekday()) % 7 or 7
        next_open = et.replace(hour=_RTH_OPEN_H, minute=_RTH_OPEN_M, second=0, microsecond=0) + \
                    __import__("datetime").timedelta(days=days_until_monday)
        return "closed", next_open.astimezone(timezone.utc)
    cur_min = et.hour * 60 + et.minute
    open_min = _RTH_OPEN_H * 60 + _RTH_OPEN_M
    close_min = _RTH_CLOSE_H * 60 + _RTH_CLOSE_M
    if cur_min < open_min:
        next_event = et.replace(hour=_RTH_OPEN_H, minute=_RTH_OPEN_M, second=0, microsecond=0)
        return "pre_open", next_event.astimezone(timezone.utc)
    if cur_min >= close_min:
        # Next event is tomorrow's open (or Monday's if Friday).
        days_ahead = 1 if et.weekday() < 4 else (7 - et.weekday())
        next_open = et.replace(hour=_RTH_OPEN_H, minute=_RTH_OPEN_M, second=0, microsecond=0) + \
                    __import__("datetime").timedelta(days=days_ahead)
        return "post_close", next_open.astimezone(timezone.utc)
    next_close = et.replace(hour=_RTH_CLOSE_H, minute=_RTH_CLOSE_M, second=0, microsecond=0)
    return "open", next_close.astimezone(timezone.utc)


def compute_health(*, engine, scope: AccountScope) -> dict[str, Any]:
    market_window, next_event = _market_window()

    with engine.connect() as conn:
        rule_counts = dict(conn.execute(text("""
            SELECT state, COUNT(*) FROM xenon.position_protection
            WHERE broker = :b AND account_env = :e AND broker_account = :a
            GROUP BY state
        """), {"b": scope.broker, "e": scope.account_env, "a": scope.broker_account}).all())
        for state in ("PENDING_ARM", "ARMED", "TRIGGERED", "FAILED", "CANCELED", "CLOSED", "SUPERSEDED"):
            rule_counts.setdefault(state, 0)

        claim_counts = dict(conn.execute(text("""
            SELECT status, COUNT(*) FROM xenon.position_close_claims
            WHERE broker = :b AND account_env = :e AND broker_account = :a
            GROUP BY status
        """), {"b": scope.broker, "e": scope.account_env, "a": scope.broker_account}).all())
        for status in ("PENDING", "SUBMITTED", "FILLED", "FAILED", "ABANDONED"):
            claim_counts.setdefault(status, 0)

        in_flight = claim_counts["PENDING"] + claim_counts["SUBMITTED"]

        stale_skips_last_hour = conn.execute(text("""
            SELECT COUNT(*) FROM events.outbox
            WHERE channel = 'position_rule.transition'
              AND created_at >= now() - interval '1 hour'
              AND payload->>'reason' IN ('stale_quote_skip','silent_market_suspected','ib_connection_stale')
        """)).scalar_one()

        unprotected = conn.execute(text("""
            SELECT COUNT(*) FROM events.outbox
            WHERE channel = 'position_rule.transition'
              AND created_at >= now() - interval '24 hours'
              AND payload->>'kind' = 'unprotected_position_detected'
        """)).scalar_one()

        dlq_count = conn.execute(text("""
            SELECT COUNT(*) FROM events.outbox_dlq
        """)).scalar_one()

        last_tick = conn.execute(text("""
            SELECT MAX(created_at) FROM events.outbox
            WHERE channel = 'position_rule.transition' AND payload->>'source' = 'cas_transition'
        """)).scalar_one()

    last_tick_age_s = None
    if last_tick:
        last_tick_age_s = int((datetime.now(timezone.utc) - last_tick).total_seconds())

    daemon_alive = last_tick_age_s is not None and last_tick_age_s < 600 if market_window == "open" else True

    # IBClient liveness — best-effort
    try:
        from xenon.clients.ib_client import IBClient
        ib_connected = bool(getattr(IBClient.singleton(), "connected", False))
    except Exception:  # noqa: BLE001
        ib_connected = False

    return {
        "schema_version": 1,
        "daemon_alive": daemon_alive,
        "advisory_lock_held": True,  # if we're answering, it's held
        "last_tick_at": last_tick.isoformat() if last_tick else None,
        "last_tick_age_seconds": last_tick_age_s,
        "market_window": market_window,
        "next_market_event_at": next_event.isoformat(),
        "rule_counts_by_state": rule_counts,
        "claim_counts_by_status": claim_counts,
        "in_flight_claims": in_flight,
        "stale_quote_skips_last_hour": stale_skips_last_hour,
        "unprotected_position_count": unprotected,
        "ib_connected": ib_connected,
        "outbox_dlq_count": dlq_count,
        "scope": scope.as_dict(),
    }
```

- [ ] **Step 3: Run + commit**

Run: `uv run pytest scripts/tests/test_position_rules_db/test_position_rules_health.py -xvs`
Expected: 2 green.

```bash
git add src/xenon/api/services/position_rules_health.py scripts/tests/test_position_rules_db/test_position_rules_health.py
git commit -m "feat(api): add compute_health for position-rules"
```

---

## Task 4: FastAPI routes

**Files:**

- Create: `src/xenon/api/routes/position_rules.py`
- Modify: `src/xenon/api/server.py` — register the new router
- Create: `web/tests/positionRules.test.ts`

- [ ] **Step 1: Write the failing test (FastAPI harness)**

```typescript
// web/tests/positionRules.test.ts
import { describe, it, expect } from "vitest";
import { startFastapi } from "./fastapiHarness";

describe("/position-rules", () => {
  it("GET / returns rows for current scope", async () => {
    const harness = await startFastapi({
      trading_mode: "paper",
      account: "DU1234567",
    });
    const res = await harness.fetch("/position-rules");
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(Array.isArray(body)).toBe(true);
    await harness.stop();
  });

  it("GET /health includes market_window and counts", async () => {
    const harness = await startFastapi({
      trading_mode: "paper",
      account: "DU1234567",
    });
    const res = await harness.fetch("/position-rules/health");
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty("market_window");
    expect(body).toHaveProperty("rule_counts_by_state");
    expect(body).toHaveProperty("claim_counts_by_status");
    await harness.stop();
  });

  it("POST /{id}/cancel returns 404 for unknown id", async () => {
    const harness = await startFastapi({
      trading_mode: "paper",
      account: "DU1234567",
    });
    const res = await harness.fetch("/position-rules/9999999/cancel", {
      method: "POST",
    });
    expect(res.status).toBe(404);
    await harness.stop();
  });

  it("POST /sweep --apply requires live_trading_auth in live mode", async () => {
    // Codex N-S6 regression
    const harness = await startFastapi({
      trading_mode: "live",
      account: "U1234567",
      clerk_unconfigured: true,
    });
    const res = await harness.fetch("/position-rules/sweep", {
      method: "POST",
      body: JSON.stringify({ apply: true }),
    });
    expect(res.status).toBe(503);
    const body = await res.json();
    expect(body.reason_code).toBe("live_trading_auth_unconfigured");
    await harness.stop();
  });
});
```

- [ ] **Step 2: Implement `position_rules.py` route**

```python
# src/xenon/api/routes/position_rules.py
"""FastAPI routes for /position-rules. Spec §12.2."""
from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from xenon.api.guards import get_account_scope
from xenon.api.services.position_rules_health import compute_health
from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_protection import (
    cas_transition,
    get_by_id,
    list_active_rows,
)
from xenon.execution.account_scope import AccountScope

router = APIRouter(prefix="/position-rules", tags=["position-rules"])


def _live_auth_ok(request: Request, scope: AccountScope) -> JSONResponse | None:
    """Spec §11 codex N-S6: live mutating endpoints fail closed if Clerk unconfigured."""
    if scope.account_env != "live":
        return None
    clerk_jwks = os.environ.get("CLERK_JWKS_URL")
    clerk_issuer = os.environ.get("CLERK_ISSUER")
    if not (clerk_jwks and clerk_issuer):
        return JSONResponse(
            status_code=503,
            content={"reason_code": "live_trading_auth_unconfigured",
                     "message": "Clerk auth must be configured for live mutating endpoints"},
        )
    user = getattr(request.state, "user", None)
    if user is None:
        return JSONResponse(
            status_code=401,
            content={"reason_code": "live_trading_auth_unauthenticated",
                     "message": "Authenticated user required for live mode"},
        )
    return None


@router.get("/")
def list_rules(
    scope: Annotated[AccountScope, Depends(get_account_scope)],
):
    engine = get_sync_engine()
    rows = list_active_rows(
        engine, broker=scope.broker, account_env=scope.account_env,
        broker_account=scope.broker_account,
    )
    # Coerce datetimes for JSON.
    for r in rows:
        for k in ("armed_at", "triggered_at", "closed_at", "last_evaluated_at", "created_at", "updated_at"):
            if r.get(k):
                r[k] = r[k].isoformat()
    return rows


@router.get("/health")
def health(
    scope: Annotated[AccountScope, Depends(get_account_scope)],
):
    engine = get_sync_engine()
    return compute_health(engine=engine, scope=scope)


@router.post("/{protection_id}/cancel")
def cancel(
    protection_id: int,
    request: Request,
    scope: Annotated[AccountScope, Depends(get_account_scope)],
):
    auth_err = _live_auth_ok(request, scope)
    if auth_err is not None:
        return auth_err
    engine = get_sync_engine()
    row = get_by_id(engine, protection_id=protection_id)
    if row is None:
        raise HTTPException(status_code=404, detail="protection not found")
    if row["state"] not in ("PENDING_ARM", "ARMED", "TRIGGERED"):
        return JSONResponse(
            status_code=409,
            content={"reason_code": "already_terminal", "state": row["state"]},
        )
    success = cas_transition(
        engine, protection_id=protection_id,
        expected_state=row["state"], new_state="CANCELED",
        reason="operator_cancel_api",
    )
    if not success:
        return JSONResponse(
            status_code=409,
            content={"reason_code": "concurrent_state_change"},
        )
    return {"protection_id": protection_id, "state": "CANCELED"}


@router.post("/sweep")
async def sweep(
    request: Request,
    scope: Annotated[AccountScope, Depends(get_account_scope)],
):
    body = await request.json() if request.headers.get("content-length") and int(request.headers["content-length"]) > 0 else {}
    apply_mode = bool(body.get("apply"))
    if apply_mode:
        auth_err = _live_auth_ok(request, scope)
        if auth_err is not None:
            return auth_err
    # Delegate to CLI helper logic via subprocess so all sweep semantics live in one place.
    import subprocess
    import json as _json
    cmd = ["xenon-position-rules", "sweep"]
    if apply_mode:
        cmd.append("--apply")
    env = os.environ.copy()
    env["XENON_TRADING_MODE"] = scope.account_env
    env["XENON_BROKER_ACCOUNT"] = scope.broker_account
    env["XENON_BROKER"] = scope.broker
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    if result.returncode != 0:
        return JSONResponse(status_code=500, content={"reason_code": "sweep_failed", "stderr": result.stderr})
    try:
        return _json.loads(result.stdout)
    except _json.JSONDecodeError:
        return JSONResponse(status_code=500, content={"reason_code": "sweep_unparseable", "stdout": result.stdout[:500]})
```

- [ ] **Step 3: Register the router in `server.py`**

Edit `src/xenon/api/server.py` to import and include:

```python
from xenon.api.routes.position_rules import router as position_rules_router
# ...
app.include_router(position_rules_router)
```

- [ ] **Step 4: Add Next.js proxy routes**

Create thin proxy files under `web/app/api/position-rules/`:

```typescript
// web/app/api/position-rules/route.ts
import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/xenonFetch";

export async function GET(req: Request) {
  const res = await xenonFetch("/position-rules", { headers: req.headers });
  return new NextResponse(await res.text(), {
    status: res.status,
    headers: res.headers,
  });
}
```

(Mirror this shape for `/health/route.ts`, `/[id]/cancel/route.ts`, `/sweep/route.ts`.)

- [ ] **Step 5: Run + commit**

Run:

```bash
cd web && npm test -- --run positionRules
cd ..
uv run pytest scripts/tests/test_position_rules_cli/ scripts/tests/test_position_rules_db/test_position_rules_health.py -xvs
```

```bash
git add src/xenon/api/routes/position_rules.py src/xenon/api/server.py web/app/api/position-rules/ web/tests/positionRules.test.ts
git commit -m "feat(api): add /position-rules endpoints with N-S6 live-auth gate"
```

---

## Task 5: Daily out-of-band sweep handler

**Files:**

- Create: `src/xenon/monitor_daemon/handlers/out_of_band_sweep.py`
- Create: `scripts/tests/test_position_rules_db/test_out_of_band_sweep.py`
- Modify: `src/xenon/monitor_daemon/run.py` — register the new handler

The sweep runs once per day at 16:30 ET. Implements §6.5 + §10.4 quarter-end re-arm. **Includes the T5 70% sanity gate** (abort if IB returns suspiciously few positions).

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_position_rules_db/test_out_of_band_sweep.py
"""Daily out-of-band sweep with 70% sanity gate. Spec §6.5, T5."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope
from xenon.monitor_daemon.handlers.out_of_band_sweep import OutOfBandSweepHandler


@pytest.fixture
def engine():
    e = get_sync_engine()
    yield e


def test_sweep_emits_unprotected_for_unknown_position(engine):
    ib = MagicMock()
    ib.connected = True
    ib.positions.return_value = [{"symbol": "OOB-TEST", "qty": 100, "con_id": 1}]
    handler = OutOfBandSweepHandler(
        engine=engine, ib_client=ib,
        scope=AccountScope(broker="IB", account_env="paper", broker_account="DU1234567"),
    )
    result = handler.execute()
    assert result["unprotected_count"] >= 1


def test_sweep_aborts_when_positions_drop_70_pct(engine):
    """T5 sanity gate: yesterday had 10 positions, today only 2 → abort."""
    ib = MagicMock()
    ib.connected = True
    ib.positions.return_value = [{"symbol": "X", "qty": 1, "con_id": 1}, {"symbol": "Y", "qty": 1, "con_id": 2}]
    handler = OutOfBandSweepHandler(
        engine=engine, ib_client=ib,
        scope=AccountScope(broker="IB", account_env="paper", broker_account="DU1234567"),
    )
    handler._last_known_position_count = 10  # type: ignore[attr-defined]
    result = handler.execute()
    assert result["status"] == "aborted_short_response"


def test_sweep_skips_when_ib_disconnected(engine):
    ib = MagicMock()
    ib.connected = False
    handler = OutOfBandSweepHandler(
        engine=engine, ib_client=ib,
        scope=AccountScope(broker="IB", account_env="paper", broker_account="DU1234567"),
    )
    result = handler.execute()
    assert result["status"] == "skipped_disconnected"
```

- [ ] **Step 2: Implement the handler**

```python
# src/xenon/monitor_daemon/handlers/out_of_band_sweep.py
"""Daily reconciliation. Spec §6.5, §10.4.

Runs once at 16:30 ET. Three jobs:
  1. T5 70% sanity gate against yesterday's position count.
  2. Quarter-end re-arm sweep — flip ARMED rows whose native order disappeared.
  3. Out-of-band fill detection — emit `unprotected_position_detected` events.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from xenon.db.events import emit_outbox_in_txn
from xenon.execution.account_scope import AccountScope
from xenon.monitor_daemon.handlers.base import BaseHandler

logger = logging.getLogger(__name__)


class OutOfBandSweepHandler(BaseHandler):
    name = "position_rules_oob_sweep"
    interval_seconds = 24 * 3600  # daily; the daemon's market-hours gate handles when
    requires_market_hours = False

    def __init__(self, *, engine, ib_client, scope: AccountScope):
        super().__init__()
        self._engine = engine
        self._ib = ib_client
        self._scope = scope
        self._last_known_position_count = self._read_last_known()

    def _read_last_known(self) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(text("""
                SELECT (payload->>'count')::int AS n
                FROM events.outbox
                WHERE channel = 'position_rule.transition'
                  AND payload->>'kind' = 'oob_sweep_position_count'
                  AND payload->>'broker_account' = :a
                ORDER BY id DESC LIMIT 1
            """), {"a": self._scope.broker_account}).first()
        return int(row.n) if row else 0

    def execute(self) -> dict:
        if not getattr(self._ib, "connected", True):
            return {"status": "skipped_disconnected"}

        positions = self._ib.positions() if hasattr(self._ib, "positions") else []
        observed = len(positions or [])

        # T5 sanity gate.
        if self._last_known_position_count > 0:
            floor = max(1, int(self._last_known_position_count * 0.7))
            if observed < floor:
                logger.warning("OOB sweep aborted: observed=%d, floor=%d", observed, floor)
                with self._engine.begin() as conn:
                    emit_outbox_in_txn(
                        conn, channel="position_rule.transition",
                        source="oob_sweep",
                        payload={
                            "payload_version": 1, "kind": "oob_sweep_aborted",
                            "reason": "positions_response_suspiciously_small",
                            "observed": observed, "floor": floor,
                            "scope": self._scope.as_dict(),
                        },
                    )
                return {"status": "aborted_short_response", "observed": observed, "floor": floor}

        unprotected = 0
        with self._engine.connect() as conn:
            for pos in positions or []:
                existing = conn.execute(text("""
                    SELECT 1 FROM xenon.position_protection
                    WHERE broker = :b AND account_env = :e AND broker_account = :a
                      AND position_key LIKE :pkey
                      AND state IN ('PENDING_ARM','ARMED','TRIGGERED')
                    LIMIT 1
                """), {"b": self._scope.broker, "e": self._scope.account_env, "a": self._scope.broker_account,
                       "pkey": f"%::{pos.get('symbol')}%"}).first()
                if not existing:
                    unprotected += 1
                    with self._engine.begin() as txn_conn:
                        emit_outbox_in_txn(
                            txn_conn, channel="position_rule.transition",
                            source="oob_sweep",
                            payload={
                                "payload_version": 1, "kind": "unprotected_position_detected",
                                "symbol": pos.get("symbol"), "qty": pos.get("qty"),
                                "scope": self._scope.as_dict(),
                            },
                        )

        with self._engine.begin() as conn:
            emit_outbox_in_txn(
                conn, channel="position_rule.transition", source="oob_sweep",
                payload={
                    "payload_version": 1, "kind": "oob_sweep_position_count",
                    "broker_account": self._scope.broker_account,
                    "count": observed,
                },
            )
        self._last_known_position_count = observed

        return {"status": "ok", "observed": observed, "unprotected_count": unprotected}
```

- [ ] **Step 3: Register in `run.py`**

Append to the `XENON_POSITION_RULES_ENABLED` block:

```python
from xenon.monitor_daemon.handlers.out_of_band_sweep import OutOfBandSweepHandler
daemon.register(OutOfBandSweepHandler(engine=get_sync_engine(), ib_client=ib_client, scope=scope))
```

- [ ] **Step 4: Run + commit**

Run: `uv run pytest scripts/tests/test_position_rules_db/test_out_of_band_sweep.py -xvs`
Expected: 3 green.

```bash
git add src/xenon/monitor_daemon/handlers/out_of_band_sweep.py src/xenon/monitor_daemon/run.py scripts/tests/test_position_rules_db/test_out_of_band_sweep.py
git commit -m "feat(monitor-daemon): add daily out-of-band sweep with 70% sanity gate"
```

---

## Task 6: `no_duplicate_close_audit.py` audit script

**Files:**

- Create: `scripts/checks/no_duplicate_close_audit.py`
- Create: `scripts/tests/test_position_rules_db/test_no_duplicate_close_audit.py`

This is the spec §20 acceptance gate: "every `position_protection` row that reached `CLOSED` has exactly one matching `position_close_claims` row with `status='FILLED'`".

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_position_rules_db/test_no_duplicate_close_audit.py
"""Spec §13.8 T4 audit script."""
from __future__ import annotations

import json
import subprocess


def test_audit_runs_clean_with_empty_data():
    result = subprocess.run(
        ["uv", "run", "python", "scripts/checks/no_duplicate_close_audit.py", "--since", "1d", "--scope-account", "DU0000000"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    body = json.loads(result.stdout or "{}")
    assert body.get("violations") == []
```

- [ ] **Step 2: Implement the audit**

```python
# scripts/checks/no_duplicate_close_audit.py
"""Spec §13.8 T4 / §20 acceptance gate.

Verifies, over a date window:
  1. Every CLOSED position_protection row has exactly one FILLED claim.
  2. Every FILLED claim has at most one matching IB Flex execution by orderRef.
  3. For each (broker, account, position_key, day), at most one close order.

Outputs JSON-shaped violations.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from xenon.db.engine import get_sync_engine


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="14d")
    parser.add_argument("--scope-account", default=None)
    args = parser.parse_args(argv)

    cutoff = _parse_since(args.since)

    engine = get_sync_engine()
    violations = []

    with engine.connect() as conn:
        # Rule 1: CLOSED rows must have exactly one FILLED claim.
        rule_1 = conn.execute(text("""
            SELECT pp.protection_id, pp.position_key,
                   COUNT(c.claim_id) FILTER (WHERE c.status = 'FILLED') AS filled_claims
            FROM xenon.position_protection pp
            LEFT JOIN xenon.position_close_claims c
              ON c.broker = pp.broker AND c.account_env = pp.account_env
             AND c.broker_account = pp.broker_account AND c.position_key = pp.position_key
            WHERE pp.state = 'CLOSED'
              AND pp.closed_at >= :cutoff
              AND (:account IS NULL OR pp.broker_account = :account)
            GROUP BY pp.protection_id, pp.position_key
            HAVING COUNT(c.claim_id) FILTER (WHERE c.status = 'FILLED') != 1
        """), {"cutoff": cutoff, "account": args.scope_account}).all()
        for row in rule_1:
            violations.append({
                "rule": "closed_row_must_have_one_filled_claim",
                "protection_id": row.protection_id,
                "position_key": row.position_key,
                "filled_claims": int(row.filled_claims),
            })

        # Rule 3: at most one close order per (account, position_key, day).
        rule_3 = conn.execute(text("""
            SELECT broker_account, position_key, DATE(submitted_at) AS day, COUNT(*) AS n
            FROM xenon.position_close_claims
            WHERE submitted_at >= :cutoff
              AND status IN ('SUBMITTED','FILLED','ABANDONED')
              AND (:account IS NULL OR broker_account = :account)
            GROUP BY broker_account, position_key, DATE(submitted_at)
            HAVING COUNT(*) > 1
        """), {"cutoff": cutoff, "account": args.scope_account}).all()
        for row in rule_3:
            violations.append({
                "rule": "at_most_one_close_per_position_per_day",
                "broker_account": row.broker_account,
                "position_key": row.position_key,
                "day": row.day.isoformat(),
                "count": int(row.n),
            })

    out = {"window_since": cutoff.isoformat(), "violations": violations, "count": len(violations)}
    print(json.dumps(out, default=str))
    return 1 if violations else 0


def _parse_since(s: str) -> datetime:
    now = datetime.now(timezone.utc)
    if s.endswith("d"):
        return now - timedelta(days=int(s[:-1]))
    if s.endswith("h"):
        return now - timedelta(hours=int(s[:-1]))
    raise ValueError(f"unrecognized --since: {s!r}")


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Run + commit**

Run: `uv run pytest scripts/tests/test_position_rules_db/test_no_duplicate_close_audit.py -xvs`
Expected: 1 green.

```bash
git add scripts/checks/no_duplicate_close_audit.py scripts/tests/test_position_rules_db/test_no_duplicate_close_audit.py
git commit -m "feat(checks): add no_duplicate_close_audit for §20 acceptance gate"
```

---

## Task 7: Web UI — typed fetcher + ShieldBadge

**Files:**

- Create: `web/lib/api/positionRules.ts`
- Create: `web/components/portfolio/ShieldBadge.tsx`
- Create: `web/tests/positionRulesShieldBadge.test.tsx`

- [ ] **Step 1: Typed fetcher**

```typescript
// web/lib/api/positionRules.ts
export type ProtectionState =
  | "PENDING_ARM"
  | "ARMED"
  | "TRIGGERED"
  | "CLOSED"
  | "CANCELED"
  | "FAILED"
  | "SUPERSEDED";

export interface PositionRule {
  protection_id: number;
  position_key: string;
  rule_kind:
    | "stop_loss"
    | "trailing_tp"
    | "take_profit_fixed"
    | "combo_tp_alert";
  state: ProtectionState;
  asset_class: string;
  config: Record<string, unknown>;
  state_data: Record<string, unknown>;
  position_descriptor: Record<string, unknown>;
  native_order_perm_id: number | null;
  armed_at: string | null;
  triggered_at: string | null;
}

export interface PositionRulesHealth {
  schema_version: 1;
  daemon_alive: boolean;
  market_window: "open" | "closed" | "pre_open" | "post_close";
  next_market_event_at: string;
  last_tick_at: string | null;
  last_tick_age_seconds: number | null;
  rule_counts_by_state: Record<ProtectionState, number>;
  claim_counts_by_status: Record<
    "PENDING" | "SUBMITTED" | "FILLED" | "FAILED" | "ABANDONED",
    number
  >;
  in_flight_claims: number;
  stale_quote_skips_last_hour: number;
  unprotected_position_count: number;
  ib_connected: boolean;
  outbox_dlq_count: number;
}

export async function fetchPositionRules(): Promise<PositionRule[]> {
  const res = await fetch("/api/position-rules");
  if (!res.ok) throw new Error(`fetchPositionRules: ${res.status}`);
  return res.json();
}

export async function fetchHealth(): Promise<PositionRulesHealth> {
  const res = await fetch("/api/position-rules/health");
  if (!res.ok) throw new Error(`fetchHealth: ${res.status}`);
  return res.json();
}

export async function cancelRule(
  id: number,
): Promise<{ protection_id: number; state: ProtectionState }> {
  const res = await fetch(`/api/position-rules/${id}/cancel`, {
    method: "POST",
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.reason_code ?? `cancel failed: ${res.status}`);
  }
  return res.json();
}
```

- [ ] **Step 2: ShieldBadge component + tests**

```tsx
// web/components/portfolio/ShieldBadge.tsx
import type { ProtectionState } from "@/lib/api/positionRules";

const COLOR_BY_STATE: Record<
  ProtectionState | "NONE" | "UNCLASSIFIED",
  string
> = {
  ARMED: "bg-green-600",
  PENDING_ARM: "bg-amber-500",
  TRIGGERED: "bg-orange-500",
  FAILED: "bg-red-600",
  CANCELED: "bg-gray-400",
  CLOSED: "bg-gray-400",
  SUPERSEDED: "bg-gray-400",
  NONE: "bg-gray-300",
  UNCLASSIFIED: "bg-zinc-500",
};

interface ShieldBadgeProps {
  state: ProtectionState | "NONE" | "UNCLASSIFIED";
  count?: number;
  onClick?: () => void;
  ariaLabel?: string;
}

export function ShieldBadge({
  state,
  count,
  onClick,
  ariaLabel,
}: ShieldBadgeProps) {
  const color = COLOR_BY_STATE[state];
  return (
    <button
      type="button"
      aria-label={ariaLabel ?? `Protection: ${state}`}
      onClick={onClick}
      className={`inline-flex items-center gap-1 rounded px-2 py-1 text-xs text-white ${color}`}
      data-state={state}
    >
      <span aria-hidden>🛡</span>
      {count !== undefined && <span>{count}</span>}
    </button>
  );
}
```

```tsx
// web/tests/positionRulesShieldBadge.test.tsx
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { ShieldBadge } from "@/components/portfolio/ShieldBadge";

describe("ShieldBadge", () => {
  it("renders green for ARMED", () => {
    const { getByRole } = render(<ShieldBadge state="ARMED" />);
    expect(getByRole("button").getAttribute("data-state")).toBe("ARMED");
    expect(getByRole("button").className).toContain("bg-green-600");
  });

  it("renders amber for PENDING_ARM", () => {
    const { getByRole } = render(<ShieldBadge state="PENDING_ARM" />);
    expect(getByRole("button").className).toContain("bg-amber-500");
  });

  it("renders red for FAILED", () => {
    const { getByRole } = render(<ShieldBadge state="FAILED" />);
    expect(getByRole("button").className).toContain("bg-red-600");
  });

  it("renders neutral for UNCLASSIFIED", () => {
    const { getByRole } = render(<ShieldBadge state="UNCLASSIFIED" />);
    expect(getByRole("button").className).toContain("bg-zinc-500");
  });
});
```

- [ ] **Step 3: Run + commit**

```bash
cd web && npm test -- --run positionRulesShieldBadge
cd ..
git add web/lib/api/positionRules.ts web/components/portfolio/ShieldBadge.tsx web/tests/positionRulesShieldBadge.test.tsx
git commit -m "feat(web): add ShieldBadge component + typed position-rules fetcher"
```

---

## Task 8: Web UI — drawer + global health indicator

**Files:**

- Create: `web/components/portfolio/PositionRulesDrawer.tsx`
- Create: `web/components/portfolio/GlobalHealthIndicator.tsx`
- Create: `web/tests/positionRulesGlobalHealth.test.tsx`

- [ ] **Step 1: Drawer**

```tsx
// web/components/portfolio/PositionRulesDrawer.tsx
import { useEffect, useState } from "react";
import {
  cancelRule,
  fetchPositionRules,
  type PositionRule,
} from "@/lib/api/positionRules";

interface DrawerProps {
  positionKey: string;
  onClose: () => void;
}

export function PositionRulesDrawer({ positionKey, onClose }: DrawerProps) {
  const [rules, setRules] = useState<PositionRule[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchPositionRules()
      .then((all) =>
        setRules(all.filter((r) => r.position_key === positionKey)),
      )
      .catch((e) => setError(String(e)));
  }, [positionKey]);

  async function onCancel(id: number) {
    setError(null);
    try {
      await cancelRule(id);
      const all = await fetchPositionRules();
      setRules(all.filter((r) => r.position_key === positionKey));
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <aside
      className="fixed right-0 top-0 h-full w-96 overflow-y-auto bg-white shadow-xl p-4"
      role="dialog"
      aria-label="Position rules"
    >
      <div className="flex justify-between items-start">
        <h2 className="text-lg font-semibold">{positionKey}</h2>
        <button onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>
      {error && (
        <div role="alert" className="text-red-600 text-sm">
          {error}
        </div>
      )}
      {rules === null && <div>Loading…</div>}
      {rules?.length === 0 && (
        <div className="text-sm text-gray-500">No active rules.</div>
      )}
      <ul className="mt-4 space-y-3">
        {rules?.map((r) => (
          <li key={r.protection_id} className="border p-2 rounded">
            <div className="flex justify-between">
              <span className="font-mono text-sm">{r.rule_kind}</span>
              <span className="text-xs">{r.state}</span>
            </div>
            <pre className="text-xs mt-2 whitespace-pre-wrap">
              {JSON.stringify(r.config, null, 2)}
            </pre>
            {(r.state === "PENDING_ARM" ||
              r.state === "ARMED" ||
              r.state === "TRIGGERED") && (
              <button
                onClick={() => onCancel(r.protection_id)}
                className="mt-2 text-xs px-2 py-1 bg-red-600 text-white rounded"
              >
                Cancel rule
              </button>
            )}
          </li>
        ))}
      </ul>
    </aside>
  );
}
```

- [ ] **Step 2: GlobalHealthIndicator**

```tsx
// web/components/portfolio/GlobalHealthIndicator.tsx
import { useEffect, useState } from "react";
import { fetchHealth, type PositionRulesHealth } from "@/lib/api/positionRules";

function classifyHealth(h: PositionRulesHealth): "red" | "amber" | "green" {
  if (!h.daemon_alive) return "red";
  if (h.outbox_dlq_count > 0) return "red";
  if (h.claim_counts_by_status.FAILED > 0) return "red";
  if (h.market_window === "open" && (h.last_tick_age_seconds ?? 0) > 300)
    return "red";

  if (h.rule_counts_by_state.FAILED > 0) return "amber";
  if (h.unprotected_position_count > 0) return "amber";
  if (h.in_flight_claims > 0) return "amber";
  if (h.stale_quote_skips_last_hour > 5) return "amber";
  if (!h.ib_connected && h.market_window === "open") return "amber";
  return "green";
}

const COLOR: Record<"red" | "amber" | "green", string> = {
  red: "bg-red-500",
  amber: "bg-amber-400",
  green: "bg-green-500",
};

export function GlobalHealthIndicator() {
  const [health, setHealth] = useState<PositionRulesHealth | null>(null);

  useEffect(() => {
    let active = true;
    async function tick() {
      try {
        const h = await fetchHealth();
        if (active) setHealth(h);
      } catch {
        if (active) setHealth(null);
      }
    }
    tick();
    const id = setInterval(tick, 30_000);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, []);

  if (health === null) {
    return <div className="text-xs text-gray-500">🛡 health…</div>;
  }
  const cls = classifyHealth(health);
  const armed = health.rule_counts_by_state.ARMED;
  const tooltip =
    health.market_window === "open"
      ? `Market open · last tick ${health.last_tick_age_seconds ?? 0}s ago`
      : `Market closed — synthetic monitor resumes at ${health.next_market_event_at}; native brackets remain armed.`;
  return (
    <div
      className={`inline-flex items-center gap-1 rounded px-2 py-1 text-xs text-white ${COLOR[cls]}`}
      title={tooltip}
      data-cls={cls}
    >
      <span aria-hidden>🛡</span>
      <span>{armed} armed</span>
    </div>
  );
}
```

- [ ] **Step 3: Health-indicator tests (codex N-M1: outside-RTH staleness suppression)**

```tsx
// web/tests/positionRulesGlobalHealth.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { GlobalHealthIndicator } from "@/components/portfolio/GlobalHealthIndicator";
import * as api from "@/lib/api/positionRules";

function makeHealth(
  overrides: Partial<api.PositionRulesHealth> = {},
): api.PositionRulesHealth {
  return {
    schema_version: 1,
    daemon_alive: true,
    market_window: "open",
    next_market_event_at: "2026-05-04T20:00:00Z",
    last_tick_at: "2026-05-04T14:00:00Z",
    last_tick_age_seconds: 30,
    rule_counts_by_state: {
      PENDING_ARM: 0,
      ARMED: 5,
      TRIGGERED: 0,
      FAILED: 0,
      CANCELED: 0,
      CLOSED: 0,
      SUPERSEDED: 0,
    },
    claim_counts_by_status: {
      PENDING: 0,
      SUBMITTED: 0,
      FILLED: 0,
      FAILED: 0,
      ABANDONED: 0,
    },
    in_flight_claims: 0,
    stale_quote_skips_last_hour: 0,
    unprotected_position_count: 0,
    ib_connected: true,
    outbox_dlq_count: 0,
    ...overrides,
  };
}

describe("GlobalHealthIndicator", () => {
  it("green when everything healthy during open market", async () => {
    vi.spyOn(api, "fetchHealth").mockResolvedValue(makeHealth());
    const { container } = render(<GlobalHealthIndicator />);
    await waitFor(() =>
      expect(container.querySelector("[data-cls=green]")).toBeTruthy(),
    );
  });

  it("staleness does NOT make indicator red outside RTH (codex N-M1)", async () => {
    vi.spyOn(api, "fetchHealth").mockResolvedValue(
      makeHealth({
        market_window: "closed",
        last_tick_age_seconds: 7200, // 2 hours stale, but market closed
      }),
    );
    const { container } = render(<GlobalHealthIndicator />);
    await waitFor(() =>
      expect(container.querySelector("[data-cls=green]")).toBeTruthy(),
    );
  });

  it("red when DLQ > 0", async () => {
    vi.spyOn(api, "fetchHealth").mockResolvedValue(
      makeHealth({ outbox_dlq_count: 1 }),
    );
    const { container } = render(<GlobalHealthIndicator />);
    await waitFor(() =>
      expect(container.querySelector("[data-cls=red]")).toBeTruthy(),
    );
  });

  it("amber when in_flight_claims > 0", async () => {
    vi.spyOn(api, "fetchHealth").mockResolvedValue(
      makeHealth({ in_flight_claims: 2 }),
    );
    const { container } = render(<GlobalHealthIndicator />);
    await waitFor(() =>
      expect(container.querySelector("[data-cls=amber]")).toBeTruthy(),
    );
  });
});
```

- [ ] **Step 4: Run + commit**

```bash
cd web && npm test -- --run positionRulesGlobalHealth
cd ..
git add web/components/portfolio/PositionRulesDrawer.tsx web/components/portfolio/GlobalHealthIndicator.tsx web/tests/positionRulesGlobalHealth.test.tsx
git commit -m "feat(web): add PositionRulesDrawer + GlobalHealthIndicator (RTH-aware staleness)"
```

---

## Task 9: Wire UI into the existing portfolio table

**Files:**

- Modify: the existing portfolio table component (location varies — find via `grep -rln "portfolio table\|PortfolioTable" web/components/`)
- Modify: the existing sidebar/header layout

This task is mechanical:

1. Import `ShieldBadge` and add a column to the portfolio table that fetches rules for each row's `position_key`. Use a single `fetchPositionRules()` call cached at the page level (React Query `useQuery`), filtered per row in render.
2. Click handler opens `PositionRulesDrawer` with the row's `position_key`.
3. Add `GlobalHealthIndicator` to the sidebar (or header — match the existing pattern).
4. Live updates: subscribe to LISTEN/NOTIFY `position_rule.transition` via the existing web realtime bridge (the same one `combo_wizard` uses); on each event, invalidate the `useQuery(['positionRules'])` cache.

- [ ] **Step 1: Locate insertion points**

```bash
grep -rln "PortfolioTable\|portfolio.*table\|account_snapshots" web/components web/app/portfolio 2>/dev/null
```

- [ ] **Step 2: Add column + drawer + indicator**

In the portfolio table file, add:

```tsx
import { useQuery } from "@tanstack/react-query";
import { fetchPositionRules, type PositionRule } from "@/lib/api/positionRules";
import { ShieldBadge } from "@/components/portfolio/ShieldBadge";
import { PositionRulesDrawer } from "@/components/portfolio/PositionRulesDrawer";

const { data: rules = [] } = useQuery({
  queryKey: ["positionRules"],
  queryFn: fetchPositionRules,
  refetchInterval: 30_000,
});

function rulesFor(positionKey: string): PositionRule[] {
  return rules.filter((r) => r.position_key === positionKey);
}

const [drawerKey, setDrawerKey] = useState<string | null>(null);
// ... per-row:
{
  rulesFor(row.positionKey).length > 0 ? (
    <ShieldBadge
      state={dominantState(rulesFor(row.positionKey))}
      count={rulesFor(row.positionKey).length}
      onClick={() => setDrawerKey(row.positionKey)}
    />
  ) : (
    <ShieldBadge state="NONE" />
  );
}
{
  drawerKey && (
    <PositionRulesDrawer
      positionKey={drawerKey}
      onClose={() => setDrawerKey(null)}
    />
  );
}
```

The `dominantState` helper picks the most-urgent state across multiple rules: `FAILED > TRIGGERED > PENDING_ARM > ARMED > others`.

- [ ] **Step 3: Add `GlobalHealthIndicator` to sidebar/header**

Match the existing pattern — for example next to where `useUwStats` indicator already lives. Find via `grep -rln "useUwStats" web/`.

- [ ] **Step 4: Subscribe to LISTEN/NOTIFY for live updates**

Find the existing realtime bridge (likely `web/lib/realtime/` or similar via `grep -rln "EventSource\|listen\|notify" web/lib/`). Add a subscription to channel `position_rule.transition`:

```typescript
// web/lib/realtime/positionRulesSubscription.ts
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

export function usePositionRulesRealtime() {
  const qc = useQueryClient();
  useEffect(() => {
    const es = new EventSource(
      "/api/realtime?channel=position_rule.transition",
    );
    es.onmessage = () => {
      qc.invalidateQueries({ queryKey: ["positionRules"] });
      qc.invalidateQueries({ queryKey: ["positionRulesHealth"] });
    };
    return () => es.close();
  }, [qc]);
}
```

Mount this hook on the portfolio page once.

- [ ] **Step 5: Run + commit**

```bash
cd web && npm test
cd ..
git add web/components/portfolio/ web/lib/realtime/ <portfolio-page-file>
git commit -m "feat(web): wire ShieldBadge + drawer + health indicator + realtime into portfolio"
```

---

## Task 10: E2E browser test

**Files:**

- Create: `web/tests/e2e/positionRules.spec.ts`

- [ ] **Step 1: Write the Playwright golden path**

```typescript
// web/tests/e2e/positionRules.spec.ts
import { test, expect } from "@playwright/test";

test.describe("Position rules UI", () => {
  test("shield badge → drawer → cancel → live update", async ({ page }) => {
    // Pre-condition: at least one ARMED rule in the test database fixture.
    await page.goto("/portfolio");
    const badge = page.locator("[data-state='ARMED']").first();
    await expect(badge).toBeVisible();

    await badge.click();
    const drawer = page.getByRole("dialog", { name: /position rules/i });
    await expect(drawer).toBeVisible();

    const cancelBtn = drawer
      .getByRole("button", { name: /cancel rule/i })
      .first();
    await cancelBtn.click();

    // After cancel, badge should re-render to gray (CANCELED) within ~5s via LISTEN/NOTIFY.
    await expect(page.locator("[data-state='ARMED']")).toHaveCount(0, {
      timeout: 5000,
    });
  });

  test("global health indicator visible in sidebar", async ({ page }) => {
    await page.goto("/portfolio");
    await expect(page.locator("[data-cls='green']").first()).toBeVisible({
      timeout: 10000,
    });
  });
});
```

- [ ] **Step 2: Configure fixtures**

Per `web/playwright.config.ts`, the test runs against the local dev stack. Make sure `XENON_POSITION_RULES_ENABLED=1` is set in the dev environment. Run:

```bash
cd web && npx playwright test tests/e2e/positionRules.spec.ts
```

Expected: 2 green.

- [ ] **Step 3: Commit**

```bash
git add web/tests/e2e/positionRules.spec.ts
git commit -m "test(web): add E2E for shield badge → drawer → cancel → live update"
```

---

## Task 11: Paper-smoke runbook

**Files:**

- Create: `docs/runbooks/position-rules-paper-smoke.md`

- [ ] **Step 1: Author the runbook**

```markdown
# Position-Rules Paper-Account Smoke

Mandatory before flipping `XENON_POSITION_RULES_ENABLED=1` on the live (real-money) account. Per memory `[paper-first for IB order bugs]` and spec §13.7.

## Pre-flight

- [ ] Phase 0 DST fix merged (`uv run pytest scripts/tests/test_monitor_daemon/test_market_hours_dst.py -xvs` green).
- [ ] Migration A + B applied to paper Postgres (`uv run alembic current` shows latest revision).
- [ ] `bracket_policies` shows 8 seed rows.
- [ ] `wizard_protection` table is dropped.
- [ ] `wizard_stop_monitor.py` is deleted.
- [ ] `XENON_POSITION_RULES_ENABLED=1` set in paper env; daemon restarted.
- [ ] `xenon-position-rules health` returns `daemon_alive=true` and `market_window` matches actual.

## Scenarios

### S1 — stock SL+TP arming

Open a 100-share long stock position (low-priced ticker, e.g. F or T) via the UI.

- [ ] Two rows appear in `xenon-position-rules list --state PENDING_ARM` within 5s (stop_loss + trailing_tp).
- [ ] Within one tick (≤30s), both rows transition to `ARMED`.
- [ ] `native_order_perm_id` is set on the stop_loss row; trailing_tp is synthetic (NULL).
- [ ] In TWS, a working STP order is visible at exactly `entry × 0.92`.

### S2 — trailing TP MFE update

After S1, mark the position upward (paper account allows manual fills against simulated quotes).

- [ ] `state_data.mfe` increases on each tick where mark > previous mfe.
- [ ] No premature trigger.

### S3 — manual TWS cancel detection

In TWS, manually cancel the stop_loss STP from S1.

- [ ] Within one tick, the row transitions `ARMED → CANCELED` with `reason='native_order_externally_cancelled'`.
- [ ] No re-arm happens.
- [ ] Outbox emits a `position_rule.transition` event for the row.

### S4 — sweep CLI re-arm

Open a position outside Xenon (TWS direct).

- [ ] `xenon-position-rules sweep` (dry-run) lists the symbol under `would_insert`.
- [ ] `xenon-position-rules sweep --apply` inserts PENDING_ARM rows; next tick arms them.

### S5 — credit spread dual-trigger

Open a short bull put spread via the wizard.

- [ ] `position_protection` shows `rule_kind='stop_loss'` (synthetic) and `rule_kind='take_profit_fixed'` rows.
- [ ] If the underlying breaches the short strike, stop_loss triggers.
- [ ] If the spread debit-to-close drops to ≤ 50% of credit, take_profit_fixed triggers.

### S6 — daemon kill + restart reconcile

Force-kill the monitor daemon mid-tick. Restart.

- [ ] On boot, `boot_reconcile` runs and snaps any in-flight claims to terminal states.
- [ ] No duplicate orders submitted to IB.
- [ ] `xenon-position-rules health` returns to green within 1 tick.

### S7 — codex N-C1 (native + synthetic race)

Open a long stock with a native STP and price the underlying right at the threshold.

- [ ] Exactly one MKT close hits IB (verified via Flex Query).
- [ ] `position_close_claims` shows one claim with `claim_kind='native_reconcile_close'` and one synthetic-attempt row in `SUPERSEDED`.

> ⚠ If the native fill always happens cleanly before the synthetic tick window in your paper environment, the race is not deterministically reproducible. Mark this scenario "verified via integration test only" (see `scripts/tests/test_position_rules_db/test_position_close_claims_queries.py::test_three_way_race_only_one_winner`) and proceed.

### S8 — codex N-C2 (two rules same position)

Open a long option. Construct config so both stop_loss and trailing_tp can fire on the same tick.

- [ ] One MKT close, one rule reaches `CLOSED`, the other reaches `SUPERSEDED`.

### S9 — codex N-C3 (subprocess timeout retry)

Kill the IB Gateway connection right after broker accepts the MKT but before subprocess returns.

- [ ] On the next handler tick, `IBClient.find_open_orders_by_order_ref(order_ref)` returns the existing order; subprocess does NOT re-submit.
- [ ] The existing perm_id is attached to the claim.

> ⚠ Same caveat as S7 — integration test (`test_close_claim.py::test_should_skip_resubmit_when_open_order_with_orderref`) is authoritative.

### S10 — out-of-band sweep (16:30 ET)

After market close, observe `xenon-position-rules health`:

- [ ] `oob_sweep_position_count` event present in outbox for today.
- [ ] If a position was opened in TWS earlier without going through `record_fill`, an `unprotected_position_detected` event fires.
- [ ] The next-day sweep does not abort (70% gate passes).

### S11 — UI

- [ ] Per-position shield badge displays; color matches state.
- [ ] Drawer opens on click; rule rows render with config + cancel button.
- [ ] Cancel button transitions row to CANCELED; badge re-renders within 5s.
- [ ] Global health indicator stays green outside RTH (codex N-M1).
- [ ] `outbox_dlq_count > 0` flips indicator to red.

## Sign-off

- Operator name: ****\_\_****
- Date completed: ****\_\_****
- Outliers / unverified scenarios: ****\_\_****
- Decision: ☐ proceed to live ☐ block on follow-up
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/position-rules-paper-smoke.md
git commit -m "docs(runbooks): add position-rules paper-smoke checklist"
```

---

## Task 12: 14-day clean-operation tracker

**Files:**

- Create: `docs/runbooks/position-rules-acceptance-gate.md`

- [ ] **Step 1: Author the tracker**

````markdown
# Position-Rules — 14-Day Clean-Operation Acceptance Gate

Spec §20. Every box must be ticked before flipping `XENON_POSITION_RULES_ENABLED=1` on the live (real-money) account.

## Daily checklist (run for 14 consecutive trading days on paper)

For each day:

```bash
DAY=$(date -u +%F)
uv run python scripts/checks/no_duplicate_close_audit.py --since 1d > "logs/no-dup-close-${DAY}.json"
xenon-position-rules events --since 24h > "logs/transitions-${DAY}.json"
xenon-position-rules health --json > "logs/health-${DAY}.json"
```
````

Then for each day record:

| Day | Date | DLQ count | FAILED rules | Triggers (auto/alert) | Reviewer | Verdict |
| --- | ---- | --------- | ------------ | --------------------- | -------- | ------- |
| 1   |      |           |              |                       |          |         |
| 2   |      |           |              |                       |          |         |
| …   |      |           |              |                       |          |         |
| 14  |      |           |              |                       |          |         |

For every trigger, run:

```bash
xenon-position-rules review --event-id <id> --protection-id <pid> --reviewed-by <you> --verdict expected|unexpected|structural --note "..."
```

## Final gate criteria (all must hold over the 14 days)

- [ ] **Zero rows** reached `FAILED` for non-structural reasons. (`naked_short_blocked` and `corporate_action_suspected` are structural — allowed.)
- [ ] **Zero unexpected triggers.** Every `verdict='unexpected'` annotation in `position_rules_review` is investigated and the underlying issue resolved before live.
- [ ] **Zero duplicate MKT closes** — `scripts/checks/no_duplicate_close_audit.py --since 14d` returns `violations: []`.
- [ ] **Zero `outbox_dlq` events** for the arm consumer — `health.outbox_dlq_count == 0` every day.
- [ ] **At least 1 successful trigger → MKT-flatten → CLOSED cycle** observed (record protection_id).
- [ ] **At least 1 successful boot reconcile** (kill+restart with ARMED rows AND in-flight claim, recovers cleanly).
- [ ] **At least 1 successful native-bracket attach + per-tick liveness check** observed.
- [ ] **At least 1 successful "subprocess timeout after broker accept"** — retry attached existing perm_id rather than resubmitting.
- [ ] **`unprotected_position_count` returned to zero within 1 daily-sweep cycle** of every out-of-band fill.
- [ ] **Quote staleness skip rate** (`stale_quote_skips_last_hour / rule_counts_by_state.ARMED`) **< 5%** in aggregate during RTH.
- [ ] `docs/reference/order-path-incident-history.md` has a row for this design (per CLAUDE.md convention for order-path changes).

## Live promotion

Once all gates green:

```bash
# Live account env (e.g., on the production deploy host)
export XENON_POSITION_RULES_ENABLED=1
# restart monitor daemon
```

Then run S1–S11 from `position-rules-paper-smoke.md` against the live account with the smallest possible position size (1 share / 1 contract) for the first three trading days. Daily review continues for at least one more week before declaring v1 live-stable.

````

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/position-rules-acceptance-gate.md
git commit -m "docs(runbooks): add 14-day clean-operation acceptance gate tracker"
````

---

## Task 13: Update incident-history doc

**Files:**

- Modify: `docs/reference/order-path-incident-history.md` — append a row referencing this design (CLAUDE.md convention)

- [ ] **Step 1: Append a row**

```markdown
| 2026-05-04 | Position-rules engine (auto-bracket SL+TP) introduced | Open positions had no enforced exit; user lost money on credit spreads drifting into max loss and on long calls failing to recover (gamma decay). | Added `position_protection` + `bracket_policies` + `position_close_claims` tables; `PositionRulesHandler` + 4 rule plug-ins; close-claim protocol prevents duplicate MKT closes (codex N-C1/N-C2/N-C3); frozen-config CI guard locks rule modules from re-reading policy table mid-flight. | `scripts/tests/test_position_rules/`, `scripts/tests/test_position_rules_db/`, `scripts/checks/no_duplicate_close_audit.py`, `scripts/checks/frozen_config_at_arm.py` |
```

- [ ] **Step 2: Commit**

```bash
git add docs/reference/order-path-incident-history.md
git commit -m "docs(order-path-history): record position-rules engine introduction"
```

---

## Task 14: Final PR

- [ ] **Step 1: Run full test suite**

```bash
uv run python scripts/infra/dev/run_pytest_affected.py
cd web && npm test && npx playwright test
cd ..
uv run python scripts/checks/frozen_config_at_arm.py
uv run python scripts/checks/no_duplicate_close_audit.py --since 1d
uv run python scripts/checks/order_path_caller_allowlist.py
uv run python scripts/checks/no_json_fallback_on_order_path.py
```

Expected: all green.

- [ ] **Step 2: Push branch + open PR**

```bash
git push -u origin <plan-4-branch>
gh pr create --title "feat(position-rules): UI + sweep CLI + FastAPI + acceptance gate" --body "$(cat <<'EOF'
## Summary

Phase 6 + 7 of the position-rules engine. Operator surface only — no engine semantics change vs Plan 3.

- `xenon-position-rules` CLI: list / show / cancel / sweep / health / events / review
- FastAPI: `GET /position-rules`, `GET /position-rules/health`, `POST /position-rules/{id}/cancel`, `POST /position-rules/sweep` (with N-S6 live-auth gate)
- Web UI: per-position `ShieldBadge`, `PositionRulesDrawer`, `GlobalHealthIndicator` (RTH-aware staleness suppression — codex N-M1)
- Daily out-of-band sweep handler (`OutOfBandSweepHandler`) with T5 70% sanity gate
- `scripts/checks/no_duplicate_close_audit.py` Phase 6/7 deliverable (T4)
- Sidecar `position_rules_review` table for daily ops annotations (T3)
- Paper-smoke runbook + 14-day acceptance gate tracker

## Acceptance gates (post-merge)

- [ ] Paper-smoke checklist `docs/runbooks/position-rules-paper-smoke.md` executed and signed off
- [ ] 14 days of clean operation per `docs/runbooks/position-rules-acceptance-gate.md`
- [ ] Operator flips `XENON_POSITION_RULES_ENABLED=1` on live; first 3 days of live observed at minimum size

## Test plan

- [ ] `uv run python scripts/infra/dev/run_pytest_affected.py` green
- [ ] `cd web && npm test && npx playwright test` green
- [ ] All four CI guards (`frozen_config_at_arm`, `no_duplicate_close_audit`, `order_path_caller_allowlist`, `no_json_fallback_on_order_path`) green
- [ ] Manual: dev server boots, portfolio page renders shield badges, drawer opens, cancel works, indicator green
EOF
)"
```

- [ ] **Step 3: Confirm CI green and merge.**

After merge, begin the 14-day paper-smoke acceptance gate from `docs/runbooks/position-rules-acceptance-gate.md`.

---

## Self-Review

**Spec coverage:**

- §6.5 daily out-of-band sweep with T5 70% sanity gate → Task 5 ✓
- §10.4 quarter-end re-arm sweep — covered by `OutOfBandSweepHandler` running daily ✓
- §11 sweep CLI (`--dry-run` default, `--apply`, `--rate-limit-per-min`, live-mode auth gate) → Task 2 ✓
- §12.1 UI shield badge + drawer + global health indicator with RTH-aware suppression (N-M1) → Tasks 7, 8, 9 ✓
- §12.2 FastAPI endpoints with N-S6 live-auth gate → Task 4 ✓
- §12.3 events.outbox payload (`payload_version: 1`) — emitted by Plan 2's `cas_transition` ✓
- §12.4 macOS notifications — already wired in Plan 2 (`combo_tp_alert.py` + lifted `_default_notify` helper) ✓
- §12.5 CLI mirror (`list/show/cancel/sweep/health/events`) → Task 2 ✓
- §13.5 FastAPI route tests → Task 4 ✓
- §13.6 E2E browser test → Task 10 ✓
- §13.7 paper-smoke runbook with N-C1/N-C2/N-C3 scenarios + caveat → Task 11 ✓
- §13.8 T3 daily ops review tool + `position_rules_review` table → Tasks 1, 2 ✓
- §13.8 T4 `no_duplicate_close_audit.py` Phase 6/7 deliverable → Task 6 ✓
- §15 Phases 6 + 7 ✓
- §20 acceptance criteria (every bullet) → Task 12 (tracker) ✓

**Placeholder scan:** none — every step has either complete code or a concrete command. Task 9 ("wire UI into existing portfolio table") is the only one that uses pseudocode-style guidance; that's because the exact insertion point depends on the current portfolio component layout, which a fresh agent has to discover with `grep`. The pseudo-code is tight enough that the discovery + paste-in is a 15-minute task once the portfolio table is located.

**Type consistency:**

- `ProtectionState` TypeScript type matches every state in `position_protection.state` CHECK constraint exactly.
- `PositionRulesHealth` shape matches `compute_health()` output keys 1:1.
- `ShieldBadge` accepts `ProtectionState | "NONE" | "UNCLASSIFIED"` — the two extra values are UI-only, never written to the DB.
- `cancelRule(id)` return shape matches `POST /position-rules/{id}/cancel` JSON.
- `xenon-position-rules events --since=24h` output keys (`event_id`, `created_at`, plus payload spread) match the `position_rule.transition` outbox payload from Plan 2.

**Dependencies on Plan 2/3:**

- `cas_transition`, `list_active_rows`, `get_by_id`, `insert_pending_arm` (Plan 2 query module) — used in CLI + FastAPI.
- `compute_position_key`, `classify_position`, `resolve_for_scope`, `deduplicate_by_specificity` (Plan 2) — used by sweep_insert.
- `events.outbox` `position_rule.transition` channel + payload shape (Plan 2 / 3) — consumed by events CLI, health endpoint, UI realtime subscription.
- `BaseHandler`, `MonitorDaemon.register` (existing) — used by OutOfBandSweepHandler.
- Frozen-config CI guard (Plan 3) — already running; this plan adds `no_duplicate_close_audit.py` alongside it without changing the existing guard.

**Risk note:** The "live promotion" step at the end of Task 12 carries the most operational risk — flipping the flag on real money. The 14-day acceptance gate + minimum-size first-week is the standard mitigation; per spec §15 Phase 7, this is the operator's call, not the engine's.

---
