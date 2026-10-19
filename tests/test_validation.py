"""Ingest validation: any invalid event rejects the whole batch with 422, and nothing is stored."""

from datetime import datetime, timedelta, timezone

import pytest

from tests.helpers import make_event, stored_rows


def from_now(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


INVALID_CHANGES = {
    "unknown action": lambda e: e.update(action="invoice.burn"),
    "bad IPv4": lambda e: e.update(ip="999.1.1.1"),
    "IPv6 instead of IPv4": lambda e: e.update(ip="2001:db8::1"),
    "email on a service actor": lambda e: e.update(actor_type="service", actor_id="svc_billing"),
    "actor_id prefix doesn't match type": lambda e: e.update(actor_id="key_123"),
    "timestamp without timezone": lambda e: e.update(occurred_at="2026-09-15T12:00:00"),
    "timestamp as a number": lambda e: e.update(occurred_at=1789000000),
    "timestamp 2 hours in the future": lambda e: e.update(occurred_at=from_now(hours=2)),
    "timestamp in year 2099": lambda e: e.update(occurred_at="2099-01-01T00:00:00.000Z"),
    "missing field": lambda e: e.pop("resource_id"),
    "unknown field": lambda e: e.update(surprise=1),
}


@pytest.mark.parametrize("case", INVALID_CHANGES)
async def test_invalid_event_rejects_the_whole_batch(client, case):
    bad = make_event()
    INVALID_CHANGES[case](bad)

    response = await client.post("/v1/events", json={"events": [make_event(), bad]})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:3] == ["body", "events", 1]  # names the bad event's index
    assert await stored_rows() == 0


@pytest.mark.parametrize("size", [0, 1001])
async def test_batch_size_limits(client, size):
    response = await client.post("/v1/events", json={"events": [make_event() for _ in range(size)]})
    assert response.status_code == 422


async def test_small_clock_skew_is_accepted(client):
    # A caller whose clock runs 30 minutes ahead is still within the 1-hour allowance
    response = await client.post("/v1/events", json={"events": [make_event(occurred_at=from_now(minutes=30))]})
    assert response.status_code == 200
    assert await stored_rows() == 1


async def test_timestamp_with_offset_is_stored_as_utc(client):
    event = make_event("t_0130", occurred_at="2026-09-15T14:00:00.000+02:00")
    await client.post("/v1/events", json={"events": [event]})

    listed = (await client.get("/v1/events", headers={"X-Tenant-ID": "t_0130"})).json()["data"]

    assert listed[0]["occurred_at"] == "2026-09-15T12:00:00.000Z"
