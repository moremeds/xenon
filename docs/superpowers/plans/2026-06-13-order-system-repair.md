# Order System Repair & Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four verified order-system bugs (fractional fills recorded as qty=0, stock fills never classified as closing, SPX chain qualify failure, Flex blotter error opacity), kill/prevent zombie API servers, and close the TWS-cancel mirroring gap.

**Architecture:** All fixes follow the existing pipeline: Postgres is the source of truth (`order_submissions` / `order_fills`), subprocess CLIs talk to IB, FastAPI serializes, Next.js renders. No new layers — each task repairs one stage of the existing flow. The Futu order-integration program is explicitly **out of scope** (documented as backlog in Task 13).

**Tech Stack:** Python 3.13 via `uv`, SQLAlchemy Core + Alembic, FastAPI, ib_async, Next.js + Vitest, pytest (per-worker PG clones — see root CLAUDE.md § Pytest infrastructure).

**Branch:** all commits go on a feature branch — `git checkout -b fix/order-system-repair` before Task 2. Never push master directly; finish with a PR.

**Background read (required):** `docs/reference/order-path-incident-history.md` before touching any order-path file. The PreToolUse hook will print the order-path checklist on edits under `src/xenon/execution/`, `src/xenon/api/server.py`, `web/lib/order/` — that is advisory, expected, and not an error.

**Verified context from the 2026-06-13 review session:**

