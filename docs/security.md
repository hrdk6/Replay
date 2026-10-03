# Security

## Controls in place

| Area | Control | Verified by |
|---|---|---|
| Tenant isolation | App-layer org filter on every ORM query and write; Postgres RLS **forced** on every tenant table; app role is not superuser and has no BYPASSRLS | `test_tenant_isolation.py` (every route, both all-foreign and mixed-id variants, plus foreign ids in bodies; B's data compared before and after); `test_rls.py` (raw SQL, writes, the ORM guard with RLS disabled, role attributes, every table forced) |
| Authentication | GitHub OAuth (state + PKCE, `read:user user:email` only); server-side sessions stored as hashes, revocable, 14-day TTL; beta allowlist and invites | `test_core_flows.py` |
| CSRF | SameSite=Lax cookie, HMAC(session) header token on unsafe methods, Origin/Referer check | `test_csrf_required_for_unsafe_methods` |
| CORS | None configured: the dashboard is same-origin and the API serves non-browser clients | — |
| API keys | 256-bit secrets; only SHA-256 stored; constant-time compare; project scope; revocation; last-used time | `test_api_key_lifecycle` |
| Provider keys | Envelope encryption (AES-256-GCM, per-key data key, wrapping key from env), bound to org and row, crypto-shredded on revoke; never returned, logged or echoed in validation errors | `test_provider_keys_are_encrypted_and_masked`, `test_envelope_encryption_*` |
| SSRF | Custom provider base URLs must be HTTPS public hosts; DNS is resolved and private, loopback, link-local and metadata ranges are rejected, both when saved and before every call | `test_ssrf_guard` |
| Input limits | 5 MB body cap (checked on Content-Length and on the stream); 1,000 spans per batch; per-field limits; JSON nesting limit; strict Pydantic validation; validation errors never echo values | `test_ingest_validation_and_limits` |
| Rate limits and quotas | Token buckets per key, user and auth IP; Postgres-backed daily trace and run quotas; monthly spend caps with atomic reservation | `test_daily_trace_quota`, `test_budget_checks` |
| PII | Redaction before storage (secrets, email, cards with Luhn, SSN, phone, custom regex with timeout, fail-closed) | `test_builtin_redaction`, `test_custom_rule_validation_and_timeout` |
| Logging and errors | Secret scrubbing by key name and value pattern; no bodies; Sentry with PII off and bodies dropped | `logs.py` |
| Headers | Dashboard: CSP, frame-ancestors none, nosniff, Referrer-Policy, Permissions-Policy, HSTS (production). API: nosniff, DENY, no-store, HSTS (production) | smoke test |
| Audit | Login, keys, provider keys, members and invites, project changes, trace/dataset/experiment deletion, export, org deletion | `audit_log` table; Organization page |
| Supply chain | Lockfiles (uv, npm); Dependabot; pip-audit and npm audit in CI; gitleaks over the full history; non-root containers; slim base images | `ci.yml` |
| Code execution | Replay never runs user code: tool results come from recordings. LLM calls use the customer's keys. | design (docs/replay.md) |

## Review checklist before launch

- [ ] Hosting chosen; TLS on both domains; HSTS preload considered.
- [ ] `ENV=production`; startup refuses development secrets (it is enforced, so confirm it happens).
- [ ] `ENCRYPTION_KEY` generated, stored in the secret manager **and** in an offline backup.
- [ ] Database: managed, encrypted at rest, automated backups and PITR on, private networking, app role
      is not superuser (`SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user`).
- [ ] Object storage: private bucket, encryption at rest, versioning or replication, least-privilege keys.
- [ ] GitHub OAuth apps per environment with exact callback URLs.
- [ ] `SIGNUP_MODE=allowlist` until legal review is done.
- [ ] `ENABLE_SIMULATOR_PROVIDER=false` in production.
- [ ] `METRICS_TOKEN` set; `/metrics` not publicly scraped without it.
- [ ] Sentry DSN set; alerting on readiness failures, 5xx rate and dead jobs.
- [ ] Branch protection on `main`, with required CI and required reviewers on the `production`
      environment.
- [ ] Restore drill repeated on staging (docs/operations.md).
- [ ] Privacy policy and terms reviewed by a lawyer; sub-processor list completed.
- [ ] Penetration test or external review of auth, isolation and the SSRF guard (recommended before
      opening signup).

## Known gaps (tracked, not hidden)

- Rate limits are per process; with N API instances, the burst allowance is N times higher. Hard quotas
  are global.
- DNS rebinding can still race the SSRF check between resolution and connection. Mitigations: HTTPS-only
  base URLs and checks before every call. A full fix would pin resolved IPs in the HTTP client.
- `ENCRYPTION_KEY` rotation lacks a bulk re-wrap job.
- Redaction is pattern-based and will miss unusual formats; customers should still avoid sending
  sensitive data they don't need.
- The dashboard's CSP allows `'unsafe-inline'` scripts, which the Next.js runtime needs without nonces.
  Moving to nonce-based CSP is a follow-up.
