# F0 — Universe Registry & Contract Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a single source of truth for the V1 trading universe (9 tickers) and for contract normalization (expiry, multiplier, symbol) — both in Python (`src/xenon/execution/`) and a generated TypeScript mirror (`web/lib/universe.ts`). No behavior change to live order paths in this phase; subsequent phases (F1 audit, F2 preflight) will import these modules.

**Architecture:** Pure-data Python registry. One canonical `UNIVERSE` dict with `is_index`, `cash_settled`, `multiplier`, `k1` metadata. Contract normalization is a small pure-function module (no I/O). The TS mirror is **generated from the Python registry** by a build-time script so the two cannot drift.

**Tech Stack:** Python 3.13 (no new deps), TypeScript (codegen via a small Python script run from `npm` lifecycle hooks), pytest + Vitest.

**Spec reference:** `docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md` §2 (universe) and §9 (contract normalization).

**Master plan:** `docs/superpowers/plans/2026-04-20-order-execution-foundation-master.md` — this is phase F0.

**Changelog v0.1 → v0.2** — applied 12 tribunal findings:
codegen writes to correct repo root (parents[3] + assertion), codegen
self-inserts `src/` on `sys.path` so `xenon` import resolves without
editable install, added `predev`/`pretest`/`pretypecheck` hooks (not
just `prebuild`), test-count expectations corrected (19 Python universe
tests, 15 Vitest mirror tests, 37 phase totals), `.gitattributes`
treated as create-if-absent, checkpoint grep expects codegen script as
a legitimate caller, Step 7 review invocation names real skills, frozen
dataclass assertion uses `FrozenInstanceError` explicitly.

---

## File structure

```
src/xenon/execution/
  universe.py                   ← NEW. UNIVERSE registry + helpers.
  contract_normalize.py         ← NEW. normalize_expiry, normalize_ticker, get_multiplier.

scripts/tests/
  test_universe.py              ← NEW.
  test_contract_normalize.py    ← NEW.

scripts/infra/dev/
  generate_universe_ts.py       ← NEW. Codegen script.

web/lib/
  universe.ts                   ← NEW (GENERATED). Do not hand-edit.

web/tests/
  universe-mirror.test.ts       ← NEW. Verifies generated values match a frozen fixture.

package.json (web/)             ← MODIFY. Add "universe:gen" script + prebuild hook.
.gitattributes                  ← MODIFY. Mark web/lib/universe.ts as generated.
```

**Why split into two modules:** `universe.py` is pure data (risk: wrong
values). `contract_normalize.py` is pure functions (risk: wrong
algorithms). Keeping them separate lets each have focused tests.

---

## Task 1: Create `universe.py` registry

**Files:**

- Create: `src/xenon/execution/universe.py`
- Test: `scripts/tests/test_universe.py`

- [ ] **Step 1: Write failing tests**

Create `scripts/tests/test_universe.py`:

