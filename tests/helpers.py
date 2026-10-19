"""Small helpers shared by the tests."""

import uuid
from datetime import datetime, timedelta, timezone

from app import db


def make_event(tenant="t_0100", occurred_at="2026-09-15T12:00:00.000Z", action="document.view", **overrides):
    """A valid event; override any field."""
    event = {
        "event_id": str(uuid.uuid4()),
        "tenant_id": tenant,
        "actor_id": "usr_tester",
        "actor_type": "user",
        "actor_email": "tester@example.com",
        "action": action,
        "resource_type": action.split(".")[0],
        "resource_id": "res_1",
        "result": "success",
        "ip": "203.0.113.5",
        "metadata": {"request_id": "req_test"},
        "occurred_at": occurred_at,
    }
    event.update(overrides)
    return event


def at_minute(minutes, base=datetime(2026, 9, 10, tzinfo=timezone.utc)):
    """ISO timestamp `minutes` after base, in the brief's format."""
    return (base + timedelta(minutes=minutes)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def headers(tenant):
    return {"X-Tenant-ID": tenant}


async def ingest(client, events):
    response = await client.post("/v1/events", json={"events": events})
    assert response.status_code == 200, response.text
    return response.json()


async def list_all(client, tenant, **params):
    """Follow next_cursor to the end; return every event_id in the order received."""
    ids, cursor = [], None
    while True:
        page_params = {**params, "cursor": cursor} if cursor else params
        response = await client.get("/v1/events", headers=headers(tenant), params=page_params)
        assert response.status_code == 200, response.text
        body = response.json()
        ids += [e["event_id"] for e in body["data"]]
        cursor = body["next_cursor"]
        if cursor is None:
            return ids


async def stored_rows():
    return await db.pool().fetchval("SELECT count(*) FROM events")


async def total_daily_counts():
    return await db.pool().fetchval("SELECT coalesce(sum(count), 0) FROM daily_action_counts")
