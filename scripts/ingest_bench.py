"""Ingest API throughput: events stored per second at several batch sizes, with concurrent callers.

Usage, with the stack running (the loaded dataset isn't changed):
    docker compose exec api python scripts/ingest_bench.py

It writes events for tenants t_9001–t_9005, which are reserved for this benchmark (the dataset
uses t_0001–t_0800), dated inside September 2026 so the Summary window doesn't move. Those
events are deleted before and after the run.

Request bodies are built before the clock starts, and requests are sent from several client
processes: one Python HTTP client process tops out around 900 requests/s, which would measure
the client instead of the API.
"""

import argparse
import asyncio
import json
import os
import random
import statistics
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import get_args

import asyncpg
import httpx

from app.models import Action

BENCH_TENANTS = [f"t_900{i}" for i in range(1, 6)]
ACTIONS = get_args(Action)
SEPT_1 = datetime(2026, 9, 1, tzinfo=timezone.utc)

# (events per request, client processes, concurrent callers per process, total events)
SCENARIOS = [
    (1, 4, 8, 20_000),
    (100, 2, 4, 100_000),
    (1_000, 2, 2, 200_000),
]


def random_event(rng):
    action = rng.choice(ACTIONS)
    occurred_at = SEPT_1 + timedelta(seconds=rng.randrange(29 * 86400))
    return {
        "event_id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
        "tenant_id": rng.choice(BENCH_TENANTS),
        "actor_id": "usr_bench",
        "actor_type": "user",
        "actor_email": "bench@example.com",
        "action": action,
        "resource_type": action.split(".")[0],
        "resource_id": f"res_{rng.randrange(10**6)}",
        "result": "failure" if rng.random() < 0.04 else "success",
        "ip": f"10.{rng.randrange(256)}.{rng.randrange(256)}.{rng.randrange(1, 255)}",
        "metadata": {"request_id": f"req_{rng.randrange(10**9)}", "note": "x" * 150},  # ~200 bytes, like the dataset
        "occurred_at": occurred_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


async def send_all(url, bodies, callers, batch_size):
    """One client process: `callers` concurrent loops posting bodies until none are left."""
    latencies_ms = []

    async def caller(client):
        while bodies:
            body = bodies.pop()
            started = time.perf_counter()
            response = await client.post("/v1/events", content=body, headers={"Content-Type": "application/json"})
            latencies_ms.append((time.perf_counter() - started) * 1000)
            response.raise_for_status()
            assert response.json()["inserted"] == batch_size

    async with httpx.AsyncClient(base_url=url, timeout=120) as client:
        await asyncio.gather(*(caller(client) for _ in range(callers)))
    return latencies_ms


def client_process(url, bodies, callers, batch_size):
    return asyncio.run(send_all(url, bodies, callers, batch_size))


def run_scenario(url, batch_size, processes, callers, total, rng):
    bodies = [json.dumps({"events": [random_event(rng) for _ in range(batch_size)]}).encode()
              for _ in range(total // batch_size)]
    shares = [bodies[i::processes] for i in range(processes)]
    with ProcessPoolExecutor(processes) as pool:
        started = time.perf_counter()
        results = list(pool.map(client_process, [url] * processes, shares, [callers] * processes, [batch_size] * processes))
        seconds = time.perf_counter() - started
    return seconds, [ms for latencies in results for ms in latencies]


async def delete_bench_events():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await conn.execute("DELETE FROM events WHERE tenant_id = ANY($1::text[])", BENCH_TENANTS)
        await conn.execute("DELETE FROM daily_action_counts WHERE tenant_id = ANY($1::text[])", BENCH_TENANTS)
        await conn.execute("VACUUM (ANALYZE) events")
        await conn.execute("VACUUM (ANALYZE) daily_action_counts")
        return await conn.fetchval("SELECT count(*) FROM events")
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    args = parser.parse_args()

    rows_before = asyncio.run(delete_bench_events())
    rng = random.Random(1)
    print(f"API: {args.url}; events in the database before: {rows_before:,}\n")
    print("| Events per request | Concurrent callers | Events | Seconds | Events/s | p50 ms | p95 ms | Max ms |")
    print("|---|---|---|---|---|---|---|---|")
    try:
        for batch_size, processes, callers, total in SCENARIOS:
            seconds, latencies = run_scenario(args.url, batch_size, processes, callers, total, rng)
            p95 = statistics.quantiles(latencies, n=20)[18]
            print(f"| {batch_size:,} | {processes * callers} | {total:,} | {seconds:.1f} | {total / seconds:,.0f} | "
                  f"{statistics.median(latencies):.1f} | {p95:.1f} | {max(latencies):.1f} |", flush=True)
    finally:
        rows_after = asyncio.run(delete_bench_events())
        print(f"\nBenchmark events deleted; events in the database after: {rows_after:,}")


if __name__ == "__main__":
    main()
