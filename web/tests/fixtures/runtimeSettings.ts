// GET /admin/settings rows for tests: every registry key (src/xenon/runtime_settings.py),
// generated from describe()'s shape with the code-default env plus XENON_FUTU_TRADING=simulate.
// One DB override (the Futu market-order cap) and the relay's live values (= its defaults).
// Settings only; no market data.
import type { RuntimeSetting } from "@/lib/runtimeSettingsTypes";

const ROWS = [
  {
    "key": "XENON_QUOTES_DEFAULT",
    "env_name": "XENON_QUOTES_DEFAULT",
    "owner": "relay",
    "category": "toggle",
    "type": "enum",
    "apply": "live",
    "rule": "free",
    "narrow_only": false,
    "confirm_rule": null,
    "label": "Default quote source",
    "unit": null,
    "description": "Quote feed for clients that do not choose one.",
    "consumers": [
      "relay futu routing reconcile"
    ],
    "choices": [
      "ib",
      "futu-first"
    ],
    "bounds": null,
    "default": "ib",
    "env_known": false,
    "env_value": null,
    "db_value": null,
    "db_value_invalid": false,
    "effective": null,
    "updated_at": null,
    "updated_by": null,
    "note": null,
    "last_change": null,
    "live_value": "ib"
  },
  {
    "key": "XENON_FUTU_OPTIONS",
    "env_name": "XENON_FUTU_OPTIONS",
    "owner": "relay",
    "category": "toggle",
    "type": "bool",
    "apply": "live",
    "rule": "free",
    "narrow_only": false,
    "confirm_rule": null,
    "label": "Futu option quotes",
    "unit": null,
    "description": "Route option contracts to the Futu feed, as stocks are.",
    "consumers": [
      "relay futu routing reconcile"
    ],
    "choices": [],
    "bounds": null,
    "default": false,
    "env_known": false,
    "env_value": null,
    "db_value": null,
    "db_value_invalid": false,
    "effective": null,
    "updated_at": null,
    "updated_by": null,
    "note": null,
    "last_change": null,
    "live_value": false
  },
  {
    "key": "IB_REALTIME_MAX_LINES",
    "env_name": "IB_REALTIME_MAX_LINES",
    "owner": "relay",
    "category": "tuning",
    "type": "int",
    "apply": "live",
    "rule": "free",
    "narrow_only": false,
    "confirm_rule": null,
    "label": "IB line budget",
    "unit": "lines",
    "description": "IB market-data lines the relay may hold. The account cap (~100) is shared by every client.",
    "consumers": [
      "relay line budget / eviction"
    ],
    "choices": [],
    "bounds": [
      10,
      100
    ],
    "default": 85,
    "env_known": false,
    "env_value": null,
    "db_value": null,
    "db_value_invalid": false,
    "effective": null,
    "updated_at": null,
    "updated_by": null,
    "note": null,
    "last_change": null,
    "live_value": 85
  },
  {
    "key": "IB_REALTIME_STALE_TICK_MS",
    "env_name": "IB_REALTIME_STALE_TICK_MS",
    "owner": "relay",
    "category": "tuning",
    "type": "int",
    "apply": "live",
    "rule": "free",
    "narrow_only": false,
    "confirm_rule": null,
    "label": "Stale tick threshold",
    "unit": "ms",
    "description": "Tick age after which the relay marks a symbol stale.",
    "consumers": [
      "relay stale computation"
    ],
    "choices": [],
    "bounds": [
      1000,
      3600000
    ],
    "default": 60000,
    "env_known": false,
    "env_value": null,
    "db_value": null,
    "db_value_invalid": false,
    "effective": null,
    "updated_at": null,
    "updated_by": null,
    "note": null,
    "last_change": null,
    "live_value": 60000
  },
  {
    "key": "FUTU_QUOTE_QUOTA",
    "env_name": "FUTU_QUOTE_QUOTA",
    "owner": "futu-quotes",
    "category": "tuning",
    "type": "int",
    "apply": "live",
    "rule": "free",
    "narrow_only": false,
    "confirm_rule": null,
    "label": "Futu quote quota",
    "unit": "units",
    "description": "Futu subscription quota the quote service may use.",
    "consumers": [
      "futu-quotes admission"
    ],
    "choices": [],
    "bounds": [
      1,
      100000
    ],
    "default": 800,
    "env_known": false,
    "env_value": null,
    "db_value": null,
    "db_value_invalid": false,
    "effective": null,
    "updated_at": null,
    "updated_by": null,
    "note": null,
    "last_change": null
  },
  {
    "key": "FUTU_UNITS_PER_SYMBOL",
    "env_name": "FUTU_UNITS_PER_SYMBOL",
    "owner": "futu-quotes",
    "category": "tuning",
    "type": "int",
    "apply": "live",
    "rule": "free",
    "narrow_only": false,
    "confirm_rule": null,
    "label": "Quota units per symbol",
    "unit": "units",
    "description": "Quota units one subscribed symbol costs.",
    "consumers": [
      "futu-quotes admission"
    ],
    "choices": [],
    "bounds": [
      1,
      10
    ],
    "default": 2,
    "env_known": false,
    "env_value": null,
    "db_value": null,
    "db_value_invalid": false,
    "effective": null,
    "updated_at": null,
    "updated_by": null,
    "note": null,
    "last_change": null
  },
  {
    "key": "FUTU_BOOK_STALE_S",
    "env_name": "FUTU_BOOK_STALE_S",
    "owner": "futu-quotes",
    "category": "tuning",
    "type": "number",
    "apply": "live",
    "rule": "free",
    "narrow_only": false,
    "confirm_rule": null,
    "label": "Order-book staleness cutoff",
    "unit": "s",
    "description": "Order-book age after which a book is not sent. 0 turns the cutoff off.",
    "consumers": [
      "futu-quotes book emit"
    ],
    "choices": [],
    "bounds": [
      0,
      3600
    ],
    "default": 0,
    "env_known": false,
    "env_value": null,
    "db_value": null,
    "db_value_invalid": false,
    "effective": null,
    "updated_at": null,
    "updated_by": null,
    "note": null,
    "last_change": null
  },
  {
    "key": "FUTU_GREEK_FACTORS",
    "env_name": "FUTU_GREEK_FACTORS",
    "owner": "futu-quotes",
    "category": "tuning",
    "type": "object",
    "apply": "live",
    "rule": "free",
    "narrow_only": false,
    "confirm_rule": null,
    "label": "Futu greek factors",
    "unit": null,
    "description": "Multipliers per option greek and IV (IV is usually 0.01). Unset: greeks and IV stay empty.",
    "consumers": [
      "futu-quotes option quote emit"
    ],
    "choices": [
      "iv",
      "delta",
      "gamma",
      "theta",
      "vega"
    ],
    "bounds": null,
    "default": null,
    "env_known": false,
    "env_value": null,
    "db_value": null,
    "db_value_invalid": false,
    "effective": null,
    "updated_at": null,
    "updated_by": null,
    "note": null,
    "last_change": null
  },
  {
    "key": "XENON_FUTU_TRADING",
    "env_name": "XENON_FUTU_TRADING",
    "owner": "api",
    "category": "safety",
    "type": "set",
    "apply": "live",
    "rule": "subset",
    "narrow_only": true,
    "confirm_rule": "real_scope_changes",
    "label": "Futu trading scope",
    "unit": null,
    "description": "Futu accounts that may trade: simulate (paper) and real (live).",
    "consumers": [
      "broker_capabilities.futu_trading_envs",
      "order gates",
      "/health futu"
    ],
    "choices": [
      "simulate",
      "real"
    ],
    "bounds": null,
    "default": [],
    "env_known": true,
    "env_value": [
      "simulate"
    ],
    "db_value": null,
    "db_value_invalid": false,
    "effective": [
      "simulate"
    ],
    "updated_at": null,
    "updated_by": null,
    "note": null,
    "last_change": null
  },
  {
    "key": "XENON_FUTU_TUPLES_LIVE",
    "env_name": "XENON_FUTU_TUPLES_LIVE",
    "owner": "api",
    "category": "safety",
    "type": "set",
    "apply": "live",
    "rule": "subset",
    "narrow_only": true,
    "confirm_rule": "any_change",
    "label": "Futu live order types",
    "unit": null,
    "description": "Order types allowed on the live Futu account, by security, time in force and session.",
    "consumers": [
      "futu_orders.allowed_tuples",
      "/health futu.capabilities"
    ],
    "choices": [],
    "bounds": null,
    "default": null,
    "env_known": true,
    "env_value": [
      "OPT:LMT:DAY:RTH",
      "OPT:LMT:GTC:RTH",
      "STK:LMT:DAY:RTH",
      "STK:LMT:GTC:RTH"
    ],
    "db_value": null,
    "db_value_invalid": false,
    "effective": [
      "OPT:LMT:DAY:RTH",
      "OPT:LMT:GTC:RTH",
      "STK:LMT:DAY:RTH",
      "STK:LMT:GTC:RTH"
    ],
    "updated_at": null,
    "updated_by": null,
    "note": null,
    "last_change": null
  },
  {
    "key": "XENON_FUTU_TUPLES_PAPER",
    "env_name": "XENON_FUTU_TUPLES_PAPER",
    "owner": "api",
    "category": "safety",
    "type": "set",
    "apply": "live",
    "rule": "subset",
    "narrow_only": true,
    "confirm_rule": null,
    "label": "Futu paper order types",
    "unit": null,
    "description": "Order types allowed on the paper Futu account, by security, time in force and session.",
    "consumers": [
      "futu_orders.allowed_tuples",
      "/health futu.capabilities"
    ],
    "choices": [],
    "bounds": null,
    "default": null,
    "env_known": true,
    "env_value": [
      "OPT:LMT:DAY:RTH",
      "STK:LMT:DAY:RTH",
      "STK:MKT:DAY:RTH"
    ],
    "db_value": null,
    "db_value_invalid": false,
    "effective": [
      "OPT:LMT:DAY:RTH",
      "STK:LMT:DAY:RTH",
      "STK:MKT:DAY:RTH"
    ],
    "updated_at": null,
    "updated_by": null,
    "note": null,
    "last_change": null
  },
  {
    "key": "XENON_FUTU_MODIFY_TYPES",
    "env_name": "XENON_FUTU_MODIFY_TYPES",
    "owner": "api",
    "category": "safety",
    "type": "set",
    "apply": "live",
    "rule": "subset",
    "narrow_only": true,
    "confirm_rule": null,
    "label": "Futu modifiable order types",
    "unit": null,
    "description": "Futu order types that may be modified after placement.",
    "consumers": [
      "futu_orders.modify_types"
    ],
    "choices": [],
    "bounds": null,
    "default": null,
    "env_known": true,
    "env_value": [
      "LMT"
    ],
    "db_value": null,
    "db_value_invalid": false,
    "effective": [
      "LMT"
    ],
    "updated_at": null,
    "updated_by": null,
    "note": null,
    "last_change": null
  },
  {
    "key": "XENON_FUTU_MKT_NOTIONAL_CAP",
    "env_name": "XENON_FUTU_MKT_NOTIONAL_CAP",
    "owner": "api",
    "category": "safety",
    "type": "number",
    "apply": "live",
    "rule": "min",
    "narrow_only": true,
    "confirm_rule": null,
    "label": "Futu market-order cap",
    "unit": "USD",
    "description": "Largest notional one Futu market order may carry.",
    "consumers": [
      "quote_guard.mkt_notional_cap"
    ],
    "choices": [],
    "bounds": null,
    "default": 25000,
    "env_known": true,
    "env_value": 25000,
    "db_value": 10000,
    "db_value_invalid": false,
    "effective": 10000,
    "updated_at": "2026-10-27T14:00:00+00:00",
    "updated_by": "internal-ui",
    "note": "tighten before first live order",
    "last_change": {
      "changed_at": "2026-10-27T14:00:00+00:00",
      "changed_by": "internal-ui",
      "old_value": null,
      "new_value": 10000,
      "note": "tighten before first live order"
    }
  },
  {
    "key": "XENON_GATE4_NETTING",
    "env_name": "XENON_GATE4_NETTING",
    "owner": "api",
    "category": "safety",
    "type": "bool",
    "apply": "live",
    "rule": "and",
    "narrow_only": true,
    "confirm_rule": null,
    "label": "BAG netting in coverage gate",
    "unit": null,
    "description": "Let guaranteed IB combo (BAG) legs net against each other when the coverage gate checks shorts.",
    "consumers": [
      "gate4.netting_enabled",
      "naked_short_audit"
    ],
    "choices": [],
    "bounds": null,
    "default": false,
    "env_known": true,
    "env_value": false,
    "db_value": null,
    "db_value_invalid": false,
    "effective": false,
    "updated_at": null,
    "updated_by": null,
    "note": null,
    "last_change": null
  },
  {
    "key": "XENON_AUDIT_CANCEL",
    "env_name": "XENON_AUDIT_CANCEL",
    "owner": "api",
    "category": "safety",
    "type": "bool",
    "apply": "live",
    "rule": "free",
    "narrow_only": false,
    "confirm_rule": "turns_on",
    "label": "Naked-short audit: cancel violations",
    "unit": null,
    "description": "After each sync, cancel open orders that the naked-short audit flags.",
    "consumers": [
      "audit_enforce.enabled"
    ],
    "choices": [],
    "bounds": null,
    "default": false,
    "env_known": true,
    "env_value": false,
    "db_value": null,
    "db_value_invalid": false,
    "effective": false,
    "updated_at": null,
    "updated_by": null,
    "note": null,
    "last_change": null
  },
  {
    "key": "orders_paused",
    "env_name": null,
    "owner": "api",
    "category": "safety",
    "type": "bool",
    "apply": "live",
    "rule": "free",
    "narrow_only": false,
    "confirm_rule": null,
    "label": "Pause orders",
    "unit": null,
    "description": "Refuse every place and modify, IB and Futu. Cancel stays allowed.",
    "consumers": [
      "guards.ensure_order_allowed"
    ],
    "choices": [],
    "bounds": null,
    "default": false,
    "env_known": true,
    "env_value": false,
    "db_value": null,
    "db_value_invalid": false,
    "effective": false,
    "updated_at": null,
    "updated_by": null,
    "note": null,
    "last_change": null
  }
] as unknown as RuntimeSetting[];

export function settingsFixture(): RuntimeSetting[] {
  return structuredClone(ROWS);
}