```python
"""Tests for the V1 trading universe registry."""

import dataclasses

import pytest

from xenon.execution.universe import (
    INDEX_UNIVERSE,
    UNIVERSE,
    UniverseEntry,
    get_multiplier,
    is_index,
    is_known,
)


def test_universe_contains_exactly_nine_tickers():
    assert set(UNIVERSE.keys()) == {
        "SPX", "NDX", "RUT",
        "SPY", "QQQ", "IWM",
        "GLD", "USO", "SIL",
    }


def test_index_universe_is_spx_ndx_rut():
    assert INDEX_UNIVERSE == {"SPX", "NDX", "RUT"}


@pytest.mark.parametrize("ticker", ["SPX", "NDX", "RUT"])
def test_index_tickers_are_cash_settled(ticker):
    entry = UNIVERSE[ticker]
    assert entry.is_index is True
    assert entry.cash_settled is True
    assert entry.multiplier == 100


@pytest.mark.parametrize("ticker", ["SPY", "QQQ", "IWM", "GLD", "USO", "SIL"])
def test_etf_tickers_are_deliverable(ticker):
    entry = UNIVERSE[ticker]
    assert entry.is_index is False
    assert entry.cash_settled is False
    assert entry.multiplier == 100


def test_uso_flagged_as_k1():
    assert UNIVERSE["USO"].k1 is True


def test_non_uso_etfs_are_not_k1():
    for t in ("SPY", "QQQ", "IWM", "GLD", "SIL"):
        assert UNIVERSE[t].k1 is False


def test_is_index_helper():
    assert is_index("SPX") is True
    assert is_index("SPY") is False


def test_is_known_helper():
    assert is_known("SPX") is True
    assert is_known("AAPL") is False


def test_get_multiplier_returns_100_for_all_v1_tickers():
    for ticker in UNIVERSE:
        assert get_multiplier(ticker) == 100


def test_get_multiplier_raises_for_unknown():
    with pytest.raises(KeyError):
        get_multiplier("AAPL")


def test_is_index_raises_for_unknown():
    with pytest.raises(KeyError):
        is_index("AAPL")


def test_universe_entry_is_frozen():
    """Registry values should be immutable to prevent accidental mutation."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        UNIVERSE["SPX"].multiplier = 50  # frozen dataclass
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3.13 -m pytest scripts/tests/test_universe.py -xvs`
Expected: FAIL with `ModuleNotFoundError: No module named 'xenon.execution.universe'`.

- [ ] **Step 3: Implement `universe.py`**

Create `src/xenon/execution/universe.py`:

```python
"""V1 trading universe registry.

Single source of truth for which tickers are tradeable and their
option contract metadata. Frontend mirror at `web/lib/universe.ts`
is generated from this module by
`scripts/infra/dev/generate_universe_ts.py` — do not hand-edit the TS.

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §2
"""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class UniverseEntry:
    ticker: str
    type: str           # "INDEX" | "ETF"
    is_index: bool
    cash_settled: bool
    multiplier: int
    k1: bool            # K-1 tax treatment (USO)


def _make_entry(
    ticker: str,
    *,
    type: str,
    is_index: bool,
    cash_settled: bool,
    multiplier: int = 100,
    k1: bool = False,
) -> UniverseEntry:
    return UniverseEntry(
        ticker=ticker,
        type=type,
        is_index=is_index,
        cash_settled=cash_settled,
        multiplier=multiplier,
        k1=k1,
    )


_RAW: dict[str, UniverseEntry] = {
    "SPX": _make_entry("SPX", type="INDEX", is_index=True, cash_settled=True),
    "NDX": _make_entry("NDX", type="INDEX", is_index=True, cash_settled=True),
    "RUT": _make_entry("RUT", type="INDEX", is_index=True, cash_settled=True),
    "SPY": _make_entry("SPY", type="ETF", is_index=False, cash_settled=False),
    "QQQ": _make_entry("QQQ", type="ETF", is_index=False, cash_settled=False),
    "IWM": _make_entry("IWM", type="ETF", is_index=False, cash_settled=False),
    "GLD": _make_entry("GLD", type="ETF", is_index=False, cash_settled=False),
    "USO": _make_entry("USO", type="ETF", is_index=False, cash_settled=False, k1=True),
    "SIL": _make_entry("SIL", type="ETF", is_index=False, cash_settled=False),
}

# Read-only public view.
UNIVERSE: MappingProxyType[str, UniverseEntry] = MappingProxyType(_RAW)

INDEX_UNIVERSE: frozenset[str] = frozenset(
    t for t, e in _RAW.items() if e.is_index
)


def is_known(ticker: str) -> bool:
    """True if ticker is in the V1 universe."""
    return ticker in UNIVERSE


def is_index(ticker: str) -> bool:
    """True if ticker is an index (cash-settled, no stock leg).

    Raises KeyError if ticker not in universe.
    """
    return UNIVERSE[ticker].is_index


def get_multiplier(ticker: str) -> int:
    """Option contract multiplier for the ticker.

    Raises KeyError if ticker not in universe.
    """
    return UNIVERSE[ticker].multiplier
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python3.13 -m pytest scripts/tests/test_universe.py -xvs`
Expected: 19 passed. (3 parametrized index + 6 parametrized ETF + 10 non-parametrized = 19.)

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/universe.py scripts/tests/test_universe.py
git commit -m "feat(execution): add V1 trading universe registry

