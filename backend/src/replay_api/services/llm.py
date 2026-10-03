"""Resolve (model, provider) to a provider client using the org's own keys (BYOK)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from replay_api.config import Settings
from replay_api.db.models import ProviderKey
from replay_api.replay.engine import ReplayConfigError
from replay_api.replay.judging import SimulatorJudge
from replay_api.replay.providers import (
    AnthropicProvider,
    OpenAIProvider,
    Provider,
    SimulatorProvider,
    infer_provider,
)
from replay_api.security.crypto import Sealed, provider_key_aad, unseal
from replay_api.security.net import UnsafeUrlError, assert_public_url


@dataclass(frozen=True)
class DecryptedKey:
    id: uuid.UUID
    provider: str
    base_url: str | None
    secret: str


async def load_keys(db: AsyncSession, settings: Settings, org_id: uuid.UUID) -> list[DecryptedKey]:
    rows = (
        (await db.execute(select(ProviderKey).where(ProviderKey.revoked_at.is_(None)).order_by(ProviderKey.created_at)))
        .scalars()
        .all()
    )
    keks = [settings.encryption_key_bytes]
    if settings.previous_encryption_key_bytes:
        keks.append(settings.previous_encryption_key_bytes)
    out = []
    for r in rows:
        secret = unseal(keks, Sealed(r.ciphertext, r.wrapped_dek), provider_key_aad(org_id, r.id))
        out.append(DecryptedKey(r.id, r.provider, r.base_url, secret))
    return out


class ProviderResolver:
    def __init__(
        self,
        settings: Settings,
        keys: list[DecryptedKey],
        recorded_outputs: list[dict[str, Any] | None] | None = None,
        judge: bool = False,
    ) -> None:
        self._settings = settings
        self._keys = keys
        self._outputs = recorded_outputs or []
        self._judge = judge
        self._cache: dict[tuple[str, str], Provider] = {}
        self.preferred_key_id: str | None = None

    async def get(self, model: str, provider_name: str | None) -> Provider:
        name = (provider_name or infer_provider(model) or "").lower()
        if model.lower().startswith("sim-") or name == "simulator":
            if not self._settings.enable_simulator_provider:
                raise ReplayConfigError("the simulator provider is disabled on this deployment")
            return SimulatorJudge() if self._judge else SimulatorProvider(self._outputs)
        if not name:
            raise ReplayConfigError(f"cannot tell which provider serves {model!r}; set provider explicitly")
        chosen = self._pick_key(name)
        cache_key = (name, str(chosen.id))
        if cache_key in self._cache:
            return self._cache[cache_key]
        s = self._settings
        if chosen.base_url:
            try:
                private_ok = not s.is_production_like
                await assert_public_url(chosen.base_url, allow_private=private_ok)
            except UnsafeUrlError as exc:
                raise ReplayConfigError(f"provider base URL rejected: {exc}") from exc
        provider: Provider
        if chosen.provider == "anthropic":
            provider = AnthropicProvider(chosen.secret, s.provider_timeout_seconds, s.provider_max_retries)
        elif chosen.provider in ("openai", "openai_compatible"):
            provider = OpenAIProvider(
                chosen.secret,
                chosen.base_url,
                s.provider_timeout_seconds,
                s.provider_max_retries,
                compatible=chosen.provider == "openai_compatible",
            )
        else:  # pragma: no cover - constrained by API validation
            raise ReplayConfigError(f"unsupported provider {chosen.provider}")
        self._cache[cache_key] = provider
        return provider

    def _pick_key(self, provider: str) -> DecryptedKey:
        if self.preferred_key_id:
            for k in self._keys:
                if str(k.id) == self.preferred_key_id:
                    return k
            raise ReplayConfigError("the selected provider key no longer exists")
        for k in self._keys:
            if k.provider == provider:
                return k
        if provider == "openai":
            for k in self._keys:
                if k.provider == "openai_compatible":
                    return k
        raise ReplayConfigError(f"no {provider} API key is configured for this organization (Settings > Provider keys)")
