# Handover — IB Gateway market-data zombie after SIGKILL recovery

> **For the next session:** read this file end-to-end before touching anything. It is self-contained — you do not need the prior conversation. The next session will run **ON the macmini** (where IBC, Docker, and IB Gateway live), so it can do things the prior remote SSH session could not: read IBC config, inspect Gateway GUI/logs, send IBC command-server commands, kick the JVM cleanly.

## TL;DR

- IB Gateway 10.45 on macmini was hung this evening (java pid 91637 alive, port 4001 bound, but the API handshake from localhost timed out — `SYN_SENT`/`CLOSE_WAIT` in netstat).
- We `kill -9`'d it remotely from a MacBook SSH session, IBC watchdog relaunched IB Gateway, user approved 2FA on iPhone. New java pid `22008`, session id `P7F5HzW6`.
- **Trading API recovered (account `U18007831` visible, all three `ib_pool` clients connected), but market data did not.** `reqMktData` on SPY, QQQ, AAPL, SPX, VIX returns `bid=nan ask=nan last=nan close=nan` with zero tick frames over 25s, and IB emits **no error code** for the reqMktData call (only the three normal farm-status messages 2104/2107/2158).
- Symptom reproduces from a **fresh `ib_async` session with a brand-new clientId** straight to `host.docker.internal:4001` — so this is NOT a `xenon-realtime` container bug. It is a Gateway-internal market-data dispatcher zombie.
- The user has IB **overnight market data** entitlement — listed stocks (SPY/QQQ/AAPL) and index options DO stream after RTH. So "market closed" is NOT a valid explanation.

## Wall-clock context

- Session was paused at **2026-06-03T00:19Z = 20:19 ET Tuesday**, ~19 min after the Gateway restart at `00:00:26Z`.
- Daily IB AutoRestart default is **23:45 ET** — about 3.5h after the pause. The natural restart may fix this on its own. If you want to test sooner, do not wait.

## What works (confirmed via `/health` on the macmini's `xenon-api-1`)

```
ib_gateway.port_listening: true
ib_gateway.upstream_dead: false
ib_gateway.service_state: reachable
ib_pool.sync.connected:    true (clientId 3)
ib_pool.orders.connected:  true (clientId 4)
ib_pool.data.connected:    true (clientId 5)
trading_mode: live
account: "U1***7831"
mode_verified: true
```

Account info, contract qualification (SPY → `conId 756733`, `primaryExchange ARCA`), order routing surfaces all healthy.

## What's broken — direct reproduction

Run from the macmini (`docker exec` into any container with `ib_async` installed, or use `uv run` from a worktree against `host.docker.internal:4001`):

```python
import asyncio
from ib_async import IB, Stock

async def main():
    ib = IB()
    errors = []
    ib.errorEvent += lambda reqId, code, msg, *a: errors.append((reqId, code, msg))
    await ib.connectAsync("host.docker.internal", 4001, clientId=78, timeout=10)
    spy = Stock("SPY", "SMART", "USD")
    await ib.qualifyContractsAsync(spy)
    ib.reqMarketDataType(4)               # Delayed-Frozen cascade
    t = ib.reqMktData(spy, "233,165", False, False)
    for i in range(10):
        await asyncio.sleep(1.0)
        print(f"t={i}s bid={t.bid} ask={t.ask} last={t.last}")
    print("errors:", errors)
    ib.disconnect()

asyncio.run(main())
```

Expected when healthy (in overnight session): non-NaN bid/ask within 1–2s.
Observed: all NaN for the full 10s, no IB error codes for the reqMktData.

## Diagnostic dead-ends — don't waste time

1. **Realtime container bounce** — already done (`docker restart xenon-realtime-1` at `00:09:12Z`). Bug persists. Not a realtime bug.
2. **`xenon-api-1` container bounce** — already done at `00:02:54Z`. All three pool clients reconnected cleanly. Bug persists.
3. **WS subscription protocol** — confirmed working: `{action:"subscribe", indexes:[{symbol:"SPX", exchange:"CBOE"}]}` is the correct shape (indexes need `{symbol, exchange}` objects, NOT bare strings — `normalizeIndexes` in `/app/scripts/infra/ib_realtime/ib_realtime_server.js:146` filters strings).
4. **clientId conflict** — ruled out by using clientId=78 (well outside any known allocation, see CLIENT_IDS in `src/xenon/clients/ib_client.py:82-92`).