Nine-ticker registry (SPX/NDX/RUT/SPY/QQQ/IWM/GLD/USO/SIL) with
is_index, cash_settled, multiplier, k1 metadata. Single source of
truth for preflight Gate 4, quote tokens, and contract normalization.

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §2
Phase: F0 of order-execution-foundation-master plan"
```

---

## Task 2: Create `contract_normalize.py`

**Files:**

- Create: `src/xenon/execution/contract_normalize.py`
- Test: `scripts/tests/test_contract_normalize.py`

- [ ] **Step 1: Write failing tests**

Create `scripts/tests/test_contract_normalize.py`:

```python
"""Tests for contract normalization helpers."""

import pytest

from xenon.execution.contract_normalize import (
    NormalizationError,
    normalize_expiry,
    normalize_ticker,
    resolve_multiplier,
)


# --- normalize_expiry ---

def test_normalize_expiry_accepts_yyyymmdd():
    assert normalize_expiry("20260117") == "20260117"


def test_normalize_expiry_strips_dashes():
    assert normalize_expiry("2026-01-17") == "20260117"


def test_normalize_expiry_strips_slashes():
    assert normalize_expiry("2026/01/17") == "20260117"


def test_normalize_expiry_rejects_too_short():
    with pytest.raises(NormalizationError):
        normalize_expiry("20260")


def test_normalize_expiry_rejects_non_digit():
    with pytest.raises(NormalizationError):
        normalize_expiry("2026JAN17")


def test_normalize_expiry_rejects_empty():
    with pytest.raises(NormalizationError):
        normalize_expiry("")


def test_normalize_expiry_rejects_none():
    with pytest.raises(NormalizationError):
        normalize_expiry(None)  # type: ignore[arg-type]


def test_normalize_expiry_rejects_impossible_month():
    with pytest.raises(NormalizationError):
        normalize_expiry("20261317")  # month 13


def test_normalize_expiry_rejects_impossible_day():
    with pytest.raises(NormalizationError):
        normalize_expiry("20260132")  # day 32


def test_normalize_expiry_accepts_feb_29_leap():
    assert normalize_expiry("20240229") == "20240229"


def test_normalize_expiry_rejects_feb_29_non_leap():
    with pytest.raises(NormalizationError):
        normalize_expiry("20230229")


# --- normalize_ticker ---

def test_normalize_ticker_uppercases():
    assert normalize_ticker("spx") == "SPX"


def test_normalize_ticker_strips_whitespace():
    assert normalize_ticker("  spy  ") == "SPY"


def test_normalize_ticker_rejects_unknown():
    with pytest.raises(NormalizationError):
        normalize_ticker("AAPL")


def test_normalize_ticker_rejects_empty():
    with pytest.raises(NormalizationError):
        normalize_ticker("")


# --- resolve_multiplier ---

def test_resolve_multiplier_all_v1_are_100():
    for ticker in ("SPX", "NDX", "RUT", "SPY", "QQQ", "IWM", "GLD", "USO", "SIL"):
        assert resolve_multiplier(ticker) == 100


def test_resolve_multiplier_rejects_unknown():
    with pytest.raises(NormalizationError):
        resolve_multiplier("AAPL")
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3.13 -m pytest scripts/tests/test_contract_normalize.py -xvs`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `contract_normalize.py`**

Create `src/xenon/execution/contract_normalize.py`:

```python
"""Contract normalization: expiry, ticker, multiplier.

Pure functions. No I/O. Called at the API boundary so downstream
code (OrderTab.tsx, ib_place_order.py, nakedShortGuard.ts) can
stop reimplementing these locally.

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §9
"""

