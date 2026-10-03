"use client";

import Link from "next/link";
import { useParams, usePathname, useRouter } from "next/navigation";
import { createContext, useContext, useState, type ComponentType, type ReactNode } from "react";
import { api } from "@/lib/api";
import { hardNavigate } from "@/lib/nav";
import type { Me, Project } from "@/lib/types";
import { useApi } from "@/lib/use-api";
import {
  CandidatesIcon,
  DatasetsIcon,
  ExperimentsIcon,
  JudgesIcon,
  LabelingIcon,
  Logo,
  MenuIcon,
  OrgIcon,
  QuickstartIcon,
  SettingsIcon,
  TracesIcon,
} from "./icons";
import { ErrorNote, Loading, ToastProvider } from "./ui";

type Shell = { me: Me; projects: Project[]; reloadProjects: () => Promise<void> };
const ShellContext = createContext<Shell | null>(null);

export function useShell(): Shell {
  const ctx = useContext(ShellContext);
  if (!ctx) throw new Error("useShell outside AppShell");
  return ctx;
}

type NavItem = { href: string; label: string; Icon: ComponentType<{ size?: number; className?: string }> };

const PROJECT_NAV: NavItem[] = [
  { href: "traces", label: "Traces", Icon: TracesIcon },
  { href: "datasets", label: "Datasets", Icon: DatasetsIcon },
  { href: "candidates", label: "Candidates", Icon: CandidatesIcon },
  { href: "experiments", label: "Experiments", Icon: ExperimentsIcon },
  { href: "judges", label: "Judges", Icon: JudgesIcon },
  { href: "labeling", label: "Labeling", Icon: LabelingIcon },
];
const PROJECT_SECONDARY: NavItem[] = [
  { href: "quickstart", label: "Quickstart", Icon: QuickstartIcon },
  { href: "settings", label: "Project settings", Icon: SettingsIcon },
];

const ITEM_H = 38; // px; keep in sync with the h-[38px] on nav links so the indicator lines up.

function NavGroup({ items, projectId, pathname, onNavigate }: { items: NavItem[]; projectId: string; pathname: string; onNavigate: () => void }) {
  const active = items.findIndex((n) => pathname.startsWith(`/p/${projectId}/${n.href}`));
  return (
    <nav className="relative">
      {/* One indicator per group that glides to the current page. */}
      <span
        aria-hidden
        className="absolute left-0 right-0 top-0 rounded-lg bg-rail-raised transition-[transform,opacity] duration-300 ease-[var(--ease-out-soft)]"
        style={{ height: ITEM_H, transform: `translateY(${Math.max(active, 0) * ITEM_H}px)`, opacity: active < 0 ? 0 : 1 }}
      >
        <span className="absolute bottom-2 left-0 top-2 w-[3px] rounded-r-full bg-lagoon-bright" />
      </span>
      {items.map(({ href, label, Icon }, i) => {
        const isActive = i === active;
        return (
          <Link
            key={href}
            href={`/p/${projectId}/${href}`}
            onClick={onNavigate}
            aria-current={isActive ? "page" : undefined}
            className={`group relative flex h-[38px] items-center gap-3 rounded-lg px-3 text-[13.5px] transition-colors ${isActive ? "font-semibold text-white" : "text-rail-text hover:bg-white/[0.04] hover:text-white"}`}
          >
            <Icon size={17} className={isActive ? "text-lagoon-bright" : "text-rail-muted transition-colors group-hover:text-rail-text"} />
            {label}
          </Link>
        );
      })}
    </nav>
  );
}

const railSelect =
  "w-full appearance-none rounded-lg border border-white/10 bg-rail-raised bg-[length:12px] bg-[right_10px_center] bg-no-repeat py-2 pl-3 pr-8 text-[13.5px] font-medium text-white transition-colors hover:border-white/20 focus:border-lagoon-bright focus:outline-none";
const chevron = {
  backgroundImage:
    "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M3 4.5 6 7.5 9 4.5' fill='none' stroke='%23aebcd6' stroke-width='1.6' stroke-linecap='round'/%3E%3C/svg%3E\")",
};