## Recommended first action (on the macmini) — try `RECONNECTDATA` before anything else

IBC ships a command server that supports `RECONNECTDATA` — it tells Gateway to re-establish the market data session WITHOUT a process restart, WITHOUT a 2FA dance. Source: [IBC userguide § Command Server](https://github.com/IbcAlpha/IBC/blob/master/userguide.md).

```bash
# 1) Verify IBC command server is enabled and find its port
grep -E "CommandServerPort|BindAddress|ControlFromOpenPath" /opt/ibc/config.ini
# Default is 7462; if disabled, set CommandServerPort=7462 and restart IBC.

# 2) Send RECONNECTDATA
nc 127.0.0.1 7462 <<< "RECONNECTDATA"

# 3) Re-run the SPY probe above. If ticks flow → fixed without restart.
# 4) If still NaN, escalate to graceful SIGTERM (NOT SIGKILL):
#    - lsof -i :4001 to find the java pid
#    - kill -TERM <pid> ; wait 30s
#    - IBC watchdog will relaunch — approve 2FA on iPhone
```

**Do not SIGKILL again.** SIGKILL is what put us here. The Java VM never gets to flush its market-data session state, so the new process inherits a half-broken backbone connection inside IB's servers. SIGTERM gives the JVM ~30s to do a clean logout, which usually preserves the market data session through the restart cycle.

## Background — why this happens (research already done, don't redo)

Three structural reasons, confirmed against authoritative sources:

1. **No clean SIGKILL recovery for market data.** IB has no documented warm-restart path for the market data session — when the JVM dies hard, the in-process subscription state is lost and the new process re-logs-in for trading/account but not for the market data dispatcher. ([twsapi groups.io memory-leak thread](https://groups.io/g/twsapi/topic/ib_gateway_memory_leak/83731954))
2. **Daily restart is required, not optional.** IB ships `AutoRestart` at 23:45 ET by default. Heap leaks (correlated with `reqMktData` rate) + session credential rollover + 2FA token expiry every Sunday ~01:00 ET. ([IBKR Auto Restart Considerations](https://www.ibkrguides.com/traderworkstation/auto-restart-considerations.htm))
3. **Rapid clientId reconnect is rejected.** `IB.disconnect()` returns synchronously but the socket closes async — immediate reconnect with same clientId is dropped. ([ib_insync #376](https://github.com/erdewit/ib_insync/issues/376))

## Xenon's defense gaps (research already done, don't redo)

Every layer says "green" while market data is dead. Each layer probes a different signal and none of them probe the actual one:

| Layer                                                              | What it checks                             | Why it missed today         |
| ------------------------------------------------------------------ | ------------------------------------------ | --------------------------- |
| `IBPool.is_connected()` (`src/xenon/api/ib_pool.py:175-183`)       | `client.ib.isConnected()` socket state     | Socket alive ≠ data flowing |
| `/health` (`src/xenon/api/server.py:902-921`)                      | TCP port + pool socket + account string    | No tick-flow probe          |
| Docker healthcheck (`docker-compose.yml:24-43`)                    | `bash -c 'echo > /dev/tcp/localhost/4001'` | Pure TCP echo, no IB API    |
| Activity poller (`src/xenon/api/services/ib_activity_mirror.py`)   | Mirrors orders + fills                     | Doesn't touch market data   |
| Launchd CLOSE_WAIT detection (`src/xenon/api/ib_gateway.py:82-98`) | Has it for launchd mode                    | **Docker mode does NOT**    |
| IBC watchdog (`/opt/ibc/ibc_watchdog.sh`)                          | Checks process exists                      | No internal-state probe     |

## Tier 1 fixes — for after market data is back

Open these as a punch-list. Don't ship them in one PR — each is independent.

1. **Stop using SIGKILL on Gateway.** Update `docs/runbooks/remote-deploy.md` and any IBC tooling to default to SIGTERM with a 30s wait, only escalate to KILL if truly hung. ETA: 30 min.
2. **Add a tick-flow probe to `/health`.** Mirror what `ib_async.Watchdog` does: fire `reqHistoricalDataAsync` on a known liquid contract (SPY 1-min bar) with a 4s timeout, surface `ib_pool.data_flowing: bool`. Without this, every future occurrence is discovered via user complaint. ETA: 2–4h. Source: [ib_async Watchdog](https://ib-api-reloaded.github.io/ib_async/_modules/ib_async/ibcontroller.html).
3. **Add `RECONNECTDATA` as the first-line response in the runbook.** Document the command-server port, the command, and verification steps. ETA: 30 min.

## Tier 2 — structural, after Tier 1 lands

4. Wire up ib_async's `Watchdog` class in api lifespan (replaces manual reconnect-on-acquire pattern).
5. Pin daily auto-restart at 04:00 ET (before pre-market 04:30) via IBC `AutoRestartTime`. Document Sunday 01:00 ET 2FA event.
6. Upgrade Docker healthcheck from `/dev/tcp` TCP echo to a Python script that opens an IB client and calls `reqCurrentTime()` with timeout. Then enable `autoheal=true` label on the ib-gateway service (autoheal sidecar already runs for other services in your stack).

## Tier 3 — architectural

7. Read-Only API mode (`ReadOnlyLogin=yes`) on a separate clientId range for observer clients (option-chain snapshotter, activity mirror, realtime data service) — none of them place orders.
8. JVM heap metric via JMX exporter to Prometheus (already on macmini), alert at 70%, auto-restart at 85%.
9. **Don't pursue dual-Gateway active/standby.** IB forbids same-username concurrent logins. Second username = compliance and account-admin overhead for marginal win.

## Files / paths to know on the macmini

| Path                                               | What it is                                                                                           |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `/opt/ibc/config.ini`                              | IBC configuration — check for `CommandServerPort`, `IbAutoClosedown`, `ClosedownAt`, `ReadOnlyLogin` |
| `/opt/ibc/ibc_watchdog.sh`                         | The watchdog that relaunches Gateway if missing — does NOT detect zombie state                       |
| `/opt/ibc/logs/ibc-3.23.0_GATEWAY-10.45_<Day>.txt` | IBC + Gateway log; look for "login" + "market data" entries around the restart                       |
| `~/Library/LaunchAgents/local.ibc-watchdog.plist`  | StartInterval=300s, points at `/Users/moremeds/trading-stack/scripts/ibc_watchdog_launchd.sh`        |
| `/opt/xenon/compose.yml`                           | Docker Compose for xenon stack on macmini                                                            |
| `/opt/xenon/.env`                                  | Live environment vars consumed by all four containers                                                |

## Container names (compose adds `-1` suffix; raw `docker restart xenon-api` fails — must be `xenon-api-1`)

```
xenon-api-1         ghcr.io/moremeds/xenon-api:0.2.0       (healthy)
xenon-web-1         ghcr.io/moremeds/xenon-web:0.2.0       (healthy)
xenon-realtime-1    ghcr.io/moremeds/xenon-realtime:0.2.0
```

PATH gotcha: non-interactive SSH does not pick up `/opt/homebrew/bin`. Either `export PATH=/opt/homebrew/bin:$PATH` first or use `docker compose` via full path. Local session on the macmini should not have this problem.

## Open follow-ups from earlier in the day (still unresolved)

- **Phantom alembic revision `2026_06_03_futu_stmt2`** in production DB — exists nowhere in any code/branch/stash/image. Force-stamped to `2026_06_02_cf_open` via raw SQL UPDATE to unstick today's v0.2.0 deploy. Masked, not resolved. Origin unknown. Needs investigation before next migration cycle.
- **Futu OpenD `connected: false`** in `/health` — known Tailscale-binding gap (`project_tailscale_migration` memory): OpenD binds 127.0.0.1, not 0.0.0.0, so the container can't reach it. Separate fix, not related to this incident.

## What changed today (PRs + releases for context)

- `#125` — option-chain snapshotter scaffolding merged.
- `#127` — CHANGELOG draft for v0.2.0.
- **v0.2.0 released and deployed to macmini.** Containers Up healthy on 0.2.0 images.
- No code change related to IB Gateway / market data stack in v0.2.0 — this incident is purely an operational artifact of the SIGKILL-restart sequence.

## Standing rules to honor in the next session

- All Python via `uv run`, never bare `python`/`pip` (per `xenon/CLAUDE.md`).
- Never commit without explicit user request. Never push to master directly — branch + PR via `gh pr create`.
- Never add `Co-Authored-By: Claude` trailer to commits (per `~/.claude/CLAUDE.md`).
- Test against paper first when in doubt; live IB is U18007831, real money. (Memory: `feedback_broker_bugs_paper_first`.)
- IB Gateway must stay on stable 10.45, never "latest" 10.46 — the latest breaks the API. (Memory: `feedback_ib_gateway_use_stable_1045`.)
