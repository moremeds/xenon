# Sub-Plan 1: Scanner Foundation (`scanner_lib/`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract shared scanner primitives into `scripts/scanner_lib/` so both `uw_scan` and the new `trend_scan` can reuse them.

**Architecture:** Move generic models, universe loading, parallel execution, JSON caching, and base scoring into a shared library. Refactor `uw_scan_lib` to import from `scanner_lib` instead of owning these primitives. Zero behavior change — all existing `uw_scan` tests must pass unchanged.

**Tech Stack:** Python 3.14, pytest, dataclasses

**Spec:** `docs/superpowers/specs/2026-04-10-trend-scanner-design.md` (Architecture section)

---

## File Structure

```
scripts/
├── scanner_lib/
│   ├── __init__.py          # CREATE — docstring + public exports
│   ├── models.py            # CREATE — BaseScanCandidate, BaseSignalHit, BaseContextFlag
│   ├── universe.py          # CREATE — UniverseLoader with pluggable sources
│   ├── executor.py          # CREATE — ParallelFetcher wrapping ThreadPoolExecutor
│   ├── cache.py             # CREATE — JSONCacheWriter (extracted from api/server.py _write_cache)
│   └── scoring.py           # CREATE — weighted_composite_score(), min_threshold_gate()
├── uw_scan_lib/
│   ├── models.py            # MODIFY — import base classes from scanner_lib, extend
│   ├── universe.py          # MODIFY — delegate to scanner_lib UniverseLoader
│   ├── ranking.py           # MODIFY — use scanner_lib scoring helpers
│   └── (signals/, context/, confluence.py — UNCHANGED)
└── uw_scan.py               # MODIFY — update imports only
```

---

### Task 1: Base Models (`scanner_lib/models.py`)

**Files:**

- Create: `scripts/scanner_lib/__init__.py`
- Create: `scripts/scanner_lib/models.py`
- Test: `scripts/tests/test_scanner_lib_models.py`

- [ ] **Step 1: Write failing tests for base models**

```python
# scripts/tests/test_scanner_lib_models.py
"""Tests for scanner_lib base models."""
from __future__ import annotations

import pytest


def test_base_signal_hit_creation():
    from scripts.scanner_lib.models import BaseSignalHit

    hit = BaseSignalHit(
        ticker="AAPL",
        signal_type="trend_ma",
        score=0.85,
        evidence={"ma_20": 185.0, "ma_50": 180.0},
    )
    assert hit.ticker == "AAPL"
    assert hit.signal_type == "trend_ma"
    assert hit.score == 0.85
    assert hit.evidence["ma_20"] == 185.0


def test_base_signal_hit_is_frozen():
    from scripts.scanner_lib.models import BaseSignalHit

    hit = BaseSignalHit(ticker="AAPL", signal_type="trend", score=0.5, evidence={})
    with pytest.raises(AttributeError):
        hit.score = 0.9  # type: ignore[misc]


def test_base_signal_hit_score_bounds():
    from scripts.scanner_lib.models import BaseSignalHit

    with pytest.raises(ValueError, match="score must be between 0 and 1"):
        BaseSignalHit(ticker="AAPL", signal_type="trend", score=1.5, evidence={})

    with pytest.raises(ValueError, match="score must be between 0 and 1"):
        BaseSignalHit(ticker="AAPL", signal_type="trend", score=-0.1, evidence={})


def test_base_context_flag_creation():
    from scripts.scanner_lib.models import BaseContextFlag

    flag = BaseContextFlag(ticker="AAPL", layer="news", label="earnings_soon", value=7.0)
    assert flag.ticker == "AAPL"
    assert flag.layer == "news"
    assert flag.label == "earnings_soon"
    assert flag.value == 7.0


def test_base_scan_candidate_creation():
    from scripts.scanner_lib.models import BaseScanCandidate

    c = BaseScanCandidate(
        ticker="NVDA",
        direction="bullish",
        final_score=0.82,
        scores={"trend": 0.91, "structure": 0.75},
    )
    assert c.ticker == "NVDA"
    assert c.direction == "bullish"
    assert c.final_score == 0.82
    assert c.scores["trend"] == 0.91


def test_base_scan_candidate_default_fields():
    from scripts.scanner_lib.models import BaseScanCandidate

    c = BaseScanCandidate(
        ticker="AAPL",
        direction="bearish",
        final_score=0.5,
        scores={},
    )
    assert c.flags == []
    assert c.summaries == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_scanner_lib_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.scanner_lib'`

