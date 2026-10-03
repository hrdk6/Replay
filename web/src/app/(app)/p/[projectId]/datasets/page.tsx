"use client";

import { useParams, useRouter } from "next/navigation";
import { DatasetsIcon } from "@/components/icons";
import { EmptyState, ErrorNote, LinkButton, Loading, PageHeader, Status, TableWrap } from "@/components/ui";
import { fmtDate } from "@/lib/format";
import type { Dataset } from "@/lib/types";
import { useApi } from "@/lib/use-api";

export default function DatasetsPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const router = useRouter();
  const { data, error } = useApi<{ datasets: Dataset[] }>(`/projects/${projectId}/datasets`, {
    refreshMs: 3000,
    poll: (d) => !!d?.datasets.some((x) => x.status === "building"),
  });
  return (
    <>
      <PageHeader
        title="Datasets"
        description="Frozen samples of traces. Experiments replay every item, so a dataset is your test set: it does not change when traces expire."
        actions={
          <LinkButton href={`/p/${projectId}/datasets/new`} variant="primary">
            New dataset
          </LinkButton>
        }
      />
      <ErrorNote error={error} />
      {!data ? (
        <Loading />
      ) : data.datasets.length === 0 ? (
        <EmptyState
          icon={<DatasetsIcon size={20} />}
          title="No datasets yet"
          action={
            <LinkButton href={`/p/${projectId}/datasets/new`} variant="primary">
              New dataset
            </LinkButton>
          }
        >
          Pick traces by tag, model, status or text, take a random sample, and freeze it.
        </EmptyState>
      ) : (
        <TableWrap>
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Status</th>
                <th className="num">Items</th>
                <th>Sample</th>
                <th>Frozen</th>
              </tr>
            </thead>
            <tbody>
              {data.datasets.map((d) => (
                <tr key={d.id} data-href onClick={() => router.push(`/p/${projectId}/datasets/${d.id}`)}>
                  <td>
                    <div className="flex items-center gap-3">
                      <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-lagoon-tint text-lagoon" aria-hidden>
                        <DatasetsIcon size={16} />
                      </span>
                      <div>
                        <div className="font-semibold">{d.name}</div>
                        {d.description ? <div className="text-[12.5px] text-slate">{d.description}</div> : null}
                      </div>
                    </div>
                  </td>
                  <td className="align-middle">
                    <Status value={d.status} />
                  </td>
                  <td className="num align-middle font-semibold">{d.item_count.toLocaleString()}</td>
                  <td className="align-middle text-slate">{d.sample_size ? `${d.sample_size} random, seed ${d.seed}` : "all matching"}</td>
                  <td className="align-middle text-slate tabular-nums">{fmtDate(d.frozen_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      )}
    </>
  );
}
