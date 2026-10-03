"""ORM models. Every tenant-owned table uses ``TenantMixin`` (adds ``org_id``)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Identity,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from replay_api.db.base import Base, TenantMixin, created_at, uuid_pk


def _json_default(kind: str) -> Any:
    return text("'[]'::jsonb") if kind == "list" else text("'{}'::jsonb")


# ---------------------------------------------------------------------------
# Identity: orgs, users, memberships, sessions
# ---------------------------------------------------------------------------


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    personal: Mapped[bool] = mapped_column(Boolean, default=False)
    quota_traces_per_day: Mapped[int]
    quota_replay_runs_per_day: Mapped[int]
    monthly_budget_usd: Mapped[Decimal]
    created_at: Mapped[datetime] = created_at()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    github_id: Mapped[int] = mapped_column(unique=True)
    github_login: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = created_at()
    last_login_at: Mapped[datetime | None]


class Membership(TenantMixin, Base):
    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # owner | admin | member
    created_at: Mapped[datetime] = created_at()

    user: Mapped[User] = relationship(lazy="joined")

    __table_args__ = (UniqueConstraint("org_id", "user_id"),)


class Invite(TenantMixin, Base):
    __tablename__ = "invites"

    id: Mapped[uuid.UUID] = uuid_pk()
    github_login: Mapped[str] = mapped_column(String(100))  # lower-cased
    role: Mapped[str] = mapped_column(String(20))
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at()
    accepted_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("org_id", "github_login"),
        Index("ix_invites_github_login", "github_login"),
    )


class Session(Base):
    """Dashboard login session. Token is stored as a SHA-256 hash only."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    active_org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orgs.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at()
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    user_agent: Mapped[str | None] = mapped_column(String(400))
    ip: Mapped[str | None] = mapped_column(String(64))


# ---------------------------------------------------------------------------
# Projects and keys
# ---------------------------------------------------------------------------


DEFAULT_REDACTION: dict[str, Any] = {
    "enabled": True,
    "builtin": ["email", "phone", "credit_card", "api_key", "ssn"],
    "custom": [],
}


class Project(TenantMixin, Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80))
    retention_days: Mapped[int] = mapped_column(default=30)
    redaction: Mapped[dict[str, Any]] = mapped_column(default=lambda: dict(DEFAULT_REDACTION))
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (UniqueConstraint("org_id", "slug"),)


class ApiKey(TenantMixin, Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at()
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


class ProviderKey(TenantMixin, Base):
    """Bring-your-own LLM key, envelope-encrypted (see security.crypto)."""

    __tablename__ = "provider_keys"

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(40))  # openai | anthropic | openai_compatible
    name: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str | None] = mapped_column(String(500))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary)
    last4: Mapped[str] = mapped_column(String(8))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at()
    revoked_at: Mapped[datetime | None]


# ---------------------------------------------------------------------------
# Traces and spans
# ---------------------------------------------------------------------------


class Trace(TenantMixin, Base):
    __tablename__ = "traces"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str | None] = mapped_column(String(300))
    start_time: Mapped[datetime | None]
    end_time: Mapped[datetime | None]
    duration_ms: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(10), default="ok")
    span_count: Mapped[int] = mapped_column(default=0)
    error_count: Mapped[int] = mapped_column(default=0)
    llm_call_count: Mapped[int] = mapped_column(default=0)
    tool_call_count: Mapped[int] = mapped_column(default=0)
    tags: Mapped[list[Any]] = mapped_column(server_default=_json_default("list"), default=list)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", server_default=_json_default("dict"), default=dict)
    model: Mapped[str | None] = mapped_column(String(200))
    input_preview: Mapped[str | None] = mapped_column(String(600))
    output_preview: Mapped[str | None] = mapped_column(String(600))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[Decimal | None]
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint("project_id", "external_id"),
        Index("ix_traces_project_start", "project_id", text("start_time DESC")),
        Index("ix_traces_tags", "tags", postgresql_using="gin"),
        Index("ix_traces_project_created", "project_id", "created_at"),
    )


