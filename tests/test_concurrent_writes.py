"""Constraint 1 under concurrency.

Callers retry on *timeouts*, so a retry can arrive while the original request is still being
processed. Overlapping writes must still store each event once, keep the daily counts exact,
and never deadlock. The requests below really run at the same time: each one holds its own
database connection from the pool.
"""

import asyncio
import random

from tests.helpers import make_event, mismatched_daily_counts, stored_rows, total_daily_counts

ACTIONS = ["user.login", "document.view", "invoice.create", "project.update"]


def varied_events(count):
    """Events spread over 5 tenants, 3 days and 4 actions, so concurrent batches share daily-count rows."""
    return [
        make_event(f"t_020{i % 5}", f"2026-09-1{i % 3}T12:{i % 60:02d}:00.000Z", ACTIONS[i % 4])
        for i in range(count)
    ]


async def post_all(client, batches):
    return await asyncio.gather(*(client.post("/v1/events", json={"events": batch}) for batch in batches))


async def test_same_batch_sent_by_ten_callers_at_once_is_stored_once(client):
    batch = varied_events(50)

    responses = await post_all(client, [batch] * 10)

    assert all(r.status_code == 200 for r in responses)
    assert sum(r.json()["inserted"] for r in responses) == 50  # exactly one request stored each event
    assert await stored_rows() == 50
    assert await total_daily_counts() == 50
    assert await mismatched_daily_counts() == 0


async def test_overlapping_batches_in_different_orders_never_deadlock(client):
    # 12 batches, each a random 120 of the same 300 events in a random order. Without a fixed
    # lock order, two transactions would wait on each other's rows and Postgres would abort one.
    events = varied_events(300)
    rng = random.Random(42)
    batches = [rng.sample(events, 120) for _ in range(12)]

    responses = await post_all(client, batches)

    unique_ids = {e["event_id"] for batch in batches for e in batch}
    assert all(r.status_code == 200 for r in responses)
    assert sum(r.json()["inserted"] for r in responses) == len(unique_ids)
    assert await stored_rows() == len(unique_ids)
    assert await mismatched_daily_counts() == 0


async def test_different_events_updating_the_same_daily_counts_lose_no_updates(client):
    # 12 batches with no events in common, all landing in the same 60 (tenant, day, action) counts,
    # each in a random order. Every count row is incremented by many transactions at once.
    rng = random.Random(7)
    batches = []
    for _ in range(12):
        batch = varied_events(60)
        rng.shuffle(batch)
        batches.append(batch)

    responses = await post_all(client, batches)

    assert all(r.status_code == 200 for r in responses)
    assert await stored_rows() == 12 * 60
    assert await total_daily_counts() == 12 * 60
    assert await mismatched_daily_counts() == 0
