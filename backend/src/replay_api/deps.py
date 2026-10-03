"""Request authentication and tenant-scoped database dependencies.

* Dashboard requests: ``replay_session`` cookie (httpOnly, SameSite=Lax) plus a
  CSRF token header on unsafe methods, plus an Origin check.
* SDK/CI requests: ``Authorization: Bearer rk_...`` API key scoped to one project.

Both resolve to a principal carrying ``org_id``; every DB session handed to a
route is a ``tenant_session`` for that org.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from replay_api.config import get_settings
from replay_api.db.models import ApiKey, Membership, Org, Project, User
from replay_api.db.models import Session as DbSession
from replay_api.db.session import system_session, tenant_session
from replay_api.errors import ApiError, forbidden, not_found
from replay_api.security.ratelimit import limiter
from replay_api.security.tokens import constant_time_equal, hmac_hex, parse_api_key, sha256_hex

SESSION_COOKIE = "replay_session"
CSRF_COOKIE = "replay_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ROLE_RANK = {"member": 1, "admin": 2, "owner": 3}


def client_ip(request: Request) -> str:
    # Trust X-Forwarded-For only for the first hop set by our proxy/PaaS.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def csrf_token_for(session_id: uuid.UUID) -> str:
    key = get_settings().session_secret.get_secret_value().encode()
    return hmac_hex(key, f"csrf:{session_id}")


def _rate_limit(key: str, per_minute: int) -> None:
    wait = limiter.hit(key, per_minute)
    if wait:
        raise ApiError(429, "rate_limited", "too many requests", headers={"Retry-After": str(int(wait) + 1)})


@dataclass(frozen=True)
class UserPrincipal:
    user_id: uuid.UUID
    github_login: str
    session_id: uuid.UUID
    org_id: uuid.UUID
    role: str
    ip: str

    def has_role(self, role: str) -> bool:
        return ROLE_RANK.get(self.role, 0) >= ROLE_RANK[role]


@dataclass(frozen=True)
class ApiKeyPrincipal:
    org_id: uuid.UUID
    project_id: uuid.UUID
    api_key_id: uuid.UUID
    ip: str


def _check_origin(request: Request) -> None:
    allowed = get_settings().public_app_url
    origin = request.headers.get("origin")
    if origin is not None:
        if origin.rstrip("/") != allowed:
            raise ApiError(403, "csrf_failed", "cross-origin request rejected")
        return
    referer = request.headers.get("referer")
    if referer is not None and not (referer == allowed or referer.startswith(allowed + "/")):
        raise ApiError(403, "csrf_failed", "cross-origin request rejected")


async def require_user(request: Request) -> UserPrincipal:
    settings = get_settings()
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > 200:
        raise ApiError(401, "unauthorized", "sign in required")
    now = datetime.now(UTC)
    async with system_session() as db:
        row = (
            await db.execute(
                select(DbSession, User)
                .join(User, User.id == DbSession.user_id)
                .where(
                    DbSession.token_hash == sha256_hex(token),
                    DbSession.revoked_at.is_(None),
                    DbSession.expires_at > now,
                )
            )
        ).first()
        if row is None:
            raise ApiError(401, "unauthorized", "session expired")
        sess, user = row
        memberships = (
            (await db.execute(select(Membership).where(Membership.user_id == user.id).order_by(Membership.created_at)))
            .scalars()
            .all()
        )
        if not memberships:
            raise ApiError(403, "no_org", "you are not a member of any organization")
        active = next((m for m in memberships if m.org_id == sess.active_org_id), memberships[0])
        if (
            sess.active_org_id != active.org_id
            or not sess.last_seen_at
            or now - sess.last_seen_at > timedelta(minutes=5)
        ):
            await db.execute(
                update(DbSession).where(DbSession.id == sess.id).values(active_org_id=active.org_id, last_seen_at=now)
            )

    if request.method not in SAFE_METHODS:
        header = request.headers.get(CSRF_HEADER, "")
        if not header or not constant_time_equal(header, csrf_token_for(sess.id)):
            raise ApiError(403, "csrf_failed", "missing or invalid CSRF token")
        _check_origin(request)

    _rate_limit(f"user:{user.id}", settings.rate_limit_api_per_minute)
    return UserPrincipal(user.id, user.github_login, sess.id, active.org_id, active.role, client_ip(request))


async def require_api_key(request: Request) -> ApiKeyPrincipal:
    settings = get_settings()
    auth = request.headers.get("authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-replay-api-key", "")
    prefix = parse_api_key(key) if key else None
    if prefix is None:
        raise ApiError(401, "unauthorized", "missing or malformed API key")
    now = datetime.now(UTC)
    async with system_session() as db:
        api_key = await db.scalar(select(ApiKey).where(ApiKey.prefix == prefix, ApiKey.revoked_at.is_(None)))
        if api_key is None or not constant_time_equal(api_key.key_hash, sha256_hex(key)):
            raise ApiError(401, "unauthorized", "invalid API key")
        if api_key.last_used_at is None or now - api_key.last_used_at > timedelta(minutes=1):
            await db.execute(update(ApiKey).where(ApiKey.id == api_key.id).values(last_used_at=now))
    _rate_limit(f"key:{api_key.id}", settings.rate_limit_ingest_per_minute)
    return ApiKeyPrincipal(api_key.org_id, api_key.project_id, api_key.id, client_ip(request))


def require_role(role: str) -> Callable[..., Coroutine[Any, Any, UserPrincipal]]:
    async def _dep(principal: Annotated[UserPrincipal, Depends(require_user)]) -> UserPrincipal:
        if not principal.has_role(role):
            raise forbidden(f"requires {role} role")
        return principal

    return _dep


async def user_db(principal: Annotated[UserPrincipal, Depends(require_user)]) -> AsyncIterator[AsyncSession]:
    async with tenant_session(principal.org_id) as db:
        yield db


async def key_db(principal: Annotated[ApiKeyPrincipal, Depends(require_api_key)]) -> AsyncIterator[AsyncSession]:
    async with tenant_session(principal.org_id) as db:
        yield db


CurrentUser = Annotated[UserPrincipal, Depends(require_user)]
AdminUser = Annotated[UserPrincipal, Depends(require_role("admin"))]
OwnerUser = Annotated[UserPrincipal, Depends(require_role("owner"))]
CurrentKey = Annotated[ApiKeyPrincipal, Depends(require_api_key)]
UserDB = Annotated[AsyncSession, Depends(user_db, scope="function")]
KeyDB = Annotated[AsyncSession, Depends(key_db, scope="function")]


async def get_project(db: AsyncSession, project_id: uuid.UUID) -> Project:
    project = await db.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        raise not_found("project")
    return project


async def get_org(db: AsyncSession, org_id: uuid.UUID) -> Org:
    org = await db.scalar(select(Org).where(Org.id == org_id))
    if org is None:
        raise not_found("organization")
    return org
