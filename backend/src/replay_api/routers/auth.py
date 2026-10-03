"""Dashboard authentication: GitHub OAuth, dev login, logout, current user."""

from __future__ import annotations

import base64
import hashlib
import hashlib as _hashlib
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from replay_api.config import get_settings
from replay_api.db.models import Membership, Org, User
from replay_api.db.models import Session as DbSession
from replay_api.db.session import system_session
from replay_api.deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    CurrentUser,
    _rate_limit,
    client_ip,
    csrf_token_for,
)
from replay_api.errors import ApiError
from replay_api.logs import get_logger
from replay_api.security.tokens import random_token, sign_payload, verify_payload
from replay_api.services.accounts import (
    GithubProfile,
    SignupNotAllowed,
    login_with_github,
    revoke_session,
)

router = APIRouter(prefix="/api", tags=["auth"])
log = get_logger(__name__)

STATE_COOKIE = "replay_oauth"
GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"  # noqa: S105 - URL, not a secret
GITHUB_API = "https://api.github.com"


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value or len(value) > 500:
        return "/"
    return value


def _set_login_cookies(response: Response, token: str, session_id: uuid.UUID) -> None:
    s = get_settings()
    max_age = s.session_ttl_hours * 3600
    response.set_cookie(
        SESSION_COOKIE, token, max_age=max_age, httponly=True, secure=s.secure_cookies, samesite="lax", path="/"
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token_for(session_id),
        max_age=max_age,
        httponly=False,
        secure=s.secure_cookies,
        samesite="lax",
        path="/",
    )


def _clear_cookies(response: Response) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/")


def _secret() -> bytes:
    return get_settings().session_secret.get_secret_value().encode()


@router.get("/auth/config")
async def auth_config() -> dict[str, Any]:
    s = get_settings()
    return {
        "github": bool(s.github_client_id),
        "dev_login": s.dev_login_enabled and s.env in ("development", "test"),
        "signup_mode": s.signup_mode,
        "public_api_url": s.public_api_url,
        "simulator": s.enable_simulator_provider,
    }