export function AppShell({ children }: { children: ReactNode }) {
  const me = useApi<Me>("/me");
  const projects = useApi<{ projects: Project[] }>("/projects");
  const params = useParams<{ projectId?: string }>();
  const pathname = usePathname();
  const router = useRouter();
  const [open, setOpen] = useState(false);

  if (me.error || projects.error) {
    return (
      <div className="mx-auto max-w-lg p-10">
        <ErrorNote error={me.error ?? projects.error} />
      </div>
    );
  }
  if (!me.data || !projects.data) {
    return (
      <div className="min-h-screen md:grid md:grid-cols-[244px_1fr]">
        <div className="hidden bg-rail md:block" />
        <div className="mx-auto w-full max-w-[1180px] px-4 py-8 md:px-10">
          <Loading rows={6} />
        </div>
      </div>
    );
  }

  const projectId = params.projectId ?? projects.data.projects[0]?.id;
  const current = projects.data.projects.find((p) => p.id === projectId);
  const org = me.data.orgs.find((o) => o.id === me.data!.active_org_id);
  const close = () => setOpen(false);

  async function switchOrg(orgId: string) {
    await api("/me/active-org", { method: "POST", body: { org_id: orgId } });
    hardNavigate("/");
  }

  async function signOut() {
    await api("/auth/logout", { method: "POST" }).catch(() => undefined);
    hardNavigate("/login");
  }

  const orgActive = pathname.startsWith("/settings");

  return (
    <ShellContext.Provider value={{ me: me.data, projects: projects.data.projects, reloadProjects: projects.reload }}>
      <ToastProvider>
        <div className="min-h-screen md:grid md:grid-cols-[244px_1fr]">
          <div className="sticky top-0 z-30 flex items-center justify-between bg-rail px-4 py-3 text-white md:hidden">
            <Link href="/" className="flex items-center gap-2.5 text-[15px] font-bold tracking-[-0.01em]">
              <Logo size={24} /> Replay
            </Link>
            <button
              type="button"
              onClick={() => setOpen(!open)}
              className="pressable grid h-9 w-9 place-items-center rounded-lg text-rail-text hover:bg-white/5 hover:text-white"
              aria-expanded={open}
              aria-label="Menu"
            >
              <MenuIcon size={20} />
            </button>
          </div>
          <aside
            className={`${open ? "fixed inset-x-0 bottom-0 top-[60px] z-20 block animate-fade overflow-y-auto" : "hidden"} bg-rail text-rail-text md:sticky md:bottom-auto md:top-0 md:z-auto md:block md:h-screen md:overflow-y-auto`}
            aria-label="Main"
          >
            <div className="flex h-full flex-col gap-6 px-3 py-4">
              <Link href="/" className="hidden items-center gap-2.5 px-2 pt-1 text-[16px] font-bold tracking-[-0.015em] text-white md:flex">
                <Logo /> Replay
              </Link>
              <div className="space-y-1.5 px-1">
                <label htmlFor="project-switch" className="block px-0.5 text-[12px] font-medium text-rail-muted">
                  Project
                </label>
                <select id="project-switch" value={projectId ?? ""} onChange={(e) => router.push(`/p/${e.target.value}/traces`)} className={railSelect} style={chevron}>
                  {projects.data.projects.map((p) => (
                    <option key={p.id} value={p.id} className="bg-rail">
                      {p.name}
                    </option>
                  ))}
                </select>
              </div>
              {projectId ? <NavGroup items={PROJECT_NAV} projectId={projectId} pathname={pathname} onNavigate={close} /> : null}
              {projectId ? (
                <div className="border-t border-white/[0.07] pt-4">
                  <NavGroup items={PROJECT_SECONDARY} projectId={projectId} pathname={pathname} onNavigate={close} />
                </div>
              ) : null}
              <div className="mt-auto space-y-3 border-t border-white/[0.07] pt-4">
                <Link
                  href="/settings"
                  onClick={close}
                  aria-current={orgActive ? "page" : undefined}
                  className={`flex items-center gap-3 rounded-lg px-3 py-2 transition-colors ${orgActive ? "bg-rail-raised text-white" : "hover:bg-white/[0.04] hover:text-white"}`}
                >
                  <OrgIcon size={17} className={orgActive ? "text-lagoon-bright" : "text-rail-muted"} />
                  <span className="min-w-0">
                    <span className="block text-[13.5px] font-medium">Organization</span>
                    <span className="block truncate text-[12px] text-rail-muted">{org?.name}</span>
                  </span>
                </Link>
                {me.data.orgs.length > 1 ? (
                  <select
                    aria-label="Switch organization"
                    value={me.data.active_org_id}
                    onChange={(e) => void switchOrg(e.target.value)}
                    className={`${railSelect} py-1.5 text-[12.5px]`}
                    style={chevron}
                  >
                    {me.data.orgs.map((o) => (
                      <option key={o.id} value={o.id} className="bg-rail">
                        {o.name}
                      </option>
                    ))}
                  </select>
                ) : null}
                <div className="flex items-center justify-between gap-2 px-3 text-[12.5px]">
                  <span className="flex min-w-0 items-center gap-2">
                    {me.data.user.avatar_url ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img src={me.data.user.avatar_url} alt="" width={22} height={22} className="rounded-full ring-1 ring-white/15" />
                    ) : (
                      <span className="grid h-[22px] w-[22px] place-items-center rounded-full bg-lagoon text-[11px] font-bold uppercase text-white" aria-hidden>
                        {me.data.user.github_login.slice(0, 1)}
                      </span>
                    )}
                    <span className="truncate text-rail-text">{me.data.user.github_login}</span>
                  </span>
                  <button type="button" onClick={() => void signOut()} className="shrink-0 font-medium text-rail-muted transition-colors hover:text-white">
                    Sign out
                  </button>
                </div>
              </div>
            </div>
          </aside>
          <main className="min-w-0 px-4 py-6 md:px-10 md:py-9">
            <div key={pathname} className="mx-auto max-w-[1180px] animate-fade">
              {current || !params.projectId ? children : <ErrorNote error={new Error("Project not found.")} />}
            </div>
          </main>
        </div>
      </ToastProvider>
    </ShellContext.Provider>
  );
}
