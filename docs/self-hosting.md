# Self-hosting

One VM (2 vCPU / 4 GB RAM is plenty to start) with Docker, ports 80 and 443 open, and two DNS records:
`app.<domain>` and `api.<domain>`, both pointing at the VM.

```bash
git clone <your fork> replay && cd replay/deploy/selfhost
cp .env.example .env            # fill in domains and secrets (instructions inside)
# set the same S3 secret in s3.json, or point S3_* at R2/S3
docker compose up -d
docker compose logs -f migrate api
python3 ../smoke_test.py --api https://api.<domain> --app https://app.<domain>
```

The stack:
- **Caddy:** automatic HTTPS; `app.` routes to the dashboard and `api.` to the API.
- **Postgres 17:** the init script creates the non-superuser app role.
- **Nightly `pg_dump` backups:** 14 days kept; copy them off the box.
- **SeaweedFS:** S3-compatible storage.
- **Services:** a migrations one-shot, then the API, worker and dashboard.

Images default to `ghcr.io/OWNER/replay-{api,web}:production`. Set `REPLAY_API_IMAGE` and
`REPLAY_WEB_IMAGE` to pin a SHA, or build locally with `docker build -f backend/Dockerfile .` and
`docker build -f web/Dockerfile .` from the repository root.

**Upgrades:**

```bash
docker compose pull && docker compose up -d
```

Migrations run first, and the API waits for them.

**GitHub OAuth app:**
- Homepage: `https://app.<domain>`
- Callback: `https://app.<domain>/api/auth/github/callback`
