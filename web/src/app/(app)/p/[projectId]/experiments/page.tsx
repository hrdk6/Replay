"use client";

import { useParams, useRouter } from "next/navigation";
import { ExperimentsIcon } from "@/components/icons";
import { EmptyState, ErrorNote, LinkButton, Loading, PageHeader, Status, TableWrap, Tag } from "@/components/ui";
import { VerdictChip } from "@/components/verdict";
import { fmtDate, fmtUsd, sentence, truncate } from "@/lib/format";
import type { Experiment } from "@/lib/types";
import { useApi } from "@/lib/use-api";

const ACTIVE = ["queued", "running"];

function ItemsDone({ e }: { e: Experiment }) {
  const pct = e.total_items ? (e.done_items / e.total_items) * 100 : 0;
  return (
    <div className="ml-auto w-[92px]">
      <div className="text-right tabular-nums">
        {e.done_items}/{e.total_items}
      </div>
      {ACTIVE.includes(e.status) ? (
        <div className="relative mt-1 h-1.5 overflow-hidden rounded-full bg-mist" aria-hidden>
          <div className="relative h-full overflow-hidden rounded-full bg-lagoon" style={{ width: `${Math.max(pct, 3)}%` }}>
            <span className="progress-glint" />
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default function ExperimentsPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const router = useRouter();
  const { data, error } = useApi<{ experiments: Experiment[] }>(`/projects/${projectId}/experiments`, {
    refreshMs: 4000,
    poll: (d) => !!d?.experiments.some((e) => ACTIVE.includes(e.status)),
  });
  return (
    <>
      <PageHeader
        title="Experiments"
        description="Each experiment replays a dataset with a candidate and a baseline, judges both, and reports what the evidence supports."
        actions={
          <LinkButton href={`/p/${projectId}/experiments/new`} variant="primary">
            New experiment
          </LinkButton>
        }
      />
      <ErrorNote error={error} />
      {!data ? (
        <Loading rows={5} />
      ) : data.experiments.length === 0 ? (
        <EmptyState
          icon={<ExperimentsIcon size={20} />}
          title="No experiments yet"
          action={
            <LinkButton href={`/p/${projectId}/experiments/new`} variant="primary">
              New experiment
            </LinkButton>
          }
        >
          You need a dataset, a candidate and a judge. The form walks you through it.
        </EmptyState>
      ) : (
        <TableWrap>
          <table className="data-table [&_td]:align-middle">
            <thead>
              <tr>
                <th>Verdict</th>
                <th>Experiment</th>
                <th>Status</th>
                <th className="num">Items</th>
                <th className="num">Spent</th>
                <th>Started</th>
              </tr>
            </thead>
            <tbody>
              {data.experiments.map((e) => (
                <tr key={e.id} data-href onClick={() => router.push(`/p/${projectId}/experiments/${e.id}`)}>
                  <td className="w-[150px]">
                    {e.verdict ? <VerdictChip verdict={e.verdict} /> : ACTIVE.includes(e.status) ? <span className="text-[12.5px] text-slate">in progress</span> : <span className="text-faint">–</span>}
                  </td>
                  <td className="max-w-[520px]">
                    <div className="flex flex-wrap items-center gap-1.5 font-semibold">
                      {e.name}
                      {e.source === "ci" ? <Tag tone="lagoon">CI{e.ci?.pull_request ? ` #${e.ci.pull_request}` : ""}</Tag> : null}
                    </div>
                    {e.headline ? <div className="mt-0.5 text-[12.5px] text-slate">{truncate(sentence(e.headline.replace(/^(SAFE|UNSAFE|INCONCLUSIVE):\s*/, "")), 160)}</div> : null}
                  </td>
                  <td>
                    <Status value={e.status} />
                  </td>
                  <td className="num">
                    <ItemsDone e={e} />
                  </td>
                  <td className="num">{fmtUsd(e.spent_usd)}</td>
                  <td className="whitespace-nowrap text-slate tabular-nums">{fmtDate(e.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      )}
    </>
  );
}