from __future__ import annotations

import datetime as _dt
import re

from xenon.execution.universe import UNIVERSE, is_known


class NormalizationError(ValueError):
    """Raised when input cannot be normalized to a canonical form."""


_EXPIRY_RX = re.compile(r"^\d{8}$")


def normalize_expiry(value: str | None) -> str:
    """Normalize an expiry string to IB canonical `YYYYMMDD`.

    Accepts: `20260117`, `2026-01-17`, `2026/01/17`.
    Rejects: anything else, including `None`, empty, month/day out of
    range, or impossible dates like 2023-02-29.
    """
    if not value or not isinstance(value, str):
        raise NormalizationError(f"expiry must be a non-empty string, got {value!r}")

    cleaned = value.replace("-", "").replace("/", "").strip()
    if not _EXPIRY_RX.match(cleaned):
        raise NormalizationError(f"expiry must be 8 digits after cleaning, got {cleaned!r}")

    try:
        _dt.date(int(cleaned[0:4]), int(cleaned[4:6]), int(cleaned[6:8]))
    except ValueError as e:
        raise NormalizationError(f"invalid calendar date {cleaned!r}: {e}") from e

    return cleaned


def normalize_ticker(value: str | None) -> str:
    """Normalize a ticker to its canonical uppercase form.

    Rejects tickers not in the V1 universe.
    """
    if not value or not isinstance(value, str):
        raise NormalizationError(f"ticker must be a non-empty string, got {value!r}")

    candidate = value.strip().upper()
    if not is_known(candidate):
        raise NormalizationError(f"ticker {candidate!r} not in V1 universe")

    return candidate


def resolve_multiplier(ticker: str) -> int:
    """Return the option contract multiplier for a V1 ticker.

    Raises NormalizationError if the ticker is unknown.
    """
    if not is_known(ticker):
        raise NormalizationError(f"ticker {ticker!r} not in V1 universe")
    return UNIVERSE[ticker].multiplier
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python3.13 -m pytest scripts/tests/test_contract_normalize.py -xvs`
Expected: 17 passed.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/contract_normalize.py scripts/tests/test_contract_normalize.py
git commit -m "feat(execution): add contract normalization helpers

normalize_expiry (YYYYMMDD, rejects invalid calendar dates),
normalize_ticker (V1-universe-aware uppercase), resolve_multiplier.
Pure functions with no I/O. Replaces scattered regex replaces in
OrderTab.tsx, ib_place_order.py, nakedShortGuard.ts in later phases.

Spec: docs/superpowers/specs/2026-04-20-single-leg-hardening-design.md §9
Phase: F0 of order-execution-foundation-master plan"
```

---

## Task 3: TypeScript mirror codegen

**Files:**

- Create: `scripts/infra/dev/generate_universe_ts.py`
- Create: `web/lib/universe.ts` (generated)
- Modify: `web/package.json`
- Create-or-modify: `.gitattributes` (file does not currently exist in
  the repo root — create it if absent, otherwise append)

- [ ] **Step 1: Write the codegen script**

Create `scripts/infra/dev/generate_universe_ts.py`:

