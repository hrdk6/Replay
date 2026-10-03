"use client";

import { useState } from "react";
import { useShell } from "@/components/app-shell";
import { Button, ErrorNote, Field, Input, Loading, Mono, Notice, PageHeader, Panel, Section, Select, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import { hardNavigate } from "@/lib/nav";
import { fmtDate, fmtUsd } from "@/lib/format";
import { useApi } from "@/lib/use-api";

type Org = {
  id: string; name: string; slug: string; role: string; personal: boolean;
  quota_traces_per_day: number; quota_replay_runs_per_day: number; monthly_budget_usd: number;
  usage: { traces_today: number; replay_runs_today: number; llm_spend_this_month_usd: number };
};
type Members = {
  members: { user_id: string; github_login: string; name: string | null; avatar_url: string | null; role: string; joined_at: string }[];
  invites: { id: string; github_login: string; role: string; created_at: string }[];
};
type ProviderKey = { id: string; provider: string; name: string; base_url: string | null; masked: string; created_at: string };
type Audit = { entries: { id: number; action: string; actor: string | null; target_type: string | null; target_id: string | null; metadata: Record<string, unknown>; ip: string | null; created_at: string }[] };

function Meter({ label, used, limit, format }: { label: string; used: number; limit: number; format: (v: number) => string }) {
  const pct = limit ? Math.min(100, (used / limit) * 100) : 0;
  const tone = pct > 90 ? "bg-unsafe" : pct > 70 ? "bg-unsure-bright" : "bg-gradient-to-r from-lagoon to-lagoon-bright";
  return (
    <div>
      <div className="flex justify-between text-[13px]">
        <span className="font-medium">{label}</span>
        <span className="tabular-nums text-slate">
          <span className="font-semibold text-ink">{format(used)}</span> of {format(limit)}
        </span>
      </div>
      <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-mist">
        <div className={`h-full rounded-full transition-[width] duration-700 ease-[var(--ease-out-soft)] ${tone}`} style={{ width: `${Math.max(pct, used ? 1.5 : 0)}%` }} />
      </div>
    </div>
  );
}

export default function OrgSettingsPage() {
  const { me, reloadProjects } = useShell();
  const org = useApi<Org>("/orgs/current");
  const members = useApi<Members>("/orgs/current/members");
  const keys = useApi<{ provider_keys: ProviderKey[] }>("/provider-keys");
  const isAdmin = me.role !== "member";
  const isOwner = me.role === "owner";
  const audit = useApi<Audit>(isAdmin ? "/orgs/current/audit" : null);
  const [error, setError] = useState<unknown>(null);
  const [invite, setInvite] = useState({ github_login: "", role: "member" });
  const [pk, setPk] = useState({ provider: "openai", name: "", api_key: "", base_url: "" });
  const [projectName, setProjectName] = useState("");
  const [confirmSlug, setConfirmSlug] = useState("");

  const toast = useToast();

  async function run(fn: () => Promise<unknown>, done?: string) {
    setError(null);
    try {
      const result = await fn();
      if (done && result !== false) toast(done);
    } catch (err) {
      setError(err);
    }
  }

  if (!org.data) return org.error ? <ErrorNote error={org.error} /> : <Loading />;
  const o = org.data;

  return (
    <>
      <PageHeader title="Organization" description={<>{o.name} <Mono className="text-slate">{o.slug}</Mono>, your role: {o.role}</>} />
      <ErrorNote error={error} />
      <div className="grid gap-10 lg:grid-cols-2">
        <div>
          <Section title="Usage and limits" description="Daily counters reset at midnight UTC. LLM spend is what replays and judges cost on your provider keys this month.">
            <Panel className="space-y-4 p-5">
              <Meter label="Traces today" used={o.usage.traces_today} limit={o.quota_traces_per_day} format={(v) => v.toLocaleString()} />
              <Meter label="Replay runs today" used={o.usage.replay_runs_today} limit={o.quota_replay_runs_per_day} format={(v) => v.toLocaleString()} />
              <Meter label="LLM spend this month" used={o.usage.llm_spend_this_month_usd} limit={o.monthly_budget_usd} format={(v) => fmtUsd(v, 2)} />
            </Panel>
          </Section>
          {isAdmin ? (
            <GeneralForm key={`${o.name}:${o.monthly_budget_usd}`} org={o} onSaved={org.reload} onError={setError} />
          ) : null}
          {isAdmin ? (
            <Section title="New project" description="Projects separate apps or environments. Each has its own traces, keys and settings.">
              <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void run(async () => { await api("/projects", { method: "POST", body: { name: projectName } }); setProjectName(""); await reloadProjects(); }, "Project created"); }}>
                <Input aria-label="Project name" required value={projectName} onChange={(e) => setProjectName(e.target.value)} placeholder="Checkout assistant" />
                <Button type="submit" variant="primary">Create project</Button>
              </form>
            </Section>
          ) : null}
          <Section title="Provider keys" description="Replays and judges run on your own LLM accounts. Keys are encrypted at rest, never shown again, and never logged.">
            {keys.data?.provider_keys.length ? (
              <ul className="mb-4 divide-y divide-mist overflow-hidden rounded-xl border border-rule bg-paper">
                {keys.data.provider_keys.map((k) => (
                  <li key={k.id} className="flex items-center justify-between gap-3 px-4 py-2.5 text-[13px]">
                    <span className="flex min-w-0 items-center gap-3">
                      <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-lagoon-tint text-[11px] font-bold uppercase text-lagoon-ink" aria-hidden>
                        {k.provider.slice(0, 2)}
                      </span>
                      <span className="min-w-0">
                        <span className="block font-semibold">{k.name}</span>
                        <span className="block truncate text-slate">
                          {k.provider} <Mono>{k.masked}</Mono>
                          {k.base_url ? `, ${k.base_url}` : ""}
                        </span>
                      </span>
                    </span>
                    {isAdmin ? (
                      <Button
                        variant="danger"
                        onClick={() =>
                          void run(async () => {
                            if (!window.confirm(`Delete key "${k.name}"? The encrypted secret is destroyed.`)) return false;
                            await api(`/provider-keys/${k.id}`, { method: "DELETE" });
                            await keys.reload();
                          }, "Provider key deleted")
                        }
                      >
                        Delete
                      </Button>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : <p className="mb-3 text-[13px] text-slate">No provider keys yet. Experiments with real models need at least one.</p>}
            {isAdmin ? (
              <form className="grid gap-3 sm:grid-cols-2" autoComplete="off" onSubmit={(e) => { e.preventDefault(); void run(async () => { await api("/provider-keys", { method: "POST", body: { ...pk, base_url: pk.base_url || null } }); setPk({ ...pk, name: "", api_key: "", base_url: "" }); await keys.reload(); }, "Provider key added"); }}>
                <Field label="Provider" htmlFor="pk-provider">
                  <Select id="pk-provider" value={pk.provider} onChange={(e) => setPk({ ...pk, provider: e.target.value })}>
                    <option value="openai">OpenAI</option><option value="anthropic">Anthropic</option><option value="openai_compatible">OpenAI-compatible</option>
                  </Select>
                </Field>
                <Field label="Label" htmlFor="pk-name"><Input id="pk-name" required value={pk.name} onChange={(e) => setPk({ ...pk, name: e.target.value })} placeholder="Team key" /></Field>
                <Field label="API key" htmlFor="pk-key"><Input id="pk-key" required type="password" value={pk.api_key} onChange={(e) => setPk({ ...pk, api_key: e.target.value })} /></Field>
                {pk.provider === "openai_compatible" ? (
                  <Field label="Base URL" htmlFor="pk-url" hint="Must be a public HTTPS endpoint."><Input id="pk-url" required value={pk.base_url} onChange={(e) => setPk({ ...pk, base_url: e.target.value })} placeholder="https://api.example.com/v1" /></Field>
                ) : <div />}
                <div><Button type="submit" variant="primary">Add key</Button></div>
              </form>
            ) : null}
          </Section>
        </div>
        <div>
          <Section title="Members" description="Invite people by GitHub username. They join when they next sign in.">
            {members.data ? (
              <ul className="mb-4 divide-y divide-mist overflow-hidden rounded-xl border border-rule bg-paper">
                {members.data.members.map((m) => (
                  <li key={m.user_id} className="flex items-center justify-between gap-3 px-4 py-2.5 text-[13px]">
                    <span className="flex items-center gap-2.5">
                      {m.avatar_url ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={m.avatar_url} alt="" width={26} height={26} className="rounded-full ring-1 ring-rule" />
                      ) : (
                        <span className="grid h-[26px] w-[26px] place-items-center rounded-full bg-lagoon text-[11px] font-bold uppercase text-white" aria-hidden>
                          {m.github_login.slice(0, 1)}
                        </span>
                      )}
                      <span className="font-semibold">{m.github_login}</span>
                      <span className="rounded-md bg-mist px-1.5 py-px text-[12px] text-slate">{m.role}</span>
                    </span>
                    <span className="flex gap-2">
                      {isOwner && m.user_id !== me.user.id ? (
                        <Select aria-label={`Role for ${m.github_login}`} className="!w-auto !py-0.5" value={m.role} onChange={(e) => void run(async () => { await api(`/orgs/current/members/${m.user_id}`, { method: "PATCH", body: { role: e.target.value } }); await members.reload(); }, "Role updated")}>
                          <option value="member">member</option><option value="admin">admin</option><option value="owner">owner</option>
                        </Select>
                      ) : null}
                      {(isAdmin && m.user_id !== me.user.id) ? (
                        <Button variant="ghost" onClick={() => void run(async () => { if (!window.confirm(`Remove ${m.github_login}?`)) return false; await api(`/orgs/current/members/${m.user_id}`, { method: "DELETE" }); await members.reload(); }, "Member removed")}>Remove</Button>
                      ) : null}
                    </span>
                  </li>
                ))}
                {members.data.invites.map((i) => (
                  <li key={i.id} className="flex items-center justify-between gap-3 bg-[#fbfcfe] px-4 py-2.5 text-[13px] text-slate">
                    <span className="flex items-center gap-2.5">
                      <span className="h-[26px] w-[26px] rounded-full border-2 border-dashed border-rule" aria-hidden />
                      <span>
                        <span className="font-semibold text-ink">{i.github_login}</span> invited as {i.role}, {fmtDate(i.created_at)}
                      </span>
                    </span>
                    {isAdmin ? <Button variant="ghost" onClick={() => void run(async () => { await api(`/orgs/current/invites/${i.id}`, { method: "DELETE" }); await members.reload(); }, "Invite cancelled")}>Cancel invite</Button> : null}
                  </li>
                ))}
              </ul>
            ) : <Loading />}
            {isAdmin ? (
              <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void run(async () => { await api("/orgs/current/invites", { method: "POST", body: invite }); setInvite({ ...invite, github_login: "" }); await members.reload(); }, "Invite added"); }}>
                <Input aria-label="GitHub username" required pattern="[A-Za-z0-9\-]+" placeholder="github-username" value={invite.github_login} onChange={(e) => setInvite({ ...invite, github_login: e.target.value })} />
                <Select aria-label="Role" className="!w-auto" value={invite.role} onChange={(e) => setInvite({ ...invite, role: e.target.value })}>
                  <option value="member">member</option><option value="admin">admin</option>
                </Select>
                <Button type="submit" variant="primary">Invite</Button>
              </form>
            ) : null}
          </Section>
          {isAdmin && audit.data ? (
            <Section title="Audit log" description="Sign-ins, key changes, deletions and other sensitive actions.">
              <div className="max-h-[380px] overflow-auto rounded-xl border border-rule bg-paper">
                <table className="data-table">
                  <thead><tr><th>When</th><th>Who</th><th>Action</th></tr></thead>
                  <tbody>
                    {audit.data.entries.map((a) => (
                      <tr key={a.id}>
                        <td className="whitespace-nowrap text-slate">{fmtDate(a.created_at)}</td>
                        <td>{a.actor ?? (a.target_type === "experiment" ? "API key" : "system")}</td>
                        <td>{a.action.replace(".", ": ").replaceAll("_", " ")}{a.metadata && Object.keys(a.metadata).length ? <span className="ml-1 text-[12px] text-faint">{JSON.stringify(a.metadata).slice(0, 80)}</span> : null}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Section>
          ) : null}
          <Section title="Your data" description="Export everything as newline-delimited JSON. Provider keys are listed without their secrets.">
            {/* A file download served by the API route handler, not a page: a plain link is correct here. */}
            {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
            {isAdmin ? <a href="/api/orgs/current/export" className="pressable inline-flex items-center gap-2 rounded-lg border border-rule bg-paper px-3.5 py-2 text-[13.5px] font-semibold hover:border-[#c6cede] hover:bg-[#fbfcfe]">Download export</a> : <p className="text-[13px] text-slate">Ask an admin for an export.</p>}
          </Section>
          {isOwner ? (
            <Section title="Delete organization" description="Revokes everyone's access immediately, then deletes all projects, traces, datasets, experiments and stored payloads.">
              <Notice tone="danger">This cannot be undone. Type the organization slug <Mono>{o.slug}</Mono> to confirm.</Notice>
              <div className="mt-3 flex gap-2">
                <Input aria-label="Confirm slug" value={confirmSlug} onChange={(e) => setConfirmSlug(e.target.value)} />
                <Button variant="danger" disabled={confirmSlug !== o.slug} onClick={() => void run(async () => { await api("/orgs/current", { method: "DELETE", body: { confirm_slug: confirmSlug } }); hardNavigate("/login"); })}>Delete organization</Button>
              </div>
            </Section>
          ) : null}
          <Section title="Delete your account" description="Removes your user. Organizations where you are the only member are deleted too.">
            <Button variant="danger" onClick={() => void run(async () => { if (window.confirm("Delete your Replay account?")) { await api("/me", { method: "DELETE" }); hardNavigate("/login"); } })}>Delete my account</Button>
          </Section>
        </div>
      </div>
    </>
  );
}

function GeneralForm({ org, onSaved, onError }: { org: Org; onSaved: () => Promise<void>; onError: (e: unknown) => void }) {
  const [orgName, setOrgName] = useState(org.name);
  const [budget, setBudget] = useState(String(org.monthly_budget_usd));
  const toast = useToast();
  return (
    <Section title="General">
      <form
        className="grid gap-3 sm:grid-cols-[1fr_160px_auto] sm:items-end"
        onSubmit={(e) => {
          e.preventDefault();
          api("/orgs/current", { method: "PATCH", body: { name: orgName, monthly_budget_usd: Number(budget) } })
            .then(onSaved)
            .then(() => toast("Organization saved"))
            .catch(onError);
        }}
      >
        <Field label="Name" htmlFor="oname"><Input id="oname" value={orgName} onChange={(e) => setOrgName(e.target.value)} /></Field>
        <Field label="Monthly LLM budget (USD)" htmlFor="obudget"><Input id="obudget" type="number" min={0} step="1" value={budget} onChange={(e) => setBudget(e.target.value)} /></Field>
        <Button type="submit" variant="primary">Save</Button>
      </form>
    </Section>
  );
}
