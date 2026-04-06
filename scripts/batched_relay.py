"""
BatchedPriceRelay — buffers incoming price ticks and flushes them
as a single batch WebSocket message at a configurable interval.

This reduces frontend rendering load by consolidating N individual
tick updates into one {"type": "batch", "updates": {...}} message
per flush cycle. Last-write-wins: if the same symbol ticks twice
within one interval, only the latest value is sent.

Can be used as a standalone relay layer between any tick source
(IB, UW, etc.) and WebSocket clients.
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable, Awaitable, Dict, Any


class BatchedPriceRelay:
    """Buffer price ticks and flush them as batched WS messages."""

    def __init__(self, flush_interval_ms: int = 100) -> None:
        self._flush_interval_s = flush_interval_ms / 1000.0
        self._buffer: Dict[str, Dict[str, Any]] = {}
        self._clients: list[Callable[[str], Awaitable[None]]] = []
        self._running = False

    def add_client(self, send_fn: Callable[[str], Awaitable[None]]) -> None:
        """Register a client send function."""
        self._clients.append(send_fn)

    def remove_client(self, send_fn: Callable[[str], Awaitable[None]]) -> None:
        """Unregister a client send function."""
        self._clients = [c for c in self._clients if c is not send_fn]

    def buffer_tick(self, symbol: str, tick_data: Dict[str, Any]) -> None:
        """Buffer a tick for a symbol (last-write-wins)."""
        self._buffer[symbol] = tick_data

    async def start(self) -> None:
        """Run the flush loop until stop() is called."""
        self._running = True
        while self._running:
            await asyncio.sleep(self._flush_interval_s)
            await self._flush()

    def stop(self) -> None:
        """Signal the flush loop to stop."""
        self._running = False

    async def _flush(self) -> None:
        """Send buffered ticks as a batch and clear the buffer."""
        if not self._buffer:
            return

        # Snapshot and clear
        updates = self._buffer.copy()
        self._buffer.clear()

        message = json.dumps({"type": "batch", "updates": updates})

        for send_fn in self._clients:
            try:
                await send_fn(message)
            except Exception:
                pass  # Client errors shouldn't crash the relay