- [ ] **Step 3: Create scanner_lib package and base models**

```python
# scripts/scanner_lib/__init__.py
"""scanner_lib: shared foundation for all Xenon scanners."""
```

```python
# scripts/scanner_lib/models.py
"""Base models shared across all scanners."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class BaseSignalHit:
    """A single signal detection result."""

    ticker: str
    signal_type: str
    score: float  # 0.0 – 1.0
    evidence: dict[str, Any]

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score must be between 0 and 1, got {self.score}")


@dataclass(frozen=True)
class BaseContextFlag:
    """Non-scoring contextual annotation."""

    ticker: str
    layer: str
    label: str
    value: float


@dataclass
class BaseScanCandidate:
    """A ranked scan result."""

    ticker: str
    direction: Literal["bullish", "bearish"]
    final_score: float
    scores: dict[str, float]
    flags: list[str] = field(default_factory=list)
    summaries: dict[str, str] = field(default_factory=dict)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_scanner_lib_models.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/scanner_lib/__init__.py scripts/scanner_lib/models.py scripts/tests/test_scanner_lib_models.py
git commit -m "feat(scanner_lib): add base models for shared scanner foundation"
```

---

### Task 2: Parallel Executor (`scanner_lib/executor.py`)

**Files:**

- Create: `scripts/scanner_lib/executor.py`
- Test: `scripts/tests/test_scanner_lib_executor.py`

- [ ] **Step 1: Write failing tests**

