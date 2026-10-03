"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { ReplayIcon, Logo } from "@/components/icons";
import { IntervalStrip } from "@/components/interval-strip";
import { Button, ErrorNote, Field, Input } from "@/components/ui";
import { api } from "@/lib/api";
import { useApi } from "@/lib/use-api";

const ERRORS: Record<string, string> = {
  not_allowed: "Replay is in private beta and this GitHub account isn't on the list yet. Ask an existing user to invite you, or contact us for access.",
  state: "The sign-in link expired or was opened in another browser. Try again.",
  oauth: "GitHub didn't complete the sign-in. Try again.",
};

function LoginForm() {
  const search = useSearchParams();
  const next = search.get("next") ?? "/";
  const error = search.get("error");
  const config = useApi<{ github: boolean; dev_login: boolean; signup_mode: string }>("/auth/config");
  const [login, setLogin] = useState("");
  const [busy, setBusy] = useState(false);
  const [devError, setDevError] = useState<unknown>(null);

  async function devLogin(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setDevError(null);
    try {
      await api("/auth/dev-login", { method: "POST", body: { login } });
      window.location.href = next.startsWith("/") ? next : "/";
    } catch (err) {
      setDevError(err);
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      {error ? <ErrorNote error={new Error(ERRORS[error] ?? "Sign-in failed. Try again.")} /> : null}
      {config.data?.github !== false ? (
        <a
          href={`/api/auth/github/login?next=${encodeURIComponent(next)}`}
          className="pressable flex w-full items-center justify-center gap-2.5 rounded-lg bg-rail px-4 py-3 text-[14.5px] font-semibold text-white shadow-[0_1px_2px_rgb(17_28_51/0.3)] hover:bg-rail-raised"
        >
          <svg width="18" height="18" viewBox="0 0 16 16" fill="currentColor" aria-hidden>
            <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
          </svg>
          Continue with GitHub
        </a>
      ) : (
        <p className="text-[13px] text-slate">GitHub sign-in isn&apos;t configured on this deployment.</p>
      )}
      {config.data?.dev_login ? (
        <form onSubmit={devLogin} className="space-y-3 rounded-xl border border-dashed border-[#c9d2e2] bg-paper/60 p-4">
          <Field label="Development sign-in" htmlFor="dev-login" hint="Only available when DEV_LOGIN_ENABLED is set in a development environment.">
            <Input id="dev-login" value={login} onChange={(e) => setLogin(e.target.value)} placeholder="any-github-login" required pattern="[A-Za-z0-9\-]+" />
          </Field>
          <ErrorNote error={devError} />
          <Button type="submit" variant="secondary" busy={busy}>
            Sign in as this user
          </Button>
        </form>
      ) : null}
      <p className="text-[12.5px] text-slate">
        By continuing you agree to the{" "}
        <Link href="/terms" className="font-medium text-lagoon-ink hover:underline">
          terms
        </Link>{" "}
        and{" "}
        <Link href="/privacy" className="font-medium text-lagoon-ink hover:underline">
          privacy policy
        </Link>
        .
      </p>
    </div>
  );
}

// --- The hero: a recorded trace being replayed against a candidate ----------------------------

type DemoSpan = { label: string; start: number; width: number; kind: "llm" | "tool" | "retrieval"; reused?: boolean };

const RECORDED: DemoSpan[] = [
  { label: "retrieve", start: 0, width: 17, kind: "retrieval" },
  { label: "plan", start: 18.5, width: 22, kind: "llm" },
  { label: "get_order", start: 42, width: 19, kind: "tool" },
  { label: "answer", start: 62.5, width: 36, kind: "llm" },
];
const REPLAYED: DemoSpan[] = [
  { label: "retrieve", start: 0, width: 17, kind: "retrieval", reused: true },
  { label: "plan", start: 18.5, width: 19, kind: "llm" },
  { label: "get_order", start: 39, width: 19, kind: "tool", reused: true },
  { label: "answer", start: 59.5, width: 31, kind: "llm" },
];

const SWEEP_MS = 2600;
const SWEEP_DELAY = 200;

function Lane({ title, spans, tone, drawn }: { title: string; spans: DemoSpan[]; tone: "base" | "lagoon"; drawn: boolean }) {
  return (
    <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-[112px_1fr] sm:items-center sm:gap-3">
      <div className="text-[12px] font-medium leading-tight text-rail-text">{title}</div>
      <div className="relative h-8 rounded-md bg-white/[0.04]">
        {drawn ? (
          // The playhead sweeps the lane being replayed.
          <div className="pointer-events-none absolute -inset-y-1.5 inset-x-0 z-10" aria-hidden>
            <div className="replay-sweep absolute inset-y-0 w-px bg-lagoon-bright shadow-[0_0_12px_2px_rgb(24_174_195/0.55)]">
              <span className="absolute -left-[4px] -top-1 h-[9px] w-[9px] rotate-45 rounded-[2px] bg-lagoon-bright" />
            </div>
          </div>
        ) : null}
        {spans.map((s) => {
          const fill =
            tone === "base"
              ? s.kind === "tool"
                ? "bg-base/45 text-white"
                : "bg-base/30 text-rail-text"
              : s.kind === "tool"
                ? "bg-lagoon-bright text-rail"
                : s.kind === "retrieval"
                  ? "bg-lagoon/70 text-white"
                  : "bg-lagoon text-white";
          return (
            <div
              key={s.label}
              className={`absolute bottom-1 top-1 flex items-center gap-1 overflow-hidden whitespace-nowrap rounded px-1.5 text-[11px] font-semibold ${fill} ${drawn ? "replay-span" : ""}`}
              style={{
                left: `${s.start}%`,
                width: `${s.width}%`,
                ...(drawn
                  ? {
                      animationDelay: `${SWEEP_DELAY + (s.start / 100) * SWEEP_MS}ms`,
                      animationDuration: `${(s.width / 100) * SWEEP_MS}ms`,
                      animationTimingFunction: "linear",
                    }
                  : {}),
              }}
            >
              {s.reused ? (
                <svg width="10" height="10" viewBox="0 0 12 12" aria-hidden className="shrink-0">
                  <path d="m2.5 6.3 2.2 2.2L9.5 3.7" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              ) : null}
              <span className="truncate">{s.label}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ReplayDemo({ onAgain }: { onAgain: () => void }) {
  const [done, setDone] = useState(false);
  useEffect(() => {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const t = window.setTimeout(() => setDone(true), reduce ? 0 : SWEEP_DELAY + SWEEP_MS + 150);
    return () => window.clearTimeout(t);
  }, []);

  return (
    <figure className="mt-10" aria-label="A recorded trace replayed against a candidate change, then scored">
      <div className="rounded-xl border border-white/[0.08] bg-rail-raised/60 p-4 sm:p-5">
        <div className="space-y-3 sm:space-y-2.5">
          <Lane title="Recorded in production" spans={RECORDED} tone="base" drawn={false} />
          <Lane title="Replayed on the candidate" spans={REPLAYED} tone="lagoon" drawn />
        </div>
        <p className="mt-3 flex items-center gap-1.5 text-[11.5px] text-rail-muted sm:pl-[124px]">
          <svg width="10" height="10" viewBox="0 0 12 12" aria-hidden>
            <path d="m2.5 6.3 2.2 2.2L9.5 3.7" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          tool and retrieval results reused from the recording
        </p>
      </div>

      <div className="mt-4 min-h-[188px]">
        {done ? (
          <div className="animate-rise rounded-xl bg-paper p-5 text-ink shadow-[0_18px_40px_-20px_rgb(0_0_0/0.6)]">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div className="readout verdict-in text-[46px] font-extrabold text-safe">SAFE</div>
              <button
                type="button"
                onClick={onAgain}
                className="pressable inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12.5px] font-semibold text-lagoon-ink hover:bg-lagoon-tint"
              >
                <ReplayIcon size={15} /> Replay again
              </button>
            </div>
            <figcaption className="mt-2 max-w-[60ch] text-[13px] text-slate">
              The candidate is at most 5 points worse than baseline: difference +1.8, 95% interval −2.4 to +6.1, over 412 items.
            </figcaption>
            <div className="mt-3">
              <IntervalStrip ci={{ estimate: 1.8, low: -2.4, high: 6.1, confidence: 0.95, method: "example" }} margin={5} verdict="SAFE" animate />
            </div>
          </div>
        ) : (
          <div className="flex h-[188px] items-center justify-center rounded-xl border border-dashed border-white/10 text-[12.5px] text-rail-muted">
            <span className="inline-flex items-center gap-2">
              <span className="live-dot h-2 w-2 rounded-full bg-lagoon-bright" aria-hidden /> Scoring both runs with the calibrated judge
            </span>
          </div>
        )}
      </div>
    </figure>
  );
}

export default function LoginPage() {
  const [run, setRun] = useState(0);
  return (
    <main className="grid min-h-screen lg:grid-cols-[1.2fr_1fr]">
      <section className="relative flex flex-col justify-between overflow-hidden bg-rail px-6 py-9 text-white sm:px-10 lg:px-16 lg:py-14">
        {/* A single soft light behind the figure; the rest of the panel stays flat. */}
        <div aria-hidden className="pointer-events-none absolute -right-40 top-1/3 h-[520px] w-[520px] rounded-full bg-lagoon/20 blur-[120px]" />
        <div className="relative hidden items-center gap-2.5 text-[16px] font-bold tracking-[-0.015em] lg:flex">
          <Logo /> Replay
        </div>
        <div className="relative max-w-[600px] lg:my-12">
          <h1 className="text-[30px] font-bold leading-[1.15] tracking-[-0.025em] sm:text-[36px]">
            Know whether a model, prompt or retrieval change makes things better or worse, before you ship it.
          </h1>
          <p className="mt-4 max-w-[54ch] text-[15px] leading-relaxed text-rail-text">
            Replay re-runs your recorded production traces against the change, scores both sides with a judge you have checked against human labels, and tells you what
            the evidence supports.
          </p>
          <ReplayDemo key={run} onAgain={() => setRun((r) => r + 1)} />
        </div>
        <p className="relative mt-10 text-[12.5px] text-rail-muted lg:mt-0">Paired designs, confidence intervals and calibrated judges, so the verdict holds up in review.</p>
      </section>
      {/* On a phone, signing in comes first; the demo follows below. */}
      <section className="order-first flex flex-col items-center justify-center bg-canvas px-6 py-10 sm:px-8 lg:order-none lg:py-12">
        <div className="mb-8 flex w-full max-w-[380px] items-center gap-2.5 text-[16px] font-bold tracking-[-0.015em] lg:hidden">
          <Logo /> Replay
        </div>
        <div className="w-full max-w-[380px] animate-rise">
          <h2 className="mb-1 text-[22px] font-bold tracking-[-0.015em]">Sign in</h2>
          <p className="mb-7 text-slate">Use your GitHub account. We only read your public profile and email.</p>
          <Suspense>
            <LoginForm />
          </Suspense>
        </div>
      </section>
    </main>
  );
}
