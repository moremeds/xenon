"use client";

import { useEffect, useRef, useState } from "react";
import type { LivelinePoint } from "liveline";
import type { PriceData } from "@/lib/pricesProtocol";
import { generateMockHistory, getBasePrice, nextMockPrice } from "./mockPriceGenerator";

/**
 * Resolve the best available price for chart rendering.
 *
 * Priority:
 *   1. `last` — actual last-trade price (positive)
 *   2. mid = (bid + ask) / 2 — when last is absent but both sides are quoted
 *   3. null — no usable price
 *
 * Returns `{ price, isMid }` so callers can surface a visual indicator when
 * falling back to mid.
 */
export function resolveChartPrice(pd: PriceData | undefined): { price: number | null; isMid: boolean } {
  if (!pd) return { price: null, isMid: false };

  // Last-trade price takes full priority
  if (pd.last != null && pd.last > 0) {
    return { price: pd.last, isMid: false };
  }

  // Mid fallback — requires both sides of the quote
  if (pd.bid != null && pd.ask != null) {
    return { price: (pd.bid + pd.ask) / 2, isMid: true };
  }

  return { price: null, isMid: false };
}

interface PriceHistoryResult {
  data: LivelinePoint[];
  value: number;
  loading: boolean;
  /** True when chart values are derived from mid price (no last-trade available). */
  isMid: boolean;
}

/**
 * Accumulates a LivelinePoint[] from real-time price updates.
 * Seeds with mock history on mount so the chart isn't empty.
 * Falls back to mock ticks when no real price arrives for >3s.
 */
export function usePriceHistory(
  ticker: string | null,
  prices: Record<string, PriceData>,
  maxPoints = 200,
): PriceHistoryResult {
  const [data, setData] = useState<LivelinePoint[]>([]);
  const [value, setValue] = useState(0);
  const [isMid, setIsMid] = useState(false);
  const lastRealRef = useRef(0);
  const mockTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastPriceRef = useRef(0);
  const tickerRef = useRef(ticker);

  // Reset on ticker change
  useEffect(() => {
    tickerRef.current = ticker;
    if (!ticker) {
      setData([]);
      setValue(0);
      setIsMid(false);
      return;
    }

    const { price: resolvedBase, isMid: baseMid } = resolveChartPrice(prices[ticker]);
    const base = resolvedBase ?? getBasePrice(ticker);
    const seed = generateMockHistory(base, 60, 1, hashStr(ticker));
    setData(seed);
    setValue(seed[seed.length - 1]?.value ?? base);
    setIsMid(baseMid);
    lastPriceRef.current = seed[seed.length - 1]?.value ?? base;
    lastRealRef.current = 0;

    return () => {
      if (mockTimerRef.current) clearTimeout(mockTimerRef.current);
    };
  }, [ticker]); // eslint-disable-line react-hooks/exhaustive-deps

  // Append real price updates (last-trade or mid fallback)
  useEffect(() => {
    if (!ticker) return;
    const pd = prices[ticker];
    const { price: resolved, isMid: mid } = resolveChartPrice(pd);
    if (resolved == null) return;

    const now = Date.now() / 1000;
    lastRealRef.current = now;
    lastPriceRef.current = resolved;

    setData((prev) => {
      const next = [...prev, { time: now, value: resolved }];
      return next.length > maxPoints ? next.slice(next.length - maxPoints) : next;
    });
    setValue(resolved);
    setIsMid(mid);
  }, [ticker, prices[ticker ?? ""]?.last, prices[ticker ?? ""]?.bid, prices[ticker ?? ""]?.ask, maxPoints]); // eslint-disable-line react-hooks/exhaustive-deps

  // Mock tick fallback when no real data arrives
  useEffect(() => {
    if (!ticker) return;

    const tick = () => {
      if (tickerRef.current !== ticker) return;

      const now = Date.now() / 1000;
      const sinceReal = now - lastRealRef.current;

      // Only generate mock ticks if no real update in 3s
      if (sinceReal > 3 || lastRealRef.current === 0) {
        const newPrice = nextMockPrice(lastPriceRef.current);
        lastPriceRef.current = newPrice;

        setData((prev) => {
          const next = [...prev, { time: now, value: newPrice }];
          return next.length > maxPoints ? next.slice(next.length - maxPoints) : next;
        });
        setValue(newPrice);
      }

      mockTimerRef.current = setTimeout(tick, 1000);
    };

    mockTimerRef.current = setTimeout(tick, 1000);

    return () => {
      if (mockTimerRef.current) clearTimeout(mockTimerRef.current);
    };
  }, [ticker, maxPoints]);

  return { data, value, loading: data.length === 0, isMid };
}

/** Simple string hash for deterministic seeding per ticker. */
function hashStr(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = (Math.imul(31, h) + s.charCodeAt(i)) | 0;
  }
  return h;
}
