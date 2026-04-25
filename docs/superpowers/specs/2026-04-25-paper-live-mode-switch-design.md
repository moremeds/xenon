# Paper / Live Mode Switch — Design

**Date:** 2026-04-25
**Status:** Draft (awaiting user review)
**Scope:** v1, minimal. Backend + runner script. UI badge deferred.

## Goal

Provide a systematic, low-friction, reliable way to switch the Xenon stack between IBKR **paper** (`DU…`) and **live** (`U…`) accounts on a per-host basis. Mac mini stays on live (production); laptop/QA can run either. Switching must be hard to do wrong — a misconfigured stack must refuse to place orders rather than route to the wrong account.

## Non-goals

- Runtime toggle (no in-app switch; mode is set in `.env`, applied on restart).
- Auto-launching or re-logging IB Gateway. The Gateway login is human-managed for v1.
- UI "PAPER" badge. Backend exposes the field; the visual pill is a separate piece of work.
- Removing the `cloud` / `docker` mode branches in `ib_gateway.py`. They stay reachable via archived scripts.

## Design

### Single source of truth

`XENON_TRADING_MODE` in `.env`. Allowed values: `paper`, `live`. Default if unset: `paper` (safe). No other env var is needed for mode selection — port and prefix are derived.

### Mode → port / prefix mapping

| Mode    | IB Gateway port | Account prefix     |
| ------- | --------------- | ------------------ |
| `paper` | 4002            | `DU`               |
| `live`  | 4001            | `U` (and not `DU`) |

The mapping lives in one place: `src/xenon/api/ib_gateway.py`. The existing `IB_GATEWAY_PORT` env var is no longer consulted; the port is computed from `XENON_TRADING_MODE`. `IB_GATEWAY_HOST` continues to be honored (defaults to `127.0.0.1`).

### Account-prefix guard

After the FastAPI process establishes its IB connection, it calls `ib.managedAccounts()` and compares the prefix against the declared mode:

- Match → set `mode_verified = True`, normal startup.
- Mismatch → log a loud `ERROR`, set `mode_verified = False`. The process keeps running so `/health` reports the mismatch, but **all `/orders/*` routes return HTTP 503** with a body explaining the mismatch and the fix.

Read-only routes (quotes, chains, portfolio view) are unaffected — only order-placement paths are gated. Rationale: a mismatch is a configuration bug, not a market-data bug; you can still see what's happening, you just can't fire orders into the wrong account.

### `/health` surface

The existing health response gains three fields:

```json
{
  "trading_mode": "paper",
  "account": "DU1234567",
  "mode_verified": true
}
```

`account` is the first managed account returned by IB. `mode_verified` is the prefix-guard result. This is consumable today via `curl` and is the data source the future UI badge will read from.

### Runner script

