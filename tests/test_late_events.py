"""Constraint 2: events can arrive up to 10 minutes late and out of order."""

from tests.helpers import headers, ingest, make_event


async def test_late_and_out_of_order_events_land_in_the_right_place(client):
    tenant = "t_0110"
    e_0001 = make_event(tenant, "2026-09-20T00:01:30.000Z")
    e_0002 = make_event(tenant, "2026-09-20T00:02:00.000Z")
    e_0003 = make_event(tenant, "2026-09-20T00:03:00.000Z")
    e_2355 = make_event(tenant, "2026-09-19T23:55:00.000Z")  # previous day, arrives ~8 minutes late
    e_2358 = make_event(tenant, "2026-09-19T23:58:00.000Z")

    # Arrival order is scrambled, and the pre-midnight events come after post-midnight ones.
    await ingest(client, [e_0001, e_0003])
    await ingest(client, [e_2355, e_0002, e_2358])

    response = await client.get("/v1/events", headers=headers(tenant))
    listed = [e["event_id"] for e in response.json()["data"]]
    assert listed == [e["event_id"] for e in (e_0003, e_0002, e_0001, e_2358, e_2355)]  # newest first

    summary = (await client.get("/v1/summary", headers=headers(tenant))).json()
    by_date = {day["date"]: day["counts"] for day in summary["days"]}
    assert by_date["2026-09-19"] == {"document.view": 2}  # late events counted on their own day
    assert by_date["2026-09-20"] == {"document.view": 3}
