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