**Move (don't delete):**

- `scripts/infra/cloud.sh` → `scripts/infra/docker/cloud.sh`
- `scripts/infra/local.sh` → `scripts/infra/docker/local.sh`
- New `scripts/infra/docker/README.md` — one short paragraph: "Archived runners for Docker / Hetzner VPS paths. Active dev path is `scripts/infra/dev.sh` (native Gateway). Use these only when you need the Docker or VPS fallback."

**New primary runner:** `scripts/infra/dev.sh`. Behavior:

1. Resolve mode: arg (`./dev.sh paper`) > `XENON_TRADING_MODE` from `.env` > default `paper`.
2. Echo the resolved mode and the derived port for the operator.
3. Probe `127.0.0.1:<port>` via TCP. If not listening, print a one-liner telling the user to log in to IB Gateway in the matching mode and exit non-zero. (Do not attempt to start Gateway — that's manual for v1.)
4. Start the same FastAPI + Next dev processes the existing scripts launch today.

The script does not edit `.env`. Mode is a `.env` value the user sets manually (or overrides per-invocation via the arg).

### What lives where

| File                                                                | Change                                                                               |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `src/xenon/api/ib_gateway.py`                                       | Add `TRADING_MODE` constant, derive `IB_PORT` from it, expose helpers for the guard. |
| `src/xenon/api/routes/health.py` (or wherever `/health` is defined) | Surface `trading_mode`, `account`, `mode_verified`.                                  |
| `src/xenon/api/routes/orders.py` (and any sibling order routes)     | Reject with 503 when `mode_verified is False`.                                       |
| `src/xenon/api/main.py` (or app startup)                            | Run the prefix guard once after IB connects; cache result.                           |
| `scripts/infra/dev.sh`                                              | New. Mode-aware launcher.                                                            |
| `scripts/infra/docker/cloud.sh`                                     | Moved from `scripts/infra/cloud.sh`.                                                 |
| `scripts/infra/docker/local.sh`                                     | Moved from `scripts/infra/local.sh`.                                                 |
| `scripts/infra/docker/README.md`                                    | New. Brief archival note.                                                            |
| `.env.example` (root)                                               | Add `XENON_TRADING_MODE=paper`.                                                      |

`ib_gateway.py`'s existing `cloud` / `docker` / `launchd` mode branches are untouched.

### Tests

- Unit: `XENON_TRADING_MODE=paper` → port 4002; `=live` → 4001; unset → port 4002 (paper default); invalid value → raise at import-time.
- Unit: prefix guard returns `(verified=True, …)` for matching mode/account, `(verified=False, …)` for mismatch and for empty `managedAccounts()`.
- Integration: with the guard mocked to `verified=False`, a POST to an order route returns 503 and the body names both the declared mode and the observed account.
- `/health` integration test asserts the three new fields are present and correctly typed.

## Switching workflow (operator's view)

```
# 1. Edit .env
XENON_TRADING_MODE=paper   # or live

# 2. Make sure IB Gateway is logged in for that mode (you do this by hand)

# 3. Restart
./scripts/infra/dev.sh
```

That's the whole "switch."

## Risks and mitigations

- **Operator forgets to relog Gateway after editing `.env`.** Runner probes the port and exits with a clear error before FastAPI starts. No silent fallback.
- **Operator edits `.env` but forgets to restart.** Module-level `TRADING_MODE` only takes effect at process start; combined with the prefix guard, an old running process either keeps serving the old (correct) account or gets caught at next restart. No mid-flight mode flip.
- **`managedAccounts()` returns multiple accounts.** Use the first; if any account in the list disagrees with the prefix, treat as mismatch. (Mixed live/paper under one login is not a real configuration in IBKR, so this is defensive only.)
- **Future regression that bypasses the guard.** Order-route 503 path is covered by the integration test above; CI will catch removal.

## Manual — switching between paper and live

Operator-facing reference. Treat each step as required; skipping any of them produces the failure mode listed beside it.

### One-time setup (per machine)

1. **Install IB Gateway natively** (no Docker, no VPS). Two installs is fine — one for live, one for paper — but a single install that you re-log between modes also works for v1.
2. **Edit `.env`** at the repo root. Add or update:
   ```
   XENON_TRADING_MODE=paper   # or live
   ```

   - Mac mini (production): `live`.
   - Laptop / QA: usually `paper`. Switch to `live` when you intentionally need read/write against the real account.
3. **Do not set `IB_GATEWAY_PORT`.** It is no longer consulted. The port is derived from `XENON_TRADING_MODE` (paper → 4002, live → 4001).
4. Confirm `IB_GATEWAY_HOST=127.0.0.1` (the default; you only override for the archived Docker/VPS paths).

### Switching modes (the actual procedure)

1. **Stop the running stack.** Ctrl-C the FastAPI process from `scripts/infra/dev.sh` (and the Next dev that it backgrounded — the trap kills it on exit).
2. **Edit `.env`** — change the single line:
   ```
   XENON_TRADING_MODE=live   # or paper
   ```
3. **Re-log IB Gateway** in the matching mode. The Gateway login screen has a `Live` / `Paper` toggle; pick the one that matches `.env`. If you keep two Gateway installs running, just bring the right one to the foreground; otherwise log out and back in.
4. **Restart the stack:**
   ```bash
   ./scripts/infra/dev.sh
   ```
   The script reads the new mode from `.env`, derives the port, probes that the Gateway is listening on it, and only then starts FastAPI + Next. If the Gateway isn't reachable on the derived port, the script exits with a clear message before any service comes up.
5. **Verify:**
   ```bash
   curl -s http://127.0.0.1:8321/health | python -m json.tool
   ```
   Look for:
   ```json
   {
     "trading_mode": "live",
     "account": "U1234567",
     "mode_verified": true
   }
   ```

   - `mode_verified: true` → safe to trade. `/orders/*` routes will accept requests.
   - `mode_verified: false` → mismatch detected. `/orders/*` returns HTTP 503 with a body naming both the declared mode and the observed account. Read-only routes (quotes, chains, portfolio view) keep working so you can diagnose.

### Per-invocation override (optional)

You can override `.env` for a single launch:

```bash
./scripts/infra/dev.sh paper
./scripts/infra/dev.sh live
```

The `.env` value is unchanged; the override only affects this process. Useful for one-shot QA against the other mode without committing an `.env` flip.

### Common failure modes and fixes

| Symptom                                                                                                     | Cause                                                                                     | Fix                                                                |
| ----------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `dev.sh` exits with `IB Gateway is NOT listening on 127.0.0.1:<port>`                                       | Gateway not running, or running in the wrong mode (so it's listening on the _other_ port) | Launch / re-log Gateway in the matching mode, then re-run `dev.sh` |
| `/health` shows `mode_verified: false` and `account` starts with `DU` while `trading_mode: live`            | `.env` says `live` but Gateway is logged in as paper                                      | Re-log Gateway as live, restart FastAPI                            |
| `/health` shows `mode_verified: false` and `account` starts with `U` (not `DU`) while `trading_mode: paper` | Reverse of the above                                                                      | Re-log Gateway as paper, restart FastAPI                           |
| `POST /orders/place` returns 503 with `Trading mode mismatch`                                               | Same as the two rows above — guard is doing its job                                       | Fix the mismatch as above                                          |
| Mode change in `.env` "didn't take"                                                                         | Forgot to restart FastAPI                                                                 | `XENON_TRADING_MODE` is read at process start; restart             |

### What this manual deliberately does NOT cover

- Auto-starting / re-logging IB Gateway from a script (manual for v1).
- Switching mode mid-session without a process restart (not supported by design).
- Running paper and live FastAPI processes side-by-side on the same machine (would require a second port and a second Gateway; out of scope).

## Out of scope (explicitly deferred)

- UI "PAPER" mode pill. (Backend field is ready; UI work is a separate task.)
- Removing the `cloud` and `docker` mode branches from `ib_gateway.py`.
- Auto-managing the IB Gateway process (start/stop/relog) from `dev.sh`.
- Per-route override (e.g. force a single endpoint to paper while the rest run live).
