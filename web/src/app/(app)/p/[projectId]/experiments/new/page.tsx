"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { Button, ErrorNote, Field, Input, Notice, PageHeader, Panel, Select } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { fmtUsd } from "@/lib/format";
import type { Candidate, Dataset, Judge } from "@/lib/types";
import { useApi } from "@/lib/use-api";

/** Shows where the margin line falls on a 0-centred scale, updating as the margin changes. */
function MarginPreview({ margin }: { margin: number }) {
  const extent = Math.max(10, margin * 2);
  const x = (v: number) => ((v + extent) / (2 * extent)) * 100;
  return (
    <div aria-hidden className="rounded-lg bg-[#f8fafd] px-3 pb-2 pt-3">
      <div className="relative h-3 overflow-hidden rounded-full bg-safe-tint">
        <div
          className="absolute inset-y-0 left-0 bg-unsafe-tint bg-[repeating-linear-gradient(45deg,rgb(217_58_64/0.2)_0_3px,transparent_3px_7px)] transition-[width] duration-300 ease-[var(--ease-out-soft)]"
          style={{ width: `${x(-margin)}%` }}
        />
        <div className="absolute inset-y-0 w-px bg-slate/60" style={{ left: "50%" }} />
      </div>
      <div className="relative mt-1 h-4 text-[11.5px] tabular-nums">
        <span className="absolute -translate-x-1/2 font-semibold text-unsafe-ink transition-[left] duration-300 ease-[var(--ease-out-soft)]" style={{ left: `${x(-margin)}%` }}>
          −{margin}
        </span>
        {50 - x(-margin) > 10 ? (
          <span className="absolute -translate-x-1/2 text-slate" style={{ left: "50%" }}>
            0
          </span>
        ) : null}
      </div>
    </div>
  );
}

