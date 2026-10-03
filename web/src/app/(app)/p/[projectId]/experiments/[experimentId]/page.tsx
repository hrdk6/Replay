"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { IntervalStrip } from "@/components/interval-strip";
import { Button, Crumb, ErrorNote, Loading, Notice, PageHeader, Panel, Section, Status, TableWrap, Tabs, Tag, useToast } from "@/components/ui";
import { CountUp, VerdictWord, verdictStyle } from "@/components/verdict";
import { api } from "@/lib/api";
import { fmtDate, fmtMs, fmtNum, fmtPct, fmtSigned, fmtUsd, sentence, splitWarning, truncate } from "@/lib/format";
import type { Block, Divergence, Experiment, Interval, PairedMetric, RateSummary, Report } from "@/lib/types";
import { useApi } from "@/lib/use-api";

type ItemRow = {
  item_id: string;
  position: number;
  input_preview: string | null;
  slices: string[][];
  baseline: ArmSummary | null;
  candidate: ArmSummary | null;
  pairwise_net: number | null;
  regressed: boolean;
};
type ArmSummary = { status: string; text: string; score: number | null; divergence: Divergence | null; error: string | null; repeat_statuses: string[] };

const ACTIVE = ["queued", "running"];

function ciText(ci: Interval | null | undefined, digits = 1): string {
  if (!ci) return "–";
  return `${fmtSigned(ci.estimate, digits)} (${fmtSigned(ci.low, digits)} to ${fmtSigned(ci.high, digits)})`;
}

