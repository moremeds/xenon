"use client";
import { useEffect, useRef } from "react";
import * as d3 from "d3";

import { useFutuEquityCurve } from "@/lib/useFutuHistory";
import ChartPanel from "@/components/charts/ChartPanel";

interface Props {
  from: string;
  to: string;
}

export function FutuEquityCurve({ from, to }: Props) {
  const { data, isLoading, error } = useFutuEquityCurve({ from, to });
  const svgRef = useRef<SVGSVGElement | null>(null);

  useEffect(() => {
    if (!data || !svgRef.current) return;
    const svg = d3.select(svgRef.current);
    svg.selectAll("*").remove();
    const w = svgRef.current.clientWidth || 600;
    const h = 240;
    const m = { top: 10, right: 12, bottom: 22, left: 48 };
    const x = d3
      .scaleUtc()
      .domain(d3.extent(data, (d) => new Date(d.date)) as [Date, Date])
      .range([m.left, w - m.right]);
    const y = d3
      .scaleLinear()
      .domain([
        d3.min(data, (d) => d.nav) ?? 0,
        d3.max(data, (d) => d.nav) ?? 1,
      ])
      .nice()
      .range([h - m.bottom, m.top]);
    const line = d3
      .line<(typeof data)[number]>()
      .x((d) => x(new Date(d.date)))
      .y((d) => y(d.nav));
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

    // Split into reconstructed prefix + reported suffix; render two paths.
    const idx = data.findIndex((d) => d.source === "reported");
    const reconstructed = idx === -1 ? data : data.slice(0, idx + 1);
    const reported = idx === -1 ? [] : data.slice(idx);
    if (reconstructed.length > 1) {
      svg
        .append("path")
        .datum(reconstructed)
        .attr("fill", "none")
        .attr("stroke", "var(--brand-accent-violet)")
        .attr("stroke-opacity", 0.45)
        .attr("stroke-width", 1.5)
        .attr("d", line);
    }
    if (reported.length > 1) {
      svg
        .append("path")
        .datum(reported)
        .attr("fill", "none")
        .attr("stroke", "var(--brand-accent-violet)")
        .attr("stroke-width", 1.8)
        .attr("d", line);
    }
  }, [data]);

  if (isLoading) return <div data-testid="futu-equity-curve-loading" />;
  if (error || !data)
    return <div data-testid="futu-equity-curve-error">Failed to load.</div>;

  return (
    <ChartPanel
      family="analytical-time-series"
      title="Futu equity curve"
      legend={[
        {
          label: "reconstructed",
          color: "var(--brand-accent-violet)",
        },
        {
          label: "reported",
          color: "var(--brand-accent-violet)",
        },
      ]}
      dataTestId="futu-equity-curve-panel"
    >
      <svg
        ref={svgRef}
        data-testid="futu-equity-curve"
        className="w-full h-60 block"
      />
    </ChartPanel>
  );
}
