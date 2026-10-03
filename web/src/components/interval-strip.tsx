"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { Interval } from "@/lib/types";
import { fmtSigned } from "@/lib/format";

type Verdict = "SAFE" | "UNSAFE" | "INCONCLUSIVE" | null | undefined;

const COLORS = {
  SAFE: "var(--color-safe)",
  UNSAFE: "var(--color-unsafe)",
  INCONCLUSIVE: "var(--color-unsure)",
};

function classify(
  verdict: Verdict,
  ci: Interval,
  margin: number,
): keyof typeof COLORS {
  return (
    verdict ??
    (ci.high < -margin ? "UNSAFE" : ci.low > -margin ? "SAFE" : "INCONCLUSIVE")
  );
}

function niceDomain(
  ci: Interval,
  margin: number,
  min?: number,
  max?: number,
): [number, number] {
  const extent =
    Math.max(Math.abs(ci.low), Math.abs(ci.high), margin * 1.6, 4) * 1.15;
  const lo = Math.max(min ?? -Infinity, -extent);
  const hi = Math.min(max ?? Infinity, extent);
  return [lo, hi];
}

/**
 * The signature visual: a confidence interval drawn against the non-inferiority margin.
 * The track is zoned: rose is worse than the regression you said you'd accept, the pale jade
 * band is "worse, but within the margin", full jade is "better than baseline". The verdict
 * only depends on where the whole interval sits relative to the margin line.
 */
