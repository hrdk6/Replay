"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { Button, Crumb, ErrorNote, KeyValue, LinkButton, Loading, Notice, PageHeader, Panel, Section, Status, Textarea, useToast } from "@/components/ui";
import { CountUp } from "@/components/verdict";
import { api } from "@/lib/api";
import { fmtDate, fmtPct, sentence } from "@/lib/format";
import type { Calibration, Judge } from "@/lib/types";
import { useApi } from "@/lib/use-api";

type JudgeDetail = Judge & {
  versions: { id: string; version: number; created_at: string }[];
  calibration: Calibration | null;
  calibration_history: Calibration[];
  labels: number;
  judgments: number;
};

const STATUS_TEXT: Record<string, string> = {
  uncalibrated: "No human labels yet. Label a sample from the queue, then compute agreement.",
  insufficient_data: "Not enough evidence for a reliable estimate yet.",
  poor: "The judge disagrees with people too often to trust its verdicts.",
  moderate: "Moderate agreement. Usable with care; consider sharpening the rubric.",
  good: "The judge agrees well with human labels (lower CI bound of κ at least 0.6).",
};

const KAPPA_TONE: Record<string, string> = {
  good: "text-safe-ink",
  moderate: "text-unsure-ink",
  poor: "text-unsafe-ink",
};

/** κ on a fixed scale with the bands the calibration status uses. */
function KappaScale({ kappa, low, high, status }: { kappa: number; low: number | null; high: number | null; status: string }) {
  const lo = -0.2;
  const hi = 1;
  const width = 520;
  const pad = 10;
  const x = (v: number) => pad + ((Math.min(Math.max(v, lo), hi) - lo) / (hi - lo)) * (width - 2 * pad);
  const mid = 26;
  const h = 18;
  const cl = low ?? kappa;
  const ch = high ?? kappa;
  const ciW = Math.max(2, x(ch) - x(cl));
  const origin = ciW > 2 ? ((x(kappa) - x(cl)) / ciW) * 100 : 50;
  // Colour follows the calibration status, which the backend derives from the interval, not the point.
  const color = status === "good" ? "var(--color-safe)" : status === "poor" ? "var(--color-unsafe)" : "var(--color-unsure)";
  return (
    <svg viewBox={`0 0 ${width} 62`} width="100%" style={{ maxWidth: width }} className="animate-gauge block overflow-visible" role="img" aria-label={`kappa ${kappa.toFixed(2)}`}>
      <defs>
        <clipPath id="kappa-track">
          <rect x={pad} y={mid - h / 2} width={width - 2 * pad} height={h} rx={h / 2} />
        </clipPath>
      </defs>
      <g clipPath="url(#kappa-track)">
        <rect x={pad} y={mid - h / 2} width={x(0.4) - pad} height={h} fill="var(--color-unsafe-tint)" />
        <rect x={x(0.4)} y={mid - h / 2} width={x(0.6) - x(0.4)} height={h} fill="var(--color-unsure-tint)" />
        <rect x={x(0.6)} y={mid - h / 2} width={width - pad - x(0.6)} height={h} fill="var(--color-safe-tint)" />
      </g>
      <rect className="gauge-ci" x={x(cl)} y={mid - 4} width={ciW} height={8} rx={4} fill={color} style={{ transformOrigin: `${origin}% 50%` }} />
      <circle className="gauge-dot" cx={x(kappa)} cy={mid} r={7} fill="var(--color-paper)" stroke={color} strokeWidth={3} />
      <g fontSize="11.5" fill="var(--color-slate)" style={{ fontVariantNumeric: "tabular-nums" }}>
        {[0, 0.4, 0.6, 1].map((t) => (
          <text key={t} x={x(t)} y={56} textAnchor="middle">
            {t.toFixed(1)}
          </text>
        ))}
        <text x={(x(0.6) + x(1)) / 2} y={mid + 4} textAnchor="middle" fill="var(--color-safe-ink)" fontWeight="600" opacity="0.8" className="gauge-label">
          good
        </text>
        <text x={(x(lo) + x(0.4)) / 2} y={mid + 4} textAnchor="middle" fill="var(--color-unsafe-ink)" fontWeight="600" opacity="0.7" className="gauge-label">
          poor
        </text>
      </g>
    </svg>
  );
}

