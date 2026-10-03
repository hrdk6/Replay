"use client";

import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { useShell } from "@/components/app-shell";
import { Button, ErrorNote, Field, Input, Loading, Mono, Notice, PageHeader, Section, TableWrap, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtDate, fmtRelative } from "@/lib/format";
import type { Project } from "@/lib/types";
import { useApi } from "@/lib/use-api";

type ApiKey = { id: string; name: string; prefix: string; created_at: string; last_used_at: string | null; revoked_at: string | null };

const BUILTIN_LABELS: Record<string, string> = {
  api_key: "Secrets and API keys",
  email: "Email addresses",
  credit_card: "Card numbers (Luhn-checked)",
  ssn: "US social security numbers",
  phone: "Phone numbers",
  ipv4: "IPv4 addresses",
};

export default function ProjectSettingsPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const router = useRouter();
  const { me, reloadProjects } = useShell();
  const project = useApi<Project>(`/projects/${projectId}`);
  const keys = useApi<{ api_keys: ApiKey[] }>(`/projects/${projectId}/api-keys`);
  const rules = useApi<{ builtin: string[] }>("/projects/redaction-rules");
  const isAdmin = me.role !== "member";
  const [error, setError] = useState<unknown>(null);
  const [newKey, setNewKey] = useState<string | null>(null);
  const [keyName, setKeyName] = useState("");
  const toast = useToast();

  if (!project.data) return project.error ? <ErrorNote error={project.error} /> : <Loading />;

  async function createKey(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await api<{ key: string }>(`/projects/${projectId}/api-keys`, { method: "POST", body: { name: keyName || "API key" } });
      setNewKey(res.key);
      setKeyName("");
      await keys.reload();
      toast("API key created");
    } catch (err) {
      setError(err);
    }
  }

  async function revoke(k: ApiKey) {
    if (!window.confirm(`Revoke "${k.name}"? Anything using it will stop sending traces immediately.`)) return;
    await api(`/projects/${projectId}/api-keys/${k.id}`, { method: "DELETE" });
    await keys.reload();
    toast("API key revoked");
  }

  async function deleteProject() {
    if (!window.confirm(`Delete project "${project.data?.name}" with all its traces, datasets and experiments? This cannot be undone.`)) return;
    try {
      await api(`/projects/${projectId}`, { method: "DELETE" });
      await reloadProjects();
      router.push("/");
    } catch (err) {
      setError(err);
    }
  }

  return (
    <>
      <PageHeader title="Project settings" description={<>Project ID <Mono>{projectId}</Mono></>} />
      {!isAdmin ? <Notice tone="info">Only admins can change project settings.</Notice> : null}
      <ProjectForm
        key={`${project.data.id}:${project.data.name}:${project.data.retention_days}`}
        project={project.data}
        isAdmin={isAdmin}
        builtinOptions={rules.data?.builtin ?? []}
        onSaved={reloadProjects}
      />

      <div className="mt-12 max-w-[860px]">
        <Section title="API keys" description="Used by the SDK, OpenTelemetry exporters and the CI gate. Each key can only write to and read from this project.">
          <div className="mb-3"><ErrorNote error={error} /></div>
          {newKey ? (
            <div className="mb-3 animate-rise">
              <Notice tone="info" title="Copy the new key now; it won't be shown again">
                <Mono className="break-all">{newKey}</Mono>
              </Notice>
            </div>
          ) : null}
          {isAdmin ? (
            <form onSubmit={createKey} className="mb-4 flex max-w-md gap-2">
              <Input aria-label="Key name" placeholder="production, ci, staging…" value={keyName} onChange={(e) => setKeyName(e.target.value)} />
              <Button type="submit" variant="primary">Create key</Button>
            </form>
          ) : null}
          {keys.data ? (
            <TableWrap>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Key</th>
                    <th>Created</th>
                    <th>Last used</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {keys.data.api_keys.map((k) => (
                    <tr key={k.id} className={k.revoked_at ? "text-faint" : ""}>
                      <td className="align-middle">
                        <span className="inline-flex items-center gap-2 font-semibold">
                          <span className={`h-2 w-2 rounded-full ${k.revoked_at ? "bg-rule" : "bg-safe"}`} aria-hidden />
                          {k.name}
                        </span>
                      </td>
                      <td className="align-middle">
                        <Mono>{k.prefix}…</Mono>
                      </td>
                      <td className="align-middle tabular-nums">{fmtDate(k.created_at)}</td>
                      <td className="align-middle">{k.revoked_at ? `revoked ${fmtRelative(k.revoked_at)}` : fmtRelative(k.last_used_at)}</td>
                      <td className="text-right align-middle">
                        {!k.revoked_at && isAdmin ? (
                          <Button variant="danger" onClick={() => void revoke(k)}>
                            Revoke
                          </Button>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          ) : (
            <Loading rows={2} />
          )}
        </Section>
        {isAdmin ? (
          <Section title="Delete project" description="Removes every trace, dataset, candidate, judge and experiment in this project, and their stored payloads.">
            <Button variant="danger" onClick={() => void deleteProject()}>Delete this project</Button>
          </Section>
        ) : null}
      </div>
    </>
  );
}

function ProjectForm({
  project,
  isAdmin,
  builtinOptions,
  onSaved,
}: {
  project: Project;
  isAdmin: boolean;
  builtinOptions: string[];
  onSaved: () => Promise<void>;
}) {
  // Seeded from props once; the parent re-keys this component when the project changes.
  const [name, setName] = useState(project.name);
  const [retention, setRetention] = useState(project.retention_days);
  const [enabled, setEnabled] = useState(project.redaction.enabled);
  const [builtin, setBuiltin] = useState<string[]>(project.redaction.builtin);
  const [custom, setCustom] = useState<{ name: string; pattern: string }[]>(project.redaction.custom);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const toast = useToast();

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api(`/projects/${project.id}`, {
        method: "PATCH",
        body: { name, retention_days: retention, redaction: { enabled, builtin, custom: custom.filter((c) => c.name && c.pattern) } },
      });
      setBusy(false);
      toast("Settings saved");
      await onSaved();
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  return (
        <form onSubmit={save} className="max-w-[720px]">
          <fieldset disabled={!isAdmin}>
            <Section title="General">
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Name" htmlFor="pname"><Input id="pname" value={name} onChange={(e) => setName(e.target.value)} /></Field>
                <Field label="Keep traces for (days)" htmlFor="ret" hint="A daily job deletes older traces and their stored payloads. Datasets are separate frozen copies and are not affected.">
                  <Input id="ret" type="number" min={1} max={3650} value={retention} onChange={(e) => setRetention(Number(e.target.value))} />
                </Field>
              </div>
            </Section>
            <Section title="Redaction" description="Applied when traces arrive, before anything is stored. Pattern-based detection catches common formats; it is a safety net, not a guarantee.">
              <label className="mb-3 flex items-center gap-2.5 text-[13.5px] font-semibold">
                <input type="checkbox" className="h-4 w-4" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Redact personal data and secrets
              </label>
              <div className={`grid gap-1.5 rounded-xl border border-rule bg-paper p-4 transition-opacity sm:grid-cols-2 ${enabled ? "" : "opacity-55"}`}>
                {builtinOptions.map((r) => (
                  <label key={r} className="flex items-center gap-2.5 py-0.5 text-[13px]">
                    <input type="checkbox" disabled={!enabled} checked={builtin.includes(r)}
                      onChange={(e) => setBuiltin(e.target.checked ? [...builtin, r] : builtin.filter((b) => b !== r))} />
                    {BUILTIN_LABELS[r] ?? r}
                  </label>
                ))}
              </div>
              <div className="mt-4 space-y-2">
                <div className="text-[13px] font-medium">Custom patterns</div>
                {custom.map((c, i) => (
                  <div key={i} className="grid grid-cols-[160px_1fr_auto] gap-2">
                    <Input aria-label="Rule name" value={c.name} placeholder="order_id" onChange={(e) => setCustom(custom.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
                    <Input aria-label="Regular expression" className="font-mono" value={c.pattern} placeholder="ORD-\d{6}" onChange={(e) => setCustom(custom.map((x, j) => (j === i ? { ...x, pattern: e.target.value } : x)))} />
                    <Button variant="ghost" onClick={() => setCustom(custom.filter((_, j) => j !== i))}>Remove</Button>
                  </div>
                ))}
                <Button variant="ghost" onClick={() => setCustom([...custom, { name: "", pattern: "" }])}>Add pattern</Button>
              </div>
            </Section>
            <ErrorNote error={error} />
            <div className="mt-4 flex items-center gap-3">
              <Button type="submit" variant="primary" busy={busy}>Save settings</Button>
            </div>
          </fieldset>
        </form>
  );
}