function Progress({ exp }: { exp: Experiment }) {
  const pct = exp.total_items ? (exp.done_items / exp.total_items) * 100 : 0;
  const counts = exp.progress ?? {};
  const spentPct = exp.budget_usd ? Math.min(100, (exp.spent_usd / exp.budget_usd) * 100) : 0;
  return (
    <Panel className="mb-8 overflow-hidden">
      <div className="p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2.5 text-[15px] font-semibold">
            <span className="live-dot h-2.5 w-2.5 rounded-full bg-lagoon-bright" aria-hidden />
            {exp.status === "queued" ? "Waiting for a worker" : `Replaying item ${exp.done_items} of ${exp.total_items}`}
          </div>
          <div className="readout text-[30px] font-bold text-lagoon-ink tabular-nums">{Math.round(pct)}%</div>
        </div>
        <div
          className="relative mt-3 h-3 overflow-hidden rounded-full bg-mist"
          role="progressbar"
          aria-label="Items replayed"
          aria-valuenow={Math.round(pct)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div className="relative h-full overflow-hidden rounded-full bg-gradient-to-r from-lagoon to-lagoon-bright transition-[width] duration-700 ease-[var(--ease-out-soft)]" style={{ width: `${Math.max(pct, 2)}%` }}>
            <span className="progress-glint" aria-hidden />
          </div>
        </div>
        <div className="mt-3 flex flex-wrap gap-x-4 gap-y-2 text-[12.5px] text-slate">
          {Object.entries(counts).map(([k, v]) => (
            <span key={k} className="inline-flex items-center gap-1.5">
              <Status value={k} /> <span className="tabular-nums">{v}</span>
            </span>
          ))}
        </div>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-mist bg-[#f8fafd] px-5 py-2.5 text-[12.5px] text-slate">
        <span className="tabular-nums">
          {fmtUsd(exp.spent_usd, 4)} spent of {fmtUsd(exp.budget_usd, 2)} budget
        </span>
        <span className="h-1.5 w-32 overflow-hidden rounded-full bg-rule" aria-hidden>
          <span className={`block h-full rounded-full ${spentPct > 85 ? "bg-unsure-bright" : "bg-base"}`} style={{ width: `${spentPct}%` }} />
        </span>
      </div>
    </Panel>
  );
}

function AnalysisCard({ block, margin, title, explain }: { block: Block; margin: number; title: string; explain: string }) {
  return (
    <Panel className="p-5">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-[14px] font-semibold">{title}</h3>
        {block.decision ? <VerdictWord verdict={block.decision as "SAFE"} size="sm" /> : null}
      </div>
      <p className="mt-0.5 text-[12.5px] text-slate">{explain}</p>
      <div className="mt-4">
        <IntervalStrip ci={block.difference} margin={margin} size="sm" verdict={(block.decision || null) as "SAFE" | null} />
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-y-1 text-[12.5px]">
        <dt className="text-slate">Difference</dt>
        <dd className="tabular-nums">{ciText(block.difference)}</dd>
        <dt className="text-slate">Items</dt>
        <dd className="tabular-nums">{block.n_items}</dd>
        {block.baseline_mean !== null ? (
          <>
            <dt className="text-slate">Baseline / candidate</dt>
            <dd className="tabular-nums">
              <span className="text-base-ink">{fmtNum(block.baseline_mean)}</span> / <span className="text-lagoon-ink">{fmtNum(block.candidate_mean)}</span>
            </dd>
          </>
        ) : null}
      </dl>
    </Panel>
  );
}

function Rate({ label, r, arm }: { label: string; r: RateSummary; arm: "baseline" | "candidate" }) {
  const pct = (r.rate ?? 0) * 100;
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-3 text-[13px]">
        <span className="text-slate">{label}</span>
        <span className="tabular-nums">
          <span className="font-semibold">{fmtPct(r.rate, 1)}</span>{" "}
          <span className="text-faint">
            ({r.count}/{r.runs} runs{r.interval ? `, CI ${fmtPct(r.interval.low)}–${fmtPct(r.interval.high)}` : ""})
          </span>
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-mist" aria-hidden>
        <div className={`h-full rounded-full ${arm === "candidate" ? "bg-lagoon" : "bg-base"}`} style={{ width: `${Math.min(100, Math.max(pct, r.count ? 1.5 : 0))}%` }} />
      </div>
    </div>
  );
}

function MetricRow({ label, m, format }: { label: string; m: PairedMetric; format: (v: number) => string }) {
  if (!m || !m.n_items) return null;
  return (
    <tr>
      <td className="font-medium">{label}</td>
      <td className="num text-base-ink">{m.baseline ? format(m.baseline.estimate) : "–"}</td>
      <td className="num text-lagoon-ink">{m.candidate ? format(m.candidate.estimate) : "–"}</td>
      <td className="num">{m.difference ? `${m.difference.estimate >= 0 ? "+" : "−"}${format(Math.abs(m.difference.estimate))}` : "–"}</td>
      <td className="num text-slate">{m.difference ? `${format(m.difference.low)} to ${format(m.difference.high)}` : "–"}</td>
      <td className="num">{m.ratio ? `${m.ratio.toFixed(2)}×` : "–"}</td>
    </tr>
  );
}

function Readout({ report }: { report: Report }) {
  const margin = report.config.margin;
  const isPairwise = report.config.metric === "pairwise";
  const ci = report.completed_only.difference;
  const s = verdictStyle(report.verdict);
  const headline = sentence(report.headline.replace(/^(SAFE|UNSAFE|INCONCLUSIVE):\s*/, ""));
  return (
    <section className="mb-8 overflow-hidden rounded-2xl border border-rule bg-paper shadow-[0_1px_2px_rgb(22_32_58/0.04),0_12px_32px_-18px_rgb(22_32_58/0.18)] lg:grid lg:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)]">
      <div className={`graph-paper ${s.tint} relative px-6 py-7 sm:px-8`}>
        <VerdictWord verdict={report.verdict} />
        <p className="mt-4 max-w-[52ch] animate-rise text-[15.5px] font-medium leading-relaxed text-ink [animation-delay:250ms]">{headline}</p>
        {report.reasons.length ? (
          <ul className="mt-4 space-y-1.5 text-[13px] text-slate">
            {report.reasons.map((r) => (
              <li key={r} className="flex gap-2">
                <span className={`mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full ${s.dot}`} aria-hidden />
                <span>{sentence(r)}</span>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
      <div className="px-6 py-7 sm:px-8">
        <div className="text-[13px] font-semibold text-slate">{isPairwise ? "Candidate net win rate" : "Candidate minus baseline"}</div>
        {ci ? (
          <>
            <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <CountUp value={ci.estimate} format={(v) => fmtSigned(v)} className={`readout text-[60px] font-bold ${s.text}`} />
              <span className="text-[14px] text-slate">points on a 0–100 scale</span>
            </div>
            <p className="mt-1 text-[13px] text-slate tabular-nums">
              {Math.round(ci.confidence * 100)}% interval {fmtSigned(ci.low)} to {fmtSigned(ci.high)}, over {report.completed_only.n_items} items
            </p>
          </>
        ) : null}
        <div className="mt-5">
          <IntervalStrip ci={ci} margin={margin} verdict={report.verdict} label={isPairwise ? "Net win rate" : "Score difference"} animate />
        </div>
        <p className="mt-3 text-[12.5px] leading-relaxed text-slate">
          The hatched rose zone is worse than the {margin}-point regression you allowed. SAFE needs the whole {Math.round(report.config.confidence * 100)}% interval to sit right of
          that line; this uses the more conservative of the methods in Statistical detail.
        </p>
      </div>
    </section>
  );
}

function ReportView({ report }: { report: Report }) {
  const margin = report.config.margin;
  const judge = report.judge ?? {};
  const cal = judge.calibration;
  const pb = judge.position_bias;
  const tested = report.slices.filter((s) => s.difference);

  return (
    <>
      <Readout report={report} />

      {report.warnings.length ? (
        <div className="mb-8 space-y-2">
          {report.warnings.map((w) => {
            const { title, body, severe } = splitWarning(w);
            return (
              <Notice key={w} tone={severe ? "danger" : "warn"} title={title ?? undefined}>
                {body}
              </Notice>
            );
          })}
        </div>
      ) : null}

      {report.sample_size ? (
        <div className="mb-10">
          <Notice tone="info">
            {report.sample_size.n_required ? (
              <>
                About <strong>{report.sample_size.n_required.toLocaleString()}</strong> items would be needed for a confident answer. {report.sample_size.explanation}
              </>
            ) : (
              report.sample_size.explanation
            )}
          </Notice>
        </div>
      ) : null}

      <Section title="Two ways of counting diverged runs" description="When a candidate asks for a tool call the recording can't answer, its run diverges. SAFE requires both views to agree, and divergence to stay under the limit.">
        <div className="grid gap-4 md:grid-cols-2">
          <AnalysisCard block={report.completed_only} margin={margin} title="Completed runs only" explain="Items where both arms finished." />
          <AnalysisCard block={report.diverged_as_failure} margin={margin} title="Diverged runs count as failures" explain="A diverged run gets the worst possible score." />
        </div>
      </Section>

      <div className="mb-10 grid gap-4 md:grid-cols-2">
        <Panel className="space-y-3 p-5">
          <h3 className="text-[14px] font-semibold">Divergence and errors</h3>
          <Rate label="Candidate diverged" r={report.divergence.candidate} arm="candidate" />
          <Rate label="Baseline diverged" r={report.divergence.baseline} arm="baseline" />
          <Rate label="Candidate errors" r={report.failures.candidate} arm="candidate" />
          <Rate label="Baseline errors" r={report.failures.baseline} arm="baseline" />
          <p className="pt-1 text-[12.5px] text-slate">
            Limit for a SAFE verdict: {fmtPct(Number(report.config.max_divergence_rate ?? 0.2))} divergence. Mode: {report.mode === "single_turn" ? "final call only" : "full agent loop"}.
          </p>
        </Panel>
        <Panel className="space-y-2.5 p-5">
          <div className="flex items-baseline justify-between">
            <h3 className="text-[14px] font-semibold">Judge</h3>
            <Status value={cal?.status ?? "uncalibrated"} />
          </div>
          <p className="text-[13px]">
            {judge.name} v{judge.version}, {judge.model}, {judge.mode}
            {judge.mode === "absolute" ? ` (${judge.scale})` : ""}
          </p>
          {cal && cal.kappa !== null ? (
            <div className="flex items-baseline gap-3">
              <span className="readout text-[34px] font-bold text-ink">κ {cal.kappa.toFixed(2)}</span>
              <span className="text-[13px] text-slate">
                agreement with humans{cal.kappa_low !== null && cal.kappa_high !== null ? ` (CI ${cal.kappa_low.toFixed(2)}–${cal.kappa_high.toFixed(2)})` : ""} on {cal.n} labels
              </span>
            </div>
          ) : (
            <p className="text-[13px] text-slate">{sentence(cal?.reason || "no human labels yet")}.</p>
          )}
          {pb?.first_position_rate ? (
            <p className="text-[13px] text-slate">
              Picks the answer shown first in {fmtPct(pb.first_position_rate.estimate)} of decisive calls (CI {fmtPct(pb.first_position_rate.low)}–{fmtPct(pb.first_position_rate.high)}); same winner in both
              orders {fmtPct(pb.consistency_rate?.estimate)}.
            </p>
          ) : null}
          {judge.errors ? <p className="text-[13px] text-unsafe-ink">{judge.errors} judgments failed.</p> : null}
        </Panel>
      </div>

      <Section title="Slices" description="Each slice is tested separately; p-values are Holm-adjusted across all slices. A flagged slice regresses significantly even after that correction. Intervals are not adjusted.">
        {tested.length ? (
          <TableWrap>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Slice</th>
                  <th className="num">Items</th>
                  <th>Difference</th>
                  <th className="num">Estimate (CI)</th>
                  <th className="num">Adj. p</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {report.slices.map((s) => (
                  <tr key={`${s.dimension}:${s.value}`}>
                    <td>
                      <Tag>{s.dimension}</Tag> <span className="font-medium">{s.value}</span>
                    </td>
                    <td className="num">{s.n_items}</td>
                    <td className="w-[240px]">{s.difference ? <IntervalStrip ci={s.difference} margin={margin} size="sm" /> : <span className="text-[12.5px] text-faint">{s.note}</span>}</td>
                    <td className="num">{ciText(s.difference)}</td>
                    <td className="num">{s.p_adjusted === null ? "–" : s.p_adjusted < 0.001 ? "<0.001" : s.p_adjusted.toFixed(3)}</td>
                    <td>
                      {s.regression ? (
                        <span className="rounded-full bg-unsafe-tint px-2 py-0.5 text-[12px] font-semibold text-unsafe-ink">regresses</span>
                      ) : s.beyond_margin ? (
                        <span className="rounded-full bg-unsure-tint px-2 py-0.5 text-[12px] font-semibold text-unsure-ink">beyond margin</span>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        ) : (
          <p className="text-[13px] text-slate">No slice has enough items to test (minimum {String(report.config.slice_min_items ?? 10)}).</p>
        )}
      </Section>

      <Section title="Cost and latency" description="Per item, averaged over repeats; intervals are 95% bootstrap intervals over items.">
        <TableWrap>
          <table className="data-table">
            <thead>
              <tr>
                <th>Metric</th>
                <th className="num">Baseline</th>
                <th className="num">Candidate</th>
                <th className="num">Difference</th>
                <th className="num">CI</th>
                <th className="num">Ratio</th>
              </tr>
            </thead>
            <tbody>
              <MetricRow label="Cost per item" m={report.cost} format={(v) => fmtUsd(v, 5)} />
              <MetricRow label="Latency per item" m={report.latency} format={(v) => fmtMs(v)} />
            </tbody>
          </table>
        </TableWrap>
      </Section>

      <Section title="Statistical detail">
        <div className="grid gap-4 md:grid-cols-2">
          {[report.completed_only, report.diverged_as_failure].map((b) => (
            <Panel key={b.name} className="p-5 text-[12.5px]">
              <h3 className="mb-2 text-[13.5px] font-semibold">{b.name === "completed_only" ? "Completed runs only" : "Diverged as failure"}</h3>
              <table className="w-full">
                <tbody>
                  {b.intervals.map((i) => (
                    <tr key={i.method} className="border-b border-mist last:border-0">
                      <td className="py-1 text-slate">{i.method}</td>
                      <td className="py-1 text-right tabular-nums">
                        {fmtSigned(i.low, 2)} to {fmtSigned(i.high, 2)}
                      </td>
                    </tr>
                  ))}
                  {b.tests.map((t) => (
                    <tr key={t.name} className="border-b border-mist last:border-0">
                      <td className="py-1 text-slate">{t.name}</td>
                      <td className="py-1 text-right tabular-nums">
                        p = {t.p_value < 0.001 ? t.p_value.toExponential(1) : t.p_value.toFixed(3)} (n={t.n})
                      </td>
                    </tr>
                  ))}
                  {b.binary_table ? (
                    <tr>
                      <td className="py-1 text-slate">Paired outcomes</td>
                      <td className="py-1 text-right tabular-nums">
                        {Object.entries(b.binary_table)
                          .map(([k, v]) => `${k.replace("_", " ")} ${v}`)
                          .join(", ")}
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </Panel>
          ))}
        </div>
        {report.repeat_variance.candidate_mean_within_item_sd !== null && report.repeat_variance.candidate_mean_within_item_sd !== undefined ? (
          <p className="mt-3 text-[12.5px] text-slate">
            Run-to-run variation (mean within-item SD over {report.repeats} repeats): baseline {fmtNum(report.repeat_variance.baseline_mean_within_item_sd)}, candidate{" "}
            {fmtNum(report.repeat_variance.candidate_mean_within_item_sd)} points.
          </p>
        ) : null}
        {report.replay_notes.length ? (
          <ul className="mt-3 list-disc space-y-0.5 pl-5 text-[12.5px] text-slate">
            {report.replay_notes.map((n) => (
              <li key={n}>{sentence(n)}</li>
            ))}
          </ul>
        ) : null}
      </Section>
    </>
  );
}

const ITEM_TABS = [
  ["failing", "Got worse"],
  ["diverged", "Diverged"],
  ["errors", "Errors"],
  ["all", "All items"],
] as const;

function Items({ projectId, exp }: { projectId: string; exp: Experiment }) {
  const [filter, setFilter] = useState<(typeof ITEM_TABS)[number][0]>("failing");
  const { data, error } = useApi<{ items: ItemRow[]; total: number }>(`/projects/${projectId}/experiments/${exp.id}/items?filter=${filter}&limit=50`);
  const router = useRouter();
  return (
    <Section title="Items" description="Side by side: what the baseline and the candidate answered for the same recorded input.">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <Tabs label="Filter items" value={filter} options={ITEM_TABS} onChange={setFilter} />
        {data ? <span className="ml-auto text-[12.5px] text-slate tabular-nums">{data.total} items</span> : null}
      </div>
      <ErrorNote error={error} />
      {!data ? (
        <Loading />
      ) : data.items.length === 0 ? (
        <p className="rounded-xl border border-dashed border-[#cdd5e3] bg-paper/70 px-4 py-8 text-center text-[13px] text-slate">No items match this filter.</p>
      ) : (
        <TableWrap>
          <table className="data-table">
            <thead>
              <tr>
                <th>Input</th>
                <th>
                  <span className="inline-flex items-center gap-1.5">
                    <span className="h-2 w-2 rounded-full bg-base" aria-hidden />
                    Baseline
                  </span>
                </th>
                <th>
                  <span className="inline-flex items-center gap-1.5">
                    <span className="h-2 w-2 rounded-full bg-lagoon" aria-hidden />
                    Candidate
                  </span>
                </th>
                <th className="num">Result</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((it) => (
                <tr key={it.item_id} data-href onClick={() => router.push(`/p/${projectId}/experiments/${exp.id}/items/${it.item_id}`)}>
                  <td className="max-w-[260px] text-[13px]">{truncate(it.input_preview, 120)}</td>
                  <td className="max-w-[300px] text-[13px] text-slate">{it.baseline ? it.baseline.status === "completed" ? truncate(it.baseline.text, 160) : <Status value={it.baseline.status} /> : "–"}</td>
                  <td className="max-w-[300px] text-[13px]">
                    {it.candidate ? (
                      it.candidate.status === "completed" ? (
                        truncate(it.candidate.text, 160)
                      ) : (
                        <span>
                          <Status value={it.candidate.status} /> <span className="text-slate">{truncate(it.candidate.divergence?.detail ?? it.candidate.error ?? "", 110)}</span>
                        </span>
                      )
                    ) : (
                      "–"
                    )}
                  </td>
                  <td className="num whitespace-nowrap text-[13px]">
                    {it.pairwise_net !== null ? (
                      it.pairwise_net > 0 ? (
                        <span className="text-safe-ink">judge preferred candidate</span>
                      ) : it.pairwise_net < 0 ? (
                        <span className="font-medium text-unsafe-ink">judge preferred baseline</span>
                      ) : (
                        "tie"
                      )
                    ) : it.baseline?.score !== null && it.candidate?.score !== null && it.baseline && it.candidate ? (
                      <span className={`inline-flex items-center gap-1.5 rounded-md px-1.5 py-0.5 font-semibold ${it.regressed ? "bg-unsafe-tint text-unsafe-ink" : ""}`}>
                        {fmtNum(it.baseline.score, 0)}
                        <svg width="12" height="8" viewBox="0 0 12 8" aria-label="to">
                          <path d="M1 4h9M7 1l3 3-3 3" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                        {fmtNum(it.candidate.score, 0)}
                      </span>
                    ) : (
                      "–"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      )}
    </Section>
  );
}

export default function ExperimentPage() {
  const { projectId, experimentId } = useParams<{ projectId: string; experimentId: string }>();
  const router = useRouter();
  const toast = useToast();
  const { data: exp, error, reload } = useApi<Experiment>(`/projects/${projectId}/experiments/${experimentId}`, {
    refreshMs: 2500,
    poll: (d) => !!d && ACTIVE.includes(d.status),
  });
  const [busy, setBusy] = useState(false);
  if (error) return <ErrorNote error={error} />;
  if (!exp) return <Loading rows={5} />;
  const refs = exp.refs ?? {};
  const baselineLabel = exp.baseline_mode === "recorded" ? "recorded outputs" : (refs.baseline_candidate?.name ?? "recorded configuration (re-run)");

  async function cancel() {
    setBusy(true);
    try {
      await api(`/projects/${projectId}/experiments/${experimentId}/cancel`, { method: "POST" });
      await reload();
      toast("Experiment stopped");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Could not stop the experiment", "error");
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    if (!window.confirm("Delete this experiment and all of its runs?")) return;
    await api(`/projects/${projectId}/experiments/${experimentId}`, { method: "DELETE" });
    router.push(`/p/${projectId}/experiments`);
  }

  return (
    <>
      <PageHeader
        crumb={<Crumb href={`/p/${projectId}/experiments`}>Experiments</Crumb>}
        title={exp.name}
        description={
          <>
            <span className="inline-flex items-center gap-1.5 font-semibold text-lagoon-ink">
              <span className="h-2 w-2 rounded-full bg-lagoon" aria-hidden />
              {refs.candidate?.name}
            </span>{" "}
            vs{" "}
            <span className="inline-flex items-center gap-1.5 font-medium text-base-ink">
              <span className="h-2 w-2 rounded-full bg-base" aria-hidden />
              {baselineLabel}
            </span>
            , on{" "}
            <Link className="font-medium text-lagoon-ink hover:underline" href={`/p/${projectId}/datasets/${exp.dataset_id}`}>
              {refs.dataset?.name}
            </Link>
            , judged by{" "}
            <Link className="font-medium text-lagoon-ink hover:underline" href={`/p/${projectId}/judges/${exp.judge_id}`}>
              {refs.judge?.name} v{refs.judge?.version}
            </Link>
            .{" "}
            {exp.source === "ci" && exp.ci?.repository ? (
              <>
                From CI: {exp.ci.repository}
                {exp.ci.pull_request ? ` #${exp.ci.pull_request}` : ""}.{" "}
              </>
            ) : null}
            Started {fmtDate(exp.created_at)}.
          </>
        }
        actions={
          ACTIVE.includes(exp.status) ? (
            <Button variant="danger" onClick={() => void cancel()} busy={busy}>
              Stop experiment
            </Button>
          ) : (
            <>
              <Status value={exp.status} />
              <Button variant="ghost" onClick={() => void remove()}>
                Delete
              </Button>
            </>
          )
        }
      />
      {ACTIVE.includes(exp.status) ? <Progress exp={exp} /> : null}
      {exp.status === "failed" ? (
        <div className="mb-8">
          <Notice tone="danger" title="The experiment failed">
            {exp.error}
          </Notice>
        </div>
      ) : null}
      {exp.report ? <ReportView report={exp.report} /> : null}
      {exp.status !== "queued" ? <Items projectId={projectId} exp={exp} /> : null}
    </>
  );
}
