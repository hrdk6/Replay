"use client";

import { useParams } from "next/navigation";
import { JsonView } from "@/components/json-view";
import { Crumb, ErrorNote, Loading, PageHeader, Section, Status, Tag } from "@/components/ui";
import { fmtMs, fmtNum, fmtUsd, sentence } from "@/lib/format";
import type { Divergence, ReplayStep } from "@/lib/types";
import { useApi } from "@/lib/use-api";

type Run = {
  id: string;
  arm: string;
  repeat: number;
  status: string;
  output: { text?: string; notes?: string[]; stop_reason?: string } | null;
  steps: ReplayStep[];
  divergence: Divergence | null;
  error: string | null;
  cost_usd: number;
  latency_ms: number | null;
  input_tokens: number;
  output_tokens: number;
};
type Judgment = {
  id: string;
  kind: string;
  run_id: string | null;
  arm: string | null;
  repeat: number;
  order: string | null;
  raw_choice: string | null;
  winner: string | null;
  score: number | null;
  normalized: number | null;
  reasoning: string | null;
  error: string | null;
};
type Detail = { item_id: string; slices: string[][]; conversation: string; recorded_output: string | null; recorded_tool_events: Record<string, unknown>[]; runs: Run[]; judgments: Judgment[] };

const MATCH = {
  exact: "bg-safe-tint text-safe-ink",
  fuzzy: "bg-unsure-tint text-unsure-ink",
  none: "bg-unsafe-tint text-unsafe-ink",
} as const;

