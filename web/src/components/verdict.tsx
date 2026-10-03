"use client";

import { useLayoutEffect, useRef } from "react";

const STYLES = {
  SAFE: { text: "text-safe-ink", word: "text-safe", tint: "bg-safe-tint", chip: "bg-safe-tint text-safe-ink", dot: "bg-safe" },
  UNSAFE: { text: "text-unsafe-ink", word: "text-unsafe", tint: "bg-unsafe-tint", chip: "bg-unsafe-tint text-unsafe-ink", dot: "bg-unsafe" },
  INCONCLUSIVE: { text: "text-unsure-ink", word: "text-unsure", tint: "bg-unsure-tint", chip: "bg-unsure-tint text-unsure-ink", dot: "bg-unsure-bright" },
} as const;

export type VerdictValue = keyof typeof STYLES;

export function verdictStyle(verdict: VerdictValue) {
  return STYLES[verdict];
}

/** The verdict as instrument signage. Large on the report, compact elsewhere. */
export function VerdictWord({ verdict, size = "lg" }: { verdict: VerdictValue; size?: "lg" | "sm" }) {
  const s = STYLES[verdict];
  if (size === "sm") {
    return <span className={`readout text-[17px] font-bold ${s.text}`}>{verdict}</span>;
  }
  return (
    <div className={`readout verdict-in text-[64px] font-extrabold sm:text-[84px] ${s.word}`} style={{ fontVariationSettings: '"opsz" 72' }}>
      {verdict}
    </div>
  );
}

/** A pill for lists and tables. */
export function VerdictChip({ verdict }: { verdict: VerdictValue | null | undefined }) {
  if (!verdict) return <span className="text-[12.5px] text-faint">no verdict</span>;
  const s = STYLES[verdict];
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full py-0.5 pl-2 pr-2.5 ${s.chip}`}>
      <span className={`h-2 w-2 rounded-full ${s.dot}`} aria-hidden />
      <span className="readout text-[15px] font-bold leading-none">{verdict}</span>
    </span>
  );
}

export function verdictTint(verdict: VerdictValue | null | undefined): string {
  return verdict ? STYLES[verdict].tint : "bg-mist";
}

/**
 * A number that counts up to its value once, when it first appears.
 * Writes to the DOM directly so it never re-renders; reduced motion shows the value at once.
 */
export function CountUp({ value, format, duration = 900, delay = 200, className = "" }: { value: number; format: (v: number) => string; duration?: number; delay?: number; className?: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  const fmt = useRef(format);
  useLayoutEffect(() => {
    fmt.current = format;
  });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) {
      el.textContent = fmt.current(value);
      return;
    }
    let raf = 0;
    const start = performance.now() + delay;
    el.textContent = fmt.current(0);
    const tick = (now: number) => {
      const t = Math.min(1, Math.max(0, (now - start) / duration));
      const eased = 1 - Math.pow(1 - t, 3);
      el.textContent = fmt.current(value * eased);
      if (t < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, duration, delay]);
  return (
    <span ref={ref} className={`tabular-nums ${className}`}>
      {format(value)}
    </span>
  );
}