```python
#!/usr/bin/env python3
"""Generate web/lib/universe.ts from src/xenon/execution/universe.py.

Runs as a prebuild/predev/pretest hook from web/package.json. The TS
file is marked generated in .gitattributes. Do not hand-edit.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Self-sufficient path setup — repo does not install `xenon` as an
# editable package. This script runs from npm hooks where PYTHONPATH
# is not guaranteed.
_HERE = Path(__file__).resolve()
# parents[0]=dev, [1]=infra, [2]=scripts, [3]=repo root
_REPO_ROOT = _HERE.parents[3]
assert (_REPO_ROOT / "web" / "package.json").exists(), (
    f"repo root detection failed; resolved {_REPO_ROOT} but no web/package.json there"
)
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from xenon.execution.universe import UNIVERSE, INDEX_UNIVERSE  # noqa: E402

HEADER = """\
// AUTO-GENERATED by scripts/infra/dev/generate_universe_ts.py
// Do not hand-edit. Source of truth: src/xenon/execution/universe.py
// Regenerate with: python3.13 scripts/infra/dev/generate_universe_ts.py

export type UniverseType = "INDEX" | "ETF";

export interface UniverseEntry {
  readonly ticker: string;
  readonly type: UniverseType;
  readonly isIndex: boolean;
  readonly cashSettled: boolean;
  readonly multiplier: number;
  readonly k1: boolean;
}

"""


def render_entry(ticker: str) -> str:
    e = UNIVERSE[ticker]
    return (
        f'  {ticker}: {{ ticker: "{e.ticker}", type: "{e.type}", '
        f"isIndex: {str(e.is_index).lower()}, "
        f"cashSettled: {str(e.cash_settled).lower()}, "
        f"multiplier: {e.multiplier}, "
        f"k1: {str(e.k1).lower()} }},"
    )


def render() -> str:
    lines = [HEADER, "export const UNIVERSE: Readonly<Record<string, UniverseEntry>> = {"]
    for ticker in sorted(UNIVERSE.keys()):
        lines.append(render_entry(ticker))
    lines.append("};")
    lines.append("")
    lines.append(
        f"export const INDEX_UNIVERSE: ReadonlySet<string> = new Set({sorted(INDEX_UNIVERSE)!r});"
    )
    lines.append("")
    lines.append("export function isKnown(ticker: string): boolean {")
    lines.append("  return Object.prototype.hasOwnProperty.call(UNIVERSE, ticker);")
    lines.append("}")
    lines.append("")
    lines.append("export function isIndex(ticker: string): boolean {")
    lines.append("  const entry = UNIVERSE[ticker];")
    lines.append("  if (!entry) throw new Error(`ticker ${ticker} not in V1 universe`);")
    lines.append("  return entry.isIndex;")
    lines.append("}")
    lines.append("")
    lines.append("export function getMultiplier(ticker: string): number {")
    lines.append("  const entry = UNIVERSE[ticker];")
    lines.append("  if (!entry) throw new Error(`ticker ${ticker} not in V1 universe`);")
    lines.append("  return entry.multiplier;")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    out_path = _REPO_ROOT / "web" / "lib" / "universe.ts"
    out_path.write_text(render())
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 2: Run the codegen script**

Run: `python3.13 scripts/infra/dev/generate_universe_ts.py`
Expected output: `wrote /Users/chenxi/projects/xenon/web/lib/universe.ts`

- [ ] **Step 3: Verify the generated file**

Run: `head -30 web/lib/universe.ts`

Expected: a header marking it generated, `UniverseEntry` interface, `UNIVERSE` const with all 9 tickers sorted alphabetically, `INDEX_UNIVERSE` set, three helper functions.

- [ ] **Step 4: Typecheck**

Run: `cd web && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 5: Mark the file generated**

Create `.gitattributes` at the repo root (Claude verified the repo does
not currently have one). If this plan step runs after a concurrent
change added a `.gitattributes`, append instead of overwriting.

Content of the new file:

```
web/lib/universe.ts linguist-generated=true
```

- [ ] **Step 6: Add codegen hooks to `web/package.json`**

The primary frontend workflows in this repo are `npm run dev` and
`npm test` (see `web/CLAUDE.md`), not `npm run build`. A prebuild-only
hook leaves the checked-in TS mirror stale during local dev and tests.
Therefore add hooks for dev, test, typecheck, and build.

Read `web/package.json` and in the `"scripts"` section add:

```json
"universe:gen": "python3.13 ../scripts/infra/dev/generate_universe_ts.py",
"predev": "npm run universe:gen",
"prebuild": "npm run universe:gen",
"pretest": "npm run universe:gen",
"pretypecheck": "npm run universe:gen"
```

