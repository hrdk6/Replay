"""Audit log for sensitive actions. Metadata is scrubbed of secrets before storage."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from replay_api.db.models import AuditLog
from replay_api.logs import get_logger, scrub

log = get_logger(__name__)


async def record(
    db: AsyncSession,
    org_id: uuid.UUID,
    action: str,
    *,
    user_id: uuid.UUID | None = None,
    api_key_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: object | None = None,
    metadata: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    entry = AuditLog(
        org_id=org_id,
        actor_user_id=user_id,
        actor_api_key_id=api_key_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        meta=scrub(metadata or {}),
        ip=ip,
    )
    db.add(entry)
    log.info("audit", action=action, org_id=str(org_id), target_type=target_type, target_id=entry.target_id)
