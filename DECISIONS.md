# Decision log

Newest last. Each entry: the decision, why, and what would make us revisit it.
**⚑ = made on your behalf because you asked for a single pass; please confirm or reverse.**

## 001 Monorepo with a uv workspace and one Next.js app
Python packages (`backend`, `packages/stats`, `packages/sdk-python`, `packages/cli`) share one lockfile;
`web/` is a separate npm project. One repo keeps API, SDK and CLI versions in step. *Revisit* if the SDK
needs an independent release cadence (it can be published from the same repo regardless).

## 002 Postgres for everything (metadata, traces, queue)
As requested. Jobs use `SELECT … FOR UPDATE SKIP LOCKED`, are at-least-once, and every handler is idempotent.
Spans live in Postgres with large payloads (>32 KB) offloaded to S3-compatible storage. *Revisit* (per the
brief, only if asked) when span volume makes Postgres the bottleneck: partition `spans` by month first.

## 003 SQLAlchemy 2 async + asyncpg
One async stack for the API and workers; asyncpg works with Windows' default event loop and is fast.
Migrations run through Alembic's async environment.

## 004 Tenant isolation enforced twice ⚑
1. **App layer.** Every DB session is `tenant_session(org_id)`. An ORM event adds `org_id = :org` to every
   SELECT/UPDATE/DELETE on tenant models, and a flush hook refuses writes for another org.
2. **Postgres row-level security.** It is `FORCE`d on every table with `org_id` and keyed on a
   transaction-local `app.org_id`, set for *every* transaction by an `after_begin` hook so a mid-request
   commit cannot drop it. The app connects as a non-superuser without BYPASSRLS. Cross-tenant code paths
   (auth lookups, queue, retention, org deletion) must use `system_session()` explicitly; grep for it to
   audit.