export function IntervalStrip({
  ci,
  margin,
  verdict,
  size = "lg",
  unit = "points",
  domain,
  label,
  animate = false,
}: {
  ci: Interval | null | undefined;
  margin: number;
  verdict?: Verdict;
  size?: "lg" | "sm";
  unit?: string;
  domain?: [number, number];
  label?: string;
  animate?: boolean;
}) {
  const uid = useId().replace(/:/g, "");
  // The large gauge draws at its real pixel width so its labels stay legible on a phone instead
  // of being scaled down with the whole SVG. ResizeObserver reports before the first paint.
  const box = useRef<HTMLDivElement>(null);
  const [measured, setMeasured] = useState<number | null>(null);
  useEffect(() => {
    const el = box.current;
    if (!el || size !== "lg") return;
    const ro = new ResizeObserver(([entry]) => {
      const w = Math.round(Math.min(640, entry.contentRect.width));
      if (w > 0) setMeasured(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [size]);
  if (!ci || ci.low === null || Number.isNaN(ci.low)) {
    return <span className="text-[12.5px] text-faint">no interval</span>;
  }
  const lg = size === "lg";
  const [lo, hi] = domain ?? niceDomain(ci, margin, -100, 100);
  const width = lg ? (measured ?? 640) : 220;
  const height = lg ? 96 : 24;
  const pad = lg ? 14 : 3;
  const x = (v: number) =>
    pad +
    ((Math.min(Math.max(v, lo), hi) - lo) / (hi - lo)) * (width - 2 * pad);
  const color = COLORS[classify(verdict, ci, margin)];
  const mid = lg ? 42 : 12;
  const trackH = lg ? 26 : 14;
  const barH = lg ? 10 : 6;
  const close = x(0) - x(-margin) < 64;
  const desc = `${label ?? "Difference"} ${fmtSigned(ci.estimate)} ${unit}, ${Math.round(ci.confidence * 100)}% interval ${fmtSigned(ci.low)} to ${fmtSigned(ci.high)}; margin −${margin}`;
  const ciX = x(ci.low);
  const ciW = Math.max(2, x(ci.high) - x(ci.low));
  // The interval grows outward from the point estimate.
  const origin = ciW > 2 ? ((x(ci.estimate) - ciX) / ciW) * 100 : 50;
  const top = mid - trackH / 2;
  const marginX = x(-margin);
  const zeroX = x(0);

  return (
    <div ref={box} className="w-full" style={{ maxWidth: lg ? 640 : width }}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        style={{ maxWidth: width }}
        role="img"
        aria-label={desc}
        className={`block overflow-visible ${animate ? "animate-gauge" : ""}`}
      >
        <title>{desc}</title>
        <defs>
          <pattern
            id={`hatch-${uid}`}
            width="7"
            height="7"
            patternUnits="userSpaceOnUse"
            patternTransform="rotate(45)"
          >
            <line
              x1="0"
              y1="0"
              x2="0"
              y2="7"
              stroke="var(--color-unsafe)"
              strokeOpacity="0.22"
              strokeWidth="3"
            />
          </pattern>
          <clipPath id={`track-${uid}`}>
            <rect
              x={pad}
              y={top}
              width={width - 2 * pad}
              height={trackH}
              rx={trackH / 2}
            />
          </clipPath>
        </defs>

        <g clipPath={`url(#track-${uid})`}>
          <rect
            x={pad}
            y={top}
            width={width - 2 * pad}
            height={trackH}
            fill="var(--color-mist)"
          />
          {marginX > pad ? (
            <>
              <rect
                x={pad}
                y={top}
                width={marginX - pad}
                height={trackH}
                fill="var(--color-unsafe-tint)"
              />
              <rect
                x={pad}
                y={top}
                width={marginX - pad}
                height={trackH}
                fill={`url(#hatch-${uid})`}
              />
            </>
          ) : null}
          <rect
            x={marginX}
            y={top}
            width={Math.max(0, zeroX - marginX)}
            height={trackH}
            fill="var(--color-safe-tint)"
            opacity="0.55"
          />
          <rect
            x={zeroX}
            y={top}
            width={Math.max(0, width - pad - zeroX)}
            height={trackH}
            fill="var(--color-safe-tint)"
          />
        </g>

        {/* zero and the margin you allowed */}
        <line
          x1={zeroX}
          x2={zeroX}
          y1={top - 5}
          y2={top + trackH + 5}
          stroke="var(--color-slate)"
          strokeOpacity="0.7"
          strokeWidth="1"
        />
        <line
          className="gauge-margin"
          x1={marginX}
          x2={marginX}
          y1={top - 5}
          y2={top + trackH + 5}
          stroke="var(--color-unsafe)"
          strokeWidth={lg ? 1.6 : 1.2}
        />

        {/* the interval and its point estimate */}
        <rect
          className="gauge-ci"
          x={ciX}
          y={mid - barH / 2}
          width={ciW}
          height={barH}
          rx={barH / 2}
          fill={color}
          style={{ transformOrigin: `${origin}% 50%` }}
        />
        <circle
          className="gauge-dot"
          cx={x(ci.estimate)}
          cy={mid}
          r={lg ? 8 : 4.5}
          fill="var(--color-paper)"
          stroke={color}
          strokeWidth={lg ? 3.5 : 2.5}
          style={
            lg
              ? { filter: "drop-shadow(0 1px 2px rgb(22 32 58 / 0.25))" }
              : undefined
          }
        />

        {lg ? (
          <g
            fontSize="12"
            fill="var(--color-slate)"
            style={{ fontVariantNumeric: "tabular-nums" }}
          >
            {/* When the margin and zero lines are close, push their labels apart. */}
            <text
              x={close ? marginX - 4 : marginX}
              y={height - 8}
              textAnchor={close ? "end" : "middle"}
              fill="var(--color-unsafe-ink)"
              fontWeight="600"
            >
              −{margin} margin
            </text>
            <text
              x={close ? zeroX + 4 : zeroX}
              y={height - 8}
              textAnchor={close ? "start" : "middle"}
            >
              0
            </text>
            <text
              x={pad}
              y={height - 8}
              textAnchor="start"
              fill="var(--color-faint)"
            >
              {fmtSigned(lo, 0)}
            </text>
            <text
              x={width - pad}
              y={height - 8}
              textAnchor="end"
              fill="var(--color-faint)"
            >
              {fmtSigned(hi, 0)}
            </text>
            <g className="gauge-label" fill="var(--color-ink)" fontWeight="600">
              {ciW < 64 ? (
                <text x={ciX + ciW / 2} y={top - 9} textAnchor="middle">
                  {fmtSigned(ci.low)} to {fmtSigned(ci.high)}
                </text>
              ) : (
                <>
                  <text x={ciX} y={top - 9} textAnchor="middle">
                    {fmtSigned(ci.low)}
                  </text>
                  <text x={ciX + ciW} y={top - 9} textAnchor="middle">
                    {fmtSigned(ci.high)}
                  </text>
                </>
              )}
            </g>
          </g>
        ) : null}
      </svg>
    </div>
  );
}
