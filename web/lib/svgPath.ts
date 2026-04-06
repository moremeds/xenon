/**
 * Minimal SVG path generators replacing d3-shape.
 * Supports line() with monotone cubic Hermite interpolation (curveMonotoneX).
 */

/** Compute tangents for monotone cubic interpolation (Fritsch-Carlson). */
function monotoneTangents(xs: number[], ys: number[]): number[] {
  const n = xs.length;
  const t = new Array<number>(n);
  const d = new Array<number>(n - 1);
  const s = new Array<number>(n - 1);

  for (let i = 0; i < n - 1; i++) {
    d[i] = xs[i + 1] - xs[i];
    s[i] = (ys[i + 1] - ys[i]) / d[i];
  }

  t[0] = s[0];
  t[n - 1] = s[n - 2];
  for (let i = 1; i < n - 1; i++) {
    if (s[i - 1] * s[i] <= 0) {
      t[i] = 0;
    } else {
      t[i] = 3 * (d[i - 1] + d[i]) / ((2 * d[i] + d[i - 1]) / s[i - 1] + (d[i] + 2 * d[i - 1]) / s[i]);
    }
  }

  // Ensure monotonicity
  for (let i = 0; i < n - 1; i++) {
    if (Math.abs(s[i]) < 1e-30) {
      t[i] = 0;
      t[i + 1] = 0;
    } else {
      const a = t[i] / s[i];
      const b = t[i + 1] / s[i];
      const h = a * a + b * b;
      if (h > 9) {
        const tau = 3 / Math.sqrt(h);
        t[i] = tau * a * s[i];
        t[i + 1] = tau * b * s[i];
      }
    }
  }

  return t;
}

/** Build SVG path string with monotone cubic Hermite interpolation. */
export function monotonePath(points: { x: number; y: number }[]): string {
  const n = points.length;
  if (n === 0) return "";
  if (n === 1) return `M${points[0].x},${points[0].y}`;

  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const t = monotoneTangents(xs, ys);

  let path = `M${xs[0]},${ys[0]}`;
  for (let i = 0; i < n - 1; i++) {
    const dx = (xs[i + 1] - xs[i]) / 3;
    path += `C${xs[i] + dx},${ys[i] + dx * t[i]},${xs[i + 1] - dx},${ys[i + 1] - dx * t[i + 1]},${xs[i + 1]},${ys[i + 1]}`;
  }

  return path;
}

/** Build a line path generator (d3-shape line() replacement with curveMonotoneX). */
export function linePath<T>(): {
  x: (fn: (d: T, i: number) => number) => ReturnType<typeof linePath<T>>;
  y: (fn: (d: T, i: number) => number) => ReturnType<typeof linePath<T>>;
  (data: T[]): string | null;
} {
  let xFn: (d: T, i: number) => number = () => 0;
  let yFn: (d: T, i: number) => number = () => 0;

  function generator(data: T[]): string | null {
    if (data.length === 0) return null;
    const points = data.map((d, i) => ({ x: xFn(d, i), y: yFn(d, i) }));
    return monotonePath(points);
  }

  generator.x = (fn: (d: T, i: number) => number) => {
    xFn = fn;
    return generator;
  };
  generator.y = (fn: (d: T, i: number) => number) => {
    yFn = fn;
    return generator;
  };

  return generator;
}
