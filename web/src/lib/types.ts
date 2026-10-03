export type Me = {
  user: { id: string; github_login: string; name: string | null; email: string | null; avatar_url: string | null };
  orgs: { id: string; name: string; slug: string; role: string }[];
  active_org_id: string;
  role: "owner" | "admin" | "member";
  csrf_token: string;
};

export type Project = {
  id: string;
  name: string;
  slug: string;
  retention_days: number;
  redaction: { enabled: boolean; builtin: string[]; custom: { name: string; pattern: string }[] };
  created_at: string;
  trace_count: number | null;
};

export type Trace = {
  id: string;
  trace_id: string;
  name: string | null;
  start_time: string | null;
  duration_ms: number | null;
  status: string;
  span_count: number;
  error_count: number;
  llm_call_count: number;
  tool_call_count: number;
  tags: string[];
  metadata: Record<string, unknown>;
  model: string | null;
  input_preview: string | null;
  output_preview: string | null;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
  created_at: string;
};

export type Span = {
  id: string;
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: string;
  status: string;
  status_message: string | null;
  start_time: string;
  end_time: string | null;
  duration_ms: number | null;
  attributes: Record<string, unknown>;
  input: unknown;
  output: unknown;
  model: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
};

export type Dataset = {
  id: string;
  name: string;
  description: string | null;
  filters: Record<string, unknown>;
  sample_size: number | null;
  seed: number;
  item_count: number;
  status: "building" | "ready" | "failed";
  build_info: Record<string, unknown>;
  frozen_at: string | null;
  created_at: string;
};

export type CandidateConfig = {
  provider?: string;
  model?: string;
  system_prompt?: string;
  prompt_template?: { template: string; role: string };
  params?: Record<string, unknown>;
  retrieval?: { top_k?: number };
  pricing?: { input_per_mtok: number; output_per_mtok: number };
};

export type Candidate = { id: string; name: string; description: string | null; config: CandidateConfig; source: string; created_at: string };

export type Calibration = {
  id: string;
  n: number;
  kappa: number | null;
  kappa_low: number | null;
  kappa_high: number | null;
  raw_agreement: number | null;
  status: "uncalibrated" | "insufficient_data" | "poor" | "moderate" | "good";
  report: { categories?: string[]; agreement?: AgreementReport; position_bias?: PositionBias; status_reason?: string };
  computed_at: string;
};

export type PositionBias = {
  n_pairs: number;
  first_position_rate: Interval | null;
  consistency_rate: Interval | null;
  test: { name: string; p_value: number; n: number };
  notes: string[];
};

export type AgreementReport = {
  n: number;
  categories: string[];
  kappa: number | null;
  kappa_ci: Interval | null;
  weighted_kappa: number | null;
  raw_agreement: Interval;
  confusion: number[][];
  notes: string[];
};

export type Judge = {
  id: string;
  family_id: string;
  version: number;
  name: string;
  mode: "absolute" | "pairwise";
  scale: "binary" | "likert5";
  provider: string;
  model: string;
  rubric: string;
  params: Record<string, unknown>;
  include_reference: boolean;
  created_at: string;
  calibration?: Calibration | null;
};

export type Interval = { estimate: number; low: number; high: number; confidence: number; method: string; degenerate?: boolean };

export type Experiment = {
  id: string;
  name: string;
  dataset_id: string;
  candidate_id: string;
  baseline_candidate_id: string | null;
  baseline_mode: string;
  judge_id: string;
  mode: string;
  repeats: number;
  status: string;
  settings: Record<string, unknown>;
  budget_usd: number;
  spent_usd: number;
  estimated_cost_usd: number | null;
  total_items: number;
  done_items: number;
  verdict: "SAFE" | "UNSAFE" | "INCONCLUSIVE" | null;
  error: string | null;
  source: string;
  ci: { repository?: string | null; pull_request?: number | null; sha?: string | null; ref?: string | null } | null;
  created_at: string;
  finished_at: string | null;
  headline?: string | null;
  report?: Report | null;
  refs?: {
    dataset?: { id: string; name: string } | null;
    candidate?: { id: string; name: string; config?: CandidateConfig } | null;
    baseline_candidate?: { id: string; name: string; config?: CandidateConfig } | null;
    judge?: { id: string; name: string; model: string; mode: string; scale: string; version: number } | null;
  };
  progress?: Record<string, number>;
};

export type Report = {
  verdict: "SAFE" | "UNSAFE" | "INCONCLUSIVE";
  headline: string;
  reasons: string[];
  warnings: string[];
  config: { metric: string; margin: number; confidence: number; [k: string]: unknown };
  n_items: number;
  completed_only: Block;
  diverged_as_failure: Block;
  divergence: { baseline: RateSummary; candidate: RateSummary; difference: number | null };
  failures: { baseline: RateSummary; candidate: RateSummary; overall_rate: number | null };
  cost: PairedMetric;
  latency: PairedMetric;
  repeat_variance: Record<string, number | null>;
  slices: Slice[];
  sample_size: { n_required: number | null; explanation: string } | null;
  judge: JudgeBlock;
  replay_notes: string[];
  baseline_mode: string;
  mode: string;
  repeats: number;
  spent_usd: number;
};

export type Block = {
  name: string;
  n_items: number;
  baseline_mean: number | null;
  candidate_mean: number | null;
  difference: Interval | null;
  intervals: Interval[];
  tests: { name: string; p_value: number; statistic: number | null; n: number; note: string }[];
  decision: string;
  reason: string;
  binary_table: Record<string, number> | null;
  sd_of_differences: number | null;
};

export type JudgeBlock = {
  id?: string;
  name?: string;
  version?: number;
  model?: string;
  mode?: string;
  scale?: string;
  errors?: number;
  calibration?: {
    status: string;
    reason: string;
    kappa: number | null;
    kappa_low: number | null;
    kappa_high: number | null;
    n: number;
    computed_at: string | null;
  };
  position_bias?: PositionBias;
  length_bias?: Record<string, unknown>;
};

export type Divergence = {
  step: number;
  reason: string;
  detail?: string;
  requested?: { name: string; arguments: unknown };
  best_similarity?: number;
};

export type ReplayStep =
  | {
      type: "llm";
      model: string;
      input_tokens: number;
      output_tokens: number;
      latency_ms: number;
      stop_reason: string | null;
      tool_calls: { name: string; arguments: unknown }[];
      text: string;
    }
  | {
      type: "tool";
      name: string;
      arguments: unknown;
      match: "exact" | "fuzzy" | "none";
      score: number;
      reused: boolean;
      recorded_index: number | null;
    };

export type RateSummary = { rate: number | null; runs: number; count: number; interval: Interval | null };
export type PairedMetric = { n_items: number; baseline: Interval | null; candidate: Interval | null; difference: Interval | null; ratio: number | null };
export type Slice = {
  dimension: string;
  value: string;
  n_items: number;
  difference: Interval | null;
  p_value: number | null;
  p_adjusted: number | null;
  regression: boolean;
  beyond_margin: boolean;
  note: string;
};
