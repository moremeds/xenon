# XENON — CLAUDE.md

Master policy file. Topic-specific guidance lives in subdirectory `CLAUDE.md` files:

| Area | File |
|------|------|
| Frontend, pricing, P&L, share cards, reports | `web/CLAUDE.md` |
| Python pipelines, scanners, commands, data files | `scripts/CLAUDE.md` |
| FastAPI, Clerk auth, IB Gateway, order lifecycle | `scripts/api/CLAUDE.md` |
| Brand tokens, typography, spectrum, UI rules | `brand/CLAUDE.md` |

## ⛔ Mandatory Rules

1. **Be concise.** No preamble, no filler.
2. **E2E browser verification for ALL UI work.** Primary: `chrome-cdp`. Fallback: Playwright (`web/playwright.config.ts`). No UI change done until visually confirmed. Don't assume code changes produce the expected visual result — verify rendered output in the browser before committing.
3. **Red/green TDD for ALL code.** Failing test → fix → green → refactor. Unit: Vitest, E2E: chrome-cdp/Playwright.
4. **95% test coverage target.** Every change includes corresponding tests.
5. **API keys** in `.env` files (see Credentials below). Fallback: `~/.zshrc`.
6. **Options structure reference:** `docs/trading/options-structures.json` + `docs/trading/options-structures.md` — 58 structures, guard decisions, P&L attribution labels. Use for order entry, structure classification, and naked short guard logic.

## Identity

**Xenon** — market structure reconstruction system. Surfaces convex opportunities from dark pool/OTC flow, vol surfaces, cross-asset positioning. Detects institutional positioning, constructs convex options structures, sizes with fractional Kelly. **Flow signal or nothing.**

Brand spec: `brand/CLAUDE.md` + `docs/reference/brand-identity.md`.

## ⛔ Four Gates — Mandatory, Sequential, No Exceptions

```
GATE 1 — CONVEXITY      : Potential gain ≥ 2× potential loss. Defined-risk only (long options, verticals).
GATE 2 — EDGE           : Specific, data-backed dark pool/OTC signal that hasn't moved price yet.
GATE 3 — RISK MGMT      : Fractional Kelly sizing. Hard cap: 2.5% of bankroll per position.
GATE 4 — NO NAKED SHORTS: Never naked short stock, calls, futures, or bonds. Every short call must be fully covered by long shares (1 contract = 100 shares). Violation = immediate cancel.
```

**Any gate fails → stop. No rationalization.** Enforcement details: `scripts/CLAUDE.md` (naked-short table + combo guardrails).

## Credentials

| File | Loader | Contains |
|------|--------|----------|
| `.env` (root) | `python-dotenv` | `MENTHORQ_USER`, `MENTHORQ_PASS`, `CLERK_JWKS_URL`, `CLERK_ISSUER`, `ALLOWED_USER_IDS` |
| `web/.env` | Next.js | `ANTHROPIC_API_KEY`, `UW_TOKEN`, `EXA_API_KEY`, `CEREBRAS_API_KEY`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY` |

## Market Hours

```bash
TZ=America/New_York date +"%A %H:%M"   # 9:30–16:00 ET, Mon–Fri
```

- **Open**: Fetch fresh. Cache TTL: flow 5min, ratings 15min.
- **Closed**: Use latest. Flag stale data.

## Output Rules

- Always: `signal → structure → Kelly math → decision`
- State probabilities; flag uncertainty
- Failing gate = immediate stop, name the gate
- **Never rationalize a bad trade**
- Executed → `trade_log.json` | NO_TRADE → `docs/status.md`

## Startup Checklist

- [ ] `scripts/cloud.sh` (default — local dev services + VPS IB Gateway via Tailscale) — OR `scripts/local.sh` (fully local with Docker gateway)
- [ ] If local mode: approve 2FA on IBKR mobile for cold start
- [ ] `curl http://localhost:8321/health` — verify `ib_gateway.port_listening: true`
- [ ] Reconciliation auto-runs → `data/reconciliation.json`
- [ ] Exit order service auto-runs (PENDING_MANUAL)
- [ ] CRI scan service running (30-min intervals)
- [ ] X scan if >12h stale
- [ ] Check market hours
