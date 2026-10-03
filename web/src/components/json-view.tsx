"use client";

import { useState } from "react";
import { api } from "@/lib/api";

function isRef(v: unknown): v is { $ref: string; bytes: number } {
  return typeof v === "object" && v !== null && "$ref" in v;
}

/** Pretty JSON with collapse for long payloads; large offloaded payloads load on demand. */
export function JsonView({ value, maxHeight = 360 }: { value: unknown; maxHeight?: number }) {
  const [loaded, setLoaded] = useState<unknown>(undefined);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState(false);

  if (isRef(value) && loaded === undefined) {
    return (
      <button
        type="button"
        className="pressable inline-flex items-center gap-2 rounded-lg border border-dashed border-lagoon-bright/40 bg-lagoon-tint/50 px-3 py-2 text-[13px] font-medium text-lagoon-ink hover:bg-lagoon-tint"
        onClick={async () => {
          setLoading(true);
          try {
            setLoaded(await api(value.$ref.replace(/^\/api/, "")));
          } finally {
            setLoading(false);
          }
        }}
      >
        {loading ? <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-r-transparent" aria-hidden /> : null}
        {loading ? "Loading payload" : `Load payload (${(value.bytes / 1024).toFixed(0)} KB, stored separately)`}
      </button>
    );
  }
  const shown = loaded !== undefined ? loaded : value;
  if (shown === null || shown === undefined) return <span className="text-[13px] text-faint">empty</span>;
  const text = typeof shown === "string" ? shown : JSON.stringify(shown, null, 2);
  const long = text.length > 1500;
  return (
    <div className={loaded !== undefined ? "animate-fade" : undefined}>
      <pre
        className="overflow-auto whitespace-pre-wrap break-words rounded-lg border border-rule bg-[#f7f9fc] p-3.5 font-mono text-[12px] leading-relaxed text-ink"
        style={{ maxHeight: expanded ? undefined : maxHeight }}
      >
        {text}
      </pre>
      {long ? (
        <button type="button" onClick={() => setExpanded(!expanded)} className="mt-1.5 text-[12.5px] font-medium text-lagoon-ink hover:underline">
          {expanded ? "Collapse" : "Show all"}
        </button>
      ) : null}
    </div>
  );
}
