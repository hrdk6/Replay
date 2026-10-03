"use client";

import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { LabelingIcon } from "@/components/icons";
import { Button, EmptyState, ErrorNote, Field, Loading, PageHeader, Panel, Select } from "@/components/ui";
import { api } from "@/lib/api";
import type { Judge } from "@/lib/types";
import { useApi } from "@/lib/use-api";

type Task = {
  mode: "absolute" | "pairwise";
  scale: string;
  rubric: string;
  conversation: string;
  categories: string[];
  response?: string;
  first?: string;
  second?: string;
  token: string;
};

function Labeler() {
  const { projectId } = useParams<{ projectId: string }>();
  const search = useSearchParams();
  const router = useRouter();
  const judges = useApi<{ judges: Judge[] }>(`/projects/${projectId}/judges`);
  const judgeId = search.get("judge") ?? judges.data?.judges[0]?.id ?? "";
  // Bumping `round` re-fetches the next task through useApi (no setState inside effects).
  const [round, setRound] = useState(0);
  const queue = useApi<{ task: Task | null; remaining: number; labeled: number }>(
    judgeId ? `/projects/${projectId}/labeling/next?judge_id=${judgeId}&round=${round}` : null,
  );
  const state = queue.data ?? null;
  const [error, setError] = useState<unknown>(null);
  const [submitting, setSubmitting] = useState(false);
  const next = () => setRound((r) => r + 1);

  async function submit(label: string) {
    if (!state?.task) return;
    setSubmitting(true);
    setError(null);
    try {
      await api(`/projects/${projectId}/labeling`, { method: "POST", body: { token: state.task.token, label } });
      next();
    } catch (err) {
      setError(err);
    }
    setSubmitting(false);
  }

  useEffect(() => {
    const task = state?.task;
    if (!task) return;
    const keys: Record<string, string> =
      task.mode === "pairwise" ? { "1": "first", "2": "second", t: "tie" } : task.scale === "binary" ? { p: "1", f: "0" } : { "1": "1", "2": "2", "3": "3", "4": "4", "5": "5" };
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLElement && ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
      const label = keys[e.key.toLowerCase()];
      if (label && !submitting) void submit(label);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const task = state?.task;
  const goal = 30;
  const kbd = (k: string) => <kbd className="ml-auto rounded border border-current/25 px-1.5 py-px font-mono text-[11px] opacity-80">{k}</kbd>;
  return (
    <>
      <PageHeader
        title="Labeling"
        description="Read the conversation, then judge the answer yourself. You won't see what the model judge decided, or which answer came from which arm. Your labels measure how far to trust the judge."
        actions={
          judges.data?.judges.length ? (
            <div className="w-64">
              <Field label="Judge" htmlFor="lj">
                <Select id="lj" value={judgeId} onChange={(e) => router.replace(`/p/${projectId}/labeling?judge=${e.target.value}`)}>
                  {judges.data.judges.map((j) => <option key={j.id} value={j.id}>{j.name} v{j.version}</option>)}
                </Select>
              </Field>
            </div>
          ) : null
        }
      />
      <ErrorNote error={error ?? queue.error ?? judges.error} />
      {judges.data && !judges.data.judges.length ? (
        <EmptyState icon={<LabelingIcon size={20} />} title="No judges yet">
          Create a judge and run an experiment with it; its decisions become labeling tasks.
        </EmptyState>
      ) : null}
      {state === null ? (
        judgeId ? (
          <Loading />
        ) : null
      ) : !task ? (
        <EmptyState icon={<LabelingIcon size={20} />} title="Nothing left to label for this judge">
          You have labelled everything this judge has decided ({state.labeled} labels in total). Run another experiment with it to get more.
        </EmptyState>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
          {/* Answers are deliberately neutral: no arm colours, so the labeller can't tell baseline from candidate. */}
          <div key={task.token} className="min-w-0 animate-rise space-y-5">
            <section>
              <h2 className="mb-2 text-[13px] font-semibold text-slate">Conversation</h2>
              <pre className="max-h-[300px] overflow-auto whitespace-pre-wrap rounded-xl border border-rule bg-paper p-4 text-[13px] leading-relaxed">{task.conversation}</pre>
            </section>
            {task.mode === "absolute" ? (
              <section>
                <h2 className="mb-2 text-[13px] font-semibold text-slate">Answer to judge</h2>
                <Panel className="whitespace-pre-wrap p-5 text-[14.5px] leading-relaxed">{task.response}</Panel>
              </section>
            ) : (
              <div className="grid gap-4 md:grid-cols-2">
                {[
                  ["Answer 1", task.first],
                  ["Answer 2", task.second],
                ].map(([label, text]) => (
                  <section key={label}>
                    <h2 className="mb-2 flex items-center gap-2 text-[13px] font-semibold text-slate">
                      <span className="grid h-5 w-5 place-items-center rounded-full bg-ink text-[11px] font-bold text-white" aria-hidden>
                        {label?.slice(-1)}
                      </span>
                      {label}
                    </h2>
                    <Panel className="whitespace-pre-wrap p-5 text-[14.5px] leading-relaxed">{text}</Panel>
                  </section>
                ))}
              </div>
            )}
          </div>
          <aside className="space-y-4 lg:sticky lg:top-6 lg:self-start">
            <Panel className="p-5">
              <h2 className="mb-1 text-[13px] font-semibold">Rubric</h2>
              <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-slate">{task.rubric}</p>
            </Panel>
            <div className="space-y-2">
              {task.mode === "pairwise" ? (
                <>
                  <Button variant="primary" className="w-full justify-center" busy={submitting} onClick={() => void submit("first")}>
                    Answer 1 is better {kbd("1")}
                  </Button>
                  <Button variant="primary" className="w-full justify-center" busy={submitting} onClick={() => void submit("second")}>
                    Answer 2 is better {kbd("2")}
                  </Button>
                  <Button className="w-full justify-center" busy={submitting} onClick={() => void submit("tie")}>
                    About the same {kbd("T")}
                  </Button>
                </>
              ) : task.scale === "binary" ? (
                <>
                  <Button variant="primary" className="w-full justify-center" busy={submitting} onClick={() => void submit("1")}>
                    Meets the rubric {kbd("P")}
                  </Button>
                  <Button variant="danger" className="w-full justify-center" busy={submitting} onClick={() => void submit("0")}>
                    Does not {kbd("F")}
                  </Button>
                </>
              ) : (
                <div className="flex gap-1.5">
                  {["1", "2", "3", "4", "5"].map((s) => (
                    <Button key={s} className="flex-1 justify-center" busy={submitting} onClick={() => void submit(s)}>
                      {s}
                    </Button>
                  ))}
                </div>
              )}
              <Button variant="ghost" className="w-full justify-center" onClick={next}>
                Skip
              </Button>
            </div>
            <Panel className="p-4">
              <div className="flex items-baseline justify-between text-[12.5px]">
                <span className="font-semibold">
                  {Math.min(state.labeled, goal)} of {goal} labels
                </span>
                <span className="text-slate tabular-nums">{state.remaining} decisions left</span>
              </div>
              <div className="mt-2 h-2 overflow-hidden rounded-full bg-mist" role="progressbar" aria-label="Labels toward a first calibration" aria-valuenow={Math.min(state.labeled, goal)} aria-valuemin={0} aria-valuemax={goal}>
                <div className="h-full rounded-full bg-lagoon transition-[width] duration-500 ease-[var(--ease-out-soft)]" style={{ width: `${Math.min(100, (state.labeled / goal) * 100)}%` }} />
              </div>
              <p className="mt-2 text-[12px] text-slate">{state.labeled >= goal ? "Enough for a first calibration. More labels narrow the interval on κ." : "Aim for at least 30 before computing agreement."}</p>
            </Panel>
          </aside>
        </div>
      )}
    </>
  );
}

export default function LabelingPage() {
  return (
    <Suspense>
      <Labeler />
    </Suspense>
  );
}