```python
# scripts/tests/test_scanner_lib_executor.py
"""Tests for scanner_lib parallel executor."""
from __future__ import annotations

import time

import pytest


def test_parallel_fetch_basic():
    from scripts.scanner_lib.executor import parallel_fetch

    def double(x: int) -> int:
        return x * 2

    results = parallel_fetch(items=[1, 2, 3, 4, 5], fn=double, max_workers=3)
    assert sorted(results) == [2, 4, 6, 8, 10]


def test_parallel_fetch_preserves_order_by_input():
    from scripts.scanner_lib.executor import parallel_fetch

    def identity(x: str) -> str:
        return x

    results = parallel_fetch(items=["a", "b", "c"], fn=identity, max_workers=2)
    assert results == ["a", "b", "c"]


def test_parallel_fetch_handles_exceptions():
    from scripts.scanner_lib.executor import parallel_fetch

    def fail_on_b(x: str) -> str:
        if x == "b":
            raise ValueError("bad ticker")
        return x

    results = parallel_fetch(items=["a", "b", "c"], fn=fail_on_b, max_workers=2)
    # Failures are skipped, not raised
    assert results == ["a", "c"]


def test_parallel_fetch_empty_input():
    from scripts.scanner_lib.executor import parallel_fetch

    results = parallel_fetch(items=[], fn=lambda x: x, max_workers=2)
    assert results == []


def test_parallel_fetch_actually_parallel():
    from scripts.scanner_lib.executor import parallel_fetch

    def slow(x: int) -> int:
        time.sleep(0.1)
        return x

    start = time.monotonic()
    results = parallel_fetch(items=list(range(10)), fn=slow, max_workers=10)
    elapsed = time.monotonic() - start
    assert len(results) == 10
    # 10 items at 0.1s each, 10 workers → should complete in ~0.1-0.3s, not 1s
    assert elapsed < 0.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_scanner_lib_executor.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement parallel executor**

```python
# scripts/scanner_lib/executor.py
"""Parallel fetch utility for scanner pipelines."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")


def parallel_fetch(
    *,
    items: list[T],
    fn: Callable[[T], R],
    max_workers: int = 10,
) -> list[R]:
    """Run fn on each item in parallel, preserving input order. Failures are logged and skipped."""
    if not items:
        return []

    results: dict[int, R] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {pool.submit(fn, item): i for i, item in enumerate(items)}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception:
                logger.warning("parallel_fetch failed for item %s", items[idx], exc_info=True)

    return [results[i] for i in sorted(results)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_scanner_lib_executor.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/scanner_lib/executor.py scripts/tests/test_scanner_lib_executor.py
git commit -m "feat(scanner_lib): add parallel executor utility"
```

---

### Task 3: JSON Cache Writer (`scanner_lib/cache.py`)

**Files:**

- Create: `scripts/scanner_lib/cache.py`
- Test: `scripts/tests/test_scanner_lib_cache.py`

- [ ] **Step 1: Write failing tests**

```python
# scripts/tests/test_scanner_lib_cache.py
"""Tests for scanner_lib JSON cache writer."""
from __future__ import annotations

import json

import pytest


def test_write_cache_creates_file(tmp_path):
    from scripts.scanner_lib.cache import write_json_cache

    path = tmp_path / "scan.json"
    data = {"scan_id": "test_001", "candidates": []}
    write_json_cache(path, data)

    assert path.exists()
    loaded = json.loads(path.read_text())
    assert loaded["scan_id"] == "test_001"


def test_write_cache_overwrites_existing(tmp_path):
    from scripts.scanner_lib.cache import write_json_cache

    path = tmp_path / "scan.json"
    write_json_cache(path, {"version": 1})
    write_json_cache(path, {"version": 2})

    loaded = json.loads(path.read_text())
    assert loaded["version"] == 2


def test_write_cache_creates_parent_dirs(tmp_path):
    from scripts.scanner_lib.cache import write_json_cache

    path = tmp_path / "nested" / "dir" / "scan.json"
    write_json_cache(path, {"ok": True})
    assert path.exists()


def test_read_cache_returns_data(tmp_path):
    from scripts.scanner_lib.cache import read_json_cache, write_json_cache

    path = tmp_path / "scan.json"
    write_json_cache(path, {"ticker": "AAPL"})
    data = read_json_cache(path)
    assert data is not None
    assert data["ticker"] == "AAPL"


def test_read_cache_returns_none_for_missing(tmp_path):
    from scripts.scanner_lib.cache import read_json_cache

    data = read_json_cache(tmp_path / "nonexistent.json")
    assert data is None


def test_read_cache_staleness(tmp_path):
    import time

    from scripts.scanner_lib.cache import read_json_cache, write_json_cache

    path = tmp_path / "scan.json"
    write_json_cache(path, {"val": 1})
    data = read_json_cache(path, max_age_secs=9999)
    assert data is not None
    assert data["val"] == 1

    # Verify stale detection works (we test the is_stale helper directly)
    from scripts.scanner_lib.cache import is_stale

    assert not is_stale(path, max_age_secs=9999)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_scanner_lib_cache.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement cache module**

```python
# scripts/scanner_lib/cache.py
"""JSON cache read/write for scanner output files."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def write_json_cache(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically via temp file + os.replace()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".cache_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_json_cache(path: Path, *, max_age_secs: float = 0) -> Optional[dict[str, Any]]:
    """Read cached JSON. Returns None if file missing. Ignores staleness if max_age_secs=0."""
    if not path.exists():
        return None
    if max_age_secs > 0 and is_stale(path, max_age_secs=max_age_secs):
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read cache %s: %s", path, e)
        return None