If any of `predev` / `prebuild` / `pretest` / `pretypecheck` already
exist, chain them: `"prebuild": "existing-command && npm run universe:gen"`.
Do not replace an existing hook.

**Note on CI:** Vercel / Next.js production builds run `npm run build`
in an environment that must have `python3.13` available. If that is
not the case for your CI, add a `python3` install step before `npm run
build`, or pre-bake the generated `web/lib/universe.ts` into the
commit (the drift regression in Task 5 will catch staleness). The
repo convention from `web/package.json` is that Python is available
(other `dev` / `ib:start` scripts also invoke `python3.13`).

- [ ] **Step 7: Commit**

```bash
git add scripts/infra/dev/generate_universe_ts.py web/lib/universe.ts .gitattributes web/package.json
git commit -m "feat(universe): codegen TS mirror from Python registry

scripts/infra/dev/generate_universe_ts.py renders web/lib/universe.ts
from src/xenon/execution/universe.py. Runs as prebuild hook. Mirror
is marked generated in .gitattributes to discourage hand-edits.

Prevents the single-source-of-truth rule from breaking as Python and
TS evolve independently.

Phase: F0 of order-execution-foundation-master plan"
```

---

## Task 4: Contract test — generated TS matches Python

**Files:**

- Create: `web/tests/universe-mirror.test.ts`

This test locks the generated TS against a frozen fixture so any drift
between the two gets caught at CI time, not at runtime in a naked-short
check.

- [ ] **Step 1: Write failing test**

Create `web/tests/universe-mirror.test.ts`:

```typescript
import { describe, expect, it } from "vitest";

import {
  UNIVERSE,
  INDEX_UNIVERSE,
  isKnown,
  isIndex,
  getMultiplier,
} from "../lib/universe";

describe("universe TS mirror", () => {
  it("contains exactly the nine V1 tickers", () => {
    expect(new Set(Object.keys(UNIVERSE))).toEqual(
      new Set(["SPX", "NDX", "RUT", "SPY", "QQQ", "IWM", "GLD", "USO", "SIL"]),
    );
  });

  it("has SPX/NDX/RUT in INDEX_UNIVERSE", () => {
    expect(INDEX_UNIVERSE).toEqual(new Set(["NDX", "RUT", "SPX"]));
  });

  it.each(["SPX", "NDX", "RUT"] as const)(
    "%s is cash-settled index with multiplier 100",
    (ticker) => {
      const e = UNIVERSE[ticker];
      expect(e.isIndex).toBe(true);
      expect(e.cashSettled).toBe(true);
      expect(e.multiplier).toBe(100);
      expect(e.type).toBe("INDEX");
    },
  );

  it.each(["SPY", "QQQ", "IWM", "GLD", "USO", "SIL"] as const)(
    "%s is ETF, deliverable, multiplier 100",
    (ticker) => {
      const e = UNIVERSE[ticker];
      expect(e.isIndex).toBe(false);
      expect(e.cashSettled).toBe(false);
      expect(e.multiplier).toBe(100);
      expect(e.type).toBe("ETF");
    },
  );

  it("USO is flagged k1, others are not", () => {
    expect(UNIVERSE.USO.k1).toBe(true);
    for (const t of [
      "SPX",
      "NDX",
      "RUT",
      "SPY",
      "QQQ",
      "IWM",
      "GLD",
      "SIL",
    ] as const) {
      expect(UNIVERSE[t].k1).toBe(false);
    }
  });

  it("isKnown returns true for V1 tickers and false for others", () => {
    expect(isKnown("SPX")).toBe(true);
    expect(isKnown("AAPL")).toBe(false);
  });

  it("isIndex throws for unknown ticker", () => {
    expect(() => isIndex("AAPL")).toThrow();
  });

  it("getMultiplier throws for unknown ticker", () => {
    expect(() => getMultiplier("AAPL")).toThrow();
  });
});
```

- [ ] **Step 2: Run test to verify pass (TS mirror already generated in Task 3)**

