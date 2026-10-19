"""One asyncpg connection pool for the whole app."""

import os

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
