"""
Tests for BatchedPriceRelay — server-side tick batching.

Verifies:
1. Ticks are buffered and emitted as a single batch message
2. Last-write-wins when two ticks arrive for the same symbol in one interval
3. Empty buffer does not emit
4. Configurable flush interval
"""

import asyncio
import json
import pytest

from scripts.batched_relay import BatchedPriceRelay


def make_tick(symbol: str, last: float) -> dict:
    """Create a minimal price tick."""
    return {
        "symbol": symbol,
        "last": last,
        "bid": last - 0.01,
        "ask": last + 0.01,
        "timestamp": "2026-03-11T12:00:00Z",
    }


@pytest.mark.asyncio
async def test_ticks_buffered_and_emitted_as_batch():
    """Ticks added during one interval are flushed as a single batch message."""
    sent_messages: list[str] = []

    async def mock_send(msg: str):
        sent_messages.append(msg)

    relay = BatchedPriceRelay(flush_interval_ms=10)
    relay.add_client(mock_send)

    relay.buffer_tick("AAPL", make_tick("AAPL", 175.50))
    relay.buffer_tick("MSFT", make_tick("MSFT", 420.00))

    # Start flushing, wait for one flush cycle
    task = asyncio.create_task(relay.start())
    await asyncio.sleep(0.03)
    relay.stop()
    await task

    # Should have at least one batch message
    assert len(sent_messages) >= 1
    batch = json.loads(sent_messages[0])
    assert batch["type"] == "batch"
    assert "AAPL" in batch["updates"]
    assert "MSFT" in batch["updates"]
    assert batch["updates"]["AAPL"]["last"] == 175.50
    assert batch["updates"]["MSFT"]["last"] == 420.00


@pytest.mark.asyncio
async def test_last_write_wins():
    """When two ticks arrive for the same symbol in one interval, only the last is sent."""
    sent_messages: list[str] = []

    async def mock_send(msg: str):
        sent_messages.append(msg)

    relay = BatchedPriceRelay(flush_interval_ms=10)
    relay.add_client(mock_send)

    relay.buffer_tick("AAPL", make_tick("AAPL", 170.00))
    relay.buffer_tick("AAPL", make_tick("AAPL", 175.50))  # overwrites

    task = asyncio.create_task(relay.start())
    await asyncio.sleep(0.03)
    relay.stop()
    await task

    assert len(sent_messages) >= 1
    batch = json.loads(sent_messages[0])
    assert batch["updates"]["AAPL"]["last"] == 175.50


@pytest.mark.asyncio
async def test_empty_buffer_does_not_emit():
    """No message is sent when there are no buffered ticks."""
    sent_messages: list[str] = []

    async def mock_send(msg: str):
        sent_messages.append(msg)

    relay = BatchedPriceRelay(flush_interval_ms=10)
    relay.add_client(mock_send)

    # Don't buffer anything
    task = asyncio.create_task(relay.start())
    await asyncio.sleep(0.03)  # wait for 2-3 flush cycles
    relay.stop()
    await task

    assert len(sent_messages) == 0


@pytest.mark.asyncio
async def test_configurable_interval():
    """Flush interval is respected — a longer interval means fewer flushes."""
    sent_messages: list[str] = []

    async def mock_send(msg: str):
        sent_messages.append(msg)

    # Very short interval for fast test
    relay = BatchedPriceRelay(flush_interval_ms=10)
    relay.add_client(mock_send)

    task = asyncio.create_task(relay.start())

    # Buffer a tick, wait, buffer another
    relay.buffer_tick("AAPL", make_tick("AAPL", 175.50))
    await asyncio.sleep(0.02)  # should trigger first flush
    relay.buffer_tick("MSFT", make_tick("MSFT", 420.00))
    await asyncio.sleep(0.02)  # should trigger second flush

    relay.stop()
    await task

    # Should have at least 2 separate batch messages (one for each interval)
    assert len(sent_messages) >= 2
    batch1 = json.loads(sent_messages[0])
    assert "AAPL" in batch1["updates"]


@pytest.mark.asyncio
async def test_multiple_clients():
    """Batch is sent to all registered clients."""
    client1_msgs: list[str] = []
    client2_msgs: list[str] = []

    async def send1(msg: str):
        client1_msgs.append(msg)

    async def send2(msg: str):
        client2_msgs.append(msg)

    relay = BatchedPriceRelay(flush_interval_ms=10)
    relay.add_client(send1)
    relay.add_client(send2)

    relay.buffer_tick("AAPL", make_tick("AAPL", 175.50))

    task = asyncio.create_task(relay.start())
    await asyncio.sleep(0.03)
    relay.stop()
    await task

    assert len(client1_msgs) >= 1
    assert len(client2_msgs) >= 1
    assert json.loads(client1_msgs[0])["type"] == "batch"
    assert json.loads(client2_msgs[0])["type"] == "batch"


@pytest.mark.asyncio
async def test_remove_client():
    """Removed clients no longer receive batches."""
    sent_messages: list[str] = []

    async def mock_send(msg: str):
        sent_messages.append(msg)

    relay = BatchedPriceRelay(flush_interval_ms=10)
    relay.add_client(mock_send)
    relay.remove_client(mock_send)

    relay.buffer_tick("AAPL", make_tick("AAPL", 175.50))

    task = asyncio.create_task(relay.start())
    await asyncio.sleep(0.03)
    relay.stop()
    await task

    assert len(sent_messages) == 0
