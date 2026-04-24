import { Type, type Static } from "@sinclair/typebox";

const PortfolioLegSchema = Type.Object({
  direction: Type.Union([Type.Literal("LONG"), Type.Literal("SHORT")]),
  contracts: Type.Number(),
  type: Type.Union([
    Type.Literal("Call"),
    Type.Literal("Put"),
    Type.Literal("Stock"),
  ]),
  strike: Type.Union([Type.Number(), Type.Null()]),
  entry_cost: Type.Number(),
  avg_cost: Type.Number(),
  market_price: Type.Union([Type.Number(), Type.Null()]),
  market_value: Type.Union([Type.Number(), Type.Null()]),
  market_price_is_calculated: Type.Optional(Type.Boolean()),
});

const PortfolioPositionSchema = Type.Object({
  id: Type.Number(),
  ticker: Type.String(),
  structure: Type.String(),
  structure_type: Type.String(),
  risk_profile: Type.String(),
  expiry: Type.String(),
  contracts: Type.Number(),
  direction: Type.String(),
  entry_cost: Type.Number(),
  max_risk: Type.Union([Type.Number(), Type.Null()]),
  market_value: Type.Union([Type.Number(), Type.Null()]),
  legs: Type.Array(PortfolioLegSchema),
  market_price_is_calculated: Type.Optional(Type.Boolean()),
  ib_daily_pnl: Type.Optional(Type.Union([Type.Number(), Type.Null()])),
  kelly_optimal: Type.Union([Type.Number(), Type.Null()]),
  target: Type.Union([Type.Number(), Type.Null()]),
  stop: Type.Union([Type.Number(), Type.Null()]),
  entry_date: Type.String(),
});

const AccountSummarySchema = Type.Object({
  net_liquidation: Type.Number(),
  daily_pnl: Type.Union([Type.Number(), Type.Null()]),
  unrealized_pnl: Type.Number(),
  realized_pnl: Type.Number(),
  settled_cash: Type.Number(),
  maintenance_margin: Type.Number(),
  excess_liquidity: Type.Number(),
  buying_power: Type.Number(),
  dividends: Type.Union([Type.Number(), Type.Null()]),
  cash: Type.Optional(Type.Number()),
  initial_margin: Type.Optional(Type.Number()),
  available_funds: Type.Optional(Type.Number()),
  equity_with_loan: Type.Optional(Type.Number()),
  previous_day_ewl: Type.Optional(Type.Number()),
  reg_t_equity: Type.Optional(Type.Number()),
  sma: Type.Optional(Type.Number()),
  gross_position_value: Type.Optional(Type.Number()),
});

export const PortfolioDataSchema = Type.Object({
  source: Type.Optional(Type.Union([Type.Literal("ib"), Type.Literal("futu")])),
  bankroll: Type.Number(),
  peak_value: Type.Number(),
  last_sync: Type.String(),
  positions: Type.Array(PortfolioPositionSchema),
  total_deployed_pct: Type.Number(),
  total_deployed_dollars: Type.Number(),
  remaining_capacity_pct: Type.Number(),
  position_count: Type.Number(),
  defined_risk_count: Type.Number(),
  undefined_risk_count: Type.Number(),
  avg_kelly_optimal: Type.Union([Type.Number(), Type.Null()]),
  account_summary: Type.Optional(AccountSummarySchema),
  trade_log_dates: Type.Optional(Type.Record(Type.String(), Type.String())),
});

export type PortfolioDataValidated = Static<typeof PortfolioDataSchema>;
