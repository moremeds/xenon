# brand/ — CLAUDE.md

Brand identity, design tokens, component kit. **Mandatory reference for any UI work.**

Full spec: `docs/brand-identity.md` + `brand/xenon-brand-system.md`. Tokens: `brand/xenon-design-tokens.json`. Tailwind: `brand/xenon-tailwind-theme.ts`. Kit: `/kit` route. Logo: `brand/xenon-app-icon.svg`.

**System name:** Xenon (not "Convex Scavenger" in UI).

## Typography

Inter (UI) + IBM Plex Mono (numeric tables, telemetry) + Söhne (display only).

## Xenon Spectrum

| Token | Hex | Meaning |
|-------|-----|---------|
| `signal.core` | `#05AD98` | Core accent |
| `signal.strong` | `#0FCFB5` | High-confidence |
| `signal.deep` | `#048A7A` | Deep data / selected |
| `warn` | `#F5A623` | Caution |
| `fault` | `#E85D6C` | Feed fault |
| `violet.extreme` | `#8B5CF6` | Extreme dislocation |
| `magenta.dislocation` | `#D946A8` | Structural dislocation |
| `neutral` | `#94a3b8` | Neutral |

## Surfaces

**Dark:** canvas `#0a0f14` | panel `#0f1519` | raised `#151c22` | grid `#1e293b`
**Light:** canvas `#FFFFFF` | panel `#FFFFFF` | raised `#F1F5F9` | grid `#BBBFBF`

## CSS Variables

`--bg-base`, `--bg-panel`, `--bg-panel-raised`, `--bg-hover`, `--border-dim`, `--line-grid`, `--signal-core`, `--signal-strong`, `--signal-deep`, `--dislocation`, `--extreme`, `--fault`, `--neutral`, `--text-secondary` — all auto-adapt dark/light in `globals.css`.

## Non-Negotiable Rules

- 4px max border-radius on panels (badges: 999px capsule)
- All colors via tokens — no raw hex
- Mono for machine, sans for product — never reversed
- Empty states describe measurement condition, not generic placeholders
- Voice: precise, calm, scientific — no hype/emojis
- Grid: 8px base, 4px micro, 16px gutters, 32px section gaps
- No decorative elements (glassmorphism, gradients, soft shadows)
- Panels = instrument modules (hairline borders, matte, device-label headers)
- Signal semantics: Baseline → Emerging → Clear → Strong → Dislocated → Extreme