def is_stale(path: Path, *, max_age_secs: float) -> bool:
    """Check if a cache file is older than max_age_secs."""
    try:
        mtime = path.stat().st_mtime
        return (time.time() - mtime) > max_age_secs
    except OSError:
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_scanner_lib_cache.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/scanner_lib/cache.py scripts/tests/test_scanner_lib_cache.py
git commit -m "feat(scanner_lib): add atomic JSON cache reader/writer"
```

---

### Task 4: Base Scoring Utilities (`scanner_lib/scoring.py`)

**Files:**

- Create: `scripts/scanner_lib/scoring.py`
- Test: `scripts/tests/test_scanner_lib_scoring.py`

- [ ] **Step 1: Write failing tests**

```python
# scripts/tests/test_scanner_lib_scoring.py
"""Tests for scanner_lib scoring utilities."""
from __future__ import annotations

import pytest


def test_weighted_composite_basic():
    from scripts.scanner_lib.scoring import weighted_composite

    scores = {"trend": 0.8, "structure": 0.6, "vol": 0.5, "flow": 0.7}
    weights = {"trend": 0.35, "structure": 0.25, "vol": 0.20, "flow": 0.20}

    result = weighted_composite(scores, weights)
    expected = (0.8 * 0.35) + (0.6 * 0.25) + (0.5 * 0.20) + (0.7 * 0.20)
    assert abs(result - expected) < 1e-9


def test_weighted_composite_missing_score_treated_as_zero():
    from scripts.scanner_lib.scoring import weighted_composite

    scores = {"trend": 0.8}
    weights = {"trend": 0.35, "structure": 0.25}
    result = weighted_composite(scores, weights)
    assert abs(result - 0.8 * 0.35) < 1e-9


def test_weighted_composite_weights_must_sum_to_one():
    from scripts.scanner_lib.scoring import weighted_composite

    scores = {"a": 0.5}
    weights = {"a": 0.5, "b": 0.3}  # sums to 0.8
    with pytest.raises(ValueError, match="weights must sum to 1.0"):
        weighted_composite(scores, weights)


def test_passes_min_thresholds_all_pass():
    from scripts.scanner_lib.scoring import passes_min_thresholds

    scores = {"trend": 0.6, "structure": 0.5}
    thresholds = {"trend": 0.4, "structure": 0.3}
    assert passes_min_thresholds(scores, thresholds) is True


def test_passes_min_thresholds_one_fails():
    from scripts.scanner_lib.scoring import passes_min_thresholds

    scores = {"trend": 0.35, "structure": 0.5}
    thresholds = {"trend": 0.4, "structure": 0.3}
    assert passes_min_thresholds(scores, thresholds) is False


def test_passes_min_thresholds_missing_score_fails():
    from scripts.scanner_lib.scoring import passes_min_thresholds

    scores = {"trend": 0.6}
    thresholds = {"trend": 0.4, "structure": 0.3}
    # Missing "structure" score → treated as 0 → fails threshold
    assert passes_min_thresholds(scores, thresholds) is False


def test_normalize_score_clamps():
    from scripts.scanner_lib.scoring import normalize_score

    assert normalize_score(1.5) == 1.0
    assert normalize_score(-0.3) == 0.0
    assert normalize_score(0.5) == 0.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_scanner_lib_scoring.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement scoring utilities**

