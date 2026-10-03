"use client";

import { useParams, useRouter } from "next/navigation";
import { useMemo, useState, type ReactNode } from "react";
import { JsonView } from "@/components/json-view";
import { Button, Crumb, ErrorNote, Loading, Mono, PageHeader, Panel, Status, Tag } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtDate, fmtMs, fmtUsd } from "@/lib/format";
import type { Span, Trace } from "@/lib/types";
import { useApi } from "@/lib/use-api";

const KIND: Record<string, { color: string; chip: string }> = {
  llm: { color: "var(--color-kind-llm)", chip: "bg-lagoon-tint text-lagoon-ink" },
  tool: { color: "var(--color-kind-tool)", chip: "bg-kind-tool-tint text-kind-tool" },
  retrieval: { color: "var(--color-kind-retrieval)", chip: "bg-kind-retrieval-tint text-kind-retrieval" },
  embedding: { color: "var(--color-kind-retrieval)", chip: "bg-kind-retrieval-tint text-kind-retrieval" },
  agent: { color: "var(--color-kind-other)", chip: "bg-base-tint text-base-ink" },
  chain: { color: "var(--color-kind-other)", chip: "bg-base-tint text-base-ink" },
  other: { color: "var(--color-faint)", chip: "bg-mist text-slate" },
};
const kind = (k: string) => KIND[k] ?? KIND.other;

type Row = Span & { depth: number };

function tree(spans: Span[]): Row[] {
  const byParent = new Map<string | null, Span[]>();
  const ids = new Set(spans.map((s) => s.span_id));
  for (const s of spans) {
    const parent = s.parent_span_id && ids.has(s.parent_span_id) ? s.parent_span_id : null;
    byParent.set(parent, [...(byParent.get(parent) ?? []), s]);
  }
  const out: Row[] = [];
  const walk = (parent: string | null, depth: number) => {
    for (const s of (byParent.get(parent) ?? []).sort((a, b) => a.start_time.localeCompare(b.start_time))) {
      out.push({ ...s, depth });
      walk(s.span_id, depth + 1);
    }
  };
  walk(null, 0);
  return out;
}

