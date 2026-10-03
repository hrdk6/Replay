"""Declarative base and shared column helpers."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, MetaData, Numeric, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        datetime: DateTime(timezone=True),
        uuid.UUID: UUID(as_uuid=True),
        Decimal: Numeric(18, 6),
        int: BigInteger,
    }


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def created_at() -> Mapped[datetime]:
    return mapped_column(server_default=func.now(), nullable=False)


class TenantMixin:
    """Every tenant-owned table carries ``org_id``.

    Isolation is enforced twice:
    1. The ORM adds ``org_id = :current_org`` to every SELECT/UPDATE/DELETE on
       these models (see ``db.session``), and refuses to flush rows for another org.
    2. Postgres row-level security policies (created in migrations) filter on
       the transaction-local ``app.org_id`` setting.
    """

    __tenant__ = True

    @declared_attr
    def org_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), index=True, nullable=False)