```python
# scripts/scanner_lib/scoring.py
"""Scoring utilities shared across scanners."""
from __future__ import annotations


def weighted_composite(scores: dict[str, float], weights: dict[str, float]) -> float:
    """Compute weighted composite score. Weights must sum to 1.0. Missing scores treated as 0."""
    total_weight = sum(weights.values())
    if abs(total_weight - 1.0) > 0.01:
        raise ValueError(f"weights must sum to 1.0, got {total_weight:.3f}")

    return sum(scores.get(k, 0.0) * w for k, w in weights.items())


def passes_min_thresholds(scores: dict[str, float], thresholds: dict[str, float]) -> bool:
    """Check that every threshold key has a score >= the threshold value."""
    return all(scores.get(k, 0.0) >= v for k, v in thresholds.items())


def normalize_score(value: float) -> float:
    """Clamp a value to [0.0, 1.0]."""
    return max(0.0, min(1.0, value))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_scanner_lib_scoring.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/scanner_lib/scoring.py scripts/tests/test_scanner_lib_scoring.py
git commit -m "feat(scanner_lib): add weighted composite scoring and threshold gates"
```

---

### Task 5: Universe Loader (`scanner_lib/universe.py`)

**Files:**

- Create: `scripts/scanner_lib/universe.py`
- Test: `scripts/tests/test_scanner_lib_universe.py`

- [ ] **Step 1: Write failing tests**

```python
# scripts/tests/test_scanner_lib_universe.py
"""Tests for scanner_lib universe loader."""
from __future__ import annotations

import json

import pytest


def test_load_from_json_file(tmp_path):
    from scripts.scanner_lib.universe import load_tickers_from_json

    path = tmp_path / "tickers.json"
    path.write_text(json.dumps(["AAPL", "MSFT", "GOOG"]))
    result = load_tickers_from_json(path)
    assert result == ["AAPL", "GOOG", "MSFT"]  # sorted


def test_load_from_json_file_with_dict_rows(tmp_path):
    from scripts.scanner_lib.universe import load_tickers_from_json

    path = tmp_path / "tickers.json"
    path.write_text(json.dumps([{"ticker": "AAPL"}, {"ticker": "MSFT"}]))
    result = load_tickers_from_json(path)
    assert result == ["AAPL", "MSFT"]


def test_load_from_json_missing_file(tmp_path):
    from scripts.scanner_lib.universe import load_tickers_from_json

    result = load_tickers_from_json(tmp_path / "nonexistent.json")
    assert result == []


def test_dedup_and_normalize():
    from scripts.scanner_lib.universe import dedup_and_normalize

    tickers = ["aapl", "MSFT", "AAPL", "goog", "msft"]
    result = dedup_and_normalize(tickers)
    assert result == ["AAPL", "GOOG", "MSFT"]  # uppercased, deduped, sorted


def test_union_sources():
    from scripts.scanner_lib.universe import union_sources

    source_a = ["AAPL", "MSFT"]
    source_b = ["GOOG", "AAPL"]
    source_c = ["NVDA", "MSFT"]

    result = union_sources(source_a, source_b, source_c)
    assert result == ["AAPL", "GOOG", "MSFT", "NVDA"]


def test_union_sources_empty():
    from scripts.scanner_lib.universe import union_sources

    result = union_sources([], [], [])
    assert result == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_scanner_lib_universe.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement universe loader**

```python
# scripts/scanner_lib/universe.py
"""Universe loading and merging utilities for scanners."""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_tickers_from_json(path: Path) -> list[str]:
    """Load tickers from a JSON file. Supports list of strings or list of {ticker: str} dicts."""
    if not path.exists():
        logger.warning("Universe file not found: %s", path)
        return []
    try:
        data = json.loads(path.read_text())
        tickers: list[str] = []
        for item in data:
            if isinstance(item, str):
                tickers.append(item)
            elif isinstance(item, dict) and "ticker" in item:
                tickers.append(item["ticker"])
        return dedup_and_normalize(tickers)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load universe from %s: %s", path, e)
        return []


def dedup_and_normalize(tickers: list[str]) -> list[str]:
    """Uppercase, deduplicate, and sort tickers."""
    seen: set[str] = set()
    result: list[str] = []
    for t in tickers:
        upper = t.upper().strip()
        if upper and upper not in seen:
            seen.add(upper)
            result.append(upper)
    return sorted(result)


