# Operations

## Environments and deploys

| Step | How |
|---|---|
| Every PR | `ci.yml` runs:<ul><li>ruff, mypy (strict)</li><li>all Python tests against real Postgres, including the tenant-isolation matrix and the statistical simulations</li><li>`alembic check` for model/migration drift</li><li>dashboard lint and build</li><li>gitleaks over the full history, pip-audit, npm audit</li><li>both Docker images build</li></ul> |
| Merge to `main` | After CI passes, `deploy-staging.yml` builds the images **once**, tags them with the commit SHA (plus `:staging`), triggers the staging deploy hooks with that exact image, and runs `deploy/smoke_test.py --expect-release <sha>`. |
| Production | You run `promote-production.yml` by hand with a SHA. The `production` environment should require reviewers. It re-tags the already-tested image as `:production` (no rebuild), triggers the production hooks, and smoke-tests the expected release. |
| Migrations | Run as a pre-deploy command (`alembic -c backend/alembic.ini upgrade head`) before new code serves traffic. Each runs in one transaction with `lock_timeout=10s`. |

**Migration rules (expand/contract).**
- Add columns as nullable, or with a server default.
- Never drop or rename a column in the same release as the code that stops using it: ship the code
  first, remove the column in a later release.
- Every new tenant table must get `enable_rls(...)` in its migration. The test
  `test_every_tenant_table_has_forced_rls` fails if one is forgotten.

**Rollback.** Promote the previous SHA. Because migrations are backwards-compatible, the previous code
still runs on the newer schema.

## Configuration

All configuration is environment variables; see `.env.example`, `deploy/selfhost/.env.example` and
`deploy/render/render.yaml`. Staging and production **refuse to start** if any of these hold:
- `DEV_LOGIN_ENABLED` is on;
- `SESSION_SECRET` or `ENCRYPTION_KEY` still has the development default;
- GitHub OAuth isn't configured;
- `PUBLIC_APP_URL` isn't HTTPS;
- in-memory storage is selected.

**Secrets to back up outside the database:** `ENCRYPTION_KEY`. Without it, stored provider keys can't be
decrypted, and users must re-enter them.

**Rotating `ENCRYPTION_KEY`:** set the old value as `ENCRYPTION_KEY_PREVIOUS` and a new
`ENCRYPTION_KEY`, then deploy. Decryption tries both keys. A re-wrap job is not built yet, so keep the
previous key set until every provider key has been re-entered or re-wrapped.

## Backups and restore

- **Managed Postgres** (recommended): enable the provider's automated daily backups and point-in-time
  recovery, then check the retention window.
- **Self-host:** the `backup` service writes a nightly `pg_dump -Fc` into the `backups` volume and keeps
  14 days. **Copy these off the machine** (for example with a cron'd `rclone` to R2 or S3); a backup on the
  same disk is not a backup.
- **Object storage:** enable bucket versioning or replication with your provider. Payload objects are
  only ever written at deterministic keys and deleted on retention or org deletion.

### Restore runbook

1. Never restore over the live database. Restore into a **new** database:
   ```bash
   PGHOST=<db-host> PGPASSWORD=<superuser-pw> deploy/backup/restore.sh <dump-file> replay_restored
   ```
   The script restores as the app role, so tables stay owned by it with their RLS policies, then runs
   `verify_restore.sql`. That checks the schema version, prints row counts, and **fails if any tenant
   table lost forced RLS**.
2. Compare the row counts with the expected state (and the incident timeline).
3. During a maintenance window, point `DATABASE_URL` at `replay_restored` (or rename databases) and
   restart the API and worker.
4. Run `deploy/smoke_test.py` against the environment.

**Drill (October 3, 2026, local).** A dump of the development database restored into a new database at
schema 0002. Counts matched the live database (161 traces, 641 spans, 4 experiments), RLS was intact on
every tenant table, and it took about 2 s. Repeat the drill on staging before launch and then
quarterly. A step-by-step log helps.

## Monitoring

- **Health.** `GET /healthz` is liveness (no dependencies) and reports `release`. `GET /readyz` checks
  the database and object storage and returns 503 when either fails; use it as the load balancer health
  check.
- **Metrics.** `GET /metrics` serves Prometheus metrics: `replay_http_requests_total{method,route,status}`
  and `replay_http_request_seconds`. It requires `Authorization: Bearer $METRICS_TOKEN`, and returns 404
  in production when no token is set.
- **Logs.** Structured JSON on stdout, one line per request (method, route template, status, ms) with a
  `request_id`, which is echoed in the `X-Request-ID` header. Secrets and cookies are scrubbed by key name
  and value pattern before logging. Request bodies are never logged.
- **Errors.** Sentry, when `SENTRY_DSN` is set: `send_default_pii=False`, request bodies dropped,
  sensitive headers masked, every event scrubbed.
- **Worth alerting on:**
  - `/readyz` failures
  - a 5xx rate above 1%
  - jobs in `dead` status: `SELECT count(*) FROM jobs WHERE status='dead'`
  - queue age: `SELECT now()-min(run_after) FROM jobs WHERE status='queued'`
  - disk usage on self-host

## Capacity reference

`scripts/load_test.py`, Docker Desktop on a laptop, two API processes, local Postgres: **about 650
traces/s (2,600 spans/s)** sustained, p50 231 ms, p95 395 ms, p99 485 ms, no errors over 1,304 batched
requests (10 traces each). Re-measure on staging hardware before setting quotas.

## Data lifecycle

- **Trace retention:** per project (default 30 days). An hourly sweep deletes traces older than the
  retention period (by ingestion time) and their payload objects.
- **Datasets** are frozen copies, independent of trace retention, and deleted with their project or org.
- **Deletion endpoints:** trace, dataset, experiment, project, organization (asynchronous: access is
  revoked immediately, then data and storage are deleted), and account.
- **Export:** `GET /api/orgs/current/export` streams NDJSON of every tenant table, payloads included and
  secrets excluded.

## Quotas

Defaults per org: 50,000 traces per day, 5,000 replay runs per day, and a $50 monthly LLM budget (the
budget is editable by org admins). Operators change the quotas with:

```bash
replay-admin set-quota <org-slug> --traces-per-day 200000 --runs-per-day 20000 --monthly-budget-usd 500
```