function Confusion({ matrix, categories }: { matrix: number[][]; categories: string[] }) {
  const max = Math.max(1, ...matrix.flat());
  return (
    <div>
      <div className="mb-2 text-[12.5px] text-slate">Rows are the judge&apos;s answer, columns the human&apos;s. The diagonal is agreement.</div>
      <table className="border-separate border-spacing-1 text-[12.5px] tabular-nums">
        <thead>
          <tr>
            <th />
            {categories.map((c) => (
              <th key={c} className="px-2 pb-1 font-semibold text-slate">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {matrix.map((row, i) => (
            <tr key={categories[i]}>
              <th className="pr-2 text-right font-semibold text-slate">{categories[i]}</th>
              {row.map((v, j) => {
                const share = v / max;
                const diag = i === j;
                return (
                  <td
                    key={j}
                    className={`h-11 w-16 rounded-lg text-center font-semibold ${share > 0.55 ? "text-white" : "text-ink"}`}
                    style={{
                      background: `color-mix(in srgb, ${diag ? "var(--color-safe)" : "var(--color-unsafe)"} ${Math.round(8 + share * (diag ? 80 : 60))}%, white)`,
                    }}
                  >
                    {v}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function JudgePage() {
  const { projectId, judgeId } = useParams<{ projectId: string; judgeId: string }>();
  const router = useRouter();
  const toast = useToast();
  const { data, error, reload } = useApi<JudgeDetail>(`/projects/${projectId}/judges/${judgeId}`);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<unknown>(null);
  const [editing, setEditing] = useState(false);
  const [rubric, setRubric] = useState("");

  if (error) return <ErrorNote error={error} />;
  if (!data) return <Loading rows={5} />;
  const cal = data.calibration;
  const agreement = cal?.report?.agreement;
  const pb = cal?.report?.position_bias;
  const status = cal?.status ?? "uncalibrated";

  async function calibrate() {
    setBusy(true);
    setActionError(null);
    try {
      await api(`/projects/${projectId}/judges/${judgeId}/calibrate`, { method: "POST" });
      await reload();
      toast("Agreement recomputed");
    } catch (err) {
      setActionError(err);
    }
    setBusy(false);
  }

  async function newVersion() {
    try {
      const j = await api<Judge>(`/projects/${projectId}/judges/${judgeId}/versions`, { method: "POST", body: { rubric } });
      router.push(`/p/${projectId}/judges/${j.id}`);
    } catch (err) {
      setActionError(err);
    }
  }

  return (
    <>
      <PageHeader
        crumb={<Crumb href={`/p/${projectId}/judges`}>Judges</Crumb>}
        title={`${data.name} v${data.version}`}
        description={`${data.mode === "pairwise" ? "Pairwise comparison" : `Absolute score (${data.scale === "binary" ? "pass/fail" : "1–5"})`} using ${data.model}.`}
        actions={
          <LinkButton href={`/p/${projectId}/labeling?judge=${data.id}`} variant="primary">
            Label outputs
          </LinkButton>
        }
      />
      <div className="grid gap-8 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)]">
        <div>
          <Section
            title="Agreement with humans"
            description="Humans label a random sample of this judge's decisions without seeing them. Cohen's κ corrects raw agreement for chance; the interval comes from a bootstrap over labelled items."
            actions={
              <Button onClick={() => void calibrate()} busy={busy}>
                Recompute
              </Button>
            }
          >
            <ErrorNote error={actionError} />
            <Panel className="p-5">
              <div className="mb-4 flex flex-wrap items-start gap-3">
                <Status value={status} />
                <span className="text-[13px] text-slate">
                  {STATUS_TEXT[status]}
                  {cal?.report?.status_reason && status !== "uncalibrated" ? <span className="block text-[12.5px] text-faint">{sentence(cal.report.status_reason)}.</span> : null}
                </span>
              </div>
              {cal && cal.n > 0 ? (
                <>
                  <div className="grid gap-6 sm:grid-cols-[auto_1fr] sm:items-center">
                    <div>
                      {cal.kappa !== null ? (
                        <div className={`readout text-[56px] font-bold ${KAPPA_TONE[status] ?? "text-ink"}`}>
                          <CountUp value={cal.kappa} format={(v) => v.toFixed(2)} />
                        </div>
                      ) : (
                        <div className="readout text-[34px] font-bold text-faint">undefined</div>
                      )}
                      <div className="mt-1 text-[12.5px] text-slate">
                        Cohen&apos;s κ{cal.kappa_low !== null ? `, 95% CI ${cal.kappa_low.toFixed(2)} to ${cal.kappa_high?.toFixed(2)}` : ""}
                      </div>
                    </div>
                    <KeyValue
                      items={[
                        ["Labelled items", cal.n],
                        ["Raw agreement", fmtPct(cal.raw_agreement, 1)],
                        ...(agreement?.weighted_kappa !== null && agreement?.weighted_kappa !== undefined ? [["Quadratic-weighted κ", agreement.weighted_kappa.toFixed(2)] as [string, string]] : []),
                        ["Computed", fmtDate(cal.computed_at)],
                      ]}
                    />
                  </div>
                  {cal.kappa !== null ? (
                    <div className="mt-5">
                      <KappaScale kappa={cal.kappa} low={cal.kappa_low} high={cal.kappa_high} status={status} />
                    </div>
                  ) : null}
                </>
              ) : null}
              {agreement?.confusion ? (
                <div className="mt-6 border-t border-mist pt-5">
                  <Confusion matrix={agreement.confusion} categories={agreement.categories} />
                </div>
              ) : null}
              {agreement?.notes?.length ? (
                <ul className="mt-4 list-disc pl-5 text-[12.5px] text-slate">
                  {agreement.notes.map((n: string) => (
                    <li key={n}>{sentence(n)}</li>
                  ))}
                </ul>
              ) : null}
            </Panel>
          </Section>
          {pb?.first_position_rate ? (
            <Section title="Position bias" description="Every pair is judged in both orders. An unbiased judge picks the first-shown answer half the time.">
              <Panel className="p-5">
                <KeyValue
                  items={[
                    ["Picks first-shown answer", `${fmtPct(pb.first_position_rate.estimate, 1)} (CI ${fmtPct(pb.first_position_rate.low)}–${fmtPct(pb.first_position_rate.high)})`],
                    ["Same winner in both orders", fmtPct(pb.consistency_rate?.estimate, 1)],
                    ["Pairs", pb.n_pairs],
                  ]}
                />
                {pb.first_position_rate.low > 0.5 || pb.first_position_rate.high < 0.5 ? (
                  <div className="mt-4">
                    <Notice>This judge has a measurable position bias. Verdicts stay valid because both orders are averaged, but the rubric may need work.</Notice>
                  </div>
                ) : null}
              </Panel>
            </Section>
          ) : null}
        </div>
        <div>
          <Section
            title="Rubric"
            actions={
              !editing ? (
                <Button
                  variant="ghost"
                  onClick={() => {
                    setRubric(data.rubric);
                    setEditing(true);
                  }}
                >
                  Edit as new version
                </Button>
              ) : null
            }
          >
            {editing ? (
              <div className="animate-fade space-y-3">
                <Textarea rows={10} value={rubric} onChange={(e) => setRubric(e.target.value)} />
                <p className="text-[12.5px] text-slate">Judges are immutable: saving creates version {Math.max(...data.versions.map((v) => v.version)) + 1}, which needs its own calibration.</p>
                <div className="flex gap-2">
                  <Button variant="primary" onClick={() => void newVersion()}>
                    Save new version
                  </Button>
                  <Button onClick={() => setEditing(false)}>Cancel</Button>
                </div>
              </div>
            ) : (
              <Panel className="whitespace-pre-wrap p-5 text-[13.5px] leading-relaxed">{data.rubric}</Panel>
            )}
          </Section>
          <Section title="Versions">
            <ol className="space-y-1.5 text-[13px]">
              {data.versions.map((v) => (
                <li key={v.id} className="flex items-center gap-2.5">
                  <span className={`h-2 w-2 rounded-full ${v.id === data.id ? "bg-lagoon" : "bg-rule"}`} aria-hidden />
                  {v.id === data.id ? (
                    <strong>v{v.version} (this one)</strong>
                  ) : (
                    <Link className="font-medium text-lagoon-ink hover:underline" href={`/p/${projectId}/judges/${v.id}`}>
                      v{v.version}
                    </Link>
                  )}
                  <span className="text-slate">{fmtDate(v.created_at)}</span>
                </li>
              ))}
            </ol>
            <p className="mt-3 text-[12.5px] text-slate">
              {data.judgments.toLocaleString()} judgments made, {data.labels} labelled by people.
            </p>
          </Section>
        </div>
      </div>
    </>
  );
}
