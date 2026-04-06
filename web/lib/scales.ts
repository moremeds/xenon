/**
 * Minimal replacements for d3-scale (scaleLinear, scaleTime).
 * Each scale supports domain(), range(), ticks(), invert().
 */

export interface ScaleLinear<R = number> {
  (value: number): R;
  domain(d: [number, number]): ScaleLinear<R>;
  range(r: [R, R]): ScaleLinear<R>;
  ticks(count?: number): number[];
  invert(output: R): number;
}

export function scaleLinear(): ScaleLinear<number> {
  let d0 = 0, d1 = 1, r0 = 0, r1 = 1;

  function scale(value: number): number {
    if (d1 === d0) return (r0 + r1) / 2;
    return r0 + ((value - d0) / (d1 - d0)) * (r1 - r0);
  }

  scale.domain = (d: [number, number]) => { d0 = d[0]; d1 = d[1]; return scale; };
  scale.range = (r: [number, number]) => { r0 = r[0]; r1 = r[1]; return scale; };
  scale.invert = (output: number): number => {
    if (r1 === r0) return (d0 + d1) / 2;
    return d0 + ((output - r0) / (r1 - r0)) * (d1 - d0);
  };
  scale.ticks = (count = 10): number[] => {
    return niceLinearTicks(d0, d1, count);
  };

  return scale;
}

/** Generate "nice" ticks for a linear scale (Wilkinson's algorithm simplified). */
function niceLinearTicks(lo: number, hi: number, count: number): number[] {
  if (count <= 0 || !Number.isFinite(lo) || !Number.isFinite(hi)) return [];
  if (lo === hi) return [lo];

  const range = hi - lo;
  const rawStep = range / count;
  const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const residual = rawStep / mag;
  let step: number;

  if (residual <= 1.5) step = mag;
  else if (residual <= 3.5) step = 2 * mag;
  else if (residual <= 7.5) step = 5 * mag;
  else step = 10 * mag;

  const start = Math.ceil(lo / step) * step;
  const ticks: number[] = [];
  for (let v = start; v <= hi + step * 0.001; v += step) {
    ticks.push(Math.round(v * 1e12) / 1e12); // avoid float artifacts
  }
  return ticks;
}

/* ── Time scale ────────────────────────────────── */

export interface ScaleTime {
  (value: Date): number;
  domain(d: [Date, Date]): ScaleTime;
  range(r: [number, number]): ScaleTime;
  ticks(count?: number): Date[];
  invert(output: number): Date;
}

/** Time intervals for tick generation, in milliseconds. */
const TIME_INTERVALS: [number, string][] = [
  [1000, "s"], [5000, "s"], [15000, "s"], [30000, "s"],       // seconds
  [60000, "m"], [300000, "m"], [900000, "m"], [1800000, "m"],  // minutes
  [3600000, "h"], [10800000, "h"], [21600000, "h"], [43200000, "h"], // hours
  [86400000, "d"],                                              // 1 day
  [172800000, "d"], [604800000, "w"],                           // 2 days, 1 week
  [2592000000, "M"], [7776000000, "M"],                         // 1 month, 3 months
  [31536000000, "Y"],                                           // 1 year
];

export function scaleTime(): ScaleTime {
  let d0 = 0, d1 = 1, r0 = 0, r1 = 1;

  function scale(value: Date): number {
    const t = value.getTime();
    if (d1 === d0) return (r0 + r1) / 2;
    return r0 + ((t - d0) / (d1 - d0)) * (r1 - r0);
  }

  scale.domain = (d: [Date, Date]) => { d0 = d[0].getTime(); d1 = d[1].getTime(); return scale; };
  scale.range = (r: [number, number]) => { r0 = r[0]; r1 = r[1]; return scale; };
  scale.invert = (output: number): Date => {
    if (r1 === r0) return new Date((d0 + d1) / 2);
    return new Date(d0 + ((output - r0) / (r1 - r0)) * (d1 - d0));
  };
  scale.ticks = (count = 10): Date[] => {
    if (count <= 0) return [];
    const range = d1 - d0;
    if (range <= 0) return [new Date(d0)];

    // Find best interval
    const targetStep = range / count;
    let interval = TIME_INTERVALS[TIME_INTERVALS.length - 1][0];
    for (const [ms] of TIME_INTERVALS) {
      if (ms >= targetStep * 0.7) { interval = ms; break; }
    }

    const start = Math.ceil(d0 / interval) * interval;
    const ticks: Date[] = [];
    for (let t = start; t <= d1; t += interval) {
      ticks.push(new Date(t));
    }
    return ticks;
  };

  return scale;
}
