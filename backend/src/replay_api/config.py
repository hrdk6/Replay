"""Runtime configuration, loaded from environment variables (see .env.example)."""

from __future__ import annotations

import base64
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]

# Development-only defaults. Production-like environments refuse to start with them.
DEV_SESSION_SECRET = "dev-only-session-secret-change-me-0123456789"  # noqa: S105 - refused outside dev
DEV_ENCRYPTION_KEY = "ZGV2LW9ubHktZW5jcnlwdGlvbi1rZXktMzJieXRlcyE="


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    env: Environment = "development"
    release: str = "dev"

    # --- Database -----------------------------------------------------------------
    # Must connect as a NON-superuser role: superusers bypass row-level security.
    database_url: str = "postgresql+asyncpg://replay:replay@localhost:5432/replay"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_statement_timeout_ms: int = 30_000

    # --- Public URLs ----------------------------------------------------------------
    public_app_url: str = "http://localhost:3000"
    public_api_url: str = "http://localhost:8000"

    # --- Secrets ----------------------------------------------------------------------
    # HMAC key for CSRF tokens, OAuth state and labeling tokens. >= 32 bytes.
    session_secret: SecretStr = SecretStr(DEV_SESSION_SECRET)
    # Key-encryption key for provider keys (envelope encryption), urlsafe base64 of 32 bytes.
    # Generate with: python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
    encryption_key: SecretStr = SecretStr(DEV_ENCRYPTION_KEY)
    # Optional previous KEK, accepted for decryption during key rotation.
    encryption_key_previous: SecretStr | None = None

    # --- Auth -------------------------------------------------------------------------
    github_client_id: str | None = None
    github_client_secret: SecretStr | None = None
    signup_mode: Literal["open", "allowlist"] = "allowlist"
    signup_allowlist: str = ""  # comma-separated GitHub logins (case-insensitive)
    dev_login_enabled: bool = False
    session_ttl_hours: int = 24 * 14
    cookie_secure: bool | None = None  # default: True outside development/test

    # --- Object storage ---------------------------------------------------------------
    storage_backend: Literal["s3", "memory"] = "s3"
    s3_endpoint_url: str | None = "http://localhost:8333"
    s3_create_bucket: bool = False  # create the bucket on startup (dev/self-host convenience)
    s3_region: str = "us-east-1"
    s3_bucket: str = "replay-payloads"
    s3_access_key_id: SecretStr | None = SecretStr("replay")
    s3_secret_access_key: SecretStr | None = SecretStr("replay-dev-secret")
    inline_payload_max_bytes: int = 32 * 1024

    # --- Limits -----------------------------------------------------------------------
    max_request_bytes: int = 5 * 1024 * 1024
    max_spans_per_batch: int = 1000
    max_json_depth: int = 64
    rate_limit_ingest_per_minute: int = 1200
    rate_limit_api_per_minute: int = 600
    rate_limit_auth_per_minute: int = 30

    # --- Default org quotas (overridable per org) -------------------------------------
    default_quota_traces_per_day: int = 50_000
    default_quota_replay_runs_per_day: int = 5_000
    default_monthly_budget_usd: float = 50.0
    max_experiment_items: int = 5_000
    max_experiment_repeats: int = 10

    # --- LLM providers ----------------------------------------------------------------
    enable_simulator_provider: bool = True
    provider_timeout_seconds: float = 120.0
    provider_max_retries: int = 2

    # --- Worker -----------------------------------------------------------------------
    worker_concurrency: int = 4
    worker_poll_interval_seconds: float = 1.0
    job_visibility_timeout_seconds: int = 900
    retention_sweep_interval_seconds: int = 3600
    llm_concurrency_per_job: int = 4

    # --- Observability ----------------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.0
    metrics_token: SecretStr | None = None

    # --- Derived helpers ----------------------------------------------------------------
    @property
    def is_production_like(self) -> bool:
        return self.env in ("staging", "production")

    @property
    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.env not in ("development", "test")

    @property
    def allowlist(self) -> set[str]:
        return {x.strip().lower() for x in self.signup_allowlist.split(",") if x.strip()}

    @property
    def encryption_key_bytes(self) -> bytes:
        return _decode_key(self.encryption_key.get_secret_value())

    @property
    def previous_encryption_key_bytes(self) -> bytes | None:
        if self.encryption_key_previous is None:
            return None
        return _decode_key(self.encryption_key_previous.get_secret_value())

    @field_validator("database_url")
    @classmethod
    def _asyncpg_url(cls, v: str) -> str:
        """Accept the postgres:// URLs managed providers hand out; asyncpg wants ssl=, not sslmode=."""
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                v = "postgresql+asyncpg://" + v[len(prefix) :]
        return v.replace("sslmode=", "ssl=")

    @field_validator("public_app_url", "public_api_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @model_validator(mode="after")
    def _check_production_safety(self) -> Settings:
        secret = self.session_secret.get_secret_value()
        if len(secret.encode()) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 bytes")
        _decode_key(self.encryption_key.get_secret_value())
        if self.is_production_like:
            problems = []
            if self.dev_login_enabled:
                problems.append("DEV_LOGIN_ENABLED must be false")
            if secret == DEV_SESSION_SECRET:
                problems.append("SESSION_SECRET is the development default")
            if self.encryption_key.get_secret_value() == DEV_ENCRYPTION_KEY:
                problems.append("ENCRYPTION_KEY is the development default")
            if not self.github_client_id or not self.github_client_secret:
                problems.append("GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET are required")
            if not self.public_app_url.startswith("https://"):
                problems.append("PUBLIC_APP_URL must be https")
            if self.storage_backend == "memory":
                problems.append("STORAGE_BACKEND=memory is not durable")
            if problems:
                raise ValueError("unsafe production configuration: " + "; ".join(problems))
        return self


def _decode_key(value: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(value.encode())
    except Exception as exc:  # pragma: no cover - defensive
        raise ValueError("ENCRYPTION_KEY must be urlsafe base64") from exc
    if len(raw) != 32:
        raise ValueError("ENCRYPTION_KEY must decode to exactly 32 bytes")
    return raw


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(settings: Settings | None) -> None:
    """Replace the process-wide settings (tests and CLI tools)."""
    global _settings
    _settings = settings