def union_sources(*sources: list[str]) -> list[str]:
    """Merge multiple ticker lists into a deduplicated, sorted union."""
    combined: list[str] = []
    for source in sources:
        combined.extend(source)
    return dedup_and_normalize(combined)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_scanner_lib_universe.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/scanner_lib/universe.py scripts/tests/test_scanner_lib_universe.py
git commit -m "feat(scanner_lib): add universe loader with multi-source union"
```

---

### Task 6: Refactor `uw_scan_lib` to Use `scanner_lib`

**Files:**

- Modify: `scripts/uw_scan_lib/models.py`
- Modify: `scripts/uw_scan_lib/universe.py`
- Existing tests: `scripts/tests/test_uw_scan*.py`

This task refactors `uw_scan_lib` to import shared primitives from `scanner_lib` while preserving its existing API. No behavior change.

- [ ] **Step 1: Run all existing uw_scan tests to establish baseline**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_uw_scan*.py -v`
Expected: All pass (record count)

- [ ] **Step 2: Update `uw_scan_lib/models.py` — keep existing names as aliases**

The existing `uw_scan_lib` models (`SignalHit`, `ContextFlag`, `ScanCandidate`) have a different shape from the new base models (e.g., `SignalHit` has `tier` and `freshness` fields). Keep them as-is — they are scanner-specific extensions, not candidates for extraction. No changes needed to this file.

The shared `scanner_lib` models are a generic foundation. `uw_scan_lib` models remain independent — they predate the shared layer and have fields (tier, freshness, confluence) that are UW-specific.

- [ ] **Step 3: Update `uw_scan_lib/universe.py` to delegate to `scanner_lib`**

Read `scripts/uw_scan_lib/universe.py` first. Then update to delegate:

```python
# scripts/uw_scan_lib/universe.py
"""uw-scan universe loading — delegates to scanner_lib for core utilities."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal, Optional

from scripts.scanner_lib.universe import dedup_and_normalize, load_tickers_from_json

logger = logging.getLogger(__name__)

Mode = Literal["watchlist", "targeted"]


def load_universe(
    *,
    mode: Mode,
    tickers: Optional[list[str]] = None,
    watchlist_path: str = "data/watchlist.json",
) -> list[str]:
    """Load scan universe. 'targeted' uses explicit list; 'watchlist' reads JSON file."""
    if mode == "targeted":
        return dedup_and_normalize(tickers or [])
    elif mode == "watchlist":
        path = Path(watchlist_path)
        if not path.exists():
            logger.warning("Watchlist not found: %s", path)
            return []
        try:
            raw = json.loads(path.read_text())
            data = raw.get("tickers", raw) if isinstance(raw, dict) else raw
            tickers_list: list[str] = []
            for item in data:
                if isinstance(item, str):
                    tickers_list.append(item)
                elif isinstance(item, dict) and "ticker" in item:
                    tickers_list.append(item["ticker"])
            return dedup_and_normalize(tickers_list)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load watchlist: %s", e)
            return []
    else:
        raise ValueError(f"Unsupported mode: {mode}")
```

- [ ] **Step 4: Run all uw_scan tests to verify zero behavior change**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_uw_scan*.py -v`
Expected: Same count, all pass — zero regressions

- [ ] **Step 5: Commit**

```bash
git add scripts/uw_scan_lib/universe.py
git commit -m "refactor(uw_scan_lib): delegate universe loading to scanner_lib"
```

---

### Task 7: Run Full Test Suite — Verify No Regressions

- [ ] **Step 1: Run all scanner_lib tests**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_scanner_lib*.py -v`
Expected: All 24 tests pass (6 models + 5 executor + 6 cache + 7 scoring)

- [ ] **Step 2: Run all uw_scan tests**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/test_uw_scan*.py -v`
Expected: All pass, same count as baseline

- [ ] **Step 3: Final commit if any cleanup needed**

If no changes needed, skip this step.