class Span(TenantMixin, Base):
    __tablename__ = "spans"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    trace_pk: Mapped[uuid.UUID] = mapped_column(ForeignKey("traces.id", ondelete="CASCADE"))
    span_id: Mapped[str] = mapped_column(String(64))
    parent_span_id: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(10))
    status_message: Mapped[str | None] = mapped_column(Text)
    start_time: Mapped[datetime]
    end_time: Mapped[datetime | None]
    duration_ms: Mapped[float | None] = mapped_column(Float)
    attributes: Mapped[dict[str, Any]] = mapped_column(server_default=_json_default("dict"), default=dict)
    input: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    input_ref: Mapped[str | None] = mapped_column(String(500))
    output_ref: Mapped[str | None] = mapped_column(String(500))
    input_bytes: Mapped[int] = mapped_column(default=0)
    output_bytes: Mapped[int] = mapped_column(default=0)
    model: Mapped[str | None] = mapped_column(String(200))
    provider: Mapped[str | None] = mapped_column(String(50))
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cost_usd: Mapped[Decimal | None]
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint("trace_pk", "span_id"),
        Index("ix_spans_trace_start", "trace_pk", "start_time"),
    )


# ---------------------------------------------------------------------------
# Datasets, candidates, judges, experiments
# ---------------------------------------------------------------------------


class Dataset(TenantMixin, Base):
    __tablename__ = "datasets"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    filters: Mapped[dict[str, Any]] = mapped_column(default=dict)
    sample_size: Mapped[int | None]
    seed: Mapped[int] = mapped_column(default=0)
    item_count: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(20), default="building", server_default="building")
    build_info: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    frozen_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at()


class DatasetItem(TenantMixin, Base):
    """A frozen copy of one trace's recording. Independent of trace retention."""

    __tablename__ = "dataset_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"))
    position: Mapped[int]
    source_trace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("traces.id", ondelete="SET NULL"))
    source_external_id: Mapped[str | None] = mapped_column(String(128))
    recording: Mapped[dict[str, Any] | None]
    recording_ref: Mapped[str | None] = mapped_column(String(500))
    slices: Mapped[list[Any]] = mapped_column(default=list)
    input_preview: Mapped[str | None] = mapped_column(String(600))
    llm_steps: Mapped[int] = mapped_column(default=0)
    tool_events: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint("dataset_id", "position"),
        Index("ix_dataset_items_dataset", "dataset_id", "position"),
    )


class Candidate(TenantMixin, Base):
    __tablename__ = "candidates"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    source: Mapped[str] = mapped_column(String(20), default="dashboard")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at()


class Judge(TenantMixin, Base):
    """A versioned judge. Edits create a new row with the same ``family_id``."""

    __tablename__ = "judges"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    version: Mapped[int]
    name: Mapped[str] = mapped_column(String(200))
    mode: Mapped[str] = mapped_column(String(20))  # absolute | pairwise
    scale: Mapped[str] = mapped_column(String(20))  # binary | likert5 (absolute only)
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(200))
    rubric: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    include_reference: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (UniqueConstraint("family_id", "version"),)


class Experiment(TenantMixin, Base):
    __tablename__ = "experiments"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"))
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"))
    baseline_candidate_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"))
    baseline_mode: Mapped[str] = mapped_column(String(20), default="replay")  # replay | recorded
    judge_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("judges.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(20))  # single_turn | full_agent
    repeats: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    budget_usd: Mapped[Decimal]
    spent_usd: Mapped[Decimal] = mapped_column(default=Decimal(0))
    estimated_cost_usd: Mapped[Decimal | None]
    total_items: Mapped[int] = mapped_column(default=0)
    done_items: Mapped[int] = mapped_column(default=0)
    verdict: Mapped[str | None] = mapped_column(String(20))
    report: Mapped[dict[str, Any] | None]
    error: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="dashboard")
    ci: Mapped[dict[str, Any] | None]
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at()
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    __table_args__ = (Index("ix_experiments_project_created", "project_id", "created_at"),)


