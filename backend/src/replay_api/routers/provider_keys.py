"""Bring-your-own LLM provider keys. Encrypted at rest; never returned or logged."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select

from replay_api.config import get_settings
from replay_api.db.models import ProviderKey
from replay_api.deps import AdminUser, CurrentUser, UserDB
from replay_api.errors import bad_request, not_found
from replay_api.security.crypto import provider_key_aad, seal
from replay_api.security.net import UnsafeUrlError, assert_public_url
from replay_api.services import audit

router = APIRouter(prefix="/api/provider-keys", tags=["provider-keys"])


def key_out(k: ProviderKey) -> dict[str, Any]:
    return {
        "id": str(k.id),
        "provider": k.provider,
        "name": k.name,
        "base_url": k.base_url,
        "masked": f"…{k.last4}",
        "created_at": k.created_at.isoformat() if k.created_at else None,
        "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
    }


class ProviderKeyIn(BaseModel):
    provider: Literal["openai", "anthropic", "openai_compatible"]
    name: str = Field(min_length=1, max_length=200)
    api_key: SecretStr = Field(min_length=8, max_length=500)
    base_url: str | None = Field(default=None, max_length=500)


@router.get("")
async def list_keys(principal: CurrentUser, db: UserDB) -> dict[str, Any]:
    keys = (
        (await db.execute(select(ProviderKey).where(ProviderKey.revoked_at.is_(None)).order_by(ProviderKey.created_at)))
        .scalars()
        .all()
    )
    return {"provider_keys": [key_out(k) for k in keys]}


@router.post("", status_code=201)
async def create_key(principal: AdminUser, db: UserDB, body: ProviderKeyIn) -> dict[str, Any]:
    settings = get_settings()
    base_url = body.base_url.rstrip("/") if body.base_url else None
    if body.provider == "openai_compatible" and not base_url:
        raise bad_request("openai_compatible keys need a base_url")
    if base_url:
        try:
            await assert_public_url(base_url, allow_private=not settings.is_production_like)
        except UnsafeUrlError as exc:
            raise bad_request(str(exc)) from exc
    secret = body.api_key.get_secret_value().strip()
    key_id = uuid.uuid4()
    sealed = seal(settings.encryption_key_bytes, secret, provider_key_aad(principal.org_id, key_id))
    row = ProviderKey(
        id=key_id,
        org_id=principal.org_id,
        provider=body.provider,
        name=body.name,
        base_url=base_url,
        ciphertext=sealed.ciphertext,
        wrapped_dek=sealed.wrapped_dek,
        last4=secret[-4:],
        created_by=principal.user_id,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    await audit.record(
        db,
        principal.org_id,
        "provider_key.create",
        user_id=principal.user_id,
        target_type="provider_key",
        target_id=row.id,
        metadata={"provider": body.provider, "name": body.name},
        ip=principal.ip,
    )
    return key_out(row)


@router.delete("/{key_id}")
async def revoke_key(key_id: uuid.UUID, principal: AdminUser, db: UserDB) -> dict[str, Any]:
    row = await db.scalar(select(ProviderKey).where(ProviderKey.id == key_id))
    if row is None:
        raise not_found("provider key")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        # Crypto-shred: drop the ciphertext and wrapped key so the secret is unrecoverable.
        row.ciphertext = b""
        row.wrapped_dek = b""
        await audit.record(
            db,
            principal.org_id,
            "provider_key.revoke",
            user_id=principal.user_id,
            target_type="provider_key",
            target_id=row.id,
            ip=principal.ip,
        )
    return key_out(row)