Tests prove each layer independently: there is a test that disables RLS inside a rolled-back
transaction and shows the ORM guard still holds. An attack matrix covers every route, and a meta-test
fails when a route has no isolation case. *Cost:* data migrations must run with `app.system=on` (Alembic
env does this) and managed Postgres must let the app role own its tables (Render's default user does).

## 005 Dashboard is same-origin; FastAPI owns auth
The browser only talks to the Next.js origin. A route handler (`/api/[...path]`) forwards to FastAPI at
request time, so one image serves staging and production. There is no CORS anywhere: the dashboard is
same-origin and the SDK is not a browser client.
- **Sessions.** Opaque random tokens in an httpOnly, SameSite=Lax cookie, stored as a SHA-256 hash with
  server-side revocation.
- **CSRF.** An HMAC(session) token in a header on unsafe methods, plus an Origin check.

## 006 API keys
Format `rk_<8 hex>_<43 chars>`. The prefix is shown and used for lookup; only a SHA-256 hash is stored.
256 random bits make a slow hash unnecessary. Keys are project-scoped and revocable, with last-used time
tracked.

## 007 Provider keys: envelope encryption ⚑
Each key gets its own random data key (AES-256-GCM). That data key is wrapped with the key-encryption key
from `ENCRYPTION_KEY`, and the ciphertext is bound to org and row via associated data. A copied
ciphertext fails to decrypt. Revoking a key destroys the ciphertext (crypto-shredding).
`ENCRYPTION_KEY_PREVIOUS` supports key rotation. *Revisit:* swap `_wrap/_unwrap` for a cloud KMS once a
provider is chosen.

## 008 Sign-up limited to an allowlist during the beta ⚑
Signing in with a GitHub account that is neither on `SIGNUP_ALLOWLIST` nor invited is refused. Open
signup would let strangers' prompt data land in your storage before the legal pages are reviewed.
Switch with `SIGNUP_MODE=open`.

## 009 Bring-your-own-key only; no fallback models
Replays and judges call providers with the org's own keys through the official `openai` and `anthropic`
SDKs. Anthropic's server-side model fallback is deliberately **not** enabled. If a model refuses, that
refusal is the arm's output; a silent substitute model would corrupt the comparison.

Sampling parameters that newer models reject (for example temperature on recent Claude models, or on
OpenAI reasoning models) are dropped *and recorded* on the run, so the report shows it.

## 010 Simulator provider for tests and demos
`sim-replay`, `sim-perturb:q=…,noise=…,div=…` and `sim-judge` are deterministic, recording-driven stand-ins.
They make the whole pipeline testable without keys and power the demo. They are disabled in production
(`ENABLE_SIMULATOR_PROVIDER=false`), and the UI labels them "testing".

## 011 Replay semantics
- **Tool results always come from the recording.** No user code runs on our servers.
- **`single_turn`** replays only the final LLM call, with recorded tool results in context. Asking for a
  tool where the recording answered counts as `DIVERGED (unexpected_tool_call)`.
- **`full_agent`** runs the loop. Tool calls are matched to recordings (decision 015); no match means
  DIVERGED at that step, and the partial output is kept.
- **Baseline** is either a re-run of the recorded configuration (the default, so both arms face identical
  replay conditions) or the recorded outputs (cheaper, with a warning that nondeterminism then affects
  only one arm).
- **`retrieval.top_k`** can only truncate recorded results. Replay never calls retrievers, and the report
  notes when top_k exceeds what was recorded.
- **Prompt templates** need recorded variables (`replay.prompt_variables(...)` in the SDK). Without them
  the run fails with an explanation rather than guessing.

## 012 Budgets reserve before spending
Each LLM or judge call reserves an upper-bound cost (input estimate plus `max_tokens`) atomically against
both the experiment budget and the org's monthly budget, then settles to the actual cost. Experiments are
refused up front when the estimate exceeds either budget.

When a reservation fails, the experiment stops cleanly as `aborted_budget` and the report covers the
completed runs. Overshoot is bounded by the estimate-versus-actual gap on calls already in flight.
Unknown model prices fall back to a deliberately high default ($15/$75 per MTok), flagged as estimated.

## 013 Statistics: verdict = CI vs a non-inferiority margin, on a conservative envelope
- **Scale.** Scores are on 0–100 points: pass rate, rubric score, or net pairwise win rate.
- **Rule.** SAFE when the lower confidence bound is above −margin; UNSAFE when the upper bound is below
  −margin; otherwise INCONCLUSIVE.
- **Interval.** The decision interval is the envelope of all reported intervals:
  - binary outcomes: Agresti–Min plus paired BCa bootstrap
  - continuous outcomes: BCa bootstrap plus Student t
- **Zero-variance samples** use a Clopper–Pearson-style bound instead of a collapsed bootstrap.
- **Two analyses** always run: completed-only and diverged-as-failure. SAFE requires both to agree and
  divergence to be at most 20%.
- **Slices** are Holm-adjusted.
- **Robustness tests** (McNemar, Wilcoxon, sign) are reported but don't drive the verdict.
- **Simulation.** At exactly the margin, the false-SAFE rate is 2.6–2.8% against a nominal 2.5%. See
  `docs/statistics.md`.
- **Default margin is 5 points**, chosen for practicality. A margin of 0 means "SAFE only if
  significantly better", and the UI says so.

## 014 Judges are immutable and versioned; calibration is visible on every verdict
Editing creates a new version, and calibration doesn't carry over. Pairwise judging runs both A/B orders
by default, which cancels position bias and measures it. Human labeling is blinded: the labeler doesn't
see the judge's decision or which arm produced which answer, display order is randomized, and HMAC tokens
are bound to the labeler.

Calibration status comes from the *lower* bound of κ's CI:
- ≥ 0.6: good
- 0.4 to 0.6: moderate
- below 0.4: poor
- fewer than 30 labels: insufficient

Any status other than good adds a loud warning to the verdict.

## 015 Typed tool-call matching (from the Phase 0.5 spike)
Untyped string-similarity matching silently served wrong recorded results (1.2–3.8% of runs), for example
matching `2026-10-05` to `2026-10-06`. The new default:
- normalizes values (case, whitespace, dates, numeric strings, schema defaults);
- applies fuzzy matching only to free-text arguments, while identifiers, dates and numbers must be equal;
- keeps the threshold at 0.85.

Measured result: 0 wrong results and divergence down from 57% to 35–38% under the spike's synthetic
assumptions. `typed_matching=false` keeps the old behavior available. *Revisit* after the real-model run.

## 016 Redaction at ingest, regex-based, fail-closed
Built-in detectors: secrets, email, Luhn-validated card numbers, SSN, North-American and `+CC` phone
numbers, and optionally IPv4. Custom rules run on the `regex` engine with a 50 ms timeout; on timeout the
whole field is replaced. Redaction is documented as a safety net, not a guarantee.

## 017 Rate limits in process; quotas in Postgres
- **Token buckets** (per key, per user, per IP for auth) stop bursts, per instance.
- **Hard quotas** use Postgres counters: traces per day (counting only *new* traces), replay runs per
  day, and monthly LLM spend.

## 018 Hosting: provider-agnostic artifacts, Render recommended ⚑
Not chosen yet; that's yours to decide. Ready to use:
- a Render Blueprint, with R2 for object storage;
- a single-VM Compose stack with Caddy;
- CI that builds images once, deploys them to staging automatically through deploy hooks, and promotes
  the same digest to production manually, with a smoke test that checks the live release SHA.

Migrations run as a pre-deploy step and follow expand/contract.

## 019 Local object storage: SeaweedFS instead of MinIO
MinIO stopped publishing community Docker images, so the dev and self-host stacks use SeaweedFS
(Apache-2.0, S3-compatible). Production can use any S3 API, with R2 recommended.

## 020 SDK: zero dependencies, never blocks or raises
- **Delivery.** A bounded queue drops data on overflow, a daemon thread batches, timeouts are 3 s, and a
  batch is retried at most once.
- **Instrumentation.** `wrap_openai` and `wrap_anthropic` handle streaming and the async SDKs' sync
  wrappers that return coroutines.
- **LLM spans are leaves** and never become the "current" span, which prevents context leaks across event
  loops.
- **Ordering.** A per-process sequence number orders spans whose timestamps tie.

## 021 CLI is standard-library only
The CLI has fast installs in CI and no dependency conflicts with the user's project. Exit codes: 0 pass or
skipped, 1 blocked by policy, 2 error. The API key is read from the environment only, and the CLI refuses
config files that contain one.
