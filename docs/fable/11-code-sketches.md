# 11. Suggested Code Changes (sketches)

Focused sketches for the highest-priority findings. Diff-style pseudocode — align names
with the real code when implementing.

## §1 Idempotent order command with ambiguity (OP-1 core)

```python
# src/xenon/execution/ib_place_order.py
def place_order(body, ...):
    order = LimitOrder(action, qty, limit_price)
    order.tif = tif
    order.orderRef = body["client_attempt_id"]   # NEW: broker-side correlation key
    trade = client.place_order(contract, order)

    # NEW: early ack line the parent can persist even if we die later
    deadline = time.monotonic() + ACK_WAIT_S      # e.g. 8s, event-driven not blind
    while time.monotonic() < deadline:
        client.ib.waitOnUpdate(timeout=0.25)
        if trade.order.permId:                    # openOrder ack arrived
            print(json.dumps({"stage": "ack",
                              "orderId": trade.order.orderId,
                              "permId": trade.order.permId,
                              "clientId": client.client_id}), flush=True)
            break
    # ... then the existing final result line: {"stage": "result", "status": ...}
```

```python
# server.py place handler — read line-streamed output
ack, result = await run_place_subprocess(args, timeout=15)   # parses both stages
if ack:
    transition(submission_id, from_states=("PENDING",), to="WORKING",
               event="IB_ACK", ib_order_id=ack["orderId"], perm_id=ack["permId"],
               placing_client_id=ack["clientId"])
if result is None:                      # timeout/kill/crash AFTER possible acceptance
    transition(submission_id,
               from_states=("PENDING", "WORKING"),
               to="UNCERTAIN", event="AMBIGUOUS_ACK",
               detail={"reason": "subprocess did not return a result"})
    raise HTTPException(502, detail={"reason_code": "ORDER_STATUS_UNCERTAIN",
                                     "message": "Broker outcome unknown — reconciling. "
                                                "Do NOT resubmit."})
```

## §2 Broker capability interface (small now; Protocol only with a second broker)

```python
# src/xenon/execution/broker_capabilities.py
@dataclass(frozen=True)
class BrokerCapabilities:
    place: bool; cancel: bool; modify: bool; stream_quotes: bool

CAPABILITIES = {
    "IB":   BrokerCapabilities(True, True, True, True),
    "FUTU": BrokerCapabilities(False, False, False, False),
}

def require_capability(scope: AccountScope, op: str) -> None:
    if not getattr(CAPABILITIES[scope.broker], op):
        raise HTTPException(403, detail={"reason_code": "READ_ONLY_BROKER",
                                         "message": f"{scope.broker} does not support {op}"})
# replaces the inline broker != "IB" branches at server.py:2140-2149 etc.
```

## §3 Bounded execution concurrency (OP-7)

```python
# server.py module scope
_ORDER_EXEC_SEMAPHORE = asyncio.Semaphore(2)   # ponytail: fixed bound; make env-tunable if contention observed
_orders_inflight = 0                            # gauge for /health + metrics

async def _run_order_subprocess(entry, args, timeout):
    global _orders_inflight
    async with _ORDER_EXEC_SEMAPHORE:
        _orders_inflight += 1
        try:
            return await _run_ib_script_with_recovery(entry, args, timeout=timeout)
        finally:
            _orders_inflight -= 1
```

## §4 Persistent broker-session ownership (Option B seed — the module already exists)

```python
# promote xenon/api/pool_order_manage.py; add place:
async def pool_place_order(pool, body) -> PlaceOutcome:
    client = await pool.get_with_reconnect("orders")        # clientId 4, pinned thread
    def _place():
        contract = qualify_cached(client, body)             # contract cache per session
        order = build_limit_order(body)                     # orderRef set as in §1
        trade = client.place_order(contract, order)
        wait_for_ack(client, trade, deadline_s=8)           # event-driven
        return outcome_from(trade)
    return await pool.run_sync("orders", _place)
# server.py: if FLAG_POOL_PLACE: outcome = await pool_place_order(...)
#            except SessionWedged: fall back to subprocess path (circuit-back)
```

