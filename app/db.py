"""One asyncpg connection pool for the whole app."""

import os
from contextlib import asynccontextmanager

import asyncpg

_pool = None


async def connect():
    # Read the URL at connect time (not import time) so tests can point at another database.
    global _pool
    _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=2, max_size=10)


async def close():
    if _pool is not None:
        await _pool.close()


def pool():
    return _pool


@asynccontextmanager
async def tenant_connection(tenant_id):
    """A connection that can only read tenant_id's rows, for the length of one transaction.

    Row-level security (migrations/002_row_level_security.sql) backs up the tenant filter that
    every read query has anyway. SET LOCAL and set_config(..., true) last only until the
    transaction ends, so the pooled connection is back to normal for the next request, even
    if this one fails half-way.
    """
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE tenant_reader")
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
            yield conn
