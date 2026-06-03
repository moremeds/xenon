"use client";

import {
  AlertTriangle,
  Gauge,
  ShieldAlert,
  Sigma,
  TrendingDown,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  DEFAULT_PERFORMANCE_CHART_HEIGHT,
  DEFAULT_PERFORMANCE_CHART_MARGINS,
  DEFAULT_PERFORMANCE_CHART_WIDTH,
  buildPerformanceChartModel,
} from "@/lib/performanceChart";
import { isPerformanceBehindPortfolioSync } from "@/lib/performanceFreshness";
import type {
  PerformanceData,
  PerformanceOk,
  PerformancePeriod,
  PerformanceSeriesPoint,
} from "@/lib/types";
import { usePerformance } from "@/lib/usePerformance";
import { MarketState } from "@/lib/useMarketHours";
import ChartPanel from "./charts/ChartPanel";
import MetricDefinitionModal from "./MetricDefinitionModal";
import PerformanceFreshness from "./PerformanceFreshness";
import PerformanceHeadlineTooltip from "./PerformanceHeadlineTooltip";
import PerformancePeriodSelector from "./PerformancePeriodSelector";

const DASH = "---";

function fmtUsd(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  const abs = Math.abs(value);
  if (abs >= 1_000_000) {
    return `${value < 0 ? "-" : ""}$${(abs / 1_000_000).toFixed(2)}M`;
  }
  return `${value < 0 ? "-" : ""}$${abs.toLocaleString("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  })}`;
}

function fmtUsdExact(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return `${value < 0 ? "-" : ""}$${Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function fmtPct(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(digits)}%`;
}

function fmtRatio(value: number | null | undefined): string {
  return value != null && Number.isFinite(value) ? value.toFixed(2) : DASH;
}

function toneClass(
  value: number | null | undefined,
): "positive" | "negative" | "neutral" {
  if (value == null || !Number.isFinite(value)) return "neutral";
  return value > 0 ? "positive" : value < 0 ? "negative" : "neutral";
}

type PerformanceCardConfig = {
  id: string;
  label: string;
  title: string;
  value: string;
  change: string;
  definition: string;
  formula: string;
  tone?: "positive" | "negative" | "neutral";
};

function StatCard({
  id,
  label,
  value,
  change,
  definition,
  formula,
  onClick,
  tone = "neutral",
}: {
  id: string;
  label: string;
  value: string;
  change: string;
  definition: string;
  formula: string;
  onClick: () => void;
  tone?: "positive" | "negative" | "neutral";
}) {
  return (
    <button
      type="button"
      className="metric-card metric-card-clickable performance-card-trigger"
      data-testid={`performance-card-${id}`}
      aria-label={`${label} metric details`}
      data-definition={definition}
      data-formula={formula}
      onClick={onClick}
    >
      <div className="metric-label">{label}</div>
      <div className={`metric-value ${tone !== "neutral" ? tone : ""}`}>
        {value}
      </div>
      <div className={`metric-change ${tone}`}>{change}</div>
    </button>
  );
}