function Steps({ steps }: { steps: ReplayStep[] }) {
  if (!steps.length) return null;
  return (
    <div className="mt-4 border-t border-mist pt-3">
      <h4 className="mb-2 text-[12.5px] font-semibold text-slate">Replay steps</h4>
      <ol className="relative space-y-2 pl-5 text-[12.5px] before:absolute before:bottom-1 before:left-[5px] before:top-1 before:w-px before:bg-rule">
        {steps.map((s, i) => (
          <li key={i} className="relative">
            <span
              className={`absolute -left-5 top-[5px] h-[11px] w-[11px] rounded-full border-2 border-paper ${s.type === "llm" ? "bg-kind-llm" : s.match === "none" ? "bg-unsafe" : "bg-kind-tool"}`}
              aria-hidden
            />
            {s.type === "llm" ? (
              <span className="text-slate">
                <span className="font-semibold text-ink">Model call</span> {s.model}, {s.input_tokens} in / {s.output_tokens} out, {fmtMs(s.latency_ms)}
                {s.tool_calls?.length ? `, asked for ${s.tool_calls.map((c) => c.name).join(", ")}` : ""}
              </span>
            ) : (
              <span className="flex flex-wrap items-center gap-x-1.5 gap-y-1">
                <span className="font-semibold">Tool</span>
                <span className="font-mono">{s.name}</span>
                <span className={`rounded-md px-1.5 py-px text-[11.5px] font-semibold ${MATCH[s.match]}`}>
                  {s.match === "none" ? "no recorded result" : `${s.match} match${s.match === "fuzzy" ? ` ${fmtNum(s.score, 2)}` : ""}${s.reused ? ", reused" : ""}`}
                </span>
                <span className="break-all font-mono text-faint">{JSON.stringify(s.arguments)}</span>
              </span>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}

function RunCard({ run, judgments }: { run: Run; judgments: Judgment[] }) {
  const mine = judgments.filter((j) => j.run_id === run.id);
  const candidate = run.arm === "candidate";
  return (
    <article className={`min-w-0 overflow-hidden rounded-xl border bg-paper shadow-[0_1px_2px_rgb(22_32_58/0.04)] ${candidate ? "border-lagoon-bright/35" : "border-rule"}`}>
      <div className={`h-1 ${candidate ? "bg-lagoon" : "bg-base"}`} aria-hidden />
      <div className="p-5">
        <div className="flex items-baseline justify-between gap-2">
          <h3 className={`text-[14px] font-bold capitalize ${candidate ? "text-lagoon-ink" : "text-base-ink"}`}>
            {run.arm}
            {run.repeat ? ` (repeat ${run.repeat + 1})` : ""}
          </h3>
          <Status value={run.status} />
        </div>
        <p className="mb-3 text-[12px] text-slate tabular-nums">
          {fmtUsd(run.cost_usd, 5)}, {fmtMs(run.latency_ms)}, {run.input_tokens + run.output_tokens} tokens
        </p>
        {run.output?.text ? <p className="whitespace-pre-wrap text-[14px] leading-relaxed">{run.output.text}</p> : <p className="text-[13px] text-faint">No final answer.</p>}
        {run.divergence ? (
          <p className="mt-3 rounded-lg bg-unsure-tint px-3 py-2 text-[12.5px] text-[#6b4204]">
            <span className="font-semibold">Diverged at step {run.divergence.step + 1}:</span> {run.divergence.detail}
          </p>
        ) : null}
        {run.error ? <p className="mt-3 rounded-lg bg-unsafe-tint px-3 py-2 text-[12.5px] text-unsafe-ink">{run.error}</p> : null}
        {mine.map((j) => (
          <div key={j.id} className="mt-3 flex gap-3 rounded-lg bg-[#f8fafd] px-3 py-2 text-[12.5px]">
            <span className="readout shrink-0 text-[26px] font-bold text-ink">{j.normalized !== null ? fmtNum(j.normalized, 0) : (j.score ?? "–")}</span>
            <span>
              <span className="font-semibold">
                Judge score {j.score ?? "–"}
                {j.normalized !== null ? <span className="font-normal text-slate">, {fmtNum(j.normalized, 0)} of 100</span> : null}
              </span>
              <span className="block text-slate">{j.reasoning ?? j.error}</span>
            </span>
          </div>
        ))}
        <Steps steps={run.steps} />
        {run.output?.notes?.length ? (
          <ul className="mt-3 list-disc pl-4 text-[12px] text-slate">
            {run.output.notes.map((n) => (
              <li key={n}>{sentence(n)}</li>
            ))}
          </ul>
        ) : null}
      </div>
    </article>
  );
}

export default function ItemPage() {
  const { projectId, experimentId, itemId } = useParams<{ projectId: string; experimentId: string; itemId: string }>();
  const { data, error } = useApi<Detail>(`/projects/${projectId}/experiments/${experimentId}/items/${itemId}`);
  if (error) return <ErrorNote error={error} />;
  if (!data) return <Loading rows={5} />;
  const pairwise = data.judgments.filter((j) => j.kind === "pairwise");
  const repeats = Array.from(new Set(data.runs.map((r) => r.repeat))).sort();
  return (
    <>
      <PageHeader
        crumb={<Crumb href={`/p/${projectId}/experiments/${experimentId}`}>Back to report</Crumb>}
        title="Item"
        description={
          <span className="flex flex-wrap gap-1">
            {data.slices.map(([k, v]) => (
              <Tag key={`${k}${v}`}>
                {k}={v}
              </Tag>
            ))}
          </span>
        }
      />
      <Section title="What the model saw">
        <pre className="max-h-[360px] overflow-auto whitespace-pre-wrap rounded-xl border border-rule bg-paper p-4 text-[13px] leading-relaxed">{data.conversation}</pre>
        {data.recorded_output ? (
          <div className="mt-3 rounded-xl border border-dashed border-[#cdd5e3] px-4 py-3 text-[13px]">
            <div className="mb-0.5 text-[12px] font-semibold text-slate">Recorded answer in production</div>
            {data.recorded_output}
          </div>
        ) : null}
      </Section>
      {repeats.map((rep) => (
        <Section key={rep} title={repeats.length > 1 ? `Repeat ${rep + 1}` : "Replayed answers"}>
          <div className="grid gap-4 lg:grid-cols-2">
            {data.runs
              .filter((r) => r.repeat === rep || (r.arm === "baseline" && !data.runs.some((x) => x.arm === "baseline" && x.repeat === rep) && r.repeat === 0))
              .sort((a, b) => a.arm.localeCompare(b.arm))
              .map((r) => (
                <RunCard key={r.id} run={r} judgments={data.judgments} />
              ))}
          </div>
          {pairwise.filter((j) => j.repeat === rep).length ? (
            <ul className="mt-4 space-y-2 text-[13px]">
              {pairwise
                .filter((j) => j.repeat === rep)
                .map((j) => (
                  <li key={j.id} className="flex flex-wrap items-baseline gap-x-2 rounded-lg bg-paper px-3 py-2 ring-1 ring-rule">
                    <span className="font-semibold">Judge, {j.order === "bc" ? "baseline shown first" : "candidate shown first"}:</span>
                    {j.winner ? (
                      <span
                        className={`rounded-md px-1.5 py-px text-[12px] font-semibold ${j.winner === "baseline" ? "bg-unsafe-tint text-unsafe-ink" : j.winner === "candidate" ? "bg-safe-tint text-safe-ink" : "bg-mist text-slate"}`}
                      >
                        {j.winner === "tie" ? "tie" : `${j.winner} wins`}
                      </span>
                    ) : (
                      <span className="text-unsafe-ink">{j.error}</span>
                    )}
                    {j.reasoning ? <span className="text-slate">{j.reasoning}</span> : null}
                  </li>
                ))}
            </ul>
          ) : null}
        </Section>
      ))}
      {data.recorded_tool_events.length ? (
        <Section title="Recorded tool results" description="What replay could serve back. Calls that matched none of these diverged.">
          <JsonView value={data.recorded_tool_events} maxHeight={300} />
        </Section>
      ) : null}
    </>
  );
}
