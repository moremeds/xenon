import type { PriceData } from "@/lib/pricesProtocol";
import { fmtPrice } from "@/lib/positionUtils";

export type QuoteTelemetryFieldKey =
  | "bid"
  | "mid"
  | "ask"
  | "spread"
  | "last"
  | "volume"
  | "high"
  | "low"
  | "day";

type QuoteTone = "positive" | "negative" | null;
type QuoteTrend = "up" | "down" | null;

export type QuoteTelemetryField = {
  label: string;
  value: string;
  tone: QuoteTone;
  trend: QuoteTrend;
};

export type QuoteTelemetryModel = Record<QuoteTelemetryFieldKey, QuoteTelemetryField>;

function roundQuoteValue(n: number): number {
  return Math.round(n * 100) / 100;
}

export function getQuoteMetrics(priceData?: Pick<PriceData, "bid" | "ask"> | null): {
  bid: number | null;
  mid: number | null;
  ask: number | null;
  spread: number | null;
  spreadBps: number | null;
} {
  const bid = priceData?.bid ?? null;
  const ask = priceData?.ask ?? null;
  const mid = bid != null && ask != null ? roundQuoteValue((bid + ask) / 2) : null;
  const spread = bid != null && ask != null ? roundQuoteValue(ask - bid) : null;
  const spreadBps = spread != null && mid != null && mid > 0
    ? Math.round((spread / mid) * 10_000)
    : null;

  return { bid, mid, ask, spread, spreadBps };
}

export function formatSpreadTelemetry(
  priceData?: Pick<PriceData, "bid" | "ask"> | null,
): string {
  const { spread, mid } = getQuoteMetrics(priceData);
  if (spread == null) return "---";
  if (mid == null || mid <= 0) return fmtPrice(spread);
  return `${fmtPrice(spread)} / ${((spread / mid) * 100).toFixed(2)}%`;
}

function formatMetricValue(value: number | null): string {
  return value != null ? fmtPrice(value) : "---";
}

function lastFieldLabel(priceData: PriceData): string {
  return priceData.lastIsCalculated ? "MARK" : "LAST";
}

export function buildQuoteTelemetryModel(
  priceData: PriceData | null,
): QuoteTelemetryModel | null {
  if (!priceData) return null;

  const { bid, mid, ask } = getQuoteMetrics(priceData);
  const { last, volume, close, high, low } = priceData;
  const dayChange = last != null && last > 0 && close != null && close > 0
    ? ((last - close) / close) * 100
    : null;
  const spreadLabel = formatSpreadTelemetry(priceData);

  return {
    bid: { label: "BID", value: formatMetricValue(bid), tone: null, trend: null },
    mid: { label: "MID", value: formatMetricValue(mid), tone: null, trend: null },
    ask: { label: "ASK", value: formatMetricValue(ask), tone: null, trend: null },
    spread: { label: "SPREAD", value: spreadLabel, tone: null, trend: null },
    last: { label: lastFieldLabel(priceData), value: formatMetricValue(last), tone: null, trend: null },
    volume: {
      label: "VOLUME",
      value: volume != null ? volume.toLocaleString("en-US") : "---",
      tone: null,
      trend: null,
    },
    high: { label: "HIGH", value: formatMetricValue(high), tone: null, trend: null },
    low: { label: "LOW", value: formatMetricValue(low), tone: null, trend: null },
    day: {
      label: "DAY",
      value: dayChange != null ? `${dayChange >= 0 ? "+" : ""}${dayChange.toFixed(2)}%` : "---",
      tone: dayChange == null ? null : dayChange >= 0 ? "positive" : "negative",
      trend: dayChange == null ? null : dayChange > 0 ? "up" : dayChange < 0 ? "down" : null,
    },
  };
}