export default function TracePage() {
  const { projectId, traceId } = useParams<{ projectId: string; traceId: string }>();
  const router = useRouter();
  const { data, error } = useApi<{ trace: Trace; spans: Span[] }>(`/projects/${projectId}/traces/${traceId}`);
  const rows = useMemo(() => (data ? tree(data.spans) : []), [data]);
  const [selected, setSelected] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  if (error) return <ErrorNote error={error} />;
  if (!data) return <Loading rows={6} />;
  const { trace } = data;
  const t0 = Math.min(...data.spans.map((s) => new Date(s.start_time).getTime()));
  const t1 = Math.max(...data.spans.map((s) => new Date(s.end_time ?? s.start_time).getTime()), t0 + 1);
  const span = rows.find((r) => r.id === selected) ?? rows.find((r) => r.kind === "llm") ?? rows[0];
  const kinds = [...new Set(rows.map((r) => r.kind))];

  async function remove() {
    if (!window.confirm("Delete this trace and its stored payloads? This cannot be undone.")) return;
    setDeleting(true);
    await api(`/projects/${projectId}/traces/${traceId}`, { method: "DELETE" });
    router.push(`/p/${projectId}/traces`);
  }

  return (
    <>
      <PageHeader
        crumb={<Crumb href={`/p/${projectId}/traces`}>Traces</Crumb>}
        title={trace.name ?? "Trace"}
        description={<Mono className="text-slate">{trace.trace_id}</Mono>}
        actions={
          <Button variant="danger" onClick={() => void remove()} busy={deleting}>
            Delete trace
          </Button>
        }
      />
      <Panel className="mb-8 grid grid-cols-2 gap-px overflow-hidden bg-mist sm:grid-cols-4">
        {(
          [
            ["Started", fmtDate(trace.start_time)],
            ["Duration", fmtMs(trace.duration_ms)],
            ["Status", <Status key="s" value={trace.status} />],
            ["Model", trace.model ?? "–"],
            ["Tokens", `${trace.input_tokens.toLocaleString()} in, ${trace.output_tokens.toLocaleString()} out`],
            ["Cost", fmtUsd(trace.cost_usd)],
            [
              "Tags",
              trace.tags.length ? (
                <span key="t" className="flex flex-wrap gap-1">
                  {trace.tags.map((t) => (
                    <Tag key={t}>{t}</Tag>
                  ))}
                </span>
              ) : (
                "–"
              ),
            ],
            ...Object.entries(trace.metadata).map(([k, v]) => [k, String(v)] as [string, string]),
          ] as [string, ReactNode][]
        ).map(([k, v]) => (
          <div key={k} className="min-w-0 bg-paper px-4 py-3">
            <div className="text-[12px] font-medium text-slate">{k}</div>
            <div className="mt-0.5 break-words text-[13.5px] font-semibold tabular-nums">{v}</div>
          </div>
        ))}
      </Panel>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)]">
        <div className="self-start overflow-hidden rounded-xl border border-rule bg-paper shadow-[0_1px_2px_rgb(22_32_58/0.04)]">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-rule bg-[#f8fafd] px-3.5 py-2.5 text-[12.5px] text-slate">
            <span className="font-semibold">{rows.length} spans</span>
            <span className="flex flex-wrap gap-3">
              {kinds.map((k) => (
                <span key={k} className="inline-flex items-center gap-1.5">
                  <span className="h-2 w-2 rounded-full" style={{ background: kind(k).color }} aria-hidden />
                  {k}
                </span>
              ))}
            </span>
          </div>
          <ul>
            {rows.map((r) => {
              const start = new Date(r.start_time).getTime();
              const end = new Date(r.end_time ?? r.start_time).getTime();
              const left = ((start - t0) / (t1 - t0)) * 100;
              const width = Math.max(1.5, ((end - start) / (t1 - t0)) * 100);
              const active = span?.id === r.id;
              return (
                <li key={r.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(r.id)}
                    aria-pressed={active}
                    className={`relative grid w-full grid-cols-[minmax(0,1fr)_140px] items-center gap-3 border-b border-mist px-3.5 py-2 text-left text-[13px] transition-colors ${active ? "bg-lagoon-tint/60" : "hover:bg-[#f5f9fc]"}`}
                  >
                    {active ? <span className="absolute inset-y-0 left-0 w-[3px] bg-lagoon-bright" aria-hidden /> : null}
                    <span className="flex min-w-0 items-center gap-2" style={{ paddingLeft: r.depth * 14 }}>
                      <span className={`w-[64px] shrink-0 rounded-md px-1.5 py-px text-center text-[11px] font-semibold ${kind(r.kind).chip}`}>{r.kind}</span>
                      <span className={`truncate ${r.status === "error" ? "font-medium text-unsafe-ink" : active ? "font-semibold" : ""}`}>{r.name}</span>
                    </span>
                    <span className="relative h-2.5 rounded-full bg-mist" title={fmtMs(r.duration_ms)}>
                      <span className="absolute top-0 h-2.5 rounded-full" style={{ left: `${left}%`, width: `${width}%`, background: r.status === "error" ? "var(--color-unsafe)" : kind(r.kind).color }} />
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>

        {span ? (
          <div key={span.id} className="min-w-0 animate-fade space-y-5 lg:sticky lg:top-6 lg:self-start">
            <div>
              <div className="flex items-center gap-2">
                <span className={`rounded-md px-1.5 py-px text-[11.5px] font-semibold ${kind(span.kind).chip}`}>{span.kind}</span>
                <h2 className="text-[16px] font-bold">{span.name}</h2>
              </div>
              <p className="mt-1 text-[13px] text-slate">
                {fmtMs(span.duration_ms)}
                {span.model ? `, ${span.model}` : ""}
                {span.input_tokens !== null ? `, ${span.input_tokens} in / ${span.output_tokens} out tokens` : ""}
              </p>
              {span.status_message ? <p className="mt-2 text-[13px] text-unsafe-ink">{span.status_message}</p> : null}
            </div>
            <div>
              <h3 className="mb-1.5 text-[13px] font-semibold">Input</h3>
              <JsonView value={span.input} />
            </div>
            <div>
              <h3 className="mb-1.5 text-[13px] font-semibold">Output</h3>
              <JsonView value={span.output} />
            </div>
            {Object.keys(span.attributes).length ? (
              <div>
                <h3 className="mb-1.5 text-[13px] font-semibold">Attributes</h3>
                <JsonView value={span.attributes} maxHeight={220} />
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </>
  );
}
