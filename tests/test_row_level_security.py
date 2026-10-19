"""Constraint 4, second layer: the database itself hides other tenants' rows from read requests.

These tests query the database directly, without the tenant filter the API's queries carry,
to show row-level security still keeps each tenant to its own rows.
"""

import pytest

from app import db
from tests.helpers import ingest, make_event

A, B = "t_0401", "t_0402"


async def test_query_without_tenant_filter_sees_only_the_scoped_tenant(client):
    await ingest(client, [make_event(A) for _ in range(4)] + [make_event(B) for _ in range(3)])

    async with db.tenant_connection(A) as conn:
        event_tenants = await conn.fetch("SELECT DISTINCT tenant_id FROM events")  # no WHERE on purpose
        event_count = await conn.fetchval("SELECT count(*) FROM events")
        count_tenants = await conn.fetch("SELECT DISTINCT tenant_id FROM daily_action_counts")

    assert [r["tenant_id"] for r in event_tenants] == [A]
    assert event_count == 4
    assert [r["tenant_id"] for r in count_tenants] == [A]


async def test_reader_with_no_tenant_set_sees_nothing(client):
    await ingest(client, [make_event(A), make_event(B)])

    async with db.pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE tenant_reader")  # role, but no app.tenant_id
            assert await conn.fetchval("SELECT count(*) FROM events") == 0
            assert await conn.fetchval("SELECT count(*) FROM daily_action_counts") == 0


async def test_reader_cannot_write(client):
    async with db.tenant_connection(A) as conn:
        with pytest.raises(Exception, match="permission denied"):
            await conn.execute("DELETE FROM events")


async def test_tenant_scope_ends_with_the_transaction_even_on_error(client):
    with pytest.raises(RuntimeError):
        async with db.tenant_connection(A):
            raise RuntimeError("request failed half-way")

    # Check every connection the pool can hand out: none may still be tenant_reader or scoped to A
    connections = [await db.pool().acquire() for _ in range(10)]
    try:
        for conn in connections:
            assert await conn.fetchval("SELECT current_user") != "tenant_reader"
            assert await conn.fetchval("SELECT coalesce(current_setting('app.tenant_id', true), '')") == ""
    finally:
        for conn in connections:
            await db.pool().release(conn)


async def test_summary_window_still_ends_on_newest_day_across_all_tenants(client):
    # The anchor lookup runs inside tenant A's restricted transaction, yet must see B's newer day
    await ingest(client, [make_event(A, "2026-09-10T08:00:00.000Z"), make_event(B, "2026-10-05T08:00:00.000Z")])

    summary = (await client.get("/v1/summary", headers={"X-Tenant-ID": A})).json()

    assert summary["to"] == "2026-10-05"