function NewExperiment() {
  const { projectId } = useParams<{ projectId: string }>();
  const search = useSearchParams();
  const router = useRouter();
  const datasets = useApi<{ datasets: Dataset[] }>(`/projects/${projectId}/datasets`);
  const candidates = useApi<{ candidates: Candidate[] }>(`/projects/${projectId}/candidates`);
  const judges = useApi<{ judges: Judge[] }>(`/projects/${projectId}/judges`);
  const [form, setForm] = useState({
    name: "",
    dataset_id: search.get("dataset") ?? "",
    candidate_id: "",
    baseline: "replay",
    judge_id: "",
    mode: "single_turn",
    repeats: "1",
    budget_usd: "5",
    margin: "5",
    max_divergence_rate: "0.2",
    min_items: "20",
  });
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });

  const ready = datasets.data?.datasets.filter((d) => d.status === "ready") ?? [];
  const missing = [
    !ready.length && { what: "a dataset", href: `/p/${projectId}/datasets/new` },
    !candidates.data?.candidates.length && { what: "a candidate", href: `/p/${projectId}/candidates` },
    !judges.data?.judges.length && { what: "a judge", href: `/p/${projectId}/judges` },
  ].filter(Boolean) as { what: string; href: string }[];
  const judge = judges.data?.judges.find((j) => j.id === form.judge_id);

  async function launch(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const baselineCandidate = form.baseline !== "replay" && form.baseline !== "recorded" ? form.baseline : null;
    try {
      const exp = await api<{ id: string }>(`/projects/${projectId}/experiments`, {
        method: "POST",
        body: {
          name: form.name || "Untitled experiment",
          dataset_id: form.dataset_id,
          candidate_id: form.candidate_id,
          baseline_candidate_id: baselineCandidate,
          baseline_mode: form.baseline === "recorded" ? "recorded" : "replay",
          judge_id: form.judge_id,
          mode: form.mode,
          repeats: Number(form.repeats),
          budget_usd: Number(form.budget_usd),
          settings: {
            margin: Number(form.margin),
            max_divergence_rate: Number(form.max_divergence_rate),
            min_items: Number(form.min_items),
          },
        },
      });
      router.push(`/p/${projectId}/experiments/${exp.id}`);
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  const estimate = error instanceof ApiError && (error.details as { estimated_cost_usd?: number } | undefined)?.estimated_cost_usd;

  return (
    <>
      <PageHeader title="New experiment" description="Compare a candidate change with a baseline on every item of a frozen dataset." />
      {missing.length ? (
        <Notice>
          Before you can run an experiment you need{" "}
          {missing.map((m, i) => (
            <span key={m.what}>
              {i ? (i === missing.length - 1 ? " and " : ", ") : ""}
              <Link className="underline" href={m.href}>{m.what}</Link>
            </span>
          ))}
          .
        </Notice>
      ) : null}
      <form onSubmit={launch} className="mt-4 grid gap-8 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="space-y-5">
          <Field label="Name" htmlFor="name"><Input id="name" value={form.name} onChange={set("name")} placeholder="Shorter system prompt vs production" /></Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Dataset" htmlFor="ds">
              <Select id="ds" required value={form.dataset_id} onChange={set("dataset_id")}>
                <option value="">Choose a dataset</option>
                {ready.map((d) => <option key={d.id} value={d.id}>{d.name} ({d.item_count} items)</option>)}
              </Select>
            </Field>
            <Field label="Judge" htmlFor="judge">
              <Select id="judge" required value={form.judge_id} onChange={set("judge_id")}>
                <option value="">Choose a judge</option>
                {judges.data?.judges.map((j) => <option key={j.id} value={j.id}>{j.name} v{j.version} ({j.mode})</option>)}
              </Select>
            </Field>
            <Field label="Candidate" htmlFor="cand">
              <Select id="cand" required value={form.candidate_id} onChange={set("candidate_id")}>
                <option value="">Choose a candidate</option>
                {candidates.data?.candidates.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </Select>
            </Field>
            <Field label="Baseline" htmlFor="base" hint="Re-running the recorded configuration exposes both arms to the same replay conditions.">
              <Select id="base" value={form.baseline} onChange={set("baseline")}>
                <option value="replay">Re-run the recorded configuration</option>
                <option value="recorded">Use recorded outputs (no re-run)</option>
                {candidates.data?.candidates.map((c) => <option key={c.id} value={c.id}>Candidate: {c.name}</option>)}
              </Select>
            </Field>
            <Field label="Replay mode" htmlFor="mode" hint={form.mode === "single_turn" ? "Replaces only the final model call; tool results stay as recorded. Best for prompt and model changes." : "Runs the whole agent loop; tool calls are matched against the recording and may diverge."}>
              <Select id="mode" value={form.mode} onChange={set("mode")}>
                <option value="single_turn">Final call only</option>
                <option value="full_agent">Full agent loop</option>
              </Select>
            </Field>
            <Field label="Repeats per item" htmlFor="rep" hint="More than 1 measures run-to-run variation.">
              <Input id="rep" type="number" min={1} max={10} value={form.repeats} onChange={set("repeats")} />
            </Field>
          </div>
          <ErrorNote error={error} />
          {estimate ? <p className="text-[13px] text-slate">Estimated cost: <span className="font-semibold text-ink">{fmtUsd(estimate, 2)}</span>.</p> : null}
          <Button type="submit" variant="primary" busy={busy} disabled={missing.length > 0}>Start experiment</Button>
        </div>
        <Panel className="h-fit space-y-4 p-5 lg:sticky lg:top-6">
          <div>
            <h2 className="text-[14px] font-semibold">Decision rule</h2>
            <p className="mt-0.5 text-[12.5px] text-slate">How much evidence a SAFE verdict needs.</p>
          </div>
          <MarginPreview margin={Number(form.margin) || 0} />
          <Field label="Allowed regression (points)" htmlFor="margin" hint="SAFE requires the whole confidence interval to stay above minus this margin. Scores are on a 0–100 scale (pass rate, rubric, or net win rate).">
            <Input id="margin" type="number" step="0.5" min={0} max={50} value={form.margin} onChange={set("margin")} />
          </Field>
          <Field label="Maximum divergence rate" htmlFor="div" hint="Above this, the verdict cannot be SAFE: replay can't vouch for runs it could not evaluate.">
            <Input id="div" type="number" step="0.05" min={0} max={1} value={form.max_divergence_rate} onChange={set("max_divergence_rate")} />
          </Field>
          <Field label="Minimum items for a verdict" htmlFor="min">
            <Input id="min" type="number" min={1} value={form.min_items} onChange={set("min_items")} />
          </Field>
          <Field label="Budget (USD)" htmlFor="budget" hint="Checked against an estimate before starting and enforced during the run.">
            <Input id="budget" type="number" step="0.5" min={0.01} value={form.budget_usd} onChange={set("budget_usd")} />
          </Field>
          {judge && judge.calibration?.status !== "good" ? (
            <Notice>This judge isn&apos;t calibrated against human labels yet, so the verdict will carry a warning.</Notice>
          ) : null}
        </Panel>
      </form>
    </>
  );
}

export default function NewExperimentPage() {
  return (
    <Suspense>
      <NewExperiment />
    </Suspense>
  );
}