## §5 Explicit order state transition (OP-8/OP-9)

```python
# orders_store.py — the single writer every state change must use
LEGAL = {
    ("PENDING", "WORKING"), ("PENDING", "REJECTED"), ("PENDING", "FAILED"),
    ("PENDING", "UNCERTAIN"), ("WORKING", "UNCERTAIN"),
    ("UNCERTAIN", "WORKING"), ("UNCERTAIN", "FILLED"), ("UNCERTAIN", "FAILED"),
    ("WORKING", "PARTIALLY_FILLED"), ("WORKING", "FILLED"), ("WORKING", "CANCELLED"),
    ("PARTIALLY_FILLED", "FILLED"), ("PARTIALLY_FILLED", "CANCELLED"),
    # RESURRECT: terminal -> WORKING only via register_from_snapshot's guarded branch
}

def transition(submission_id, *, from_states, to, event, detail=None, **cols):
    with engine.begin() as conn:
        res = conn.execute(
            update(order_submissions)
            .where(order_submissions.c.submission_id == submission_id,
                   order_submissions.c.state.in_(from_states))
            .values(state=to, updated_at=func.now(), **cols))
        if res.rowcount == 0:
            return False                     # lost the race — caller decides, never clobber
        conn.execute(insert(order_events).values(          # SAME transaction: audit row
            submission_id=submission_id, event_type=event,
            detail={**(detail or {}), "from": list(from_states), "to": to}))
        return True
# + alembic: CHECK (state IN (...)) on order_submissions.state
```

## §6 Quote messages with timestamp and sequence metadata (QS-3)

```js
// relay: module scope
let globalSeq = 0;
function flushBatches() {
  const relayTs = Date.now();
  for (const [client, buf] of clientBatchBuffers) {
    if (!buf.size) continue;
    sendBounded(client, {
      type: "batch",
      seq: ++globalSeq, // strictly increasing per relay lifetime
      relay_ts: relayTs, // receive-time clock, named honestly
      prices: Object.fromEntries(buf),
    });
    buf.clear();
  }
}
// client (usePrices): if (msg.seq <= lastSeqRef.current) return;  // drop stale/reordered
```

## §7 Per-client bounded WebSocket delivery (QS-1)

```js
const MAX_BUFFERED_BYTES = 512 * 1024; // ponytail: fixed cap; tune from /status metrics
const MAX_QUEUED_SYMBOLS = 500;

function sendBounded(client, payload) {
  if (client.readyState !== client.OPEN) return;
  if (client.bufferedAmount > MAX_BUFFERED_BYTES) {
    client.droppedFlushes = (client.droppedFlushes || 0) + 1; // metric
    return; // LWW buffer keeps latest state; next flush delivers a superset
  }
  try {
    client.send(JSON.stringify(payload));
  } catch {
    /* counted, close on pong timeout */
  }
}

function bufferPriceForClient(client, symbol, data) {
  const buf = clientBatchBuffers.get(client);
  if (!buf) return;
  if (!buf.has(symbol) && buf.size >= MAX_QUEUED_SYMBOLS) return; // hard cap
  buf.set(symbol, { ...data });
}
```

## §8 Structured correlation logging (P1.1)

```python
# server.py — one line per stage, keyed by the id that already flows end-to-end
def _stage(attempt_id: str, stage: str, t0: float, **kw):
    logger.info("order_stage", extra={
        "client_attempt_id": attempt_id, "stage": stage,
        "elapsed_ms": round((time.monotonic() - t0) * 1000, 1), **kw})

# in the place handler:
t0 = time.monotonic()
_stage(cid, "gates_done", t0)
_stage(cid, "reserved", t0, submission_id=submission_id)
_stage(cid, "subprocess_spawned", t0)
_stage(cid, "ack", t0, order_id=ack["orderId"])       # from the §1 early ack line
_stage(cid, "persisted", t0)
# Next.js already emits X-Request-Id; add client_attempt_id to its route logs for joins.
```
