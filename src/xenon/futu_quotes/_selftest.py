"""``xenon-futu-quotes --self-test`` — fake-OpenD scenario over a real
loopback WebSocket.

No live OpenD needed: a scripted ``OpenDLike`` fake drives the service
through connect → want/assign → tick/batch → OpenD loss, over an actual
``websockets`` server + client on 127.0.0.1 (ephemeral port). Exercises
the real socket path, JSON framing and eval pump; the live OpenD smoke
test on the mini is a separate manual step (docs/runbooks/futu-quotes.md).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Optional

from xenon.futu_quotes.opend import OpenDDownError
from xenon.futu_quotes.server import FutuQuotesService


class _FakeOpenD:
    """Scripted OpenDLike — connected US session, three snapshot codes."""

    def __init__(self) -> None:
        self.connected = False
        self.subscribed: set[str] = set()
        self.on_quote_rows: Optional[Callable[[list[dict]], None]] = None
        self.on_book_rows: Optional[Callable[[list[dict]], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None
        self.on_reconnected: Optional[Callable[[], None]] = None
        self._snapshots = {
            "US.AAPL": {"code": "US.AAPL", "sec_type": "STOCK"},
            "US.MSFT": {"code": "US.MSFT", "sec_type": "STOCK"},
        }

    def _require_conn(self) -> None:
        if not self.connected:
            raise OpenDDownError("self-test fake is down")

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def get_market_snapshot(self, codes: list[str]) -> list[dict]:
        self._require_conn()
        return [self._snapshots[c] for c in codes if c in self._snapshots]

    async def subscribe(self, codes: list[str]) -> dict[str, Optional[str]]:
        self._require_conn()
        out: dict[str, Optional[str]] = {}
        for c in codes:
            self.subscribed.add(c)
            out[c] = None
        return out

    async def unsubscribe(self, codes: list[str]) -> dict[str, Optional[str]]:
        self._require_conn()
        out: dict[str, Optional[str]] = {}
        for c in codes:
            self.subscribed.discard(c)
            out[c] = None
        return out

    async def query_subscription(self) -> dict:
        self._require_conn()
        return {
            "total_used": len(self.subscribed),
            "own_used": len(self.subscribed),
            "remain": 997,
            "sub_list": {"QUOTE": sorted(self.subscribed)},
        }

    async def get_global_state(self) -> dict:
        self._require_conn()
        return {"market_us": "AFTERNOON"}

    def push(self, rows: list[dict]) -> None:
        if self.on_quote_rows:
            self.on_quote_rows(rows)

    def drop(self) -> None:
        self.connected = False
        if self.on_disconnected:
            self.on_disconnected()


def _ok(step: str, detail: str = "") -> None:
    print(f"  ok  {step}{' — ' + detail if detail else ''}")


async def _wait_frame(
    ws: Any, pred: Callable[[dict], bool], label: str, timeout: float = 8.0
) -> dict:
    """Read frames until ``pred`` matches; raise TimeoutError otherwise."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for {label}")
        frame = json.loads(await asyncio.wait_for(ws.recv(), remaining))
        if pred(frame):
            return frame


async def run_self_test() -> int:
    try:
        from websockets.asyncio.server import serve
        from websockets.asyncio.client import connect
    except ImportError:  # pragma: no cover
        from websockets import connect, serve  # type: ignore

    fake = _FakeOpenD()
    svc = FutuQuotesService(fake)

    print("futu-quotes --self-test (fake OpenD, real loopback WebSocket)")

    try:
        async with serve(
            svc.ws_entry, "127.0.0.1", 0, ping_interval=10, ping_timeout=20
        ) as server:
            port = server.sockets[0].getsockname()[1]  # type: ignore[index]
            _ok("server", f"127.0.0.1:{port}")

            await fake.connect()
            await svc.opend_recovered()
            _ok("opend", "connected, session AFTERNOON, quota pulled")

            async with connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(
                    json.dumps(
                        {
                            "action": "want",
                            "gen": 1,
                            "symbols": ["AAPL", "MSFT", "ZZZZ"],
                        }
                    )
                )
                assign = await _wait_frame(
                    ws,
                    lambda f: f.get("type") == "assign"
                    and set(f.get("ok", [])) == {"AAPL", "MSFT"},
                    "converged assign",
                )
                assert {"symbol": "ZZZZ", "reason": "unknown_symbol"} in assign[
                    "rejected"
                ], assign
                _ok(
                    "assign",
                    f"ok={assign['ok']} rejected={assign['rejected']}",
                )

                fake.push(
                    [
                        {
                            "code": "US.AAPL",
                            "data_date": "2026-10-23",
                            "data_time": "10:30:05",
                            "last_price": 101.25,
                            "volume": 1200,
                        }
                    ]
                )
                batch = await _wait_frame(
                    ws, lambda f: f.get("type") == "batch", "batch frame"
                )
                upd = batch["updates"]["AAPL"]
                assert upd["last"] == 101.25 and upd["source"] == "futu", batch
                assert upd["timestamp"] == "2026-10-23T14:30:05Z", batch
                _ok("batch", f"AAPL last={upd['last']} ts={upd['timestamp']}")

                fake.drop()
                assign = await _wait_frame(
                    ws,
                    lambda f: f.get("type") == "assign" and f.get("ok") == [],
                    "opend_down assign",
                )
                assert {p["reason"] for p in assign["pending"]} == {
                    "opend_down"
                }, assign
                health = await _wait_frame(
                    ws,
                    lambda f: f.get("type") == "health"
                    and f.get("opend_connected") is False,
                    "health opend_connected=false",
                )
                _ok("opend loss", f"health={health}")
    except (AssertionError, TimeoutError, OSError, asyncio.TimeoutError) as exc:
        print(f"SELF-TEST FAIL: {exc}")
        return 1
    print("SELF-TEST PASS")
    return 0
