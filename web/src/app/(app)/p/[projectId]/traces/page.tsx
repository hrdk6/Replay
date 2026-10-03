"use client";

import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { TracesIcon } from "@/components/icons";
import { Button, EmptyState, ErrorNote, Input, LinkButton, Loading, PageHeader, Select, Status, Tag } from "@/components/ui";
import { api, withQuery } from "@/lib/api";
import { fmtDate, fmtMs, fmtUsd, truncate } from "@/lib/format";
import type { Trace } from "@/lib/types";
import { useApi } from "@/lib/use-api";

type Page = { traces: Trace[]; next_cursor: string | null };
type Facets = { models: string[]; tags: string[]; total: number };

function Calls({ llm, tools }: { llm: number; tools: number }) {
  return (
    <span className="inline-flex items-center justify-end gap-1.5 text-[12.5px] font-semibold">
      <span className="rounded-md bg-lagoon-tint px-1.5 py-px text-lagoon-ink" title="LLM calls">
        {llm} llm
      </span>
      <span className={`rounded-md px-1.5 py-px ${tools ? "bg-kind-tool-tint text-kind-tool" : "bg-mist text-faint"}`} title="Tool calls">
        {tools} tool
      </span>
    </span>
  );
}

export default function TracesPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const router = useRouter();
  const [q, setQ] = useState("");
  const [applied, setApplied] = useState({ q: "", status: "", tag: "", model: "" });
  const facets = useApi<Facets>(`/projects/${projectId}/traces/facets`);
  const first = useApi<Page>(withQuery(`/projects/${projectId}/traces`, { ...applied, limit: 50 }));
  // Extra pages belong to one filter combination; changing filters discards them.
  const filterKey = JSON.stringify(applied);
  const [pages, setPages] = useState<{ key: string; items: Trace[]; cursor: string | null }>({ key: "", items: [], cursor: null });
  const [loadingMore, setLoadingMore] = useState(false);
  const extra = pages.key === filterKey ? pages : null;
  const cursor = extra ? extra.cursor : (first.data?.next_cursor ?? null);

  async function loadMore() {
    if (!cursor) return;
    setLoadingMore(true);
    const page = await api<Page>(`/projects/${projectId}/traces`, { query: { ...applied, limit: 50, cursor } });
    setPages({ key: filterKey, items: [...(extra?.items ?? []), ...page.traces], cursor: page.next_cursor });
    setLoadingMore(false);
  }

  const traces = [...(first.data?.traces ?? []), ...(extra?.items ?? [])];
  const datasetHref = withQuery(`/p/${projectId}/datasets/new`, applied);
  const filtered = Object.values(applied).some(Boolean);

  return (
    <>
      <PageHeader
        title="Traces"
        description="Requests and agent runs captured from your application. Each one can be replayed against a candidate change."
        actions={
          facets.data?.total ? (
            <LinkButton href={datasetHref} variant="primary">
              {filtered ? "Create dataset from these traces" : "Create dataset"}
            </LinkButton>
          ) : null
        }
      />
      <form
        className="mb-4 flex flex-wrap items-center gap-2 rounded-xl border border-rule bg-paper p-2.5 shadow-[0_1px_2px_rgb(22_32_58/0.04)]"
        onSubmit={(e) => {
          e.preventDefault();
          setApplied((a) => ({ ...a, q }));
        }}
      >
        <div className="relative w-full sm:w-80">
          <svg className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
            <circle cx="11" cy="11" r="7" />
            <path d="m20 20-3.5-3.5" strokeLinecap="round" />
          </svg>
          <Input aria-label="Search traces" placeholder="Search name, input, output or trace id" value={q} onChange={(e) => setQ(e.target.value)} className="pl-9" />
        </div>
        <Select aria-label="Status" className="!w-auto" value={applied.status} onChange={(e) => setApplied((a) => ({ ...a, status: e.target.value }))}>
          <option value="">Any status</option>
          <option value="ok">OK</option>
          <option value="error">Errors</option>
        </Select>
        <Select aria-label="Tag" className="!w-auto" value={applied.tag} onChange={(e) => setApplied((a) => ({ ...a, tag: e.target.value }))}>
          <option value="">Any tag</option>
          {facets.data?.tags.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </Select>
        <Select aria-label="Model" className="!w-auto" value={applied.model} onChange={(e) => setApplied((a) => ({ ...a, model: e.target.value }))}>
          <option value="">Any model</option>
          {facets.data?.models.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </Select>
        <Button type="submit">Search</Button>
        {facets.data ? <span className="ml-auto pr-2 text-[12.5px] text-slate tabular-nums">{facets.data.total.toLocaleString()} traces in project</span> : null}
      </form>
      <ErrorNote error={first.error} />
      {first.loading ? (
        <Loading rows={6} />
      ) : traces.length === 0 ? (
        facets.data?.total ? (
          <EmptyState title="No traces match these filters">Clear a filter or search for something shorter.</EmptyState>
        ) : (
          <EmptyState
            icon={<TracesIcon size={20} />}
            title="No traces yet"
            action={
              <LinkButton href={`/p/${projectId}/quickstart`} variant="primary">
                Send your first trace
              </LinkButton>
            }
          >
            Instrument your app with the Python SDK or send OpenTelemetry spans; traces appear here within seconds.
          </EmptyState>
        )
      ) : (
        <div className="overflow-x-auto rounded-xl border border-rule bg-paper shadow-[0_1px_2px_rgb(22_32_58/0.04)]">
          <table className="data-table">
            <thead>
              <tr>
                <th>Started</th>
                <th>Trace</th>
                <th>Status</th>
                <th className="num">Calls</th>
                <th className="num">Tokens</th>
                <th className="num">Cost</th>
                <th className="num">Duration</th>
              </tr>
            </thead>
            <tbody>
              {traces.map((t) => (
                <tr key={t.id} data-href onClick={() => router.push(`/p/${projectId}/traces/${t.id}`)}>
                  <td className="whitespace-nowrap text-slate tabular-nums">{fmtDate(t.start_time)}</td>
                  <td className="max-w-[460px]">
                    <div className="font-semibold">{t.name ?? t.trace_id}</div>
                    <div className="truncate text-[12.5px] text-slate">{truncate(t.input_preview, 140)}</div>
                    {t.tags.length || t.model ? (
                      <div className="mt-1.5 flex flex-wrap gap-1">
                        {t.model ? <Tag tone="lagoon">{t.model}</Tag> : null}
                        {t.tags.map((tag) => (
                          <Tag key={tag}>{tag}</Tag>
                        ))}
                      </div>
                    ) : null}
                  </td>
                  <td>
                    <Status value={t.status} />
                  </td>
                  <td className="num">
                    <Calls llm={t.llm_call_count} tools={t.tool_call_count} />
                  </td>
                  <td className="num">{(t.input_tokens + t.output_tokens).toLocaleString()}</td>
                  <td className="num">{fmtUsd(t.cost_usd)}</td>
                  <td className="num">{fmtMs(t.duration_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {cursor ? (
            <div className="border-t border-rule p-3 text-center">
              <Button onClick={() => void loadMore()} busy={loadingMore}>
                Load more
              </Button>
            </div>
          ) : null}
        </div>
      )}
    </>
  );
}
