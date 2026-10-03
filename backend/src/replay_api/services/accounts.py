"""Sign-up / sign-in, personal orgs, invites and sessions."""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from replay_api.config import Settings
from replay_api.db.models import Invite, Membership, Org, Project, User
from replay_api.db.models import Session as DbSession
from replay_api.db.session import system_session
from replay_api.security.tokens import random_token, sha256_hex
from replay_api.services import audit


class SignupNotAllowed(Exception):
    pass


@dataclass(frozen=True)
class GithubProfile:
    github_id: int
    login: str
    name: str | None
    email: str | None
    avatar_url: str | None


@dataclass(frozen=True)
class LoginResult:
    user_id: uuid.UUID
    session_id: uuid.UUID
    session_token: str
    org_id: uuid.UUID
    created_user: bool


def slugify(value: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (s or "org")[:max_len]


async def _unique_org_slug(db: AsyncSession, base: str) -> str:
    slug = slugify(base)
    for _ in range(10):
        exists = await db.scalar(select(Org.id).where(Org.slug == slug))
        if exists is None:
            return slug
        slug = f"{slugify(base, 32)}-{secrets.token_hex(3)}"
    return f"org-{secrets.token_hex(6)}"


def new_org(settings: Settings, name: str, slug: str, personal: bool) -> Org:
    return Org(
        id=uuid.uuid4(),
        name=name,
        slug=slug,
        personal=personal,
        quota_traces_per_day=settings.default_quota_traces_per_day,
        quota_replay_runs_per_day=settings.default_quota_replay_runs_per_day,
        monthly_budget_usd=Decimal(str(settings.default_monthly_budget_usd)),
    )


async def _accept_invites(db: AsyncSession, user: User) -> list[uuid.UUID]:
    invites = (
        (
            await db.execute(
                select(Invite).where(Invite.github_login == user.github_login.lower(), Invite.accepted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    joined: list[uuid.UUID] = []
    for inv in invites:
        existing = await db.scalar(
            select(Membership.id).where(Membership.org_id == inv.org_id, Membership.user_id == user.id)
        )
        if existing is None:
            db.add(Membership(org_id=inv.org_id, user_id=user.id, role=inv.role))
            await audit.record(db, inv.org_id, "member.joined", user_id=user.id, target_type="user", target_id=user.id)
        inv.accepted_at = datetime.now(UTC)
        joined.append(inv.org_id)
    return joined


async def login_with_github(
    settings: Settings, profile: GithubProfile, user_agent: str | None, ip: str | None, method: str = "github"
) -> LoginResult:
    login = profile.login.lower()
    now = datetime.now(UTC)
    async with system_session() as db:
        user = await db.scalar(select(User).where(User.github_id == profile.github_id))
        created = False
        if user is None:
            has_invite = await db.scalar(
                select(Invite.id).where(Invite.github_login == login, Invite.accepted_at.is_(None))
            )
            if settings.signup_mode != "open" and login not in settings.allowlist and has_invite is None:
                raise SignupNotAllowed(login)
            user = User(
                id=uuid.uuid4(),
                github_id=profile.github_id,
                github_login=profile.login,
                name=profile.name,
                email=profile.email,
                avatar_url=profile.avatar_url,
            )
            db.add(user)
            await db.flush()
            created = True
        else:
            user.github_login = profile.login
            user.name = profile.name
            user.email = profile.email
            user.avatar_url = profile.avatar_url
        user.last_login_at = now

        await _accept_invites(db, user)
        await db.flush()
        memberships = (
            (await db.execute(select(Membership).where(Membership.user_id == user.id).order_by(Membership.created_at)))
            .scalars()
            .all()
        )
        if not memberships:
            org = new_org(settings, f"{profile.login}'s org", await _unique_org_slug(db, profile.login), personal=True)
            db.add(org)
            await db.flush()
            db.add(Membership(org_id=org.id, user_id=user.id, role="owner"))
            db.add(Project(org_id=org.id, name="Default project", slug="default"))
            await audit.record(db, org.id, "org.created", user_id=user.id, target_type="org", target_id=org.id)
            org_id = org.id
        else:
            org_id = memberships[0].org_id

        token = random_token(32)
        sess = DbSession(
            id=uuid.uuid4(),
            user_id=user.id,
            token_hash=sha256_hex(token),
            active_org_id=org_id,
            expires_at=now + timedelta(hours=settings.session_ttl_hours),
            last_seen_at=now,
            user_agent=(user_agent or "")[:400] or None,
            ip=ip,
        )
        db.add(sess)
        await audit.record(db, org_id, "auth.login", user_id=user.id, ip=ip, metadata={"method": method})
        return LoginResult(user.id, sess.id, token, org_id, created)


async def revoke_session(session_id: uuid.UUID) -> None:
    async with system_session() as db:
        await db.execute(update(DbSession).where(DbSession.id == session_id).values(revoked_at=datetime.now(UTC)))
