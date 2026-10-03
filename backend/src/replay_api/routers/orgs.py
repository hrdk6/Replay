"""Organization settings: members, invites, usage, audit log, export, deletion."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from replay_api.db import models as m
from replay_api.db.models import AuditLog, Invite, Membership, Org, UsageCounter, User
from replay_api.db.session import system_session, tenant_session
from replay_api.deps import AdminUser, CurrentUser, OwnerUser, UserDB, get_org
from replay_api.errors import ApiError, bad_request, conflict, forbidden, not_found
from replay_api.services import audit, usage
from replay_api.services.storage import get_store
from replay_api.worker.queue import enqueue

router = APIRouter(prefix="/api", tags=["orgs"])


def org_out(o: Org) -> dict[str, Any]:
    return {
        "id": str(o.id),
        "name": o.name,
        "slug": o.slug,
        "personal": o.personal,
        "quota_traces_per_day": o.quota_traces_per_day,
        "quota_replay_runs_per_day": o.quota_replay_runs_per_day,
        "monthly_budget_usd": float(o.monthly_budget_usd),
        "created_at": o.created_at.isoformat() if o.created_at else None,
    }


@router.get("/orgs/current")
async def current_org(principal: CurrentUser, db: UserDB) -> dict[str, Any]:
    org = await get_org(db, principal.org_id)
    return {
        **org_out(org),
        "role": principal.role,
        "usage": {
            "traces_today": float(await usage.get_value(db, org.id, usage.METRIC_TRACES)),
            "replay_runs_today": float(await usage.get_value(db, org.id, usage.METRIC_REPLAY_RUNS)),
            "llm_spend_this_month_usd": float(await usage.month_spend(db, org.id)),
        },
    }


class OrgPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    monthly_budget_usd: float | None = Field(default=None, ge=0, le=100_000)


@router.patch("/orgs/current")
async def update_org(principal: AdminUser, db: UserDB, body: OrgPatch) -> dict[str, Any]:
    org = await get_org(db, principal.org_id)
    meta: dict[str, Any] = {}
    if body.name is not None:
        org.name = body.name
        meta["name"] = body.name
    if body.monthly_budget_usd is not None:
        org.monthly_budget_usd = Decimal(str(body.monthly_budget_usd))
        meta["monthly_budget_usd"] = body.monthly_budget_usd
    await audit.record(
        db,
        org.id,
        "org.update",
        user_id=principal.user_id,
        target_type="org",
        target_id=org.id,
        metadata=meta,
        ip=principal.ip,
    )
    return org_out(org)


# --- Members & invites ---------------------------------------------------------------


@router.get("/orgs/current/members")
async def list_members(principal: CurrentUser, db: UserDB) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(Membership, User).join(User, User.id == Membership.user_id).order_by(Membership.created_at)
        )
    ).all()
    invites = (await db.execute(select(Invite).where(Invite.accepted_at.is_(None)))).scalars().all()
    return {
        "members": [
            {
                "user_id": str(u.id),
                "github_login": u.github_login,
                "name": u.name,
                "avatar_url": u.avatar_url,
                "role": mem.role,
                "joined_at": mem.created_at.isoformat() if mem.created_at else None,
            }
            for mem, u in rows
        ],
        "invites": [
            {
                "id": str(i.id),
                "github_login": i.github_login,
                "role": i.role,
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
            for i in invites
        ],
    }


class InviteIn(BaseModel):
    github_login: str = Field(min_length=1, max_length=39, pattern=r"^[A-Za-z0-9-]+$")
    role: Literal["member", "admin"] = "member"


@router.post("/orgs/current/invites", status_code=201)
async def create_invite(principal: AdminUser, db: UserDB, body: InviteIn) -> dict[str, Any]:
    login = body.github_login.lower()
    already = await db.scalar(
        select(Membership.id).join(User, User.id == Membership.user_id).where(func.lower(User.github_login) == login)
    )
    if already is not None:
        raise conflict("that user is already a member")
    if await db.scalar(select(Invite.id).where(Invite.github_login == login, Invite.accepted_at.is_(None))):
        raise conflict("an invite is already pending for that user")
    await db.execute(delete(Invite).where(Invite.github_login == login))
    invite = Invite(org_id=principal.org_id, github_login=login, role=body.role, invited_by=principal.user_id)
    db.add(invite)
    await db.flush()
    await audit.record(
        db,
        principal.org_id,
        "member.invite",
        user_id=principal.user_id,
        target_type="invite",
        target_id=invite.id,
        metadata={"github_login": login, "role": body.role},
        ip=principal.ip,
    )
    return {"id": str(invite.id), "github_login": login, "role": body.role}


@router.delete("/orgs/current/invites/{invite_id}")
async def delete_invite(invite_id: uuid.UUID, principal: AdminUser, db: UserDB) -> dict[str, Any]:
    invite = await db.scalar(select(Invite).where(Invite.id == invite_id))
    if invite is None:
        raise not_found("invite")
    await db.delete(invite)
    await audit.record(
        db,
        principal.org_id,
        "member.invite_revoke",
        user_id=principal.user_id,
        target_type="invite",
        target_id=invite_id,
        ip=principal.ip,
    )
    return {"ok": True}


async def _owner_count(db: Any) -> int:
    return int(await db.scalar(select(func.count()).select_from(Membership).where(Membership.role == "owner")) or 0)


class RoleIn(BaseModel):
    role: Literal["member", "admin", "owner"]


@router.patch("/orgs/current/members/{user_id}")
async def change_role(user_id: uuid.UUID, principal: OwnerUser, db: UserDB, body: RoleIn) -> dict[str, Any]:
    mem = await db.scalar(select(Membership).where(Membership.user_id == user_id))
    if mem is None:
        raise not_found("member")
    if mem.role == "owner" and body.role != "owner" and await _owner_count(db) <= 1:
        raise conflict("an organization needs at least one owner")
    mem.role = body.role
    await audit.record(
        db,
        principal.org_id,
        "member.role_change",
        user_id=principal.user_id,
        target_type="user",
        target_id=user_id,
        metadata={"role": body.role},
        ip=principal.ip,
    )
    return {"ok": True}


@router.delete("/orgs/current/members/{user_id}")
async def remove_member(user_id: uuid.UUID, principal: CurrentUser, db: UserDB) -> dict[str, Any]:
    mem = await db.scalar(select(Membership).where(Membership.user_id == user_id))
    if mem is None:
        raise not_found("member")
    leaving_self = user_id == principal.user_id
    if not leaving_self and not principal.has_role("admin"):
        raise forbidden("requires admin role")
    if mem.role == "owner" and not leaving_self and not principal.has_role("owner"):
        raise forbidden("only owners can remove owners")
    if mem.role == "owner" and await _owner_count(db) <= 1:
        raise conflict("an organization needs at least one owner")
    await db.delete(mem)
    await audit.record(
        db,
        principal.org_id,
        "member.remove",
        user_id=principal.user_id,
        target_type="user",
        target_id=user_id,
        ip=principal.ip,
    )
    return {"ok": True}


# --- Usage & audit ------------------------------------------------------------------


@router.get("/orgs/current/usage")
async def usage_history(
    principal: CurrentUser, db: UserDB, days: int = Query(default=30, ge=1, le=366)
) -> dict[str, Any]:
    since = datetime.now(UTC).date() - timedelta(days=days)
    rows = (
        (await db.execute(select(UsageCounter).where(UsageCounter.day >= since).order_by(UsageCounter.day)))
        .scalars()
        .all()
    )
    series: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        series.setdefault(r.metric, []).append({"day": r.day.isoformat(), "value": float(r.value)})
    return {"since": since.isoformat(), "series": series}


@router.get("/orgs/current/audit")
async def audit_log(
    principal: AdminUser, db: UserDB, before: int | None = None, limit: int = Query(default=50, ge=1, le=200)
) -> dict[str, Any]:
    stmt = select(AuditLog, User.github_login).outerjoin(User, User.id == AuditLog.actor_user_id)
    if before is not None:
        stmt = stmt.where(AuditLog.id < before)
    rows = (await db.execute(stmt.order_by(AuditLog.id.desc()).limit(limit))).all()
    return {
        "entries": [
            {
                "id": a.id,
                "action": a.action,
                "actor": login,
                "actor_api_key_id": str(a.actor_api_key_id) if a.actor_api_key_id else None,
                "target_type": a.target_type,
                "target_id": a.target_id,
                "metadata": a.meta,
                "ip": a.ip,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a, login in rows
        ],
        "next_before": rows[-1][0].id if len(rows) == limit else None,
    }


# --- Export & deletion --------------------------------------------------------------

EXPORT_TABLES: list[tuple[str, Any]] = [
    ("project", m.Project),
    ("api_key", m.ApiKey),
    ("trace", m.Trace),
    ("span", m.Span),
    ("dataset", m.Dataset),
    ("dataset_item", m.DatasetItem),
    ("candidate", m.Candidate),
    ("judge", m.Judge),
    ("experiment", m.Experiment),
    ("experiment_run", m.ExperimentRun),
    ("judge_result", m.JudgeResult),
    ("human_label", m.HumanLabel),
    ("judge_calibration", m.JudgeCalibration),
    ("audit_log", m.AuditLog),
    ("usage_counter", m.UsageCounter),
    ("provider_key", m.ProviderKey),
]
EXPORT_EXCLUDE = {"key_hash", "ciphertext", "wrapped_dek", "token_hash"}


def _row_dict(obj: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in obj.__table__.columns:
        if col.name in EXPORT_EXCLUDE:
            continue
        v = getattr(obj, col.key)
        if isinstance(v, datetime | date):
            v = v.isoformat()
        elif isinstance(v, uuid.UUID | Decimal):
            v = str(v)
        elif isinstance(v, bytes):
            continue
        out[col.name] = v
    return out


async def _export_stream(org_id: uuid.UUID) -> AsyncIterator[bytes]:
    store = get_store()
    async with tenant_session(org_id) as db:
        org = await db.scalar(select(Org).where(Org.id == org_id))
        yield (json.dumps({"type": "org", "data": _row_dict(org)}) + "\n").encode()
        for name, model in EXPORT_TABLES:
            pk = model.__table__.primary_key.columns.values()[0]
            last = None
            while True:
                stmt = select(model).order_by(pk).limit(500)
                if last is not None:
                    stmt = stmt.where(pk > last)
                rows = (await db.execute(stmt)).scalars().all()
                if not rows:
                    break
                for r in rows:
                    data = _row_dict(r)
                    for ref_field in ("input_ref", "output_ref", "recording_ref"):
                        ref = data.get(ref_field)
                        if ref:
                            try:
                                data[ref_field.removesuffix("_ref") + "_payload"] = json.loads(await store.get(ref))
                            except FileNotFoundError:
                                data[ref_field.removesuffix("_ref") + "_payload"] = None
                    yield (json.dumps({"type": name, "data": data}, default=str) + "\n").encode()
                last = getattr(rows[-1], pk.key)


@router.get("/orgs/current/export")
async def export_org(principal: AdminUser, db: UserDB) -> StreamingResponse:
    await audit.record(db, principal.org_id, "org.export", user_id=principal.user_id, ip=principal.ip)
    filename = f"replay-export-{principal.org_id}-{datetime.now(UTC):%Y%m%d}.ndjson"
    return StreamingResponse(
        _export_stream(principal.org_id),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class DeleteOrgIn(BaseModel):
    confirm_slug: str


@router.delete("/orgs/current")
async def delete_org(principal: OwnerUser, db: UserDB, body: DeleteOrgIn) -> dict[str, Any]:
    org = await get_org(db, principal.org_id)
    if body.confirm_slug != org.slug:
        raise bad_request("confirmation does not match the organization slug")
    # Revoke access immediately; the worker deletes data and stored payloads.
    await db.execute(delete(Membership))
    await db.execute(delete(Invite))
    await audit.record(
        db,
        org.id,
        "org.delete_requested",
        user_id=principal.user_id,
        target_type="org",
        target_id=org.id,
        ip=principal.ip,
    )
    await enqueue(
        db, "org.delete", {"org_id": str(org.id)}, org_id=org.id, priority=10, dedupe_key=f"org.delete:{org.id}"
    )
    return {"ok": True, "status": "deletion_scheduled"}


@router.delete("/me")
async def delete_account(principal: CurrentUser) -> dict[str, Any]:
    """Delete the signed-in user. Sole-member orgs are deleted with them."""
    async with system_session() as db:
        memberships = (
            (await db.execute(select(Membership).where(Membership.user_id == principal.user_id))).scalars().all()
        )
        to_delete: list[uuid.UUID] = []
        for mem in memberships:
            others = await db.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.org_id == mem.org_id, Membership.user_id != principal.user_id)
            )
            if others:
                owners = await db.scalar(
                    select(func.count())
                    .select_from(Membership)
                    .where(
                        Membership.org_id == mem.org_id,
                        Membership.role == "owner",
                        Membership.user_id != principal.user_id,
                    )
                )
                if mem.role == "owner" and not owners:
                    raise ApiError(
                        409, "conflict", "transfer ownership of shared organizations before deleting your account"
                    )
            else:
                to_delete.append(mem.org_id)
        for org_id in to_delete:
            await audit.record(
                db, org_id, "org.delete_requested", user_id=principal.user_id, target_type="org", target_id=org_id
            )
            await enqueue(
                db, "org.delete", {"org_id": str(org_id)}, org_id=org_id, priority=10, dedupe_key=f"org.delete:{org_id}"
            )
        await db.execute(delete(Membership).where(Membership.user_id == principal.user_id))
        await db.execute(delete(User).where(User.id == principal.user_id))
    return {"ok": True}
