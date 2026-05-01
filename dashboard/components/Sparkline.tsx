"use client";

export function Sparkline({
  values,
  width = 100,
  height = 28,
  tone = "brand",
  fill = true,
}: {
  values: number[];
  width?: number;
  height?: number;
  tone?: "brand" | "accent" | "magenta" | "warn" | "danger";
  fill?: boolean;
}) {
  if (!values.length) {
    return <div style={{ width, height }} className="opacity-40" />;
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const step = values.length > 1 ? width / (values.length - 1) : 0;

  const points = values.map((v, i) => {
    const x = i * step;
    const y = height - ((v - min) / range) * (height - 2) - 1;
    return [x, y] as const;
  });

  const line = points.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
  const area =
    `M 0,${height} ` +
    points.map(([x, y]) => `L ${x.toFixed(2)},${y.toFixed(2)}`).join(" ") +
    ` L ${width},${height} Z`;

  const colorMap = {
    brand: { stroke: "#b48bff", fill: "rgba(139,92,246,0.25)" },
    accent: { stroke: "#10d993", fill: "rgba(16,217,147,0.22)" },
    magenta: { stroke: "#ec4899", fill: "rgba(236,72,153,0.22)" },
    warn: { stroke: "#f5a524", fill: "rgba(245,165,36,0.22)" },
    danger: { stroke: "#ef4560", fill: "rgba(239,69,96,0.22)" },
  };
  const c = colorMap[tone];
  const gradId = `spark-${tone}`;

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
    >
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={c.stroke} stopOpacity="0.45" />
          <stop offset="100%" stopColor={c.stroke} stopOpacity="0" />
        </linearGradient>
      </defs>
      {fill ? <path d={area} fill={`url(#${gradId})`} /> : null}
      <polyline
        fill="none"
        stroke={c.stroke}
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        points={line}
      />
    </svg>
  );
}

/**
 * Generate a reproducible pseudo-random walk for mock sparklines.
 * Deterministic based on seed so the SSR output matches the client.
 */
export function seededWalk(seed: number, n = 24, vol = 0.15): number[] {
  let s = seed;
  function r() {
    s = (s * 9301 + 49297) % 233280;
    return s / 233280;
  }
  const out: number[] = [];
  let v = 1;
  for (let i = 0; i < n; i++) {
    v = v * (1 + (r() - 0.5) * vol);
    out.push(v);
  }
  return out;
}
