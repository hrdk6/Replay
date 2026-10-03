"use client";

import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { Button, ErrorNote, Field, Input, PageHeader, Panel, Select } from "@/components/ui";
import { api } from "@/lib/api";
import { truncate } from "@/lib/format";
import { useApi } from "@/lib/use-api";

type Preview = { matching: number; will_sample: number; examples: { id: string; name: string | null; input_preview: string | null }[] };

function NewDataset() {
  const { projectId } = useParams<{ projectId: string }>();
  const search = useSearchParams();
  const router = useRouter();
  const facets = useApi<{ models: string[]; tags: string[] }>(`/projects/${projectId}/traces/facets`);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [q, setQ] = useState(search.get("q") ?? "");
  const [status, setStatus] = useState(search.get("status") ?? "");
  const [tag, setTag] = useState(search.get("tag") ?? "");
  const [model, setModel] = useState(search.get("model") ?? "");
  const [sample, setSample] = useState("200");
  const [seed, setSeed] = useState("1");
  const [sliceKeys, setSliceKeys] = useState("route, topic");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const filters = {
    q: q || undefined,
    status: status || undefined,
    tags: tag ? [tag] : [],
    model: model || undefined,
    slice_keys: sliceKeys.split(",").map((s) => s.trim()).filter(Boolean),
  };
  const body = { filters, sample_size: sample ? Number(sample) : null, seed: Number(seed) || 0 };
  const key = JSON.stringify(body);

  useEffect(() => {
    const id = window.setTimeout(() => {
      api<Preview>(`/projects/${projectId}/datasets/preview`, { method: "POST", body: JSON.parse(key) })
        .then(setPreview)
        .catch(setError);
    }, 250);
    return () => window.clearTimeout(id);
  }, [key, projectId]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const ds = await api<{ id: string }>(`/projects/${projectId}/datasets`, {
        method: "POST",
        body: { ...body, name, description: description || null },
      });
      router.push(`/p/${projectId}/datasets/${ds.id}`);
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader title="New dataset" description="Choose which traces to test against. The sample is frozen when you create it." />
      <form onSubmit={create} className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,380px)]">
        <div className="space-y-5">
          <Field label="Name" htmlFor="name"><Input id="name" required value={name} onChange={(e) => setName(e.target.value)} placeholder="Billing questions, October" /></Field>
          <Field label="Description" htmlFor="desc"><Input id="desc" value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Text search" htmlFor="q"><Input id="q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Matches name, input or output" /></Field>
            <Field label="Status" htmlFor="status">
              <Select id="status" value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="">Any</option><option value="ok">OK only</option><option value="error">Errors only</option>
              </Select>
            </Field>
            <Field label="Tag" htmlFor="tag">
              <Select id="tag" value={tag} onChange={(e) => setTag(e.target.value)}>
                <option value="">Any</option>
                {facets.data?.tags.map((t) => <option key={t}>{t}</option>)}
              </Select>
            </Field>
            <Field label="Recorded model" htmlFor="model">
              <Select id="model" value={model} onChange={(e) => setModel(e.target.value)}>
                <option value="">Any</option>
                {facets.data?.models.map((m) => <option key={m}>{m}</option>)}
              </Select>
            </Field>
            <Field label="Sample size" htmlFor="sample" hint="Leave empty to include every matching trace (up to 5,000).">
              <Input id="sample" type="number" min={1} max={5000} value={sample} onChange={(e) => setSample(e.target.value)} />
            </Field>
            <Field label="Random seed" htmlFor="seed" hint="Same filters and seed give the same sample.">
              <Input id="seed" type="number" value={seed} onChange={(e) => setSeed(e.target.value)} />
            </Field>
          </div>
          <Field label="Slice by metadata keys" htmlFor="slices" hint="Trace tags are always slices. Results are broken down by these metadata keys too, with a multiple-comparison correction.">
            <Input id="slices" value={sliceKeys} onChange={(e) => setSliceKeys(e.target.value)} />
          </Field>
          <ErrorNote error={error} />
          <Button type="submit" variant="primary" busy={busy} disabled={!preview?.will_sample}>
            Create and freeze {preview?.will_sample ? `${preview.will_sample} items` : "dataset"}
          </Button>
        </div>
        <Panel className="h-fit p-5 lg:sticky lg:top-6">
          <h2 className="text-[14px] font-semibold">Preview</h2>
          {preview ? (
            <>
              <div className="mt-2 flex items-baseline gap-2">
                <span key={preview.will_sample} className="readout animate-fade text-[44px] font-bold text-lagoon-ink tabular-nums">
                  {preview.will_sample.toLocaleString()}
                </span>
                <span className="text-[13px] text-slate">items, from {preview.matching.toLocaleString()} matching traces</span>
              </div>
              <p className="mt-1 text-[12.5px] text-slate">Traces without an LLM call are skipped because there is nothing to replay.</p>
              <ul className="mt-4 space-y-2">
                {preview.examples.map((ex) => (
                  <li key={ex.id} className="rounded-lg bg-[#f8fafd] px-3 py-2 text-[13px]">
                    <div className="font-semibold">{ex.name}</div>
                    <div className="text-slate">{truncate(ex.input_preview, 120)}</div>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <div className="mt-3 space-y-2">
              <div className="skeleton h-10 w-1/2" />
              <div className="skeleton h-12" />
              <div className="skeleton h-12" />
            </div>
          )}
        </Panel>
      </form>
    </>
  );
}

export default function NewDatasetPage() {
  return (
    <Suspense>
      <NewDataset />
    </Suspense>
  );
}
