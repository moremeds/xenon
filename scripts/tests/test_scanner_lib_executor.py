"""Tests for scanner_lib parallel executor."""

from __future__ import annotations

import time

import pytest


def test_parallel_fetch_basic():
    from scanners._shared.executor import parallel_fetch

    def double(x: int) -> int:
        return x * 2

    results = parallel_fetch(items=[1, 2, 3, 4, 5], fn=double, max_workers=3)
    assert sorted(results) == [2, 4, 6, 8, 10]


def test_parallel_fetch_preserves_order_by_input():
    from scanners._shared.executor import parallel_fetch

    def identity(x: str) -> str:
        return x

    results = parallel_fetch(items=["a", "b", "c"], fn=identity, max_workers=2)
    assert results == ["a", "b", "c"]


def test_parallel_fetch_handles_exceptions():
    from scanners._shared.executor import parallel_fetch

    def fail_on_b(x: str) -> str:
        if x == "b":
            raise ValueError("bad ticker")
        return x

    results = parallel_fetch(items=["a", "b", "c"], fn=fail_on_b, max_workers=2)
    assert results == ["a", "c"]


def test_parallel_fetch_empty_input():
    from scanners._shared.executor import parallel_fetch

    results = parallel_fetch(items=[], fn=lambda x: x, max_workers=2)
    assert results == []


def test_parallel_fetch_actually_parallel():
    from scanners._shared.executor import parallel_fetch

    def slow(x: int) -> int:
        time.sleep(0.1)
        return x

    start = time.monotonic()
    results = parallel_fetch(items=list(range(10)), fn=slow, max_workers=10)
    elapsed = time.monotonic() - start
    assert len(results) == 10
    assert elapsed < 0.5
