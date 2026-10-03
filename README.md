# Replay

**Will this change to my model, prompt or retrieval make my LLM app better or worse, and by how much?**

Replay captures production traces from LLM apps and agents, replays them against a candidate change,
scores both sides with an LLM judge you have checked against human labels, and returns a verdict you
can defend: **SAFE**, **UNSAFE** or **INCONCLUSIVE**, with confidence intervals. It can block pull
requests that make things significantly worse.

```
your app ──SDK / OTLP──► API ──► Postgres (metadata, traces, job queue)   ┐
                          │  └─► S3-compatible storage (large payloads)    │ one org's data, isolated
dashboard (Next.js) ──────┘                                                │ in code AND by Postgres RLS
worker ◄── jobs ── replay (your LLM keys) ─► judge ─► statistics ─► verdict┘
CI: `replay check` / GitHub Action ─► API ─► PR comment + pass/fail status
```

## Status

All six feature areas from the brief are built and tested:
- **Capture:** SDK, OTLP, redaction, retention.
- **Replay:** single-turn and full-agent modes, divergence handling, budgets, bring-your-own keys.
- **Judging:** judges, calibration, bias checks.
- **Statistics:** the stats library and verdict.
- **Dashboard:** all pages.
- **CI gate:** CLI and GitHub Action.

The Phase 0.5 divergence spike was run and its findings shipped; see
[`spike/divergence/REPORT.md`](spike/divergence/REPORT.md).

**Not live yet.** Going live needs accounts that only you can create: a hosting provider, a domain,
GitHub OAuth apps, an object-storage bucket and Sentry. The steps are in
[`docs/launch-checklist.md`](docs/launch-checklist.md). Everything else (CI/CD, Render Blueprint,
self-host stack, smoke tests, backups) is ready to use.

## Run it locally

Requirements: Docker, [uv](https://docs.astral.sh/uv/), Node 20+.

```bash
docker compose up --build          # Postgres, S3 (SeaweedFS), migrations, API :8000, worker, dashboard :3000
uv sync                            # Python workspace for scripts and tests
uv run python scripts/seed_demo.py # demo workspace: traces, dataset, candidates, judges, 3 experiments
```

Open http://localhost:3000, use **Development sign-in** with the login `demo`, and open Experiments.
The demo uses the built-in *simulator* provider, so no LLM keys are needed. To use real models, add an
OpenAI or Anthropic key under Organization settings, then create candidates and judges that use them.

### Develop on the host (hot reload)

```bash
docker compose up -d postgres s3
cp .env.example .env               # defaults point at the containers above
uv run alembic -c backend/alembic.ini upgrade head
uv run uvicorn replay_api.app:app --reload --port 8000
uv run replay-worker
npm --prefix web install && npm --prefix web run dev
```

## Send your first trace

```python
import replay_sdk as replay
from openai import OpenAI

replay.init()                                # REPLAY_API_KEY, REPLAY_ENDPOINT
client = replay.wrap_openai(OpenAI())

@replay.tool()
def get_order(order_id: str) -> dict: ...

@replay.trace(tags=["support"], metadata={"route": "/support"})
def answer(question: str) -> str: ...
```

Or POST JSON to `/v1/ingest`, or point any OpenTelemetry exporter at `/v1/otlp/v1/traces`. The
dashboard's Quickstart page has copy-paste versions with your key filled in.

## Tests

```bash
uv run pytest backend packages -q            # ~250 tests; needs `docker compose up -d postgres`
uv run pytest packages/stats -m slow -q      # statistical simulations only
npm --prefix web run lint && npm --prefix web run build
```

The suite includes:
- **Tenant-isolation matrix:** attacks every API route across tenants. A meta-test fails if a new route
  has no case.
- **Row-level security tests:** run against raw SQL and the ORM, including one that shows the ORM guard
  holds with RLS disabled.
- **Statistics:** known-answer and simulation tests, covering the false-SAFE rate at the margin and
  interval coverage.
- **End-to-end:** capture → replay → judge → verdict, and the real CLI gating a PR in a git repo.
- **SDK:** never blocks and never raises.

## Repository layout

| Path | What |
|---|---|
| `backend/` | FastAPI API, worker, replay engine, Alembic migrations (`replay-api`) |
| `packages/stats/` | Statistics library: intervals, tests, agreement, verdicts (`replay-stats`) |
| `packages/sdk-python/` | Python SDK, zero dependencies (`replay-sdk`) |
| `packages/cli/` | `replay` CLI / CI gate, standard library only (`replay-cli`) |
| `action/` | GitHub Action wrapping the CLI |
| `web/` | Next.js dashboard |
| `spike/divergence/` | Phase 0.5 research spike and report |
| `deploy/` | Render Blueprint, self-host stack (Caddy), backups, smoke test, deploy hooks |
| `docs/` | Architecture, replay semantics, statistics, operations, security, launch checklist |
| `DECISIONS.md` | Architecture decision log |

## Documentation

- [Architecture](docs/architecture.md)
- [Replay and divergence](docs/replay.md)
- [Statistics and verdicts](docs/statistics.md)
- [Operations](docs/operations.md): deploys, migrations, backups and restore, monitoring
- [Security](docs/security.md): controls and review checklist
- [Self-hosting](docs/self-hosting.md)
- [CI gate / GitHub Action](docs/github-action.md)
- [API overview](docs/api.md)
- [Launch checklist](docs/launch-checklist.md)
