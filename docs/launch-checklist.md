# Launch checklist: what only you can do

Everything below needs an account, a payment method or a legal decision. Each step lists what it
unblocks.

## 1. Decide (blocking)

- [ ] **Hosting.**
  - Recommended: **Render** for the API, worker, dashboard and Postgres, with **Cloudflare R2** for
    storage. Rough cost for staging plus production on the smallest production-grade plans is
    $50–100 a month; check current pricing.
  - Alternatives: Fly.io with Tigris storage, or one VM per environment using `deploy/selfhost`.
- [ ] **Region:** US or EU. EU matters if you expect EU customers (GDPR).
- [ ] **Domain**, and who manages its DNS.
- [ ] **Repository** visibility and **license**.
- [ ] Confirm or reverse the ⚑ decisions in `DECISIONS.md`: allowlist signup, RLS, hosting
  recommendation.

## 2. Create accounts and secrets

- [ ] **GitHub repository.**
  - Push this code.
  - Enable branch protection on `main` and require the CI checks.
  - Create environments `staging` and `production`, with required reviewers on production.
- [ ] **GitHub OAuth apps, one per environment.** Callbacks:
  - `http://localhost:3000/api/auth/github/callback`
  - `https://staging-app.<domain>/api/auth/github/callback`
  - `https://app.<domain>/api/auth/github/callback`
- [ ] **Object storage.** Two private buckets (staging and production), each with a scoped
  access-key pair.
- [ ] **Sentry** (optional, free tier): one project; use the DSN.
- [ ] **Hosting.** Apply `deploy/render/render.yaml` twice (staging and production) after replacing
  `OWNER`, and fill in every `sync: false` variable:

| Variable | Value |
|---|---|
| `ENCRYPTION_KEY` | 32 random bytes, urlsafe base64. Store it in your password manager as well. |
| `PUBLIC_APP_URL` / `PUBLIC_API_URL` | The environment's two HTTPS URLs |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | From that environment's OAuth app |
| `S3_*` | Bucket name, endpoint and keys |
| `SIGNUP_ALLOWLIST` | Comma-separated GitHub logins |
| `SENTRY_DSN` | Optional |

- [ ] **Custom domains:** `app.` → `replay-web` and `api.` → `replay-api`, with TLS automatic.
- [ ] **GitHub secrets and variables, per environment:**
  - secrets `DEPLOY_HOOK_API`, `DEPLOY_HOOK_WORKER`, `DEPLOY_HOOK_WEB`, taken from each Render
    service's settings;
  - variables `API_URL` and `APP_URL`;
  - secret `SMOKE_API_KEY`: a key for a dedicated smoke-test project in that environment.

## 3. Verify

- [ ] Merge to `main`, then watch `Deploy to staging` go green, including the smoke test that checks
  the live release SHA.
- [ ] Sign in on staging, create a key, run the quickstart `curl`, and see the trace.
- [ ] Add a real provider key and run one small real experiment (about 20 items) end to end.
- [ ] Run the divergence spike with real models (`spike/divergence/REPORT.md`, next steps).
- [ ] Do a restore drill on staging (`docs/operations.md`).
- [ ] Promote to production, invite the first beta users (Organization → Members, or the allowlist),
  and watch Sentry and `/readyz`.

## 4. Legal (before opening signup)

- [ ] Have a lawyer review `/privacy` and `/terms`. They are drafts that describe the actual behavior.
- [ ] Complete the sub-processor list (hosting, database, storage, Sentry, GitHub).
- [ ] Add a privacy contact address.
- [ ] Decide on a DPA for business customers.
