# P4.5 — Rename/repair the prod "gateway restart" branch (QS-6)

- **Date:** 2026-07-05
- **Branch:** `fix/p4-5-gateway-restart-honest`
- **Finding:** QS-6 (Medium) — the stale-data watchdog's `restartIBGateway` in docker/cloud
  mode only bounces the relay's OWN IB socket; it cannot recover a wedged Gateway. Only the
  non-docker (LaunchD) branch shells to a real IBC restart. The name over-promises: in prod
  the watchdog cannot fix the failure it detects.
- **Goal:** Make the watchdog's prod action match its name — rename the function/log lines to
  reflect "reconnect socket, cannot restart Gateway", and emit an explicit loud alert so an
  operator knows the Gateway itself may be wedged.
- **Acceptance (roadmap):** watchdog action matches its name in the docker topology.

## Re-verify preamble (MANDATORY — executes after S7 + P1.2)

S7 and P1.2 touch the send/upgrade/status paths, NOT the stale-data watchdog. Confirm the
anchors are unchanged at HEAD:

```bash
cd scripts/infra/ib_realtime
grep -n "async function restartIBGateway\|GATEWAY_MODE\|STALE_DATA_THRESHOLD_MS\|ibGatewayRestarting" ib_realtime_server.js
grep -n "broadcastStatus\|ibConnectionIssue\|classifyIBConnectionError" ib_realtime_server.js | head
```

If `restartIBGateway` no longer branches on `GATEWAY_MODE === "cloud" || "docker"`, STOP —
the branch was already reshaped; re-plan against the new structure.

## Key facts (verified at HEAD)

- `restartIBGateway()` (`ib_realtime_server.js` ~661): guarded by `ibGatewayRestarting`;
  logs red `[stale-data] No ticks received during market hours — handling stale data`; then:
  - `GATEWAY_MODE === "cloud" || "docker"` → `ib.disconnect()` + `scheduleReconnect()` +
    log `${GATEWAY_MODE} mode — disconnecting and scheduling reconnect`. **This is a socket
    reconnect, not a Gateway restart.**
  - else (LaunchD) → `execSync(".../ibc/bin/restart-secure-ibc-service.sh")` — a real restart.
  - 120 s cooldown resets `ibGatewayRestarting`.
- `GATEWAY_MODE = process.env.IB_GATEWAY_MODE || "docker"` (~659) — prod defaults to docker.
- Trigger: stale-data watchdog — `STALE_DATA_THRESHOLD_MS = 45_000`, `STALE_CHECK_INTERVAL_MS
= 30_000` (~640), RTH-gated via `isUSMarketHours()`.
- Status broadcast exists: `broadcastStatus()` (~790) sends `{ type: "status", ib_connected,
ib_issue, ib_status_message, subscriptions }` to every client; frontend `usePrices`
  `case "status"` sets `ibIssue`/`ibStatusMessage`. `ibConnectionIssue` is the carrier object
  (`{ code, operatorMessage }` shape per `classifyIBConnectionError`).
- Prod topology (memory `project_macmini_deploy_mechanism`): relay runs in the macmini Docker
  stack (`/opt/xenon/compose.yml`); there is NO in-container capability to restart the sibling
  IB Gateway process. A true prod restart would require a compose-level hook / external
  supervisor — explicitly out of scope for a relay-only PR.

## Non-goals

- Do NOT implement an actual cross-container Gateway restart (needs compose/infra changes and
  Gateway-side automation — a separate infra task; note it in the PR as the follow-up).
- Do NOT change the LaunchD branch behavior (it already does a real restart).
- Do NOT change stale-detection thresholds or RTH gating.

## Steps (TDD-lite — this is a naming + alerting change)

### Step 1 — Rename the function and split the two actions honestly

Rename `restartIBGateway` → `handleStaleData` (the watchdog's real job is "handle stale
data", which may or may not be able to restart the Gateway). Update its single caller (the
stale-check timer — `grep -n "restartIBGateway(" ib_realtime_server.js`).

Rewrite the docker/cloud branch to name reality AND alert:

