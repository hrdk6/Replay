"""Projects, per-project settings (retention, redaction) and API keys."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from replay_api.db.models import ApiKey, Project, Trace
from replay_api.deps import AdminUser, CurrentUser, UserDB, get_project
from replay_api.errors import bad_request, conflict, not_found
from replay_api.security.tokens import generate_api_key
from replay_api.services import audit
from replay_api.services.accounts import slugify
from replay_api.services.redaction import BUILTIN_ORDER, RedactionConfigError, validate_config

router = APIRouter(prefix="/api/projects", tags=["projects"])


def project_out(p: Project, trace_count: int | None = None) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "name": p.name,
        "slug": p.slug,
        "retention_days": p.retention_days,
        "redaction": p.redaction,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "trace_count": trace_count,
    }


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    redaction: dict[str, Any] | None = None


@router.get("")
async def list_projects(principal: CurrentUser, db: UserDB) -> dict[str, Any]:
    rows = (await db.execute(select(Project).order_by(Project.created_at))).scalars().all()
    counts = dict((await db.execute(select(Trace.project_id, func.count()).group_by(Trace.project_id))).all())
    return {"projects": [project_out(p, counts.get(p.id, 0)) for p in rows]}


@router.post("", status_code=201)
async def create_project(principal: AdminUser, db: UserDB, body: ProjectIn) -> dict[str, Any]:
    slug = slugify(body.name)
    if await db.scalar(select(Project.id).where(Project.slug == slug)):
        slug = f"{slug[:32]}-{uuid.uuid4().hex[:6]}"
    project = Project(org_id=principal.org_id, name=body.name, slug=slug)
    db.add(project)
    await db.flush()
    await audit.record(
        db,
        principal.org_id,
        "project.create",
        user_id=principal.user_id,
        target_type="project",
        target_id=project.id,
        ip=principal.ip,
    )
    await db.refresh(project)
    return project_out(project, 0)


@router.get("/redaction-rules")
async def redaction_rules(principal: CurrentUser) -> dict[str, Any]:
    return {"builtin": BUILTIN_ORDER}


@router.get("/{project_id}")
async def read_project(project_id: uuid.UUID, principal: CurrentUser, db: UserDB) -> dict[str, Any]:
    project = await get_project(db, project_id)
    count = await db.scalar(select(func.count()).select_from(Trace).where(Trace.project_id == project.id))
    return project_out(project, count or 0)


@router.patch("/{project_id}")
async def update_project(project_id: uuid.UUID, principal: AdminUser, db: UserDB, body: ProjectPatch) -> dict[str, Any]:
    project = await get_project(db, project_id)
    changes: dict[str, Any] = {}
    if body.name is not None:
        project.name = body.name
        changes["name"] = body.name
    if body.retention_days is not None:
        project.retention_days = body.retention_days
        changes["retention_days"] = body.retention_days
    if body.redaction is not None:
        try:
            project.redaction = validate_config(body.redaction)
        except RedactionConfigError as exc:
            raise bad_request(str(exc)) from exc
        changes["redaction"] = {
            "enabled": project.redaction["enabled"],
            "builtin": project.redaction["builtin"],
            "custom_rules": len(project.redaction["custom"]),
        }
    await audit.record(
        db,
        principal.org_id,
        "project.update",
        user_id=principal.user_id,
        target_type="project",
        target_id=project.id,
        metadata=changes,
        ip=principal.ip,
    )
    return project_out(project)


@router.delete("/{project_id}")
async def delete_project(project_id: uuid.UUID, principal: AdminUser, db: UserDB) -> dict[str, Any]:
    project = await get_project(db, project_id)
    remaining = await db.scalar(select(func.count()).select_from(Project))
    if (remaining or 0) <= 1:
        raise conflict("cannot delete the last project in an organization")
    await db.delete(project)
    await audit.record(
        db,
        principal.org_id,
        "project.delete",
        user_id=principal.user_id,
        target_type="project",
        target_id=project_id,
        ip=principal.ip,
    )
    # Payload objects are removed by the retention sweep's orphan pass.
    return {"ok": True}


# --- API keys ---------------------------------------------------------------------


def key_out(k: ApiKey) -> dict[str, Any]:
    return {
        "id": str(k.id),
        "name": k.name,
        "prefix": f"rk_{k.prefix}_",
        "project_id": str(k.project_id),
        "created_at": k.created_at.isoformat() if k.created_at else None,
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
    }


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


@router.get("/{project_id}/api-keys")
async def list_keys(project_id: uuid.UUID, principal: CurrentUser, db: UserDB) -> dict[str, Any]:
    await get_project(db, project_id)
    keys = (
        (await db.execute(select(ApiKey).where(ApiKey.project_id == project_id).order_by(ApiKey.created_at.desc())))
        .scalars()
        .all()
    )
    return {"api_keys": [key_out(k) for k in keys]}


@router.post("/{project_id}/api-keys", status_code=201)
async def create_key(project_id: uuid.UUID, principal: AdminUser, db: UserDB, body: ApiKeyIn) -> dict[str, Any]:
    await get_project(db, project_id)
    new = generate_api_key()
    key = ApiKey(
        org_id=principal.org_id,
        project_id=project_id,
        name=body.name,
        prefix=new.prefix,
        key_hash=new.key_hash,
        created_by=principal.user_id,
    )
    db.add(key)
    await db.flush()
    await db.refresh(key)
    await audit.record(
        db,
        principal.org_id,
        "api_key.create",
        user_id=principal.user_id,
        target_type="api_key",
        target_id=key.id,
        metadata={"name": body.name, "prefix": new.prefix},
        ip=principal.ip,
    )
    # The full key is returned exactly once.
    return {**key_out(key), "key": new.full_key}


@router.delete("/{project_id}/api-keys/{key_id}")
async def revoke_key(project_id: uuid.UUID, key_id: uuid.UUID, principal: AdminUser, db: UserDB) -> dict[str, Any]:
    key = await db.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.project_id == project_id))
    if key is None:
        raise not_found("API key")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        await audit.record(
            db,
            principal.org_id,
            "api_key.revoke",
            user_id=principal.user_id,
            target_type="api_key",
            target_id=key.id,
            metadata={"prefix": key.prefix},
            ip=principal.ip,
        )
    return key_out(key)