Run: `cd web && npm test -- universe-mirror.test.ts`
Expected: 15 tests passed. (6 standalone `it` blocks + 3 from `it.each`
over index tickers + 6 from `it.each` over ETF tickers = 15.)

If the test fails, the generated file doesn't match expectations — regenerate via `npm run universe:gen` and re-run.

- [ ] **Step 3: Commit**

```bash
git add web/tests/universe-mirror.test.ts
git commit -m "test(universe): contract test locking TS mirror to Python registry

Guards against drift between src/xenon/execution/universe.py and the
generated web/lib/universe.ts. If a future ticker addition or metadata
change is made in Python without regenerating TS, CI fails here.

Phase: F0 of order-execution-foundation-master plan"
```

---

## Task 5: Python↔TS drift regression

This task adds a cheap CI check that re-runs the codegen in a temp
location and diffs against the checked-in file. If they don't match,
someone edited Python without regenerating TS.

**Files:**

- Create: `scripts/tests/test_universe_ts_drift.py`

- [ ] **Step 1: Write the drift test**

Create `scripts/tests/test_universe_ts_drift.py`:

```python
"""Regression: web/lib/universe.ts must match what generate_universe_ts.py
would produce right now. Guards against silent drift between the Python
registry and the checked-in TS mirror.
"""

from pathlib import Path

from scripts.infra.dev.generate_universe_ts import render


def test_checked_in_universe_ts_matches_codegen():
    repo_root = Path(__file__).resolve().parents[2]
    checked_in = (repo_root / "web" / "lib" / "universe.ts").read_text()
    expected = render()
    assert checked_in == expected, (
        "web/lib/universe.ts is stale. Regenerate with: "
        "python3.13 scripts/infra/dev/generate_universe_ts.py"
    )
```

- [ ] **Step 2: Verify `scripts/infra/dev/` is importable**

Run: `python3.13 -c "from scripts.infra.dev.generate_universe_ts import render; print(len(render()))"`

Expected: prints an integer (the length of the rendered TS). If import
fails with "No module named scripts", add an empty
`scripts/infra/dev/__init__.py` if one doesn't already exist.

- [ ] **Step 3: Run the drift test**

Run: `python3.13 -m pytest scripts/tests/test_universe_ts_drift.py -xvs`
Expected: 1 passed.

- [ ] **Step 4: Commit**

```bash
git add scripts/tests/test_universe_ts_drift.py
git commit -m "test(universe): CI regression against Python/TS mirror drift

Re-runs the codegen in-process and diffs against the checked-in
web/lib/universe.ts. Fails CI if the Python registry was updated
without regenerating the TS mirror.

Phase: F0 of order-execution-foundation-master plan"
```

---

## Task 6: Phase checkpoint

- [ ] **Step 1: Run all Python tests for this phase**

Run:

```bash
python3.13 -m pytest scripts/tests/test_universe.py scripts/tests/test_contract_normalize.py scripts/tests/test_universe_ts_drift.py -v
```

Expected: 37 tests passed, 0 failed.
(19 from test_universe.py + 17 from test_contract_normalize.py + 1 from test_universe_ts_drift.py.)

- [ ] **Step 2: Run all JS tests for this phase**

Run: `cd web && npm test -- universe-mirror.test.ts`
Expected: all tests passed.

- [ ] **Step 3: Coverage spot-check**

Run:

```bash
python3.13 -m pytest scripts/tests/test_universe.py scripts/tests/test_contract_normalize.py --cov=xenon.execution.universe --cov=xenon.execution.contract_normalize --cov-report=term-missing
```

Expected: both modules ≥95% coverage.

- [ ] **Step 4: Verify no unintended callers were introduced**

F0 is a pure addition. The expected consumers at this phase are:

- `scripts/tests/test_universe.py`
- `scripts/tests/test_contract_normalize.py`
- `scripts/tests/test_universe_ts_drift.py`
- `scripts/infra/dev/generate_universe_ts.py` (codegen; imports
  `UNIVERSE` and `INDEX_UNIVERSE` from `xenon.execution.universe`)