```js
async function handleStaleData() {
  if (staleHandlingInFlight) return;
  staleHandlingInFlight = true;
  console.log(
    "\x1b[31m[stale-data] No ticks during market hours — handling stale data\x1b[0m",
  );

  if (GATEWAY_MODE === "cloud" || GATEWAY_MODE === "docker") {
    // Prod (docker/cloud): the relay CANNOT restart the sibling IB Gateway
    // container/process. Best effort is to bounce our own IB socket; if the
    // Gateway itself is wedged, only an operator/supervisor can fix it — so
    // raise a loud, client-visible alert instead of pretending we restarted it.
    console.error(
      `\x1b[31m[stale-data] ${GATEWAY_MODE} mode: reconnecting IB socket only — ` +
        `relay CANNOT restart the Gateway. If ticks do not resume, the IB Gateway ` +
        `may be wedged and needs an operator/supervisor restart.\x1b[0m`,
    );
    ibConnectionIssue = {
      code: "STALE_DATA_NO_TICKS",
      operatorMessage:
        "No market data ticks during RTH. Relay reconnected its IB socket; " +
        "if quotes stay frozen the IB Gateway may need a manual restart.",
    };
    broadcastStatus(); // surface the alert to every connected client now
    try {
      ib.disconnect();
    } catch {
      /* ignore */
    }
    scheduleReconnect();
  } else {
    // LaunchD mode — shell out to restart IBC service
    try {
      execSync(`${homedir()}/ibc/bin/restart-secure-ibc-service.sh`, {
        timeout: 60_000,
        stdio: "pipe",
      });
      console.log(
        "[stale-data] IB Gateway restart initiated — waiting for reconnect",
      );
    } catch (err) {
      console.error("[stale-data] Failed to restart IB Gateway:", err.message);
    }
  }

  setTimeout(() => {
    staleHandlingInFlight = false;
  }, 120_000);
}
```

Rename the module flag `ibGatewayRestarting` → `staleHandlingInFlight` (search + replace all
occurrences — verify there are no others with `grep -n ibGatewayRestarting`).

**Keep the LaunchD-branch comment byte-identical** (`// LaunchD mode — shell out to restart
IBC service`) — the existing regression suite `web/tests/ib-realtime-restart-modes.test.ts`
anchors a source regex on that exact comment; changing it breaks the suite for no gain.

### Step 2 — Verify the alert reaches the client (shape check)

Confirm `ibConnectionIssue` written here matches what `broadcastStatus`/`sendStatus` read:
`ib_issue: ibConnectionIssue?.code`, `ib_status_message: ibConnectionIssue?.operatorMessage`.
The object above uses `.code` + `.operatorMessage` — matches. Frontend `usePrices`
`case "status"` already maps these to `ibIssue`/`ibStatusMessage`; no frontend change needed
for the alert to appear wherever those are rendered.

### Step 3 — VERIFY-ONLY: recovery already clears the issue

Recovery clearing **already exists at HEAD**: the `ib.on(EventName.connected, ...)` handler
(~2179-2195) sets `ibConnected = true; ibConnectionIssue = null;` and ends with
`broadcastStatus()`. Do NOT add anything — just confirm with
`grep -n "ibConnectionIssue = null" ib_realtime_server.js` that the connected handler still
clears it, so the `STALE_DATA_NO_TICKS` alert cannot stick after a healthy reconnect. If the
grep comes back empty, STOP — the connected handler drifted; report before patching.

### Step 4 — Update the existing regression suite (MANDATORY)

`web/tests/ib-realtime-restart-modes.test.ts` already pins this branch by reading the relay
source (its `"keeps cloud and docker on reconnect-only recovery"` case, ~36-49, regex-matches
the `GATEWAY_MODE === "cloud" || GATEWAY_MODE === "docker"` block). Extend that describe
block — same source-assertion style — to pin the new honesty guarantees:

```ts
it("docker/cloud branch raises the stale-data alert instead of claiming a restart", () => {
  const cloudDockerBlock =
    source.match(
      /if \(GATEWAY_MODE === "cloud" \|\| GATEWAY_MODE === "docker"\) \{[\s\S]*?\n  \} else \{/,
    )?.[0] ?? "";
  expect(cloudDockerBlock).toContain("CANNOT restart the Gateway");
  expect(cloudDockerBlock).toContain('code: "STALE_DATA_NO_TICKS"');
  expect(cloudDockerBlock).toContain("broadcastStatus()");
  expect(cloudDockerBlock).not.toContain("restart initiated");
});

it("recovery path clears the connection issue on IB connected", () => {
  expect(source).toContain("ibConnectionIssue = null");
});
```

Also update any existing assertions that reference the old names (`restartIBGateway` /
`ibGatewayRestarting`) — run `grep -n "restartIBGateway\|ibGatewayRestarting" web/tests/` and
fix every hit to the new names.

## Verification matrix

| Check                                     | Command                                                                                                                                                             | Expected                                                                                                     |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| No stale refs to old names                | `grep -rn "restartIBGateway\|ibGatewayRestarting" scripts/infra/ib_realtime/`                                                                                       | 0 matches                                                                                                    |
| Relay boots                               | `node scripts/infra/ib_realtime/ib_realtime_server.js --port 8899` (Ctrl-C)                                                                                         | listening, no throw                                                                                          |
| Docker-branch log honesty (simulated)     | run with `IB_GATEWAY_MODE=docker`, force a stale window (see below)                                                                                                 | error log says "relay CANNOT restart the Gateway"; NO "restart initiated" line                               |
| Alert broadcast (paper)                   | `scripts/infra/dev.sh paper`; induce stale (disconnect IB feed 45s+ during RTH, OR temporarily set `STALE_DATA_THRESHOLD_MS=5000` via a local edit and block ticks) | connected client receives a `status` message with `ib_issue: "STALE_DATA_NO_TICKS"` and the operator message |
| Recovery clears alert (existing behavior) | let IB reconnect                                                                                                                                                    | subsequent `status` has `ib_issue: null`                                                                     |
| LaunchD branch untouched                  | `git diff` the else-branch                                                                                                                                          | only the flag rename, no logic change, comment byte-identical                                                |
| Regression suite (updated)                | `cd web && npm test -- ib-realtime-restart-modes`                                                                                                                   | all pass, incl. the two new source assertions                                                                |

Simulating stale without waiting 45 s: set `IB_REALTIME_STALE_TICK_MS` is unrelated; the
watchdog uses the hardcoded `STALE_DATA_THRESHOLD_MS`. For a manual probe, temporarily lower
it in a LOCAL uncommitted edit, verify, then restore before committing (tripwire below).

## Tripwires / abort

- STOP if `restartIBGateway`'s docker branch already logs an honest "cannot restart" message
  and broadcasts an alert — QS-6 already fixed.
- Do NOT commit any temporary lowered `STALE_DATA_THRESHOLD_MS` used for probing — restore it.
- Do NOT attempt a real cross-container Gateway restart in this PR (out of scope).
- No live IB. No orders.
- File set: `ib_realtime_server.js` + `web/tests/ib-realtime-restart-modes.test.ts` — 2
  files. If a frontend RENDER edit seems needed for the alert to appear, STOP and confirm —
  the `status` plumbing already exists; a new UI surface is a separate concern.

## Rollback

Discard the branch. Relay rename + added logging/alert and the matching test-suite update;
reverting both hunks restores the prior behavior. No schema, no migration, no UI, no infra
change.