function PerformanceChart({ data }: { data: PerformanceOk }) {
  const benchmarkLabel = data.benchmark ?? "—";
  const {
    equityPath,
    benchmarkPath,
    areaPath,
    latestEquity,
    latestBenchmark,
    yAxisTicks,
    xAxisTicks,
    plotBottom,
    plotLeft,
    plotRight,
  } = useMemo(
    () =>
      buildPerformanceChartModel(
        data,
        DEFAULT_PERFORMANCE_CHART_WIDTH,
        DEFAULT_PERFORMANCE_CHART_HEIGHT,
      ),
    [data],
  );

  return (
    <ChartPanel
      family="analytical-time-series"
      title="YTD Equity Curve"
      badge={
        <span className="pill neutral">{data.series.length} SESSIONS</span>
      }
      legend={[
        { label: "Portfolio", role: "primary" },
        { label: `${benchmarkLabel} rebased`, role: "comparison" },
      ]}
      bodyClassName="performance-chart-shell"
      dataTestId="performance-chart-panel"
    >
      <svg
        data-testid="performance-equity-chart"
        viewBox={`0 0 ${DEFAULT_PERFORMANCE_CHART_WIDTH} ${DEFAULT_PERFORMANCE_CHART_HEIGHT}`}
        className="performance-chart"
        role="img"
        aria-label="YTD portfolio equity curve versus benchmark"
      >
        <defs>
          <linearGradient
            id="performanceAreaGradient"
            x1="0"
            y1="0"
            x2="0"
            y2="1"
          >
            <stop offset="0%" stopColor="var(--chart-fill-primary-start)" />
            <stop offset="100%" stopColor="var(--chart-fill-primary-end)" />
          </linearGradient>
        </defs>
        {yAxisTicks.map((tick) => {
          const isBaseline = Math.abs(tick.y - plotBottom) < 0.5;
          return (
            <line
              key={tick.value}
              x1={plotLeft}
              x2={plotRight}
              y1={tick.y}
              y2={tick.y}
              className={
                isBaseline ? "performance-axis-line" : "performance-grid-line"
              }
            />
          );
        })}
        <g data-testid="performance-y-axis">
          <line
            x1={plotLeft}
            x2={plotLeft}
            y1={DEFAULT_PERFORMANCE_CHART_MARGINS.top}
            y2={plotBottom}
            className="performance-axis-line"
          />
          {yAxisTicks.map((tick) => (
            <g key={`y-${tick.value}`} className="performance-axis-tick">
              <line
                x1={plotLeft - 6}
                x2={plotLeft}
                y1={tick.y}
                y2={tick.y}
                className="performance-axis-line"
              />
              <text
                x={plotLeft - 12}
                y={tick.y}
                textAnchor="end"
                dominantBaseline="middle"
                className="performance-axis-label"
                data-testid="performance-axis-y-label"
              >
                {tick.label}
              </text>
            </g>
          ))}
        </g>
        <path d={areaPath} fill="url(#performanceAreaGradient)" />
        <path
          d={benchmarkPath}
          className="performance-line performance-line-benchmark"
        />
        <path
          d={equityPath}
          className="performance-line performance-line-equity"
        />
        <g data-testid="performance-x-axis">
          <line
            x1={plotLeft}
            x2={plotRight}
            y1={plotBottom}
            y2={plotBottom}
            className="performance-axis-line"
          />
          {xAxisTicks.map((tick, index) => (
            <g key={`x-${tick.index}`} className="performance-axis-tick">
              <line
                x1={tick.x}
                x2={tick.x}
                y1={plotBottom}
                y2={plotBottom + 6}
                className="performance-axis-line"
              />
              <text
                x={tick.x}
                y={plotBottom + 18}
                textAnchor={
                  index === 0
                    ? "start"
                    : index === xAxisTicks.length - 1
                      ? "end"
                      : "middle"
                }
                className="performance-axis-label"
                data-testid="performance-axis-x-label"
              >
                {tick.label}
              </text>
            </g>
          ))}
        </g>
      </svg>
      <div className="performance-chart-meta">
        <div className="performance-meta-item">
          <span className="performance-meta-label">Portfolio</span>
          <span className="performance-meta-value">
            {fmtUsdExact(latestEquity)}
          </span>
        </div>
        <div className="performance-meta-item">
          <span className="performance-meta-label">
            {benchmarkLabel} Rebased
          </span>
          <span className="performance-meta-value">
            {fmtUsdExact(latestBenchmark)}
          </span>
        </div>
        <div className="performance-meta-item">
          <span className="performance-meta-label">Benchmark Return</span>
          <span
            className={`performance-meta-value ${toneClass(data.benchmark_total_return)}`}
          >
            {fmtPct(data.benchmark_total_return)}
          </span>
        </div>
      </div>
    </ChartPanel>
  );
}

function drawdownLeader(series: PerformanceSeriesPoint[]): string {
  if (series.length === 0) return DASH;
  const worst = series.reduce(
    (acc, point) => (point.drawdown < acc.drawdown ? point : acc),
    series[0],
  );
  return worst?.date ?? DASH;
}

type PerformancePanelProps = {
  portfolioLastSync?: string | null;
  marketState?: MarketState;
  /** Broker tab — IB (default) or FUTU. Determines which /performance scope to load. */
  broker?: "IB" | "FUTU";
};