Verify:

```bash
grep -rn "from xenon.execution.universe" src/ scripts/ 2>/dev/null
grep -rn "from xenon.execution.contract_normalize" src/ scripts/ 2>/dev/null
```

Expected matches for `universe`: the three test files + the codegen
script (4 matches total).
Expected matches for `contract_normalize`: only `test_contract_normalize.py`.

Anything else is an unintended caller and the plan step fails.

- [ ] **Step 5: Typecheck frontend**

Run: `cd web && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 6: Run full existing test suites to verify no regressions**

Run:

```bash
python3.13 scripts/infra/dev/run_pytest_affected.py
```

Expected: existing suite passes (F0 adds code; it doesn't modify any
existing module).

Run: `cd web && npm test`
Expected: all existing Vitest suites pass.

- [ ] **Step 7: Independent review on the branch**

Run the project's `/codex-review` slash-command skill (the Codex +
Gemini + Claude tribunal) against the uncommitted diff or the open
PR. Alternatively, use the Anthropic `superpowers:requesting-code-review`
skill if the project-local tribunal is unavailable.

Exit condition: one independent review pass with no unresolved
CRITICAL or IMPORTANT consensus items. MINOR items may be accepted
if purely cosmetic. Review output archived in the PR thread.

- [ ] **Step 8: Open PR, merge to master, confirm clean**

```bash
gh pr create --title "F0: universe registry + contract normalize" --body "$(cat <<'EOF'
## Summary
Phase F0 of the Order Execution Foundation master plan.

- Adds `src/xenon/execution/universe.py` — 9-ticker V1 registry (SPX/NDX/RUT index; SPY/QQQ/IWM/GLD/USO/SIL ETF)
- Adds `src/xenon/execution/contract_normalize.py` — expiry/ticker/multiplier helpers
- Generates `web/lib/universe.ts` from the Python registry (prebuild hook)
- CI regression guards against drift between Python and TS

No behavior change to live order paths. Subsequent phases (F1 audit, F2 preflight) will import these modules.

## Test plan
- [x] Python unit tests (37 tests, coverage ≥95%)
- [x] TS mirror contract test
- [x] Python↔TS drift test
- [x] Frontend typecheck
- [x] Full Python + JS test suites pass (no regressions)
- [x] Tribunal review passed
EOF
)"
```

After merge to master: phase F0 is complete. Move to F1 (audit parity).

---

## Self-review checklist

1. **Spec coverage (§2 + §9 of the single-leg hardening spec):**
   - §2 nine-ticker universe → Task 1 ✓
   - §2 `is_index`, `cash_settled`, `multiplier`, `k1` metadata → Task 1 ✓
   - §2 `INDEX_UNIVERSE` derived set → Task 1 ✓
   - §2 TS mirror generated from Python → Tasks 3+4 ✓
   - §9 expiry canonicalization `YYYYMMDD` → Task 2 ✓
   - §9 symbol canonicalization + multiplier lookup → Task 2 ✓
   - §9 build-time check for local reimplementation → deferred to
     F1/F2 (where the existing call-sites get updated)

2. **Placeholders:** none — every code block is complete.

3. **Type consistency:**
   - `UniverseEntry` dataclass fields match TS `UniverseEntry` interface
     (ticker/type/isIndex/cashSettled/multiplier/k1) — ✓
   - `NormalizationError` raised from all three normalize\_\* helpers — ✓
   - `is_known`, `is_index`, `get_multiplier` Python ↔ `isKnown`,
     `isIndex`, `getMultiplier` TS — ✓ (sed: Python snake_case, TS
     camelCase, same semantics)

4. **DRY/YAGNI/TDD/frequent commits:**
   - Every task is red → green → commit ✓
   - Six commits in this phase ✓
   - No speculative fields (e.g. `underlying_exchange`) — added only in
     F2 if preflight needs them
