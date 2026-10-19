"""Constraint 3: paging must not show duplicates or miss events while new events arrive."""

from tests.helpers import at_minute, headers, ingest, list_all, make_event

TENANT = "t_0120"


async def test_paging_is_stable_while_new_and_late_events_arrive(client):
    # 300 events, one per minute; newest first means minute 299 comes first
    existing = [make_event(TENANT, at_minute(i)) for i in range(300)]
    await ingest(client, existing)

    first = (await client.get("/v1/events", headers=headers(TENANT), params={"limit": 50})).json()
    seen = [e["event_id"] for e in first["data"]]  # minutes 299..250
    cursor = first["next_cursor"]

    # While the user is on page 1, more events arrive:
    newer = [make_event(TENANT, at_minute(300 + i)) for i in range(20)]               # newer than everything
    late_in_read_pages = [make_event(TENANT, at_minute(260.5 + i)) for i in range(20)]  # sort into page 1 (already read)...
    late_in_later_pages = [make_event(TENANT, at_minute(100.5 + i)) for i in range(20)]  # ...and into pages not read yet
    await ingest(client, newer + late_in_read_pages[:10] + late_in_later_pages)

    while cursor:
        page = (await client.get("/v1/events", headers=headers(TENANT), params={"limit": 50, "cursor": cursor})).json()
        seen += [e["event_id"] for e in page["data"]]
        cursor = page["next_cursor"]

    assert len(seen) == len(set(seen)), "an event was shown twice"
    assert set(seen) == {e["event_id"] for e in existing}, "the listing must be exactly what existed at page 1"

    # A fresh listing (a refresh) shows everything, including the events that arrived mid-paging
    assert len(await list_all(client, TENANT, limit=50)) == 300 + 20 + 10 + 20


async def test_pages_follow_newest_first_order_with_ties(client):
    # Same occurred_at for every event: event_id must break the tie so no event is skipped or repeated
    events = [make_event(TENANT, "2026-09-15T12:00:00.000Z") for _ in range(7)]
    await ingest(client, events)

    ids = await list_all(client, TENANT, limit=3)

    assert ids == sorted((e["event_id"] for e in events), reverse=True)
