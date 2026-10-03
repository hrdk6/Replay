"use client";

import { useParams } from "next/navigation";
import { useState } from "react";
import { CandidatesIcon } from "@/components/icons";
import { Button, EmptyState, ErrorNote, Field, Input, Loading, PageHeader, Panel, Select, TableWrap, Tag, Textarea, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtDate, truncate } from "@/lib/format";
import type { Candidate, CandidateConfig } from "@/lib/types";
import { useApi } from "@/lib/use-api";

function changes(c: CandidateConfig): string[] {
  const parts: string[] = [];
  if (c.model) parts.push(`model ${c.model}`);
  if (c.system_prompt) parts.push("new system prompt");
  if (c.prompt_template) parts.push("new prompt template");
  if (c.params && Object.keys(c.params).length) parts.push(...Object.entries(c.params).map(([k, v]) => `${k}=${v}`));
  if (c.retrieval?.top_k) parts.push(`retrieval top_k=${c.retrieval.top_k}`);
  return parts;
}

export default function CandidatesPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const list = useApi<{ candidates: Candidate[] }>(`/projects/${projectId}/candidates`);
  const keys = useApi<{ provider_keys: { id: string; provider: string; name: string }[] }>("/provider-keys");
  const [form, setForm] = useState({ name: "", provider: "", model: "", system_prompt: "", template: "", template_role: "user", temperature: "", max_tokens: "", top_k: "", provider_key_id: "" });
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [fresh, setFresh] = useState<string | null>(null);
  const toast = useToast();
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const config: CandidateConfig & { provider_key_id?: string } = {};
    if (form.provider) config.provider = form.provider;
    if (form.model) config.model = form.model;
    if (form.system_prompt) config.system_prompt = form.system_prompt;
    if (form.template) config.prompt_template = { template: form.template, role: form.template_role };
    const params: Record<string, number> = {};
    if (form.temperature) params.temperature = Number(form.temperature);
    if (form.max_tokens) params.max_tokens = Number(form.max_tokens);
    if (Object.keys(params).length) config.params = params;
    if (form.top_k) config.retrieval = { top_k: Number(form.top_k) };
    if (form.provider_key_id) config.provider_key_id = form.provider_key_id;
    try {
      const created = await api<{ id: string }>(`/projects/${projectId}/candidates`, { method: "POST", body: { name: form.name, config } });
      setForm({ ...form, name: "", system_prompt: "", template: "" });
      await list.reload();
      setFresh(created.id);
      toast("Candidate saved");
    } catch (err) {
      setError(err);
    }
    setBusy(false);
  }

  return (
    <>
      <PageHeader
        title="Candidates"
        description="A candidate is a proposed change. Anything you leave empty keeps the value recorded in each trace, so an empty candidate is a faithful baseline."
      />
      <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_400px]">
        <div>
          <ErrorNote error={list.error} />
          {!list.data ? <Loading /> : list.data.candidates.length === 0 ? (
            <EmptyState icon={<CandidatesIcon size={20} />} title="No candidates yet">
              Describe the change you want to test using the form.
            </EmptyState>
          ) : (
            <TableWrap>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Change</th>
                    <th>Created</th>
                  </tr>
                </thead>
                <tbody>
                  {list.data.candidates.map((c) => {
                    const diff = changes(c.config);
                    return (
                      <tr key={c.id} className={c.id === fresh ? "animate-rise" : undefined}>
                        <td className="font-semibold">
                          <span className="inline-flex items-center gap-2">
                            <span className="h-2 w-2 shrink-0 rounded-full bg-lagoon" aria-hidden />
                            {c.name}
                          </span>
                        </td>
                        <td className="max-w-[420px]">
                          {diff.length ? (
                            <span className="flex flex-wrap gap-1">
                              {diff.map((d) => (
                                <Tag key={d} tone="lagoon">
                                  {d}
                                </Tag>
                              ))}
                            </span>
                          ) : (
                            <span className="text-slate">Replays the recorded configuration unchanged</span>
                          )}
                          {c.config.system_prompt ? <div className="mt-1.5 rounded-md bg-[#f7f9fc] px-2 py-1 font-mono text-[12px] text-ink">{truncate(c.config.system_prompt, 160)}</div> : null}
                        </td>
                        <td className="whitespace-nowrap text-slate tabular-nums">{fmtDate(c.created_at)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </TableWrap>
          )}
        </div>
        <Panel className="h-fit p-5 lg:sticky lg:top-6">
          <h2 className="mb-3 text-[14px] font-semibold">New candidate</h2>
          <form onSubmit={create} className="space-y-4">
            <Field label="Name" htmlFor="c-name"><Input id="c-name" required value={form.name} onChange={set("name")} placeholder="Shorter system prompt" /></Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Provider" htmlFor="c-provider">
                <Select id="c-provider" value={form.provider} onChange={set("provider")}>
                  <option value="">As recorded</option>
                  <option value="openai">OpenAI</option>
                  <option value="anthropic">Anthropic</option>
                  <option value="openai_compatible">OpenAI-compatible</option>
                  <option value="simulator">Simulator (testing)</option>
                </Select>
              </Field>
              <Field label="Model" htmlFor="c-model"><Input id="c-model" value={form.model} onChange={set("model")} placeholder="As recorded" /></Field>
            </div>
            <Field label="System prompt" htmlFor="c-system" hint="Replaces the recorded system message.">
              <Textarea id="c-system" rows={5} value={form.system_prompt} onChange={set("system_prompt")} />
            </Field>
            <Field label="Prompt template" htmlFor="c-template" hint={<>Uses {"{{variable}}"} placeholders filled from the variables recorded with each trace (replay.prompt_variables in the SDK).</>}>
              <Textarea id="c-template" rows={3} value={form.template} onChange={set("template")} />
            </Field>
            <div className="grid grid-cols-3 gap-3">
              <Field label="Temperature" htmlFor="c-temp"><Input id="c-temp" type="number" step="0.1" min="0" max="2" value={form.temperature} onChange={set("temperature")} /></Field>
              <Field label="Max tokens" htmlFor="c-max"><Input id="c-max" type="number" min="1" value={form.max_tokens} onChange={set("max_tokens")} /></Field>
              <Field label="Retrieval top_k" htmlFor="c-topk"><Input id="c-topk" type="number" min="1" value={form.top_k} onChange={set("top_k")} /></Field>
            </div>
            {keys.data?.provider_keys.length ? (
              <Field label="Provider key" htmlFor="c-key">
                <Select id="c-key" value={form.provider_key_id} onChange={set("provider_key_id")}>
                  <option value="">First key for the provider</option>
                  {keys.data.provider_keys.map((k) => <option key={k.id} value={k.id}>{k.name} ({k.provider})</option>)}
                </Select>
              </Field>
            ) : null}
            <p className="text-[12.5px] text-slate">Retrieval top_k can only shrink recorded results; replay never calls your retriever.</p>
            <ErrorNote error={error} />
            <Button type="submit" variant="primary" busy={busy}>Save candidate</Button>
          </form>
        </Panel>
      </div>
    </>
  );
}