export default function PerformancePanel({
  portfolioLastSync = null,
  marketState,
  broker = "IB",
}: PerformancePanelProps) {
  const isMarketActive = marketState !== MarketState.CLOSED;
  const [period, setPeriod] = useState<PerformancePeriod>("YTD");
  const { data, loading, error, syncNow } = usePerformance(
    isMarketActive,
    broker,
    period,
  );
  const [activeCardId, setActiveCardId] = useState<string | null>(null);
  const requestedPortfolioSyncRef = useRef<string | null>(null);

  useEffect(() => {
    if (!data || data.status !== "ok" || !portfolioLastSync) return;
    if (!isPerformanceBehindPortfolioSync(data, portfolioLastSync)) return;
    if (requestedPortfolioSyncRef.current === portfolioLastSync) return;
    requestedPortfolioSyncRef.current = portfolioLastSync;
    syncNow();
  }, [data, portfolioLastSync, syncNow]);

  const cardConfigs = useMemo<PerformanceCardConfig[]>(() => {
    if (!data || data.status !== "ok") return [];
    const { summary, benchmark } = data;
    const benchmarkLabel = benchmark ?? "—";

    return [
      {
        id: "ytd-return",
        label: "YTD Return",
        title: "YTD Return",
        value: fmtPct(summary.total_return),
        change: `${fmtUsd(summary.pnl)} P&L`,
        tone: toneClass(summary.total_return),
        definition:
          "Cumulative return from the first trading session of the year through the current portfolio snapshot.",
        formula:
          "YTD Return = (Ending Equity / Starting Equity) - 1\n" +
          "P&L = Ending Equity - Starting Equity",
      },
      {
        id: "sharpe-ratio",
        label: "Sharpe Ratio",
        title: "Sharpe Ratio",
        value: fmtRatio(summary.sharpe_ratio),
        change: `VOL ${fmtPct(summary.annualized_volatility)}`,
        tone: toneClass(summary.sharpe_ratio),
        definition: "Risk-adjusted return per unit of total volatility.",
        formula:
          "Sharpe Ratio = Mean(Daily Returns) / StdDev(Daily Returns) * sqrt(252)",
      },
      {
        id: "sortino-ratio",
        label: "Sortino Ratio",
        title: "Sortino Ratio",
        value: fmtRatio(summary.sortino_ratio),
        change: `DN DEV ${fmtPct(summary.downside_deviation)}`,
        tone: toneClass(summary.sortino_ratio),
        definition:
          "Risk-adjusted return that only penalizes downside volatility.",
        formula:
          "Sortino Ratio = Mean(Daily Returns) / Downside Deviation * sqrt(252)",
      },
      {
        id: "max-drawdown",
        label: "Max Drawdown",
        title: "Max Drawdown",
        value: fmtPct(summary.max_drawdown),
        change: `${summary.max_drawdown_duration_days} DAYS`,
        tone: toneClass(summary.max_drawdown),
        definition: "Largest peak-to-trough decline in the YTD equity curve.",
        formula:
          "Drawdown_t = (Equity_t / Running Peak_t) - 1\n" +
          "Max Drawdown = minimum Drawdown_t over the YTD curve",
      },
      {
        id: "beta",
        label: "Beta",
        title: "Beta",
        value: fmtRatio(summary.beta),
        change: benchmarkLabel,
        tone: summary.beta != null ? toneClass(summary.beta - 1) : "neutral",
        definition: `Sensitivity of portfolio returns to ${benchmarkLabel}.`,
        formula: `Beta = Cov(Portfolio, ${benchmarkLabel}) / Var(${benchmarkLabel})`,
      },
      {
        id: "alpha",
        label: "Alpha",
        title: "Alpha",
        value: fmtPct(summary.alpha),
        change: "ANNUALIZED",
        tone: toneClass(summary.alpha),
        definition: `Annualized excess return after adjusting for ${benchmarkLabel} beta.`,
        formula: `Alpha = (Mean(Portfolio) - Beta * Mean(${benchmarkLabel})) * 252`,
      },
      {
        id: "information-ratio",
        label: "Information Ratio",
        title: "Information Ratio",
        value: fmtRatio(summary.information_ratio),
        change: `TE ${fmtPct(summary.tracking_error)}`,
        tone: toneClass(summary.information_ratio),
        definition: `Active return per unit of benchmark-relative volatility vs ${benchmarkLabel}.`,
        formula:
          "Tracking Error = StdDev(Active Return) * sqrt(252)\n" +
          "Information Ratio = Mean(Active Return) / StdDev(Active Return) * sqrt(252)",
      },
      {
        id: "calmar-ratio",
        label: "Calmar Ratio",
        title: "Calmar Ratio",
        value: fmtRatio(summary.calmar_ratio),
        change: `CUR DD ${fmtPct(summary.current_drawdown)}`,
        tone: toneClass(summary.calmar_ratio),
        definition: "Annualized return scaled by the worst drawdown.",
        formula: "Calmar Ratio = Annualized Return / abs(Max Drawdown)",
      },
    ];
  }, [data]);

  const activeCard = useMemo(
    () => cardConfigs.find((card) => card.id === activeCardId) ?? null,
    [activeCardId, cardConfigs],
  );

  if (loading && !data) {
    return (
      <div className="section">
        <div className="section-header">
          <div className="section-title">
            <Gauge size={14} />
            Performance
          </div>
          <span className="pill neutral">LOADING</span>
        </div>
        <div className="section-body performance-empty">
          Reconstructing YTD portfolio performance...
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="section">
        <div className="section-header">
          <div className="section-title">
            <ShieldAlert size={14} />
            Performance
          </div>
          <span className="pill undefined">UNAVAILABLE</span>
        </div>
        <div className="section-body performance-empty">
          {error ?? "No performance data available."}
        </div>
      </div>
    );
  }

  // Cold-start envelope (n < 5 sessions). Spec §4.
  if (data.status === "insufficient_history") {
    return (
      <div className="section" data-testid="performance-panel-insufficient">
        <div className="section-header">
          <div className="section-title">
            <Gauge size={14} />
            Performance
          </div>
          <span className="pill neutral">COLLECTING</span>
        </div>
        <div className="section-body performance-empty">
          <div>
            <strong>{data.days_collected}</strong> /{" "}
            {data.days_required_for_curve} sessions of NAV history collected.
          </div>
          <div style={{ marginTop: 8, opacity: 0.7 }}>
            Net liquidation: <strong>{fmtUsdExact(data.hero_net_liq)}</strong> (
            {data.currency})
            {data.inception_date ? (
              <> · inception {data.inception_date}</>
            ) : null}
          </div>
          <div style={{ marginTop: 8, opacity: 0.6, fontSize: "0.9em" }}>
            Risk metrics unlock at {data.days_required_for_metrics} sessions.
          </div>
        </div>
      </div>
    );
  }

  // Narrow to the OK variant for the rest of the render.
  const ok: PerformanceOk = data;
  const { summary, benchmark } = ok;
  const benchmarkLabel = benchmark ?? "—";

  return (
    <div className="performance-panel" data-testid="performance-panel">
      <div className="section performance-hero">
        <div className="section-body performance-hero-body">
          <div>
            <div className="section-label-mono">
              {ok.methodology.basis.toUpperCase()} {ok.period_label}
            </div>
            <div className="performance-hero-value">
              <PerformanceHeadlineTooltip
                summary={summary}
                currency={ok.currency}
              />
            </div>
            <div className="performance-hero-subtitle">
              Ending equity {fmtUsdExact(summary.ending_equity)}
              {benchmark ? (
                <>
                  {" "}
                  · {benchmarkLabel} {fmtPct(ok.benchmark_total_return)}
                </>
              ) : null}
              {" · as of "} {ok.as_of}
            </div>
            <PerformanceFreshness data={data} />
          </div>
          <div className="performance-hero-pills">
            <PerformancePeriodSelector value={period} onChange={setPeriod} />
            <span className="pill neutral">
              {ok.scope.broker} {ok.scope.account_env.toUpperCase()}
            </span>
            <span className="pill neutral">{summary.trading_days} DAYS</span>
            {summary.low_confidence ? (
              <span
                className="pill undefined"
                data-testid="performance-low-confidence-badge"
                title={
                  summary.sharpe_se != null
                    ? `Sharpe SE ≈ ${summary.sharpe_se.toFixed(2)} — interpret with caution`
                    : "Low statistical confidence at this sample size"
                }
              >
                LOW CONFIDENCE
              </span>
            ) : null}
            <span
              className={`pill ${summary.max_drawdown < -0.1 ? "undefined" : "defined"}`}
            >
              MAX DD {fmtPct(summary.max_drawdown)}
            </span>
          </div>
        </div>
      </div>

      <div className="section">
        <div className="section-header">
          <div className="section-title">
            <Gauge size={14} />
            Core Performance
          </div>
          <span className="pill defined">INSTITUTIONAL</span>
        </div>
        <div className="section-body">
          <div className="metrics-grid">
            {cardConfigs.slice(0, 4).map((card) => (
              <StatCard
                key={card.id}
                {...card}
                onClick={() => setActiveCardId(card.id)}
              />
            ))}
          </div>

          <div className="metrics-grid">
            {cardConfigs.slice(4).map((card) => (
              <StatCard
                key={card.id}
                {...card}
                onClick={() => setActiveCardId(card.id)}
              />
            ))}
          </div>
        </div>
      </div>

      <PerformanceChart data={ok} />

      <div className="performance-grid-2">
        <div className="section">
          <div className="section-header">
            <div className="section-title">
              <TrendingDown size={14} />
              Tail And Path Risk
            </div>
            <span className="pill neutral">DAILY</span>
          </div>
          <div className="section-body">
            <div className="performance-metric-list">
              <div>
                <span>VaR 95%</span>
                <strong>{fmtPct(summary.var_95)}</strong>
              </div>
              <div>
                <span>CVaR 95%</span>
                <strong>{fmtPct(summary.cvar_95)}</strong>
              </div>
              <div>
                <span>Tail Ratio</span>
                <strong>{fmtRatio(summary.tail_ratio)}</strong>
              </div>
              <div>
                <span>Ulcer Index</span>
                <strong>{fmtRatio(summary.ulcer_index)}</strong>
              </div>
              <div>
                <span>Worst Day</span>
                <strong>{fmtPct(summary.worst_day)}</strong>
              </div>
              <div>
                <span>Drawdown Trough</span>
                <strong>{drawdownLeader(ok.series)}</strong>
              </div>
            </div>
          </div>
        </div>

        <div className="section">
          <div className="section-header">
            <div className="section-title">
              <Sigma size={14} />
              Distribution And Capture
            </div>
            <span className="pill neutral">{benchmarkLabel}</span>
          </div>
          <div className="section-body">
            <div className="performance-metric-list">
              <div>
                <span>Hit Rate</span>
                <strong>{fmtPct(summary.hit_rate)}</strong>
              </div>
              <div>
                <span>Upside Capture</span>
                <strong>{fmtRatio(summary.upside_capture)}</strong>
              </div>
              <div>
                <span>Downside Capture</span>
                <strong>{fmtRatio(summary.downside_capture)}</strong>
              </div>
              <div>
                <span>Correlation</span>
                <strong>{fmtRatio(summary.correlation)}</strong>
              </div>
              <div>
                <span>Skew</span>
                <strong>{fmtRatio(summary.skew)}</strong>
              </div>
              <div>
                <span>Kurtosis</span>
                <strong>{fmtRatio(summary.kurtosis)}</strong>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="performance-grid-2">
        <div className="section">
          <div className="section-header">
            <div className="section-title">
              <AlertTriangle size={14} />
              Methodology
            </div>
            <span className="pill neutral">
              {ok.methodology.basis.toUpperCase()}
            </span>
          </div>
          <div className="section-body performance-meta-grid">
            <div className="performance-meta-item">
              <span className="performance-meta-label">Basis</span>
              <span className="performance-meta-value">
                {ok.methodology.basis}
              </span>
            </div>
            <div className="performance-meta-item">
              <span className="performance-meta-label">Annualization</span>
              <span className="performance-meta-value">
                {ok.methodology.annualization_periods}
              </span>
            </div>
            <div className="performance-meta-item">
              <span className="performance-meta-label">Primary Source</span>
              <span className="performance-meta-value">
                {ok.price_sources.primary}
              </span>
            </div>
            <div className="performance-meta-item">
              <span className="performance-meta-label">Benchmark Source</span>
              <span className="performance-meta-value">
                {ok.price_sources.benchmark}
              </span>
            </div>
          </div>
        </div>

        <div className="section">
          <div className="section-header">
            <div className="section-title">
              <AlertTriangle size={14} />
              Warnings
            </div>
            <span className="pill undefined">{ok.warnings.length} FLAGS</span>
          </div>
          <div className="section-body">
            <ul className="performance-note-list">
              {ok.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
              {ok.contracts_missing_history.length > 0 && (
                <li>
                  {ok.contracts_missing_history.length} contract(s) were missing
                  historical marks and were marked to zero where no price
                  history was available.
                </li>
              )}
              {ok.warnings.length === 0 &&
              ok.contracts_missing_history.length === 0 ? (
                <li style={{ opacity: 0.6 }}>No warnings.</li>
              ) : null}
            </ul>
          </div>
        </div>
      </div>

      {activeCard && (
        <MetricDefinitionModal
          open
          title={activeCard.title}
          value={activeCard.value}
          definition={activeCard.definition}
          formula={activeCard.formula}
          onClose={() => setActiveCardId(null)}
        />
      )}
    </div>
  );
}