- Two zombie FastAPI processes (PIDs change — rediscover, don't trust 27796/77392) from the deleted `.worktrees/performance-holistic-upgrade/` worktree hold port 8321.
- `xenon.order_fills` contains 4 rows with `qty=0` (exec_ids `00020ac8.6a2aeca9.01.01`, `00020ac8.6a21bd96.01.01`, `0000f993.69f4d630.01.01`, `00020ac8.69f3982e.01.01`) — fractional-share executions truncated by `int()`.
- `uv run python -m xenon.trade_blotter.flex_query --json` exits 1 with IB `ErrorCode 1001` — the saved Flex query is CSV-format; the legacy servlet only serves XML (see memory `feedback_flex_legacy_endpoint_xml_only`). Operator action in Task 11.
- `xenon-ib-option-chain` hardcodes `Stock(symbol, "SMART", "USD")` at `src/xenon/execution/ib_option_chain.py:33` — SPX/NDX/RUT can never qualify.

---

## Task 1: Kill zombie API servers and restart clean (ops — no commit)

**Files:** none (operational).

- [ ] **Step 1: Identify every listener on 8321 and verify it is a zombie**

```bash
lsof -nP -iTCP:8321 -sTCP:LISTEN
# For each PID returned:
lsof -p <PID> -a -d cwd
```

Expected: cwd points at `/Users/chenxi/projects/xenon/.worktrees/performance-holistic-upgrade/web` (a deleted directory) or another non-main checkout. **If cwd is the main checkout and the process is the user's active dev session, stop and ask the user before killing.**

- [ ] **Step 2: Kill the zombies**

```bash
kill $(lsof -t -iTCP:8321 -sTCP:LISTEN)
sleep 2
lsof -nP -iTCP:8321 -sTCP:LISTEN   # expected: no output
```

- [ ] **Step 3: Also check the Next.js port and the realtime server for strays from the same worktree**

```bash
lsof -nP -iTCP:3000 -sTCP:LISTEN
# kill any whose cwd is a .worktrees/ path that no longer exists
```

- [ ] **Step 4: Restart the dev stack and re-verify the panels**

The user normally runs this themselves; if running autonomously, start it in the background:

```bash
scripts/infra/dev.sh paper
```

Then `curl -s http://localhost:8321/health` — expect `"status":"ok"`. Re-check the Orders page and Historical Trades panel in the browser; note which of the original screenshot symptoms persist (the qty-0 executed rows will persist — they are DB data, fixed in Tasks 3–7; the Flex panel should now show a 502/error rather than "not configured" — fixed in Task 11).

---

## Task 2: dev.sh refuses to start when the API port is already bound

**Files:**

- Modify: `scripts/infra/dev.sh` (insert after the core_dev guard block that ends near line 135 with `exit 2`, **before** the alembic upgrade section)
- Test: `scripts/tests/test_dev_sh_port_guard.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""dev.sh refuses to start when the FastAPI port already has a listener.

Zombie uvicorn pairs (e.g. surviving a deleted worktree) otherwise coexist
with the fresh stack and serve stale code/env. Discovered 2026-06-13: two
FastAPI processes from the deleted performance-holistic-upgrade worktree
held 8321 and served a 9-day-old branch.

Pure subprocess test — no PG, no FastAPI, no IB Gateway required. The
port guard fires after the core_dev DB guard and before alembic, so the
subprocess exits in milliseconds.
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_SH = REPO_ROOT / "scripts" / "infra" / "dev.sh"


def _run_dev_sh(
    *,
    env_file: Path,
    extra_env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    env_file.write_text("DATABASE_URL=postgresql://xenon_dev@localhost:5432/core_test\n")
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "XENON_ENV_FILE": str(env_file),
        "XENON_PAPER_ACCOUNT": "DU9999999",
        "XENON_LIVE_ACCOUNT": "U9999999",
    }
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(DEV_SH), "paper"],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_dev_sh_refuses_busy_api_port(tmp_path: Path) -> None:
    """A bound API port exits 3 with the FATAL listener marker."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        proc = _run_dev_sh(
            env_file=tmp_path / ".env",
            extra_env={"XENON_API_PORT": str(port)},
        )
    finally:
        sock.close()
    assert proc.returncode == 3, proc.stderr
    assert "already has a listener" in proc.stderr
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest scripts/tests/test_dev_sh_port_guard.py -xvs
```

Expected: FAIL — dev.sh today has no port guard, so the script proceeds to alembic (likely times out or exits non-3).

- [ ] **Step 3: Add the guard to dev.sh**

Insert directly after the existing core_dev FATAL guard block (the `if [[ "$_db_name" == "core_dev" ]]; then ... exit 2; fi` block around lines 126–140) and **before** the alembic section:

```bash
# 2.5 Refuse to start when the FastAPI port is already bound. Zombie
# uvicorn pairs (e.g. surviving a deleted worktree) otherwise coexist
# with the fresh stack and serve stale code/env from a checkout that no
# longer exists. XENON_API_PORT is a test seam; production stays 8321.
API_PORT="${XENON_API_PORT:-8321}"
if lsof -nP -iTCP:"$API_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  log_err "FATAL: port $API_PORT already has a listener — a previous xenon-api is still running."
  lsof -nP -iTCP:"$API_PORT" -sTCP:LISTEN >&2 || true
  log_err "Kill it first:  kill \$(lsof -t -iTCP:$API_PORT -sTCP:LISTEN)"
  exit 3
fi
```

**Design choice:** this guard _detects and refuses_, it does not auto-kill. Auto-killing whatever holds the port is rejected as too dangerous — it could be the operator's own active session or an unrelated service. Cleanup is the deliberate manual step in Task 1 (verify cwd is a dead worktree, then kill). Detection-and-refuse converts a silent stale-server bug into a loud, actionable startup failure, which is the goal.

- [ ] **Step 4: Run the new test AND the existing dev.sh guard tests**

```bash
uv run pytest scripts/tests/test_dev_sh_port_guard.py scripts/tests/test_dev_sh_db_guard.py -v
```

Expected: all PASS. (The DB-guard tests must still exit 2 _before_ reaching the port guard — if they now exit 3 because a real dev server is running, the guard is inserted too early; it must come after the core_dev block.)

- [ ] **Step 5: Commit**

```bash
git add scripts/infra/dev.sh scripts/tests/test_dev_sh_port_guard.py
git commit -m "fix(dev.sh): refuse to start when the API port already has a listener"
```

---

## Task 3: Failing test — fractional-share fills must keep their quantity

**Files:**

- Test: `scripts/tests/test_fractional_fill_qty.py` (create)

- [ ] **Step 1: Write the failing test**

Model the execution dict on `scripts/tests/test_record_external_fills_resolves_submission.py::_execution` (same field names the IB reconcile path consumes):

```python
"""Fractional-share executions must not be truncated to qty=0.

Discovered 2026-06-13: recurring fractional buys (QQQ/SPY placed outside
Xenon) were recorded with qty=0 because record_external_fills coerced
IB's float `shares` through int(), and order_fills.qty was an Integer
column. Result: the executed-orders panel showed quantity 0 and net
price "—" for every fractional stock fill.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_fills
from xenon.execution.account_scope import AccountScope
from xenon.execution.ib_reconcile import record_external_fills

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU0000000")


def _fractional_execution() -> dict:
    return {
        "exec_id": "frac.test.01.01",
        "perm_id": None,
        "ib_order_id": None,
        "con_id": 320227571,
        "time": datetime(2026, 6, 11, 19, 17, 15, tzinfo=timezone.utc),
        "symbol": "QQQ",
        "sec_type": "STK",
        "side": "BOT",
        "shares": 0.4977,
        "price": 703.34,
        "exchange": "IBKRATS",
        "commission": 0.35,
        "realized_pnl": 0.0,
        "strike": None,
        "expiry": None,
        "right": None,
    }


def test_fractional_shares_survive_recording() -> None:
    result = record_external_fills([_fractional_execution()], scope=SCOPE)
    assert result["inserted"] == 1

    engine = get_sync_engine()
    with engine.connect() as conn:
        qty = conn.execute(
            select(order_fills.c.qty).where(order_fills.c.exec_id == "frac.test.01.01")
        ).scalar_one()
    assert Decimal(str(qty)) == Decimal("0.4977")
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest scripts/tests/test_fractional_fill_qty.py -xvs
```

Expected: FAIL — `assert Decimal('0') == Decimal('0.4977')` (the `int()` truncation).

- [ ] **Step 3: Commit the red test** (it documents the bug even before the fix; CI runs on the branch, not master, so a red commit mid-branch is acceptable — alternatively squash at PR time)

```bash
git add scripts/tests/test_fractional_fill_qty.py
git commit -m "test: failing — fractional fills truncated to qty=0"
```

---

## Task 4: Schema + migration — `order_fills.qty` and `trades.quantity` go Numeric

**Files:**

- Modify: `src/xenon/db/schema.py:548` (`order_fills.qty`) and the `trades` table's `quantity` column
- Create: `src/xenon/db/migrations/versions/2026_06_13_fill_qty_numeric.py`

The per-worker pytest DBs are created from `schema.py` metadata, so the schema edit is what turns Task 3 green at the column level; the Alembic migration is for the real `core_test`/`core_dev` databases. **Note:** local `alembic heads` shows two heads, but one (`2026_06_02_futu_orders`) is from an **untracked** file that isn't on master — the true committed head is the single `58107314f4c9`. Step 2 computes this from tracked files only; do not parent on the untracked futu revision.

- [ ] **Step 1: Edit `schema.py`**

In the `order_fills` table:

```python
    Column("qty", Numeric(20, 8), nullable=False),
```

(was `Column("qty", Integer, nullable=False)`). In the `trades` table:

```python
    Column("quantity", Numeric(20, 8), nullable=False),
```

(was `Integer` — the aggregator writes fractional stock trades here after Task 6). `Numeric(20, 8)` matches the existing `futu_orders.quantity` convention.

- [ ] **Step 2: Confirm the true committed head (do NOT trust local `alembic heads`)**

The local working tree contains an **untracked** migration `2026_06_02_futu_orders_and_fees.py` (belongs to the separate `feat/futu-orders-fees-ingestion` branch — `git status` shows it as `??`). It pollutes `alembic heads`, which is why the review session saw two heads. The real committed head on master is a single revision. Verify:

```bash
python3 - <<'PY'
import subprocess, re, pathlib
files = subprocess.check_output(["git","ls-files","src/xenon/db/migrations/versions/*.py"]).decode().split()
revs, downs = {}, set()
for f in files:
    t = pathlib.Path(f).read_text()
    rev = re.search(r'^revision[^=]*=\s*["\']([^"\']+)["\']', t, re.M)
    m = re.search(r'^down_revision[^=]*=\s*(.+)$', t, re.M)
    if rev: revs[rev.group(1)] = f
    if m:
        for d in re.findall(r'["\']([0-9a-zA-Z_]+)["\']', m.group(1)): downs.add(d)
print("Committed heads:", [r for r in revs if r not in downs])
PY
```

Expected (as of 2026-06-13): `Committed heads: ['58107314f4c9']`. Use that single value as `down_revision`. **Do NOT** parent on `2026_06_02_futu_orders` — it isn't committed, so CI on a fresh checkout would fail with "Can't locate revision '2026_06_02_futu_orders'". If this command prints more than one head (e.g. the futu migration has since merged to master), set `down_revision` to a tuple of all printed heads to merge them.

- [ ] **Step 3: Write the migration (hand-authored)**

```python
"""order_fills.qty and trades.quantity: Integer -> Numeric(20, 8)

Fractional-share executions (recurring QQQ/SPY buys) were truncated to
qty=0 by the Integer column.

Revision ID: 2026_06_13_fill_qty_numeric
Revises: 58107314f4c9
Create Date: 2026-06-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2026_06_13_fill_qty_numeric"
# Single committed head on master (see Step 2). If Step 2 printed multiple
# heads, make this a tuple of them.
down_revision: Union[str, Sequence[str], None] = "58107314f4c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "order_fills",
        "qty",
        type_=sa.Numeric(20, 8),
        existing_type=sa.Integer(),
        existing_nullable=False,
        schema="xenon",
    )
    op.alter_column(
        "trades",
        "quantity",
        type_=sa.Numeric(20, 8),
        existing_type=sa.Integer(),
        existing_nullable=False,
        schema="xenon",
    )


def downgrade() -> None:
    op.alter_column(
        "trades",
        "quantity",
        type_=sa.Integer(),
        existing_type=sa.Numeric(20, 8),
        existing_nullable=False,
        schema="xenon",
    )
    op.alter_column(
        "order_fills",
        "qty",
        type_=sa.Integer(),
        existing_type=sa.Numeric(20, 8),
        existing_nullable=False,
        schema="xenon",
    )
```

- [ ] **Step 4: Apply to the dev DB by targeting the specific revision**

`alembic upgrade head` will error with "Multiple head revisions are present" because the untracked futu migration is a stray second head in the working tree. Target the new revision explicitly instead — this applies its path regardless of other heads and leaves the stray file untouched:

```bash
uv run alembic upgrade 2026_06_13_fill_qty_numeric
```

Expected: upgrade applies cleanly. (Do not delete or commit the untracked futu file — it is not part of this work.)

**Prod note:** `ALTER COLUMN ... TYPE` rewrites the table and takes an `ACCESS EXCLUSIVE` lock on `order_fills`/`trades` for the duration. These tables are small (low thousands of rows), so the rewrite is sub-second, but the macmini `migrator` applies this to `core_dev` during deploy — expect a brief write-lock on the order-fills path at that moment. No concurrent-index gymnastics needed at this scale.

- [ ] **Step 5: Run the Task 3 test — still red, but differently**

```bash
uv run pytest scripts/tests/test_fractional_fill_qty.py -xvs
```

Expected: still FAIL (`Decimal('0')`) — the column now holds fractions but the recorder still truncates via `int()`. That is Task 5.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/db/schema.py src/xenon/db/migrations/versions/2026_06_13_fill_qty_numeric.py
git commit -m "feat(db): order_fills.qty + trades.quantity Integer -> Numeric(20,8)"
```

- [ ] **Step 7: Pre-PR head re-check (do this again right before opening the PR).** Re-run the Step 2 head-computation against an up-to-date master (`git fetch origin && git log origin/master..HEAD --oneline` to see what landed). If the futu migration (or any other) has merged to master since you branched, master now has a second head — update `down_revision` to a tuple merging both and re-run Step 4. CI runs `alembic upgrade head` on a clean checkout and will fail on multiple heads.

---

## Task 5: Recorder + serializer stop truncating quantities

**Files:**

- Modify: `src/xenon/execution/ib_reconcile.py:312`
- Modify: `src/xenon/execution/orders_store.py` (`record_fill` signature, ~line 553)
- Modify: `src/xenon/api/routes/orders.py` (`_executed_order`, ~line 135)

- [ ] **Step 1: Fix the recorder coercion in `ib_reconcile.py`**

Line 312, inside `record_external_fills`:

```python
            qty=_decimal(_field(execution, "shares", "qty")),
```

(was `qty=int(_field(execution, "shares", "qty"))` — `_decimal` is the module's existing helper, already used for `price`.)

- [ ] **Step 2: Update the `record_fill` type hint in `orders_store.py`**

```python
    qty: Decimal,
```

(was `qty: int`; `Decimal` is already imported in that module. Callers passing `int` still work — SQLAlchemy coerces.)

- [ ] **Step 3: Fix the API serializer in `routes/orders.py`**

In `_executed_order`:

```python
        "quantity": float(row["qty"]),
```

(was `int(row["qty"])`. The frontend type `ExecutedOrder.quantity` is already `number` — no TS change needed. Leave `_open_order`'s `int(row["quantity"])` alone: `order_submissions.quantity` stays Integer; Xenon-placed orders are whole units.)

- [ ] **Step 4: Run the Task 3 test — green — plus the neighbors**

```bash
uv run pytest scripts/tests/test_fractional_fill_qty.py \
  scripts/tests/test_record_external_fills_resolves_submission.py \
  scripts/tests/test_record_external_fills_commission_lag.py \
  scripts/tests/test_ib_activity_mirror.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/ib_reconcile.py src/xenon/execution/orders_store.py src/xenon/api/routes/orders.py
git commit -m "fix(fills): record fractional-share quantities instead of truncating to 0"
```

---

## Task 6: trade_aggregator handles fractional quantities

**Files:**

- Modify: `src/xenon/execution/trade_aggregator.py` (lines 238, 270, 295, 343 — every `int(fill["qty"])`)
- Test: `scripts/tests/test_trade_aggregator_fractional.py` (create)

- [ ] **Step 1: Write the failing test**

`aggregate_trade_from_fills` requires DB rows; test the pure helpers instead. Check the exact names first (`grep -n "def _quantity\|def aggregate" src/xenon/execution/trade_aggregator.py`); the helpers verified on 2026-06-13 are `_quantity(fills)` (line 292) and the `int()` coercions at 238/270/295/343.

```python
"""trade_aggregator must not truncate fractional stock fill quantities."""

from __future__ import annotations

from decimal import Decimal

from xenon.execution.trade_aggregator import _quantity


def _fill(qty: str, side: str) -> dict:
    return {
        "qty": Decimal(qty),
        "side": side,
        "price": Decimal("703.34"),
        "ticker": "QQQ",
        "metadata": {"sec_type": "STK"},
        "con_id": 320227571,
    }


def test_quantity_keeps_fractions() -> None:
    fills = [_fill("0.4977", "BUY"), _fill("0.5023", "BUY")]
    assert _quantity(fills) == Decimal("1.0000")
```

If `_quantity`'s `_instrument_key` needs more fields than `_fill` provides, extend the fixture dict — read `_instrument_key` and mirror its `.get()` keys exactly; do not change `_instrument_key`.

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest scripts/tests/test_trade_aggregator_fractional.py -xvs
```

Expected: FAIL — `int(fill["qty"])` truncates both fills to 0, so `_quantity` returns 0.

- [ ] **Step 3: Replace the four `int()` coercions**

- Line 238: `qty = Decimal(str(fill["qty"]))` (was `qty = int(fill["qty"])`)
- Line 270: `net_qty[key] += direction * Decimal(str(fill["qty"]))`
- Line 295 (inside `_quantity`): `by_instrument[_instrument_key(fill)][fill["side"]] += Decimal(str(fill["qty"]))` — and change the defaultdict to `defaultdict(lambda: {"BUY": Decimal(0), "SELL": Decimal(0)})`; update `_quantity`'s return annotation from `int` to `Decimal`.
- Line 343 (legs detail dict, JSON-bound): `"qty": float(fill["qty"]),`

`_is_entry_fill(current_net_qty, fill_direction)` compares against 0 — Decimal comparison works unchanged; update its annotation if it bothers the type checker, behavior identical.

**Leave `_state` and `_expected_quantity` (lines 299–322) as `int`** — they read `order_submissions.quantity`, which stays Integer (Xenon-placed orders are whole units; see Task 5). Their `quantity < expected` check is the only place a Decimal (`_quantity` result) meets an int (`_expected_quantity` result); Python's `Decimal < int` works correctly, and fractional external fills have no submission `source`, so `_expected_quantity` returns `None` and the comparison is skipped. No change needed — this note exists so the engineer doesn't "fix" them and break the whole-unit path. Line 260 already uses `Decimal(fill["qty"])` — leave it.

- [ ] **Step 4: Run the new test + the full aggregator suite**

```bash
uv run pytest scripts/tests/test_trade_aggregator_fractional.py -xvs
uv run pytest scripts/tests/ -k "aggregat" -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/trade_aggregator.py scripts/tests/test_trade_aggregator_fractional.py
git commit -m "fix(aggregator): Decimal-safe quantities for fractional fills"
```

---

## Task 7: Repair the four existing qty=0 rows

**Files:**

- Create: `scripts/migrations/_2026_06_13_repair_zero_qty_fills.py`

The corrupt rows are 6+ weeks old — IB's executions API lookback can't re-fetch them, and Flex is broken until the Task 11 operator step. So: a one-shot script with `--list` (show corrupt rows) and `--apply mapping.json` (operator supplies true quantities from IB activity statements). No automatic guessing — this is money data.

- [ ] **Step 1: Write the script**

```python
"""One-shot repair: order_fills rows recorded with qty=0.

Root cause (fixed 2026-06-13): fractional-share executions truncated by
int() into an Integer column. This script patches the historical rows;
quantities must come from IB activity statements / Flex — never guessed.

Usage:
    uv run python scripts/migrations/_2026_06_13_repair_zero_qty_fills.py --list
    uv run python scripts/migrations/_2026_06_13_repair_zero_qty_fills.py --apply repairs.json

repairs.json shape: {"<exec_id>": "<qty>", ...}, e.g.
    {"00020ac8.6a2aeca9.01.01": "0.4977"}
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, update

from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_fills


def list_zero_qty() -> int:
    engine = get_sync_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                order_fills.c.exec_id,
                order_fills.c.ticker,
                order_fills.c.side,
                order_fills.c.price,
                order_fills.c.commission,
                order_fills.c.filled_at,
            ).where(order_fills.c.qty == 0)
        ).all()
    for row in rows:
        print(f"{row.exec_id}  {row.ticker:<5} {row.side:<4} price={row.price} "
              f"commission={row.commission} filled_at={row.filled_at}")
    print(f"{len(rows)} row(s) with qty=0")
    return 0


def apply_repairs(mapping_path: Path) -> int:
    """All-or-nothing repair of qty=0 fills, then re-derive affected trades.

    Money data: validate the entire mapping up-front and apply every patch
    in ONE transaction. Any problem (non-positive qty, a missing/already-
    changed row) rolls the whole batch back and exits non-zero — never a
    silent partial apply. After committing, re-run the trade aggregator for
    every affected group so xenon.trades (and the blotter that reads it)
    stops showing the stale qty=0-derived quantity/cost.
    """
    mapping = json.loads(mapping_path.read_text())
    parsed: dict[str, Decimal] = {}
    for exec_id, qty_str in mapping.items():
        qty = Decimal(str(qty_str))
        if qty <= 0:
            print(f"refusing non-positive qty for {exec_id}", file=sys.stderr)
            return 1
        parsed[exec_id] = qty

    engine = get_sync_engine()
    groups: set[tuple[str, str]] = set()  # (kind, key) for re-aggregation
    try:
        with engine.begin() as conn:
            # Fetch ALL targeted rows regardless of current qty so the script
            # is idempotent/re-runnable: a row already patched to qty>0 (e.g.
            # a prior run committed the patch but its re-aggregation failed)
            # is "already done", not "missing". Only a truly absent exec_id
            # is a hard error.
            rows = {
                r.exec_id: r
                for r in conn.execute(
                    select(
                        order_fills.c.exec_id,
                        order_fills.c.submission_id,
                        order_fills.c.metadata,
                        order_fills.c.qty,
                    ).where(order_fills.c.exec_id.in_(list(parsed)))
                ).all()
            }
            absent = sorted(set(parsed) - set(rows))
            if absent:
                raise RuntimeError(f"no order_fills row for exec_ids {absent}; nothing applied")

            for exec_id, qty in parsed.items():
                row = rows[exec_id]
                if row.qty == 0:
                    res = conn.execute(
                        update(order_fills)
                        .where(order_fills.c.exec_id == exec_id, order_fills.c.qty == 0)
                        .values(qty=qty)
                    )
                    if res.rowcount != 1:
                        raise RuntimeError(f"{exec_id} changed under us (rowcount={res.rowcount}); rolled back")
                elif Decimal(str(row.qty)) != qty:
                    # Already non-zero but disagrees with the mapping — refuse
                    # to silently overwrite money data; operator must reconcile.
                    raise RuntimeError(
                        f"{exec_id} already has qty={row.qty}, mapping says {qty}; rolled back"
                    )
                # else: already at the requested qty — no-op, still re-aggregate.
                if row.submission_id:
                    groups.add(("submission", row.submission_id))
                elif (row.metadata or {}).get("legacy_id"):
                    groups.add(("legacy", row.metadata["legacy_id"]))
    except RuntimeError as exc:
        print(f"abort: {exc}", file=sys.stderr)
        return 1

    # Re-derive affected trades from the now-correct fills. Mirrors how
    # record_external_fills groups (submission_id when known, else the
    # metadata legacy_id). VERIFIED 2026-06-13: record_external_fills stores
    # legacy_id in order_fills.metadata["legacy_id"], and
    # aggregate_trade_from_fills(legacy_id=...) filters on exactly that key
    # (_fills_stmt → metadata["legacy_id"].astext == legacy_id,
    # trade_aggregator.py:87) — the grouping is identical.
    from xenon.execution.trade_aggregator import aggregate_trade_from_fills

    for kind, key in groups:
        if kind == "submission":
            aggregate_trade_from_fills(submission_id=key)
        else:
            aggregate_trade_from_fills(legacy_id=key)
    print(f"patched {len(parsed)} row(s); re-aggregated {len(groups)} trade group(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true")
    group.add_argument("--apply", type=Path, metavar="MAPPING_JSON")
    args = parser.parse_args()
    if args.list:
        return list_zero_qty()
    return apply_repairs(args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run `--list` against the dev DB**

```bash
uv run python scripts/migrations/_2026_06_13_repair_zero_qty_fills.py --list
```

Expected: the 4 known rows (2× QQQ BUY, 1× SPY BUY, 1× QQQ BUY from 2026-04-30…2026-06-11).

- [ ] **Step 3: Commit (script only — the `--apply` run is an operator step once true quantities are read off the IB statement, and must also be run against `core_dev` via the macmini deploy path)**

```bash
git add scripts/migrations/_2026_06_13_repair_zero_qty_fills.py
git commit -m "chore(migrations): one-shot repair script for qty=0 fractional fills"
```

- [ ] **Step 4: Leave an operator TODO** — add to the PR description: "After merge: read true fractional quantities for the 4 exec_ids from IB activity statements, run the repair script with `--apply` on core_test and core_dev."

---

## Task 8: Stock fills can be closing fills (frontend classification)

**Files:**

- Modify: `web/components/WorkspaceSections.tsx:282` (export) and `:292-295` (`isClosingFill`)
- Test: `web/tests/executed-orders-stock-close.test.ts` (create)

- [ ] **Step 1: Export the grouping function for tests**

Line 282: change `function groupExecutedOrders(` to `export function groupExecutedOrders(`.

- [ ] **Step 2: Write the failing test**

```typescript
import { describe, expect, it } from "vitest";
import { groupExecutedOrders } from "../components/WorkspaceSections";
import type { ExecutedOrder } from "../lib/types";

function stockFill(overrides: Partial<ExecutedOrder>): ExecutedOrder {
  return {
    execId: "x-1",
    symbol: "QQQ",
    contract: {
      conId: 320227571,
      symbol: "QQQ",
      secType: "STK",
      strike: null,
      right: null,
      expiry: null,
    },
    side: "SLD",
    quantity: 1,
    avgPrice: 703.34,
    commission: 0.35,
    realizedPNL: -42.5,
    time: "2026-06-11T19:17:15+00:00",
    exchange: "IBKRATS",
    ...overrides,
  };
}

describe("groupExecutedOrders — stock closing fills", () => {
  it("classifies a stock sell with realized P&L as CLOSE with the P&L total", () => {
    const groups = groupExecutedOrders([stockFill({})]);
    expect(groups).toHaveLength(1);
    expect(groups[0].isClosing).toBe(true);
    expect(groups[0].totalPnL).toBeCloseTo(-42.5);
  });

  it("keeps a stock buy with zero realized P&L as OPEN", () => {
    const groups = groupExecutedOrders([
      stockFill({ execId: "x-2", side: "BOT", realizedPNL: 0 }),
    ]);
    expect(groups[0].isClosing).toBe(false);
    expect(groups[0].totalPnL).toBeNull();
  });
});
```

If `OrderContract`'s nullable fields differ (check `web/lib/types.ts` around line 120), adjust the fixture to the actual type — do not loosen the type.

- [ ] **Step 3: Run it to verify the first case fails**

```bash
cd web && npx vitest run tests/executed-orders-stock-close.test.ts
```

Expected: FAIL — `isClosing` is `false` because `isClosingFill` requires `secType === "OPT"`.

- [ ] **Step 4: Fix the classifier**

`WorkspaceSections.tsx:292-295`:

```typescript
const isClosingFill = (fill: ExecutedOrder): boolean =>
  (fill.contract.secType === "OPT" || fill.contract.secType === "STK") &&
  fill.realizedPNL != null &&
  Math.abs(fill.realizedPNL) > 0.01;
```

**Known limitation (unchanged, pre-existing):** this heuristic still misses a _break-even_ close (realizedPNL ≈ 0 → classified OPEN) and can mislabel a same-minute reversal — true for options today too. The real fix is direction + position-lineage grouping (group by `perm_id`, not symbol+minute+realizedPNL), tracked in the Task 13 backlog. This task only closes the "stock close shows OPEN / no P&L" gap from the screenshot, not the break-even tail.

- [ ] **Step 5: Run the new test + the full Vitest suite**

```bash
cd web && npx vitest run tests/executed-orders-stock-close.test.ts && npm test
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add web/components/WorkspaceSections.tsx web/tests/executed-orders-stock-close.test.ts
git commit -m "fix(web): stock fills with realized P&L classify as closing"
```

---

## Task 9: Executed-orders times render in ET

**Files:**

- Create: `web/lib/timeFormat.ts`
- Modify: `web/components/WorkspaceSections.tsx:2091` and `:2161`
- Test: `web/tests/time-format.test.ts` (create)

- [ ] **Step 1: Write the failing test**

```typescript
import { describe, expect, it } from "vitest";
import { formatEtTime } from "../lib/timeFormat";

describe("formatEtTime", () => {
  it("renders a UTC timestamp in Eastern time with the ET suffix", () => {
    // June = EDT (UTC-4): 19:17:15Z -> 15:17:15 ET
    expect(formatEtTime("2026-06-11T19:17:15+00:00")).toBe("15:17:15 ET");
  });

  it("renders winter timestamps in EST (UTC-5)", () => {
    expect(formatEtTime("2026-01-15T19:17:15+00:00")).toBe("14:17:15 ET");
  });

  it("returns an em dash for unparseable input", () => {
    expect(formatEtTime("not-a-date")).toBe("—");
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd web && npx vitest run tests/time-format.test.ts
```

Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `web/lib/timeFormat.ts`**

```typescript
// Blotter timestamps are pinned to exchange time (ET), not browser-local
// time — a 15:17 ET fill must not render as 03:17 for a UTC+8 operator.
const ET_TIME = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  hour12: false,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

export function formatEtTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${ET_TIME.format(d)} ET`;
}
```

- [ ] **Step 4: Replace both render sites in `WorkspaceSections.tsx`**

Add the import at the top of the file alongside the other `../lib` imports (or `@/lib` — match the file's existing import style):

```typescript
import { formatEtTime } from "@/lib/timeFormat";
```

Line 2091: `<td>{formatEtTime(group.time)}</td>` (was `{new Date(group.time).toLocaleTimeString()}`)
Line 2161: `{formatEtTime(e.time)}` (was `{new Date(e.time).toLocaleTimeString()}`)

(Leave line 2303's `toLocaleDateString` — a date has no intraday-timezone ambiguity worth the churn.)

- [ ] **Step 5: Run tests + typecheck**

```bash
cd web && npx vitest run tests/time-format.test.ts && npm run typecheck
```

Expected: PASS, no type errors.

- [ ] **Step 6: Commit**

```bash
git add web/lib/timeFormat.ts web/components/WorkspaceSections.tsx web/tests/time-format.test.ts
git commit -m "fix(web): pin blotter fill times to ET instead of browser-local"
```

---

## Task 10: SPX (index) option chain qualifies correctly

**Files:**

- Modify: `src/xenon/execution/ib_option_chain.py`
- Test: `scripts/tests/test_option_chain_underlying.py` (create)

- [ ] **Step 1: Write the failing test (pure helper — no IB connection)**

```python
"""xenon-ib-option-chain must qualify indices as Index, not Stock.

Root cause of the CHAIN-tab 502 'Could not qualify SPX' (2026-06-13):
the CLI hardcoded Stock(symbol, "SMART", "USD") and secType "STK" for
every symbol. SPX/NDX/RUT are cash-settled indices — Stock can never
qualify, and reqSecDefOptParams needs underlyingSecType="IND".
"""

from __future__ import annotations

from ib_async import Index, Stock

from xenon.execution.ib_option_chain import underlying_contract


def test_spx_is_an_index_on_cboe() -> None:
    contract, sec_type = underlying_contract("SPX")
    assert isinstance(contract, Index)
    assert contract.exchange == "CBOE"
    assert sec_type == "IND"


def test_ndx_routes_to_nasdaq() -> None:
    contract, sec_type = underlying_contract("NDX")
    assert isinstance(contract, Index)
    assert contract.exchange == "NASDAQ"
    assert sec_type == "IND"


def test_etf_stays_a_smart_stock() -> None:
    contract, sec_type = underlying_contract("QQQ")
    assert isinstance(contract, Stock)
    assert contract.exchange == "SMART"
    assert sec_type == "STK"


def test_unknown_ticker_defaults_to_stock() -> None:
    contract, sec_type = underlying_contract("AAPL")  # not in the V1 universe
    assert isinstance(contract, Stock)
    assert sec_type == "STK"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest scripts/tests/test_option_chain_underlying.py -xvs
```

Expected: FAIL — `ImportError: cannot import name 'underlying_contract'`.

- [ ] **Step 3: Implement the helper and rewire `main()`**

In `src/xenon/execution/ib_option_chain.py`, add above `main()` (and move the ib_async import to module level — replace the inline `from ib_async import Stock` at line 31):

```python
from ib_async import Index, Stock

# CBOE is the home exchange for SPX/RUT index options; NDX lives on NASDAQ.
# Mirrors _preferred_index_exchange in src/xenon/api/server.py.
_PREFERRED_INDEX_EXCHANGE = {"NDX": "NASDAQ"}


def underlying_contract(symbol: str):
    """Return (contract, underlyingSecType) for chain qualification.

    Indices in the V1 universe (universe.is_index raises KeyError for
    unknown tickers, hence the is_known gate) qualify as Index on their
    home exchange; everything else stays Stock/SMART.
    """
    from xenon.execution.universe import is_index, is_known

    upper = symbol.upper()
    if is_known(upper) and is_index(upper):
        exchange = _PREFERRED_INDEX_EXCHANGE.get(upper, "CBOE")
        return Index(upper, exchange), "IND"
    return Stock(symbol, "SMART", "USD"), "STK"
```

Then in `main()`, replace lines 30–39:

```python
        # Qualify the underlying to get a valid conId (required by reqSecDefOptParams)
        contract, sec_type = underlying_contract(args.symbol)
        client._ib.qualifyContracts(contract)
        if not contract.conId:
            print(json.dumps({"error": f"Could not qualify {args.symbol}"}))
            return

        chains = client._ib.reqSecDefOptParams(contract.symbol, "", sec_type, contract.conId)
```

**This is a proven pattern, not a guess.** `src/xenon/api/server.py:774–784` (`_preferred_index_exchange` + `_fetch_ib_index_option_chain`) already qualifies indices the same way in production: NDX→NASDAQ else CBOE, `Index(symbol, exchange)`, and `reqSecDefOptParams(symbol, <exch>, "IND", conId)`. The plan's helper mirrors it. The second `reqSecDefOptParams` arg (`futFopExchange`) is `""` here = "all exchanges" per the IB API — the original STK path used `""` successfully; it is strictly more permissive than passing a single exchange, so SPX's CBOE chains are still returned.

- [ ] **Step 4: Run the test**

```bash
uv run pytest scripts/tests/test_option_chain_underlying.py -xvs
```

Expected: PASS.

- [ ] **Step 5: Live verification (requires IB Gateway connected — paper is fine)**

```bash
uv run xenon-ib-option-chain --symbol SPX --port 4002
```

Expected: JSON with `expirations` (not the `Could not qualify` error). Then per root CLAUDE.md, verify the CHAIN tab renders for SPX in the browser before calling the UI symptom fixed.

**Already live-verified (2026-06-13, against `100.66.147.98:4001`).** A throwaway script ran both code paths against the real Gateway:

- OLD `Stock('SPX','SMART','USD')` → IB Error 200 "No security definition has been found" → `conId=0` → the exact "Could not qualify SPX" 502.
- NEW `Index('SPX','CBOE')` → `conId=416904` → `reqSecDefOptParams('SPX','','IND',416904)` → **6 chains, 57 expirations, 778 strikes**.
- `Index('NDX','NASDAQ')` → conId 416843, 12 chains; `Stock('QQQ','SMART','USD')` → conId 320227571, 20 chains (ETF branch unaffected).

So this Step is confirmation, not discovery — if the live run differs, something regressed.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/execution/ib_option_chain.py scripts/tests/test_option_chain_underlying.py
git commit -m "fix(chain): qualify SPX/NDX/RUT as Index — CHAIN tab 502 root cause"
```

---

## Task 11: Flex blotter — surface real failures instead of a 502, and fix the saved query

**Files:**

- Modify: `src/xenon/api/server.py` (the `blotter_sync` handler, `raise HTTPException(status_code=502, ...)` branch around line 2862)
- Modify: `web/components/WorkspaceSections.tsx` (Historical Trades panel, near the `configured === false` branch ~line 2341)
- Test: `scripts/tests/test_blotter_flex_error.py` (create)

- [ ] **Step 1: Write the failing API test**

```python
"""/blotter surfaces a Flex failure as a structured payload, not a bare 502.

A Flex saved-query misconfiguration (e.g. IB ErrorCode 1001 — CSV format
on the XML-only legacy servlet) is an actionable operator error. The UI
needs the error text, not an opaque 502.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import xenon.api.server as server_mod
from xenon.api.guards import get_account_scope
from xenon.api.subprocess import ScriptResult
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU0000000")


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def fake_run_module(module: str, args=None, timeout: float = 30.0) -> ScriptResult:
        return ScriptResult(
            ok=False,
            error="Flex Query request failed: Statement could not be generated at this time. (code: 1001)",
            exit_code=1,
        )

    monkeypatch.setattr(server_mod, "run_module", fake_run_module)
    monkeypatch.setattr(
        server_mod, "fetch_blotter_pg",
        lambda conn, scope, days: {"closed_trades": [], "open_trades": []},
    )
    monkeypatch.setattr(server_mod, "blotter_has_trades", lambda payload: False)
    server_mod.app.dependency_overrides[get_account_scope] = lambda: SCOPE
    try:
        yield TestClient(server_mod.app)
    finally:
        server_mod.app.dependency_overrides.pop(get_account_scope, None)


def test_flex_failure_returns_structured_payload(client: TestClient) -> None:
    resp = client.post("/blotter")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert "1001" in body["flex_error"]
    assert body["closed_trades"] == []
```

(If `ScriptResult`'s constructor differs — check `src/xenon/api/subprocess.py` dataclass fields — match it. If `TestClient` needs `app.state` pre-seeding per memory `feedback_testclient_skips_lifespan`, mirror the autouse conftest pattern already used by other tests in `scripts/tests/`.)

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest scripts/tests/test_blotter_flex_error.py -xvs
```

Expected: FAIL — today this path raises `HTTPException(502)`.

- [ ] **Step 3: Replace the 502 branch in `blotter_sync`**

Replace:

```python
        if pg_has:
            return pg_payload
        raise HTTPException(status_code=502, detail=result.error)
```

with:

```python
        if pg_has:
            pg_payload["configured"] = True
            pg_payload["flex_error"] = result.error
            return pg_payload
        return {
            "configured": True,
            "flex_error": result.error,
            "as_of": None,
            "summary": {
                "closed_trades": 0,
                "open_trades": 0,
                "total_commissions": 0,
                "realized_pnl": 0,
            },
            "closed_trades": [],
            "open_trades": [],
            "source": "none",
            "message": (
                "IB Flex Query is configured but the fetch failed. "
                "If the error mentions code 1001, set the saved Flex query's "
                "format to XML in the IB portal (the legacy servlet rejects CSV)."
            ),
        }
```

- [ ] **Step 4: Surface `flex_error` in the Historical Trades panel**

In `WorkspaceSections.tsx`, locate the Historical Trades panel (the `configured === false` empty-state branch is ~line 2341). Directly after that branch's block, inside the same panel container, add:

```tsx
{
  data?.flex_error ? (
    <div className="muted" style={{ padding: "8px 0" }}>
      Flex sync failed: {data.flex_error}
      {data?.message ? <div>{data.message}</div> : null}
    </div>
  ) : null;
}
```

Match the panel's existing class names (reuse whatever class the unconfigured message uses rather than `muted` if it differs — read the surrounding JSX first; brand rules: tokens only, no raw hex). Add `flex_error?: string | null;` to the `BlotterData` type in **`web/lib/types.ts`** (verified location — the `configured?: boolean` field is at `types.ts:469`, inside `export type BlotterData = {` at line 455). Do not add it to `useBlotter.ts` — that file consumes `BlotterData`, it doesn't declare it.

- [ ] **Step 5: Run tests + typecheck**

```bash
uv run pytest scripts/tests/test_blotter_flex_error.py -xvs
cd web && npm run typecheck && npx vitest run tests/historical-trades-unconfigured.test.tsx
```

Expected: all PASS (the unconfigured test must still pass — exit-2 behavior unchanged).

- [ ] **Step 6: Commit**

```bash
git add src/xenon/api/server.py web/components/WorkspaceSections.tsx web/lib/types.ts scripts/tests/test_blotter_flex_error.py
git commit -m "fix(blotter): surface Flex fetch failures with actionable detail instead of 502"
```

- [ ] **Step 7: Operator step (manual, document in PR):** log in to IB Account Management → Performance & Reports → Flex Queries → edit saved query `1529245` → set delivery/format to **XML** → save. Then click Refresh on the Historical Trades panel and confirm trades load.

**Diagnosis verified 2026-06-13 (as far as is possible without portal access):** the client hits the legacy XML-only servlet (`flex_query.py:60`, `Universal/servlet/FlexStatementService.SendRequest`, `v=3` is the API version — _not_ an output-format override). Re-probed 3× this session; every attempt returned `code: 1001` ("Statement could not be generated…") at the SendRequest step — reproducible and permanent, **not** the transient throttle code `1018`. The format is a server-side property of the saved query with no request-param lever, so flipping it to XML in the portal is the only remediation and is inherently operator-side (cannot be scripted or unit-tested here). After the flip, the existing exit-2 / configured path stays intact; this Step just turns the (now-handled) error state into live data.

---

## Task 12: Mirror TWS-side cancels into Postgres (cancel sweep)

**Files:**

- Modify: `src/xenon/api/services/ib_activity_mirror.py` (add `sweep_disappeared_orders`, rewire `run_activity_poll_tick`)
- Test: `scripts/tests/test_tws_cancel_sweep.py` (create)

This closes the documented gap: a `WORKING` row whose order is cancelled in TWS currently stays `WORKING` forever. Disambiguation per `api/CLAUDE.md`: disappearance **plus** recorded fills ⇒ FILLED; disappearance for **two consecutive ticks** without full fills ⇒ CANCELLED (the one-tick grace prevents misclassifying an order that filled mid-tick, whose fill row lands on the next fills tick).

- [ ] **Step 1: Write the failing tests**

Seed helper mirrors `_seed_snapshot_row` in `scripts/tests/test_record_external_fills_resolves_submission.py` — copy its insert-values shape (read that file for the full column list; it already compiles against the current schema).

```python
"""TWS-side cancels must transition WORKING rows out of WORKING.

Decision table (api/CLAUDE.md § activity mirror):
  disappeared + fills cover quantity        -> FILLED   (first sweep)
  disappeared twice + fills incomplete      -> CANCELLED (reason TWS_CANCEL_MIRROR)
  disappeared once then reappears           -> stays WORKING, grace cleared
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import insert, select

from xenon.api.services.ib_activity_mirror import sweep_disappeared_orders
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_fills, order_submissions
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU0000000")
NOW = datetime(2026, 6, 13, 14, 30, tzinfo=timezone.utc)


def _seed_working(perm_id: str, *, quantity: int = 2, ib_order_id: str = "0") -> str:
    submission_id = f"snapshot-{perm_id}"
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=submission_id,
                user_id="snapshot",
                client_attempt_id=f"ca-{perm_id}",
                state="WORKING",
                ticker="QQQ",
                security_type="STK",
                action="BUY",
                quantity=quantity,
                limit_price=Decimal("700.00"),
                tif="DAY",
                perm_id=perm_id,
                ib_order_id=ib_order_id,
                submitted_at=NOW,
                updated_at=NOW,
                modify_sequence=0,
                broker=SCOPE.broker,
                account_env=SCOPE.account_env,
                broker_account=SCOPE.broker_account,
            )
        )
    return submission_id


def _seed_fill(perm_id: str, *, qty: str, exec_id: str) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_fills).values(
                exec_id=exec_id,
                submission_id=f"snapshot-{perm_id}",
                perm_id=perm_id,
                ticker="QQQ",
                side="BUY",
                qty=Decimal(qty),
                price=Decimal("700.00"),
                commission=Decimal("0.35"),
                filled_at=NOW,
                metadata={"sec_type": "STK", "legacy_source": "test"},
                broker=SCOPE.broker,
                account_env=SCOPE.account_env,
                broker_account=SCOPE.broker_account,
            )
        )


def _state_of(submission_id: str) -> str:
    engine = get_sync_engine()
    with engine.connect() as conn:
        return conn.execute(
            select(order_submissions.c.state).where(
                order_submissions.c.submission_id == submission_id
            )
        ).scalar_one()


# A non-empty snapshot of some *other* still-open order. Every "target is
# absent" test passes this so the target's disappearance is real, not an
# empty-snapshot artifact (the empty-snapshot path is its own test).
OTHER = [{"permId": "999999", "orderId": "888888"}]


def test_disappeared_with_full_fills_marks_filled_first_sweep() -> None:
    sid = _seed_working("9001", quantity=2)
    _seed_fill("9001", qty="2", exec_id="sweep-fill-1")
    grace: set[str] = set()
    sweep_disappeared_orders(OTHER, scope=SCOPE, grace=grace)
    assert _state_of(sid) == "FILLED"
    assert sid not in grace


def test_disappeared_without_fills_cancels_on_second_sweep() -> None:
    sid = _seed_working("9002")
    grace: set[str] = set()
    sweep_disappeared_orders(OTHER, scope=SCOPE, grace=grace)
    assert _state_of(sid) == "WORKING"          # first sweep: grace only
    assert sid in grace
    sweep_disappeared_orders(OTHER, scope=SCOPE, grace=grace)
    assert _state_of(sid) == "CANCELLED"        # second sweep: confirmed gone


def test_reappearing_order_clears_grace() -> None:
    sid = _seed_working("9003")
    grace: set[str] = set()
    sweep_disappeared_orders(OTHER, scope=SCOPE, grace=grace)
    assert sid in grace
    sweep_disappeared_orders(OTHER + [{"permId": "9003"}], scope=SCOPE, grace=grace)
    assert _state_of(sid) == "WORKING"
    assert sid not in grace


def test_empty_snapshot_never_cancels() -> None:
    """An empty open-order snapshot must not cancel working rows — it is a
    post-reconnect/stale-read signature, not 'everything cancelled'."""
    sid = _seed_working("9004")
    grace: set[str] = set()
    result = sweep_disappeared_orders([], scope=SCOPE, grace=grace)
    assert result.get("skipped") == "empty_snapshot"
    assert _state_of(sid) == "WORKING"
    # Even a second empty sweep does not cancel.
    sweep_disappeared_orders([], scope=SCOPE, grace=grace)
    assert _state_of(sid) == "WORKING"


def test_present_by_order_id_survives_permid_race() -> None:
    """An order still open but reported with permId=0 (the documented
    client-side race) must be matched by ib_order_id, not cancelled."""
    sid = _seed_working("9005", ib_order_id="55")
    grace: set[str] = set()
    # Snapshot has the order under orderId=55 with permId 0 (race).
    sweep_disappeared_orders(
        [{"permId": 0, "orderId": "55"}], scope=SCOPE, grace=grace
    )
    assert _state_of(sid) == "WORKING"
    assert sid not in grace
```

(If `order_submissions` has additional NOT NULL columns the insert misses, read the table def in `schema.py:453+` and the seed helper in the existing test — extend `_seed_working` accordingly. `committed_db` marker is NOT needed: everything runs in-process on the test transaction.)

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest scripts/tests/test_tws_cancel_sweep.py -xvs
```

Expected: FAIL — `ImportError: cannot import name 'sweep_disappeared_orders'`.

- [ ] **Step 3: Implement the sweep in `ib_activity_mirror.py`**

Add imports near the top (`Decimal`, sqlalchemy pieces are imported inside the function to keep module import light, matching the file's best-effort style):

```python
TWS_CANCEL_REASON = "TWS_CANCEL_MIRROR"

# submission_ids that were missing from the open-order snapshot on the
# previous sweep. One-tick grace: an order that fills mid-tick disappears
# before its fill row lands; cancelling on first disappearance would
# misclassify it. Module-level on purpose — survives across poller ticks
# within one FastAPI process; a restart just waits one extra tick.
_SWEEP_GRACE: set[str] = set()


def sweep_disappeared_orders(
    open_orders: list[dict],
    *,
    scope: AccountScope,
    grace: set[str] | None = None,
) -> dict:
    """Transition WORKING/PARTIALLY_FILLED rows that vanished from IB's
    open-order snapshot to FILLED (fills cover quantity) or CANCELLED
    (missing two consecutive sweeps). Returns counters for the tick log.
    """
    from decimal import Decimal

    from sqlalchemy import func, select

    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import order_fills, order_submissions
    from xenon.execution import orders_store

    tracked = _SWEEP_GRACE if grace is None else grace
    # Match IB's identity logic in sync_open_orders_to_postgres: a BAG
    # fetched from a non-originating client has orderId=0 and is keyed by
    # permId; a fresh order has permId=0 until the openOrder ack (the
    # documented permId=0 race) and is keyed by orderId. An order is
    # "present" if EITHER its perm_id OR its ib_order_id appears in the
    # snapshot — otherwise the permId=0 race would mark live orders as
    # disappeared and cancel them.
    open_perm_ids = {str(o.get("permId")) for o in open_orders if o.get("permId")}
    open_order_ids = {str(o.get("orderId")) for o in open_orders if o.get("orderId")}

    engine = get_sync_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                order_submissions.c.submission_id,
                order_submissions.c.perm_id,
                order_submissions.c.ib_order_id,
                order_submissions.c.quantity,
                order_submissions.c.security_type,
            ).where(
                order_submissions.c.state.in_(("WORKING", "PARTIALLY_FILLED")),
                order_submissions.c.perm_id.isnot(None),
                order_submissions.c.broker == scope.broker,
                order_submissions.c.account_env == scope.account_env,
                order_submissions.c.broker_account == scope.broker_account,
            )
        ).mappings().all()

    # Safety against the production Gateway-bounce failure mode: an empty
    # snapshot while WORKING rows exist is far more likely a stale/
    # post-reconnect read than every order vanishing at once. Skip the
    # whole sweep — never mass-cancel on an empty snapshot. (Cost: a TWS
    # cancel of your *only* open order isn't mirrored until the next
    # non-empty snapshot or boot rehydrate — acceptable vs mass-cancel.)
    if not open_orders and rows:
        logger.warning(
            "cancel_sweep: empty open-order snapshot with %d working row(s) — skipping sweep",
            len(rows),
        )
        return {"filled": 0, "cancelled": 0, "graced": 0, "skipped": "empty_snapshot"}

    filled = cancelled = graced = 0
    missing_now: set[str] = set()

    for row in rows:
        sid = row["submission_id"]
        present = (
            str(row["perm_id"]) in open_perm_ids
            or (row["ib_order_id"] and str(row["ib_order_id"]) in open_order_ids)
        )
        if present:
            tracked.discard(sid)
            continue

        is_bag = row["security_type"] == "BAG"
        with engine.connect() as conn:
            scope_where = (
                order_fills.c.perm_id == str(row["perm_id"]),
                order_fills.c.broker == scope.broker,
                order_fills.c.account_env == scope.account_env,
                order_fills.c.broker_account == scope.broker_account,
            )
            q = select(
                func.coalesce(func.sum(order_fills.c.qty), 0),
                func.coalesce(func.sum(order_fills.c.qty * order_fills.c.price), 0),
            ).where(*scope_where)
            if is_bag:
                # Per-leg rows duplicate the envelope economically; count
                # only the envelope fill against the combo quantity.
                q = q.where(order_fills.c.metadata["sec_type"].astext == "BAG")
            fill_qty, fill_value = conn.execute(q).one()
            # For a BAG we must distinguish "no fills at all" (genuine
            # cancel candidate) from "leg fills exist but no envelope row"
            # (ambiguous — IB didn't emit a combo-level execution). The
            # latter must NOT be auto-cancelled: a filled combo whose
            # envelope we can't read would be wrongly killed.
            any_fill = False
            if is_bag:
                any_fill = bool(
                    conn.execute(select(func.count()).select_from(order_fills).where(*scope_where)).scalar()
                )

        fill_qty = Decimal(str(fill_qty or 0))
        if is_bag and fill_qty == 0 and any_fill:
            # Ambiguous combo: leg fills present, envelope absent. Stay
            # WORKING, hold in grace, and log — favour never wrongly
            # cancelling a filled combo over closing the gap fast.
            logger.warning(
                "cancel_sweep: BAG %s has leg fills but no envelope row — skipping cancel",
                sid,
            )
            missing_now.add(sid)
            graced += 1
            continue
        order_qty = Decimal(str(row["quantity"]))
        # `fill_qty > 0` guard: a quantity-0 working row (e.g. a fractional
        # open order truncated by the Integer order_submissions.quantity
        # column — see Task 5's note) must never be marked FILLED on zero
        # fills (0 >= 0 would otherwise be True).
        if fill_qty > 0 and fill_qty >= order_qty:
            avg = (Decimal(str(fill_value)) / fill_qty) if fill_qty else None
            orders_store.mark_terminal(
                submission_id=sid,
                state="FILLED",
                reason_code=None,
                filled_qty=int(fill_qty),
                avg_fill_price=avg,
            )
            orders_store.record_event(
                sid, "RECONCILED", {"source": "cancel_sweep", "filled_qty": str(fill_qty)}
            )
            tracked.discard(sid)
            filled += 1
        elif sid in tracked:
            orders_store.mark_terminal(
                submission_id=sid,
                state="CANCELLED",
                reason_code=TWS_CANCEL_REASON,
                filled_qty=int(fill_qty),
                avg_fill_price=None,
            )
            orders_store.record_event(sid, TWS_CANCEL_REASON, {"source": "cancel_sweep"})
            tracked.discard(sid)
            cancelled += 1
        else:
            missing_now.add(sid)
            graced += 1

    # Grace = exactly the ids missing on THIS sweep. Reappeared/filled/
    # cancelled ids were discarded in the loop; stale ids from prior sweeps
    # (orders that left WORKING by another path, e.g. user cancel) are
    # dropped by the clear(). NOTE: module-global _SWEEP_GRACE is shared
    # process-wide — safe today (one scope per process). If a process ever
    # polls multiple scopes, key the grace by scope.
    tracked.clear()
    tracked.update(missing_now)
    return {"filled": filled, "cancelled": cancelled, "graced": graced}
```

- [ ] **Step 4: Rewire `run_activity_poll_tick` to fetch once and sweep after fills**

Replace the body's final `return {...}` (lines ~136–139):

```python
    try:
        open_orders = _fetch_open_orders(client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ib_activity_mirror tick: fetch_open_orders failed: %s", exc)
        open_orders = None

    if open_orders is None:
        oo_result: dict = {"error": "fetch_open_orders failed"}
    else:
        try:
            oo_result = _sync_open_orders_to_postgres(open_orders, scope=scope)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ib_activity_mirror tick: sync_open_orders_to_postgres failed: %s", exc)
            oo_result = {"error": str(exc)}

    fills_result = _safe_fills_tick(client, scope=scope, lookback_days=lookback_days)

    # Sweep only when BOTH feeds succeeded this tick — a failed open-order
    # fetch would otherwise mass-cancel everything, and missing fills data
    # would misclassify mid-tick fills as cancels.
    if open_orders is not None and "error" not in fills_result:
        try:
            sweep_result = sweep_disappeared_orders(open_orders, scope=scope)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ib_activity_mirror tick: cancel sweep failed: %s", exc)
            sweep_result = {"error": str(exc)}
    else:
        sweep_result = {"skipped": True}

    return {"open_orders": oo_result, "fills": fills_result, "cancel_sweep": sweep_result}
```

Keep `_safe_open_orders_tick` itself — other tests import it; only the tick stops calling it. Add `cancel_sweep` counters to the `activity_poller_loop` log line (extend the existing `logger.info` with `sweep[f=%s c=%s g=%s]`, `sweep.get("filled")`, `sweep.get("cancelled")`, `sweep.get("graced")` where `sweep = result.get("cancel_sweep") or {}`).

- [ ] **Step 5: Run the new tests + the mirror suite**

```bash
uv run pytest scripts/tests/test_tws_cancel_sweep.py scripts/tests/test_ib_activity_mirror.py -v
```

Expected: all PASS. If `test_ib_activity_mirror.py` asserts the old tick return shape, update those assertions to include `cancel_sweep`.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/api/services/ib_activity_mirror.py scripts/tests/test_tws_cancel_sweep.py scripts/tests/test_ib_activity_mirror.py
git commit -m "feat(mirror): sweep TWS-cancelled orders to CANCELLED with one-tick grace"
```

---

## Task 13: Documentation — incident history, backlog (Futu program + deferred hardening)

**Files:**

- Modify: `docs/reference/order-path-incident-history.md`
- Modify: `docs/todo-backlog.md` (Inbox section at the bottom)

- [ ] **Step 1: Append incident-history rows**

Read the file first and match its existing row format exactly. Content to record (one row each):

1. **Fractional fills truncated to qty=0** — cause: `int()` coercion in `record_external_fills` + Integer `order_fills.qty` column; external fractional executions (recurring QQQ/SPY buys) only; fix: `_decimal` coercion + `Numeric(20,8)` migration `2026_06_13_fill_qty_numeric` + repair script; regression test: `scripts/tests/test_fractional_fill_qty.py`.
2. **TWS-side cancels never mirrored** — cause: poller had no disappearance handling (documented gap); fix: `sweep_disappeared_orders` with fills-disambiguation + one-tick grace; regression test: `scripts/tests/test_tws_cancel_sweep.py`.
3. **SPX chain 502 "Could not qualify"** — cause: `ib_option_chain.py` hardcoded `Stock/SMART/STK` for all symbols; fix: `underlying_contract()` consulting the universe registry; regression test: `scripts/tests/test_option_chain_underlying.py`.

- [ ] **Step 2: Append backlog entries to `docs/todo-backlog.md` Inbox**

```markdown
- 2026-06-13 — **Futu order integration (multi-broker order system)** — extend the IB order
  system to Futu: same behavior, different API. Explicitly deferred from the 2026-06-13
  order-system repair plan.
  - **Notes:** Inventory from the 2026-06-13 review: data layer is ~70% ready —
    `AccountScope` carries `broker: "IB"|"FUTU"`; `order_fills`/`positions`/
    `account_snapshots`/`nav_history` already allow FUTU; `futu_orders` + `futu_order_fees`
    tables exist (migration `2026_06_02_futu_orders_and_fees.py`) but have no writer.
    Blockers: `order_submissions` has `CheckConstraint("broker = 'IB'")` (schema.py ~501)
    plus IB-only identity columns (`con_id`, `ib_order_id`, `perm_id`, `placing_client_id`);
    `server.py` `_orders_place_from_body` hard-403s non-IB (~1968-1988); execution modules
    (`ib_place_order`, `ib_order_manage`, `single_leg_rehydrate`, `combo_wizard/ib_adapter`)
    import `ib_async` directly; `FutuClient` is read-only by design (no order methods wrapped).
    Natural seam: the subprocess boundary — each broker op is already JSON-in/JSON-out over a
    CLI; spec the stdout/exit-code contract, then a `xenon-futu-place-order` CLI slots in
    behind the dispatch point without touching gate logic. Sequencing: (1) generalize
    order_submissions (lift constraint, generic broker-ref columns), (2) write the subprocess
    JSON contract doc, (3) Futu read-side order/deal ingestion via `order_list_query`/
    `deal_list_query` polling into the same UI surfaces, (4) write-side single-leg only
    (no combo concept on Futu), simulated env first per paper-first rule. The external-fills
    path is what Futu will stress — hardened by the 2026-06-13 fractional-qty fix.
- 2026-06-13 — **Order-surface hardening deferred from the repair plan**
  - **Notes:** (a) `permId`/`orderId` serialized as `0` when NULL in
    `routes/orders.py::_open_order` — indistinguishable from the genuine client-side
    permId=0 race; emit `null` and update frontend types (also Futu groundwork).
    (b) Executed-orders grouping uses symbol+minute buckets
    (`WorkspaceSections.tsx::groupExecutedOrders`) — combos straddling a minute boundary
    split; group by `perm_id` lineage instead (already on every fill row).
    (c) Persist BAG combo legs on `order_submissions` (JSONB) — unblocks BAG qty-increase
    modify at restrictive tiers and gives structure classification real order lineage.
    (d) Structured reason codes for `/blotter` and `/options/chain` subprocess failures
    (pattern: `READ_ONLY_MODE`/`LIMIT_OFF_TICK`).
    (e) Cancel-sweep BAG gap (Task 12): a genuinely TWS-cancelled _partially-filled_
    combo whose envelope fill never materialises stays WORKING until the next boot
    rehydrate (the sweep deliberately won't cancel it — favouring "never wrongly
    cancel a filled combo"). Robust fix needs envelope-or-leg reconciliation or a
    position-change third source like single_leg_rehydrate uses.
    (f) `ib_option_chain.py` defaults to a hardcoded `--client-id 27` instead of the
    `client_id="auto"` pool-range allocation the other subprocess CLIs use — a
    separate clientId-collision path that can also surface as a chain error. Switch
    to auto allocation (argparse default can't be the string "auto" with `type=int`;
    make `--client-id` optional and pass `"auto"` when unset).
    (g) Cancel-sweep state-transition race (Task 12): `orders_store.mark_terminal`
    does an unconditional `UPDATE ... WHERE submission_id = ?` with no state guard,
    so a sweep that read a row as WORKING can clobber a state another path
    (user `/orders/cancel`, fills aggregation) set in the interim. Low probability
    at the 60s cadence and the clobber is usually benign (both paths agree the order
    left WORKING), but the correct fix is a conditional update —
    `WHERE submission_id = ? AND state IN ('WORKING','PARTIALLY_FILLED')` — applied
    in mark_terminal or a sweep-specific variant.
```

- [ ] **Step 3: Commit**

```bash
git add docs/reference/order-path-incident-history.md docs/todo-backlog.md
git commit -m "docs: incident-history rows for 2026-06-13 fixes + backlog Futu program"
```

---

## Final verification & PR

- [ ] **Step 1: Full test suites**

```bash
uv run pytest -n auto
cd web && npm test && npm run typecheck && npm run lint
```

Expected: all green. (Python full suite uses the per-worker DB clones — do not skip.)

**Env gotcha (hit 2026-06-13):** if any test or CLI fails with `ModuleNotFoundError: No module named 'eventkit'`, the local `.venv` has a corrupt `aeventkit` install (only the `.dist-info` survived; `uv sync` reports "checked, OK" and does **not** repair it). Fix: `uv pip install --reinstall aeventkit`, then re-run. `ib_async` imports `eventkit` (the top-level module `aeventkit` provides), so a broken aeventkit silently breaks every IB code path.

- [ ] **Step 2: Browser verification (root CLAUDE.md rule 2 — required for the UI tasks)**

With `dev.sh paper` running: Orders page shows fractional quantities (after the Task 7 repair `--apply`, otherwise still 0 for historical rows — note which), stock closes show CLOSE + P&L, times show `ET` suffix, SPX ticker page CHAIN tab renders strikes, Historical Trades shows either trades or the actionable Flex error.

- [ ] **Step 3: Open the PR (never push master)**

```bash
git push -u origin fix/order-system-repair
gh pr create --title "fix: order-system repair — fractional fills, TWS cancel mirror, SPX chain, Flex surfacing" --body "$(cat <<'EOF'
## Summary
- kill/prevent zombie API servers (dev.sh port guard)
- record fractional-share fills correctly (Numeric qty migration + recorder fix + repair script)
- stock fills classify as closing with realized P&L
- blotter times pinned to ET
- SPX/NDX/RUT chain qualification (Index, not Stock)
- /blotter surfaces Flex failures with actionable detail
- TWS-side cancels mirrored to CANCELLED with one-tick grace

## Operator follow-ups after merge
- [ ] Flip saved Flex query 1529245 to XML format in IB portal (ErrorCode 1001)
- [ ] Run repair script --apply with true fractional quantities on core_test AND core_dev

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Out of scope (tracked, not planned here)

- **Futu order integration** — documented as a backlog issue in Task 13 with the full inventory; gets its own brainstorm + plan.
- permId-null serialization, perm_id-lineage grouping, BAG leg persistence, subprocess reason-code taxonomy — backlog (Task 13), each small enough to plan when picked up.
