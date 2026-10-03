"use client";

import { useParams, useRouter } from "next/navigation";
import { Fragment, useState } from "react";
import { JsonView } from "@/components/json-view";
import { Button, Crumb, ErrorNote, KeyValue, LinkButton, Loading, Notice, PageHeader, Panel, Status, TableWrap, Tag } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtDate, truncate } from "@/lib/format";
import type { Dataset } from "@/lib/types";
import { useApi } from "@/lib/use-api";

type Item = { id: string; position: number; source_trace_id: string | null; input_preview: string | null; slices: string[][]; llm_steps: number; tool_events: number };

export default function DatasetPage() {
  const { projectId, datasetId } = useParams<{ projectId: string; datasetId: string }>();
  const router = useRouter();
  const ds = useApi<Dataset>(`/projects/${projectId}/datasets/${datasetId}`, { refreshMs: 2000, poll: (d) => d?.status === "building" });
  const items = useApi<{ items: Item[] }>(ds.data?.status === "ready" ? `/projects/${projectId}/datasets/${datasetId}/items?limit=200` : null);
  const [open, setOpen] = useState<string | null>(null);
  const detail = useApi<{ recording: Record<string, unknown> }>(open ? `/projects/${projectId}/datasets/${datasetId}/items/${open}` : null);
  const [error, setError] = useState<unknown>(null);

  if (ds.error) return <ErrorNote error={ds.error} />;
  if (!ds.data) return <Loading />;
  const d = ds.data;

  async function remove() {
    if (!window.confirm(`Delete dataset "${d.name}"? Experiments that used it are kept only if you delete them first.`)) return;
    try {
      await api(`/projects/${projectId}/datasets/${datasetId}`, { method: "DELETE" });
      router.push(`/p/${projectId}/datasets`);
    } catch (err) {
      setError(err);
    }
  }

  return (
    <>
      <PageHeader
        crumb={<Crumb href={`/p/${projectId}/datasets`}>Datasets</Crumb>}
        title={d.name}
        description={d.description}
        actions={
          <>
            <Button variant="danger" onClick={() => void remove()}>
              Delete
            </Button>
            {d.status === "ready" ? (
              <LinkButton href={`/p/${projectId}/experiments/new?dataset=${d.id}`} variant="primary">
                Run an experiment
              </LinkButton>
            ) : null}
          </>
        }
      />
      <ErrorNote error={error} />
      <Panel className="mb-6 p-5">
        <KeyValue
          items={[
            ["Status", <Status key="s" value={d.status} />],
            ["Items", d.item_count.toLocaleString()],
            ["Sampling", d.sample_size ? `${d.sample_size} random traces, seed ${d.seed}` : "all matching traces"],
            ["Filters", <span key="f" className="font-mono text-[12px]">{JSON.stringify(d.filters)}</span>],
            ["Frozen", fmtDate(d.frozen_at)],
          ]}
        />
      </Panel>
      <div className="space-y-2">
        {d.status === "building" ? <Notice tone="info">Copying recordings into the dataset. This page updates when it is ready.</Notice> : null}
        {d.status === "failed" ? (
          <Notice tone="danger" title="Building failed">
            {String(d.build_info.error ?? "unknown error")}
          </Notice>
        ) : null}
        {Number(d.build_info.skipped_unreplayable ?? 0) > 0 ? <Notice>{String(d.build_info.skipped_unreplayable)} matching traces had no LLM call and were left out.</Notice> : null}
      </div>
      {d.status === "building" ? <Loading rows={4} /> : null}
      {items.data ? (
        <div className="mt-4">
          <TableWrap>
            <table className="data-table">
              <thead>
                <tr>
                  <th className="num">#</th>
                  <th>Input</th>
                  <th>Slices</th>
                  <th className="num">LLM steps</th>
                  <th className="num">Recorded tool results</th>
                </tr>
              </thead>
              <tbody>
                {items.data.items.map((it) => {
                  const isOpen = open === it.id;
                  return (
                    <Fragment key={it.id}>
                      <tr data-href onClick={() => setOpen(isOpen ? null : it.id)} aria-expanded={isOpen}>
                        <td className="num text-slate">
                          <span className="inline-flex items-center gap-1.5">
                            <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden className={`text-faint transition-transform duration-200 ${isOpen ? "rotate-90" : ""}`}>
                              <path d="M3.5 2 6.5 5 3.5 8" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                            </svg>
                            {it.position + 1}
                          </span>
                        </td>
                        <td className="max-w-[520px]">{truncate(it.input_preview, 180)}</td>
                        <td>
                          <span className="flex flex-wrap gap-1">
                            {it.slices.map(([k, v]) => (
                              <Tag key={`${k}:${v}`}>
                                {k}={v}
                              </Tag>
                            ))}
                          </span>
                        </td>
                        <td className="num">{it.llm_steps}</td>
                        <td className="num">{it.tool_events}</td>
                      </tr>
                      {isOpen ? (
                        <tr>
                          <td colSpan={5} className="bg-[#f8fafd]">
                            <div className="animate-fade">{detail.data ? <JsonView value={detail.data.recording} maxHeight={480} /> : <Loading rows={2} />}</div>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </TableWrap>
        </div>
      ) : null}
    </>
  );
}
