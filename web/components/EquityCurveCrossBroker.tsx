"use client";
import { useEffect, useRef, useState } from "react";
import * as d3 from "d3";

import { useEquityCurveCrossBroker } from "@/lib/useEquityCurve";
import ChartPanel from "@/components/charts/ChartPanel";

type Point = {
  date: string;
  IB: number | null;
  FUTU: number | null;
  Combined: number | null;
};

export function EquityCurveCrossBroker() {
  const [combined, setCombined] = useState(false);
  const { data, isLoading } = useEquityCurveCrossBroker({ combined });
  const svgRef = useRef<SVGSVGElement | null>(null);

  useEffect(() => {
    if (!data || !svgRef.current) return;
    const dates = Array.from(
      new Set([
        ...(data.per_broker.IB?.map((r) => r.date) ?? []),
        ...(data.per_broker.FUTU?.map((r) => r.date) ?? []),
        ...(data.combined?.map((r) => r.date) ?? []),
      ]),
    ).sort();
    const ibByDate = new Map(
      (data.per_broker.IB ?? []).map((r) => [r.date, r.nav]),
    );
    const fuByDate = new Map(
      (data.per_broker.FUTU ?? []).map((r) => [r.date, r.nav]),
    );
    const coByDate = new Map((data.combined ?? []).map((r) => [r.date, r.nav]));
    const points: Point[] = dates.map((d) => ({
      date: d,
      IB: ibByDate.get(d) ?? null,
      FUTU: fuByDate.get(d) ?? null,
      Combined: coByDate.get(d) ?? null,
    }));

    const svg = d3.select(svgRef.current);
    svg.selectAll("*").remove();
    const w = svgRef.current.clientWidth || 600;
    const h = 240;
    const m = { top: 10, right: 12, bottom: 22, left: 48 };

    const allNavs = points.flatMap((p) =>
      [p.IB, p.FUTU, p.Combined].filter((v): v is number => v != null),
    );
    const x = d3
      .scaleUtc()
      .domain(d3.extent(points, (p) => new Date(p.date)) as [Date, Date])
      .range([m.left, w - m.right]);
    const y = d3
      .scaleLinear()
      .domain([d3.min(allNavs) ?? 0, d3.max(allNavs) ?? 1])
      .nice()
      .range([h - m.bottom, m.top]);

    svg
      .append("g")
      .attr("transform", `translate(0,${h - m.bottom})`)
      .call(d3.axisBottom(x).ticks(6))
      .attr("color", "var(--brand-text-muted)");
    svg
      .append("g")
      .attr("transform", `translate(${m.left},0)`)
      .call(d3.axisLeft(y).ticks(5))
      .attr("color", "var(--brand-text-muted)");

    const drawLine = (
      key: "IB" | "FUTU" | "Combined",
      color: string,
      width: number,
    ) => {
      const series = points
        .filter((p) => p[key] != null)
        .map((p) => ({ d: new Date(p.date), v: p[key] as number }));
      if (series.length < 2) return;
      const line = d3
        .line<{ d: Date; v: number }>()
        .x((s) => x(s.d))
        .y((s) => y(s.v));
      svg
        .append("path")
        .datum(series)
        .attr("fill", "none")
        .attr("stroke", color)
        .attr("stroke-width", width)
        .attr("d", line);
    };
    drawLine("IB", "var(--brand-accent-cyan)", 1.6);
    drawLine("FUTU", "var(--brand-accent-violet)", 1.6);
    if (combined) drawLine("Combined", "var(--brand-accent-amber)", 1.8);
  }, [data, combined]);

  if (isLoading || !data) return <div data-testid="ecb-loading" />;

  return (
    <ChartPanel
      family="analytical-time-series"
      title="Equity curve · cross-broker"
      legend={[
        { label: "IB", color: "var(--brand-accent-cyan)" },
        { label: "FUTU", color: "var(--brand-accent-violet)" },
        ...(combined
          ? [{ label: "Combined", color: "var(--brand-accent-amber)" }]
          : []),
      ]}
      dataTestId="equity-curve-cross-broker-panel"
    >
      <div className="flex items-center justify-end pb-2">
        <label className="flex items-center gap-2 text-xs text-zinc-400">
          <input
            type="checkbox"
            checked={combined}
            onChange={(e) => setCombined(e.target.checked)}
          />
          Combined NAV
        </label>
      </div>
      <svg
        ref={svgRef}
        data-testid="equity-curve-cross-broker"
        className="w-full h-60 block"
      />
    </ChartPanel>
  );
}
