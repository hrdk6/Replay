"use client";

import { useParams } from "next/navigation";
import { useState, type ReactNode } from "react";
import { CopyBlock } from "@/components/copy-block";
import { CheckIcon } from "@/components/icons";
import { Button, ErrorNote, LinkButton, Notice, PageHeader, Tabs, useToast } from "@/components/ui";
import { api } from "@/lib/api";
import type { Project } from "@/lib/types";
import { useApi } from "@/lib/use-api";

function Step({ n, title, description, done, last, children }: { n: number; title: string; description?: ReactNode; done: boolean; last?: boolean; children: ReactNode }) {
  return (
    <li className="relative grid grid-cols-[36px_minmax(0,1fr)] gap-4 pb-10">
      {!last ? <span className={`absolute bottom-0 left-[17px] top-10 w-[2px] rounded-full transition-colors duration-500 ${done ? "bg-lagoon/40" : "bg-rule"}`} aria-hidden /> : null}
      <span
        className={`relative z-10 grid h-9 w-9 place-items-center rounded-full text-[14px] font-bold transition-colors duration-300 ${done ? "bg-lagoon text-white" : "border-2 border-rule bg-paper text-slate"}`}
        aria-hidden
      >
        {done ? <CheckIcon size={17} className="animate-fade" /> : n}
      </span>
      <div className="min-w-0 pt-1">
        <h2 className="text-[16px] font-bold tracking-[-0.01em]">
          <span className="sr-only">Step {n}{done ? ", done" : ""}: </span>
          {title}
        </h2>
        {description ? <p className="mt-0.5 max-w-[70ch] text-[13px] text-slate">{description}</p> : null}
        <div className="mt-3">{children}</div>
      </div>
    </li>
  );
}

const METHODS = [
  ["python", "Python SDK"],
  ["curl", "curl"],
  ["otel", "OpenTelemetry"],
] as const;

export default function QuickstartPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const toast = useToast();
  const config = useApi<{ public_api_url: string }>("/auth/config");
  const project = useApi<Project>(`/projects/${projectId}`, { refreshMs: 3000, poll: (p) => !p?.trace_count });
  const [key, setKey] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [method, setMethod] = useState<(typeof METHODS)[number][0]>("python");
  const endpoint = config.data?.public_api_url ?? "https://your-replay-api";
  const shownKey = key ?? "rk_your_key";
  const received = project.data?.trace_count ?? 0;

  async function createKey() {
    setBusy(true);
    setError(null);
    try {
      const res = await api<{ key: string }>(`/projects/${projectId}/api-keys`, { method: "POST", body: { name: "Quickstart" } });
      setKey(res.key);
      toast("API key created");
    } catch (err) {
      setError(err);
    }
    setBusy(false);
  }

  const python = `pip install replay-sdk

export REPLAY_API_KEY=${shownKey}
export REPLAY_ENDPOINT=${endpoint}`;

  const code = `import replay_sdk as replay
from openai import OpenAI

replay.init()                                # reads REPLAY_API_KEY and REPLAY_ENDPOINT
client = replay.wrap_openai(OpenAI())        # or replay.wrap_anthropic(Anthropic())

@replay.tool()
def get_order(order_id: str) -> dict:        # tool results are what replay serves back
    return {"order_id": order_id, "status": "shipped"}

@replay.trace(tags=["support"], metadata={"route": "/support"})
def answer(question: str) -> str:
    ...                                      # your agent loop, unchanged`;

  const curl = `curl -X POST ${endpoint}/v1/ingest \\
  -H "Authorization: Bearer ${shownKey}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "spans": [{
      "trace_id": "hello-1", "span_id": "llm-1", "name": "chat", "kind": "llm",
      "start_time": "${new Date().toISOString()}",
      "attributes": {"gen_ai.request.model": "gpt-4o-mini", "gen_ai.usage.input_tokens": 12, "gen_ai.usage.output_tokens": 9},
      "input": {"messages": [{"role": "user", "content": "Hello!"}]},
      "output": {"role": "assistant", "content": "Hi! How can I help?"}
    }],
    "traces": [{"trace_id": "hello-1", "tags": ["quickstart"]}]
  }'`;

  const otel = `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=${endpoint}/v1/otlp/v1/traces
OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Bearer ${shownKey}"
OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`;

  return (
    <>
      <PageHeader
        title="Send your first trace"
        description={`Connect ${project.data?.name ?? "this project"} to your application. Integration takes a few lines; the SDK never blocks or crashes your app.`}
      />
      <ol className="max-w-[860px]">
        <Step n={1} title="Create an API key" description="Keys belong to this project. The full key is shown once; Replay stores only a hash." done={!!key || received > 0}>
          {key ? (
            <div className="animate-rise">
              <Notice tone="info" title="Copy this key now">
                <span className="break-all font-mono text-[12.5px]">{key}</span>
              </Notice>
            </div>
          ) : (
            <Button variant="primary" onClick={() => void createKey()} busy={busy}>
              Create API key
            </Button>
          )}
          <div className="mt-2">
            <ErrorNote error={error} />
          </div>
        </Step>
        <Step
          n={2}
          title="Instrument your app"
          description={
            method === "python"
              ? "Install, set two environment variables, and wrap your client."
              : method === "curl"
                ? "Send a single span by hand to check the connection."
                : "Any OTLP/HTTP exporter works. Replay understands the OpenTelemetry GenAI conventions, plus OpenLLMetry and OpenInference attributes."
          }
          done={received > 0}
        >
          <div className="mb-3">
            <Tabs label="Integration method" value={method} options={METHODS} onChange={setMethod} />
          </div>
          <div key={method} className="animate-fade space-y-3">
            {method === "python" ? (
              <>
                <CopyBlock code={python} />
                <CopyBlock code={code} />
              </>
            ) : method === "curl" ? (
              <CopyBlock code={curl} />
            ) : (
              <CopyBlock code={otel} />
            )}
          </div>
        </Step>
        <Step n={3} title="See it arrive" done={received > 0} last>
          {received ? (
            <div className="flex flex-wrap items-center gap-4 rounded-xl border border-safe/25 bg-safe-tint px-4 py-3 animate-rise">
              <span className="readout text-[34px] font-bold text-safe-ink tabular-nums">{received.toLocaleString()}</span>
              <span className="text-[13.5px] text-safe-ink">traces received</span>
              <span className="ml-auto">
                <LinkButton href={`/p/${projectId}/traces`} variant="primary">
                  Open traces
                </LinkButton>
              </span>
            </div>
          ) : (
            <p className="inline-flex items-center gap-2.5 rounded-lg bg-paper px-3.5 py-2.5 text-[13px] text-slate ring-1 ring-rule">
              <span className="live-dot h-2 w-2 rounded-full bg-lagoon-bright" aria-hidden />
              Waiting for the first trace. This page checks every few seconds.
            </p>
          )}
        </Step>
      </ol>
    </>
  );
}
