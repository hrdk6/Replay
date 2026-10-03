"""Row-level security and the ORM tenant guard, tested independently of the HTTP layer."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from replay_api.db.models import Org, Project, Trace
from replay_api.db.session import (
    TenantContextError,
    bare_session,
    get_session_factory,
    system_session,
    tenant_session,
)
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError


async def _two_orgs() -> tuple[uuid.UUID, uuid.UUID]:
    a, b = uuid.uuid4(), uuid.uuid4()
    async with system_session() as db:
        for oid, slug in ((a, "org-a"), (b, "org-b")):
            db.add(
                Org(
                    id=oid,
                    name=slug,
                    slug=slug,
                    quota_traces_per_day=10,
                    quota_replay_runs_per_day=10,
                    monthly_budget_usd=Decimal(1),
                )
            )
        await db.flush()
        for oid in (a, b):
            p = Project(id=uuid.uuid4(), org_id=oid, name="p", slug="p")
            db.add(p)
            await db.flush()
            for i in range(3):
                db.add(Trace(org_id=oid, project_id=p.id, external_id=f"t{i}"))
    return a, b


async def test_app_role_cannot_bypass_rls() -> None:
    async with bare_session() as db:
        row = (
            await db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))
        ).first()
    assert row == (False, False), "the app must not connect as a superuser or BYPASSRLS role"


async def test_every_tenant_table_has_forced_rls() -> None:
    async with bare_session() as db:
        rows = (
            await db.execute(
                text(
                    """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
              AND EXISTS (SELECT 1 FROM information_schema.columns col
                          WHERE col.table_name = c.relname AND col.column_name = 'org_id')
            """
                )
            )
        ).all()
    assert len(rows) >= 19
    missing = [r.relname for r in rows if not (r.relrowsecurity and r.relforcerowsecurity)]
    assert not missing, f"tables with org_id but no forced RLS: {missing}"


async def test_raw_sql_is_filtered_by_rls() -> None:
    a, _ = await _two_orgs()
    async with tenant_session(a) as db:
        assert await db.scalar(text("SELECT count(*) FROM traces")) == 3
        assert await db.scalar(text("SELECT count(*) FROM orgs")) == 1
    async with bare_session() as db:
        # No org context: RLS hides everything, even to raw SQL.
        assert await db.scalar(text("SELECT count(*) FROM traces")) == 0
    async with system_session() as db:
        assert await db.scalar(text("SELECT count(*) FROM traces")) == 6


async def test_rls_blocks_cross_tenant_writes() -> None:
    a, b = await _two_orgs()
    async with system_session() as db:
        b_project = await db.scalar(select(Project.id).where(Project.org_id == b))
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(a) as db:
            await db.execute(
                text(
                    "INSERT INTO traces (id, org_id, project_id, external_id, status, span_count, error_count, "
                    "llm_call_count, tool_call_count, input_tokens, output_tokens) "
                    "VALUES (:id, :org, :p, 'evil', 'ok', 0, 0, 0, 0, 0, 0)"
                ),
                {"id": uuid.uuid4(), "org": b, "p": b_project},
            )
    async with tenant_session(a) as db:
        res = await db.execute(text("UPDATE traces SET name = 'pwned' WHERE org_id = :b"), {"b": b})
        assert res.rowcount == 0  # type: ignore[attr-defined]
        res = await db.execute(text("DELETE FROM traces WHERE org_id = :b"), {"b": b})
        assert res.rowcount == 0  # type: ignore[attr-defined]
    async with system_session() as db:
        assert await db.scalar(text("SELECT count(*) FROM traces WHERE org_id = :b AND name IS NULL"), {"b": b}) == 3


async def test_orm_guard_works_even_without_rls() -> None:
    """Disable FORCE RLS inside a rolled-back transaction to prove the app-layer filter stands alone."""
    a, b = await _two_orgs()
    factory = get_session_factory()
    async with factory() as db:
        db.info["org_id"] = a
        await db.execute(text("ALTER TABLE traces NO FORCE ROW LEVEL SECURITY"))
        await db.execute(text("ALTER TABLE orgs NO FORCE ROW LEVEL SECURITY"))
        # Owner without FORCE is not subject to RLS, so only the ORM criteria protect us here.
        assert await db.scalar(text("SELECT count(*) FROM traces")) == 6
        rows = (await db.execute(select(Trace))).scalars().all()
        assert {t.org_id for t in rows} == {a}
        await db.execute(update(Trace).values(name="orm-update"))
        await db.execute(delete(Trace).where(Trace.external_id == "t0"))
        assert await db.scalar(text("SELECT count(*) FROM traces WHERE org_id = :b AND name IS NULL"), {"b": b}) == 3
        assert await db.scalar(text("SELECT count(*) FROM traces WHERE org_id = :b"), {"b": b}) == 3
        await db.rollback()


async def test_orm_refuses_tenant_queries_without_context() -> None:
    await _two_orgs()
    with pytest.raises(TenantContextError):
        async with bare_session() as db:
            await db.execute(select(Trace))


async def test_orm_refuses_writes_for_another_org() -> None:
    a, b = await _two_orgs()
    with pytest.raises(TenantContextError):
        async with tenant_session(a) as db:
            db.add(Project(org_id=b, name="evil", slug="evil"))
            await db.flush()


async def test_tenant_context_survives_mid_session_commit() -> None:
    a, _ = await _two_orgs()
    async with tenant_session(a) as db:
        assert await db.scalar(text("SELECT count(*) FROM traces")) == 3
        await db.commit()
        # New transaction: the after_begin hook must re-apply app.org_id.
        assert await db.scalar(text("SELECT count(*) FROM traces")) == 3
