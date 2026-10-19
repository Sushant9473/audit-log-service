"""Test setup: a separate audit_test database with the real schema. The loaded data is never touched.

Run inside the stack: docker compose exec api pytest -q
"""

import asyncio
import os
from pathlib import Path

import asyncpg
import httpx
import pytest

from app import db
from app.main import app

MAIN_URL = os.environ["DATABASE_URL"]
TEST_URL = MAIN_URL.rsplit("/", 1)[0] + "/audit_test"
SCHEMA_SQL = (Path(__file__).parent.parent / "migrations" / "001_schema.sql").read_text()
os.environ.setdefault("CURSOR_SECRET", "test-cursor-secret")


async def _create_test_database():
    assert TEST_URL != MAIN_URL, "tests must never run against the main database"
    admin = await asyncpg.connect(MAIN_URL)
    try:
        await admin.execute("DROP DATABASE IF EXISTS audit_test WITH (FORCE)")
        await admin.execute("CREATE DATABASE audit_test")
    finally:
        await admin.close()
    conn = await asyncpg.connect(TEST_URL)
    try:
        await conn.execute(SCHEMA_SQL)
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """Recreated once per test run, so it always has the current schema."""
    asyncio.run(_create_test_database())
    os.environ["DATABASE_URL"] = TEST_URL
    yield
    os.environ["DATABASE_URL"] = MAIN_URL


@pytest.fixture
async def client():
    """The app in-process, on empty tables. ASGITransport doesn't run the lifespan, so connect here."""
    await db.connect()
    await db.pool().execute("TRUNCATE events, daily_action_counts")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await db.close()
