"""Payload storage: large span inputs/outputs and dataset recordings.

Small payloads live inline in Postgres (JSONB); anything larger than
``INLINE_PAYLOAD_MAX_BYTES`` goes to an S3-compatible bucket. Keys are always
prefixed with ``orgs/<org_id>/`` so org deletion is a single prefix delete.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Protocol

from replay_api.config import Settings, get_settings


class PayloadStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete_many(self, keys: list[str]) -> None: ...
    async def delete_prefix(self, prefix: str) -> int: ...
    async def check(self) -> None: ...


class MemoryPayloadStore:
    """In-process store for tests."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes) -> None:
        self.objects[key] = data

    async def get(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError as exc:
            raise FileNotFoundError(key) from exc

    async def delete_many(self, keys: list[str]) -> None:
        for k in keys:
            self.objects.pop(k, None)

    async def delete_prefix(self, prefix: str) -> int:
        doomed = [k for k in self.objects if k.startswith(prefix)]
        for k in doomed:
            del self.objects[k]
        return len(doomed)

    async def check(self) -> None:
        return None


class S3PayloadStore:
    def __init__(self, settings: Settings) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url or None,
            region_name=settings.s3_region,
            aws_access_key_id=(settings.s3_access_key_id.get_secret_value() if settings.s3_access_key_id else None),
            aws_secret_access_key=(
                settings.s3_secret_access_key.get_secret_value() if settings.s3_secret_access_key else None
            ),
            config=Config(
                connect_timeout=5,
                read_timeout=30,
                retries={"max_attempts": 3, "mode": "standard"},
                s3={"addressing_style": "path"},
            ),
        )
        self._bucket_checked = not settings.s3_create_bucket

    async def _ensure_bucket(self) -> None:
        if self._bucket_checked:
            return

        def _create() -> None:
            from botocore.exceptions import ClientError

            try:
                self._client.head_bucket(Bucket=self.bucket)
            except ClientError:
                self._client.create_bucket(Bucket=self.bucket)

        await asyncio.to_thread(_create)
        self._bucket_checked = True

    async def put(self, key: str, data: bytes) -> None:
        await self._ensure_bucket()
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType="application/json",
        )

    async def get(self, key: str) -> bytes:
        await self._ensure_bucket()

        def _get() -> bytes:
            from botocore.exceptions import ClientError

            try:
                obj = self._client.get_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                    raise FileNotFoundError(key) from exc
                raise
            body: bytes = obj["Body"].read()
            return body

        return await asyncio.to_thread(_get)

    async def delete_many(self, keys: list[str]) -> None:
        if not keys:
            return
        await self._ensure_bucket()
        for i in range(0, len(keys), 1000):
            chunk = [{"Key": k} for k in keys[i : i + 1000]]
            await asyncio.to_thread(
                self._client.delete_objects, Bucket=self.bucket, Delete={"Objects": chunk, "Quiet": True}
            )

    async def delete_prefix(self, prefix: str) -> int:
        await self._ensure_bucket()

        def _list() -> list[str]:
            keys: list[str] = []
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                keys.extend(o["Key"] for o in page.get("Contents", []))
            return keys

        keys = await asyncio.to_thread(_list)
        await self.delete_many(keys)
        return len(keys)

    async def check(self) -> None:
        await self._ensure_bucket()
        await asyncio.to_thread(self._client.head_bucket, Bucket=self.bucket)


_store: PayloadStore | None = None


def get_store() -> PayloadStore:
    global _store
    if _store is None:
        s = get_settings()
        _store = MemoryPayloadStore() if s.storage_backend == "memory" else S3PayloadStore(s)
    return _store


def set_store(store: PayloadStore | None) -> None:
    global _store
    _store = store


# --- Key layout ---------------------------------------------------------------------


def org_prefix(org_id: uuid.UUID) -> str:
    return f"orgs/{org_id}/"


def span_payload_key(org_id: uuid.UUID, project_id: uuid.UUID, trace_key: str, span_key: str, field: str) -> str:
    return f"orgs/{org_id}/projects/{project_id}/spans/{trace_key}/{span_key}/{field}.json"


def dataset_item_key(org_id: uuid.UUID, dataset_id: uuid.UUID, item_id: uuid.UUID) -> str:
    return f"orgs/{org_id}/datasets/{dataset_id}/items/{item_id}.json"


def dumps(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str).encode()


async def load_json(store: PayloadStore, key: str) -> Any:
    return json.loads(await store.get(key))
