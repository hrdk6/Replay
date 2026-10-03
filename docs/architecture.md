# Architecture

## Components

| Component | Runs | Talks to |
|---|---|---|
| **API** (`replay-api`, FastAPI) | HTTP on :8000 | Postgres, object storage |
| **Worker** (`replay-worker`) | Claims jobs from Postgres; runs replays and judges with org keys | Postgres, object storage, LLM providers |
| **Dashboard** (Next.js, standalone) | HTTP on :3000; proxies `/api/*` to the API | API only |
| **Postgres** | Metadata, traces/spans, datasets, experiments, job queue, usage, audit | — |
| **Object storage** (S3 API) | Span payloads > 32 KB, large dataset recordings | — |

There are two public hosts: `app.<domain>` (dashboard) and `api.<domain>` (SDK, OTLP, CI).

## Request paths

- **Dashboard:** browser → Next.js route handler `/api/[...path]` → FastAPI `/api/...`. Auth uses the
  session cookie (host-only on the app domain) plus a CSRF header and an Origin check. Every request
  resolves to a principal with `org_id`; every DB session is `tenant_session(org_id)`.
- **SDK and OTLP:** client → `api.<domain>/v1/ingest` or `/v1/otlp/v1/traces` with
  `Authorization: Bearer rk_…`. The key is scoped to one project. The ingest steps are:
  1. validate
  2. check the quota (new traces only)
  3. redact
  4. offload large payloads
  5. upsert traces, which locks their rows so concurrent batches serialize
  6. upsert spans
  7. recompute trace aggregates
- **CI:** the `replay` CLI → `POST /v1/ci/experiments` creates CI-scoped candidates and an experiment,
  then polls `GET /v1/ci/experiments/{id}`, renders the verdict and applies the policy.

## Experiment pipeline (worker)

```
experiment.start      create pending runs (items × arms × repeats); one job per item
experiment.item       replay each pending run (bounded concurrency) → judge → recount done items
experiment.finalize   wait for item jobs → close orphaned runs → replay_stats.analyze_experiment → report
dataset.build         filter + seeded sample traces → freeze recordings (inline or object storage)
retention.sweep       hourly (cron row in Postgres): delete traces past each project's retention
org.delete            delete tenant rows in batches, the org's storage prefix, then the org
```

Jobs are claimed with `FOR UPDATE SKIP LOCKED`, heartbeat every 60 s, and are requeued if a worker dies
(15 min visibility timeout). Failures back off exponentially and end up `dead` after 5 attempts.
Handlers are idempotent: finished runs are skipped on retry, and judgments are keyed by run or by
(repeat, order).

## Data model (main tables)

`orgs`, `users`, `memberships`, `invites`, `sessions`, `projects`, `api_keys`, `provider_keys`,
`traces`, `spans`, `datasets`, `dataset_items`, `candidates`, `judges`, `experiments`,
`experiment_runs`, `judge_results`, `human_labels`, `judge_calibrations`, `audit_log`,
`usage_counters`, `jobs`, `cron_state`.

Every tenant table has `org_id`, an index on it, and a forced RLS policy. See DECISIONS 004.

## Code map

```
backend/src/replay_api/
  main.py            app factory, middleware (size limit, request id, security headers, metrics)
  deps.py            authentication (session / API key), CSRF, roles, tenant DB sessions
  db/                models, base, session (tenant scoping + RLS context)
  routers/           auth, orgs, projects, ingest (+OTLP), traces, datasets, judges, experiments, ci, health
  services/          ingest, otlp, redaction, storage, pricing, usage/quotas, audit, accounts,
                     datasets, experiments (create, estimate, budget guard, analysis), calibration, llm
  replay/            canonical formats, recording builder, matching, providers, engine, judging
  worker/            queue primitives, handlers, worker loop
packages/stats/      pure statistics (no I/O)
packages/sdk-python/ capture SDK
packages/cli/        CI gate
web/src/             dashboard (App Router)
```
