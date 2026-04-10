"use client";

import { useEffect, useRef } from "react";
import { usePathname } from "next/navigation";

/**
 * Temporary render-tracking hook. Logs render count per component every
 * `intervalMs` (default 5s), tagged with the current route so you can
 * see which PAGE is driving the renders.
 *
 * Console output example:
 *   [perf /discover] WorkspaceSections: 47 renders in 5.0s (9.4/s)
 *   [perf /discover] DiscoverSections: 2 renders in 5.0s (0.4/s)
 *
 * Remove after profiling. Do NOT ship to production.
 */
export function usePerfTracker(name: string, intervalMs = 5000) {
  const renderCount = useRef(0);
  const lastReport = useRef(Date.now());
  const pathname = usePathname();

  // Increment on every render (runs during render phase — ~zero cost)
  renderCount.current += 1;

  useEffect(() => {
    const id = setInterval(() => {
      const now = Date.now();
      const elapsed = (now - lastReport.current) / 1000;
      const count = renderCount.current;
      const rps = (count / elapsed).toFixed(1);

      const style =
        Number(rps) > 5
          ? "color: #ff6b6b; font-weight: bold"
          : Number(rps) > 1
            ? "color: #ffd93d"
            : "color: #6bff6b";

      console.log(
        `%c[perf ${pathname}] ${name}: ${count} renders in ${elapsed.toFixed(1)}s (${rps}/s)`,
        style,
      );
      renderCount.current = 0;
      lastReport.current = now;
    }, intervalMs);

    return () => clearInterval(id);
  }, [name, intervalMs, pathname]);
}
