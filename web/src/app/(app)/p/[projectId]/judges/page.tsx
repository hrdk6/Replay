"use client";

import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { JudgesIcon } from "@/components/icons";
import { Button, EmptyState, ErrorNote, Field, Input, Loading, PageHeader, Panel, Select, Status, TableWrap, Tag, Textarea } from "@/components/ui";
import { api } from "@/lib/api";
import type { Judge } from "@/lib/types";
import { useApi } from "@/lib/use-api";

const RUBRIC_EXAMPLE = `The answer must:
- state the correct order status from the tool result;
- not invent delivery dates or carriers that the tools did not return;
- be polite and under 80 words.`;

export default function JudgesPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const router = useRouter();
  const list = useApi<{ judges: Judge[] }>(`/projects/${projectId}/judges`);
  const [form, setForm] = useState({ name: "", mode: "pairwise", scale: "binary", provider: "anthropic", model: "claude-opus-5-5", rubric: "", include_reference: false });
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) =>
    setForm({ ...form, [k]: e.target.type === "checkbox" ? (e.target as HTMLInputElement).checked : e.target.value });

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const j = await api<Judge>(`/projects/${projectId}/judges`, { method: "POST", body: form });
      router.push(`/p/${projectId}/judges/${j.id}`);
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader title="Judges" description="An LLM judge scores outputs against your rubric. Verdicts are only as good as the judge, so check it against human labels before trusting it." />
      <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_420px]">
        <div>
          <ErrorNote error={list.error} />
          {!list.data ? (
            <Loading />
          ) : list.data.judges.length === 0 ? (
            <EmptyState icon={<JudgesIcon size={20} />} title="No judges yet">
              Write a rubric that describes a good answer for your use case.
            </EmptyState>
          ) : (
            <TableWrap>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Judge</th>
                    <th>Mode</th>
                    <th>Model</th>
                    <th>Calibration</th>
                  </tr>
                </thead>
                <tbody>
                  {list.data.judges.map((j) => (
                    <tr key={j.id} data-href onClick={() => router.push(`/p/${projectId}/judges/${j.id}`)}>
                      <td>
                        <span className="font-semibold">{j.name}</span> <span className="text-slate">v{j.version}</span>
                      </td>
                      <td>{j.mode === "pairwise" ? "pairwise" : `absolute, ${j.scale === "binary" ? "pass/fail" : "1–5"}`}</td>
                      <td>
                        <Tag tone="lagoon">{j.model}</Tag>
                      </td>
                      <td>
                        <span className="inline-flex flex-wrap items-center gap-2">
                          <Status value={j.calibration?.status ?? "uncalibrated"} />
                          {j.calibration?.kappa !== null && j.calibration?.kappa !== undefined ? (
                            <span className="text-[12.5px] text-slate tabular-nums">
                              κ <span className="font-semibold text-ink">{j.calibration.kappa.toFixed(2)}</span>, n={j.calibration.n}
                            </span>
                          ) : null}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          )}
        </div>
        <Panel className="h-fit p-5 lg:sticky lg:top-6">
          <h2 className="mb-3 text-[14px] font-semibold">New judge</h2>
          <form onSubmit={create} className="space-y-4">
            <Field label="Name" htmlFor="j-name"><Input id="j-name" required value={form.name} onChange={set("name")} placeholder="Support answer quality" /></Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Mode" htmlFor="j-mode">
                <Select id="j-mode" value={form.mode} onChange={set("mode")}>
                  <option value="pairwise">Pairwise (A vs B)</option>
                  <option value="absolute">Absolute score</option>
                </Select>
              </Field>
              {form.mode === "absolute" ? (
                <Field label="Scale" htmlFor="j-scale">
                  <Select id="j-scale" value={form.scale} onChange={set("scale")}>
                    <option value="binary">Pass / fail</option>
                    <option value="likert5">1 to 5</option>
                  </Select>
                </Field>
              ) : <div />}
              <Field label="Provider" htmlFor="j-provider">
                <Select id="j-provider" value={form.provider} onChange={set("provider")}>
                  <option value="anthropic">Anthropic</option>
                  <option value="openai">OpenAI</option>
                  <option value="openai_compatible">OpenAI-compatible</option>
                  <option value="simulator">Simulator (testing)</option>
                </Select>
              </Field>
              <Field label="Model" htmlFor="j-model"><Input id="j-model" required value={form.model} onChange={set("model")} /></Field>
            </div>
            <Field label="Rubric" htmlFor="j-rubric" hint="Pairwise judging runs both A/B orders to cancel position bias; that doubles judge calls.">
              <Textarea id="j-rubric" required rows={7} minLength={10} value={form.rubric} onChange={set("rubric")} placeholder={RUBRIC_EXAMPLE} />
            </Field>
            {form.mode === "absolute" ? (
              <label className="flex items-start gap-2.5 rounded-lg bg-[#f8fafd] p-3 text-[13px]">
                <input type="checkbox" checked={form.include_reference} onChange={set("include_reference")} className="mt-0.5 h-4 w-4 accent-[var(--color-lagoon)]" />
                <span>Show the judge the recorded production answer as a reference. This tends to favour answers that look like the baseline.</span>
              </label>
            ) : null}
            <ErrorNote error={error} />
            <Button type="submit" variant="primary" busy={busy}>Create judge</Button>
          </form>
        </Panel>
      </div>
    </>
  );
}