class ExperimentRun(TenantMixin, Base):
    __tablename__ = "experiment_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"))
    dataset_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dataset_items.id", ondelete="CASCADE"))
    arm: Mapped[str] = mapped_column(String(20))  # baseline | candidate
    repeat_index: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    output: Mapped[dict[str, Any] | None]
    steps: Mapped[list[Any]] = mapped_column(default=list)
    divergence: Mapped[dict[str, Any] | None]
    cost_usd: Mapped[Decimal] = mapped_column(default=Decimal(0))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("experiment_id", "dataset_item_id", "arm", "repeat_index"),
        Index("ix_runs_experiment_status", "experiment_id", "status"),
    )


class JudgeResult(TenantMixin, Base):
    __tablename__ = "judge_results"

    id: Mapped[uuid.UUID] = uuid_pk()
    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"))
    dataset_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dataset_items.id", ondelete="CASCADE"))
    judge_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("judges.id", ondelete="CASCADE"), index=True)
    repeat_index: Mapped[int] = mapped_column(default=0)
    kind: Mapped[str] = mapped_column(String(20))  # absolute | pairwise
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("experiment_runs.id", ondelete="CASCADE"))
    arm: Mapped[str | None] = mapped_column(String(20))
    order: Mapped[str | None] = mapped_column(String(4))  # pairwise: "bc" (baseline shown first) | "cb"
    raw_choice: Mapped[str | None] = mapped_column(String(8))  # pairwise: A | B | tie
    winner: Mapped[str | None] = mapped_column(String(20))  # baseline | candidate | tie
    score: Mapped[float | None] = mapped_column(Float)  # absolute: raw rubric score
    normalized: Mapped[float | None] = mapped_column(Float)  # absolute: 0-100
    reasoning: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[Decimal] = mapped_column(default=Decimal(0))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (Index("ix_judge_results_experiment", "experiment_id", "dataset_item_id"),)


class HumanLabel(TenantMixin, Base):
    __tablename__ = "human_labels"

    id: Mapped[uuid.UUID] = uuid_pk()
    judge_result_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("judge_results.id", ondelete="CASCADE"))
    judge_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("judges.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    label: Mapped[str] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (UniqueConstraint("judge_result_id", "user_id"),)


class JudgeCalibration(TenantMixin, Base):
    __tablename__ = "judge_calibrations"

    id: Mapped[uuid.UUID] = uuid_pk()
    judge_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("judges.id", ondelete="CASCADE"), index=True)
    n: Mapped[int]
    kappa: Mapped[float | None] = mapped_column(Float)
    kappa_low: Mapped[float | None] = mapped_column(Float)
    kappa_high: Mapped[float | None] = mapped_column(Float)
    raw_agreement: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30))
    report: Mapped[dict[str, Any]] = mapped_column(default=dict)
    computed_at: Mapped[datetime] = created_at()


# ---------------------------------------------------------------------------
# Operations: audit, usage, jobs
# ---------------------------------------------------------------------------


class AuditLog(TenantMixin, Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_api_key_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_keys.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(80))
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(80))
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (Index("ix_audit_org_created", "org_id", "created_at"),)


class UsageCounter(TenantMixin, Base):
    __tablename__ = "usage_counters"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    day: Mapped[date] = mapped_column(Date)
    metric: Mapped[str] = mapped_column(String(40))
    value: Mapped[Decimal] = mapped_column(default=Decimal(0))

    __table_args__ = (UniqueConstraint("org_id", "day", "metric"),)


class Job(Base):
    """Postgres-backed job queue (claimed with FOR UPDATE SKIP LOCKED)."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    priority: Mapped[int] = mapped_column(default=100)
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=5)
    run_after: Mapped[datetime] = created_at()
    locked_at: Mapped[datetime | None]
    locked_by: Mapped[str | None] = mapped_column(String(120))
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = created_at()
    finished_at: Mapped[datetime | None]

    __table_args__ = (
        Index(
            "ix_jobs_claim",
            "priority",
            "run_after",
            postgresql_where=text("status = 'queued'"),
        ),
    )


class CronState(Base):
    __tablename__ = "cron_state"

    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    last_run_at: Mapped[datetime | None]


TENANT_MODELS: tuple[type[Base], ...] = tuple(
    m.class_ for m in Base.registry.mappers if getattr(m.class_, "__tenant__", False)
)