@router.get("/auth/github/login")
async def github_login(request: Request, next: str | None = None) -> Response:
    s = get_settings()
    _rate_limit(f"auth:{client_ip(request)}", s.rate_limit_auth_per_minute)
    if not s.github_client_id:
        raise ApiError(503, "github_not_configured", "GitHub sign-in is not configured")
    nonce = random_token(16)
    verifier = random_token(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = sign_payload(_secret(), {"n": nonce, "next": _safe_next(next)}, ttl_seconds=600)
    params = {
        "client_id": s.github_client_id,
        "redirect_uri": f"{s.public_app_url}/api/auth/github/callback",
        "scope": "read:user user:email",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "allow_signup": "true",
    }
    response = RedirectResponse(f"{GITHUB_AUTHORIZE}?{urlencode(params)}", status_code=302)
    response.set_cookie(
        STATE_COOKIE,
        sign_payload(_secret(), {"n": nonce, "v": verifier}, ttl_seconds=600),
        max_age=600,
        httponly=True,
        secure=s.secure_cookies,
        samesite="lax",
        path="/api/auth",
    )
    return response


async def _fetch_github_profile(code: str, verifier: str) -> GithubProfile:
    s = get_settings()
    assert s.github_client_secret is not None
    async with httpx.AsyncClient(timeout=10) as client:
        tok = await client.post(
            GITHUB_TOKEN,
            headers={"Accept": "application/json"},
            data={
                "client_id": s.github_client_id,
                "client_secret": s.github_client_secret.get_secret_value(),
                "code": code,
                "redirect_uri": f"{s.public_app_url}/api/auth/github/callback",
                "code_verifier": verifier,
            },
        )
        access = tok.json().get("access_token") if tok.status_code == 200 else None
        if not access:
            raise ApiError(400, "oauth_failed", "GitHub sign-in failed")
        headers = {"Authorization": f"Bearer {access}", "Accept": "application/vnd.github+json"}
        user_resp = await client.get(f"{GITHUB_API}/user", headers=headers)
        if user_resp.status_code != 200:
            raise ApiError(400, "oauth_failed", "could not read GitHub profile")
        u = user_resp.json()
        email = u.get("email")
        if not email:
            emails = await client.get(f"{GITHUB_API}/user/emails", headers=headers)
            if emails.status_code == 200:
                primary = next((e for e in emails.json() if e.get("primary") and e.get("verified")), None)
                email = primary.get("email") if primary else None
    return GithubProfile(int(u["id"]), str(u["login"]), u.get("name"), email, u.get("avatar_url"))


@router.get("/auth/github/callback")
async def github_callback(request: Request, code: str | None = None, state: str | None = None) -> Response:
    s = get_settings()
    _rate_limit(f"auth:{client_ip(request)}", s.rate_limit_auth_per_minute)
    cookie = request.cookies.get(STATE_COOKIE)
    st = verify_payload(_secret(), state) if state else None
    ck = verify_payload(_secret(), cookie) if cookie else None
    if not code or st is None or ck is None or st.get("n") != ck.get("n"):
        return RedirectResponse("/login?error=state", status_code=302)
    try:
        profile = await _fetch_github_profile(code, str(ck.get("v", "")))
        result = await login_with_github(s, profile, request.headers.get("user-agent"), client_ip(request))
    except SignupNotAllowed:
        return RedirectResponse("/login?error=not_allowed", status_code=302)
    except ApiError:
        return RedirectResponse("/login?error=oauth", status_code=302)
    response = RedirectResponse(_safe_next(str(st.get("next"))), status_code=302)
    response.delete_cookie(STATE_COOKIE, path="/api/auth")
    _set_login_cookies(response, result.session_token, result.session_id)
    return response


class DevLoginIn(BaseModel):
    login: str = Field(min_length=1, max_length=39, pattern=r"^[A-Za-z0-9-]+$")


@router.post("/auth/dev-login")
async def dev_login(request: Request, body: DevLoginIn) -> Response:
    """Local development only: sign in as any GitHub login without OAuth."""
    s = get_settings()
    if not (s.dev_login_enabled and s.env in ("development", "test")):
        raise ApiError(404, "not_found", "not found")
    fake_id = -int(_hashlib.sha256(body.login.lower().encode()).hexdigest()[:12], 16)
    profile = GithubProfile(fake_id, body.login, body.login, None, None)
    try:
        result = await login_with_github(
            s, profile, request.headers.get("user-agent"), client_ip(request), method="dev"
        )
    except SignupNotAllowed as exc:
        raise ApiError(403, "not_allowed", "this account is not on the beta allowlist") from exc
    response = JSONResponse({"ok": True, "user_id": str(result.user_id), "org_id": str(result.org_id)})
    _set_login_cookies(response, result.session_token, result.session_id)
    return response


@router.post("/auth/logout")
async def logout(principal: CurrentUser) -> Response:
    await revoke_session(principal.session_id)
    response = JSONResponse({"ok": True})
    _clear_cookies(response)
    return response


@router.get("/me")
async def me(principal: CurrentUser) -> dict[str, Any]:
    async with system_session() as db:
        user = await db.get(User, principal.user_id)
        rows = (
            await db.execute(
                select(Membership, Org)
                .join(Org, Org.id == Membership.org_id)
                .where(Membership.user_id == principal.user_id)
            )
        ).all()
    assert user is not None
    return {
        "user": {
            "id": str(user.id),
            "github_login": user.github_login,
            "name": user.name,
            "email": user.email,
            "avatar_url": user.avatar_url,
        },
        "orgs": [{"id": str(o.id), "name": o.name, "slug": o.slug, "role": m.role} for m, o in rows],
        "active_org_id": str(principal.org_id),
        "role": principal.role,
        "csrf_token": csrf_token_for(principal.session_id),
    }


class ActiveOrgIn(BaseModel):
    org_id: uuid.UUID


@router.post("/me/active-org")
async def set_active_org(principal: CurrentUser, body: ActiveOrgIn) -> dict[str, Any]:
    async with system_session() as db:
        member = await db.scalar(
            select(Membership.id).where(Membership.user_id == principal.user_id, Membership.org_id == body.org_id)
        )
        if member is None:
            raise ApiError(404, "not_found", "organization not found")
        await db.execute(
            update(DbSession).where(DbSession.id == principal.session_id).values(active_org_id=body.org_id)
        )
    return {"ok": True, "active_org_id": str(body.org_id)}
