"""Summary API: events per action per day, last 30 days ending on the newest day in the data."""

from tests.helpers import headers, ingest, make_event

TENANT = "t_0140"


async def test_window_is_zero_filled_and_matches_raw_counts(client):
    await ingest(client, [
        make_event(TENANT, "2026-08-31T23:59:59.999Z", "user.login"),       # day before the window: excluded
        make_event(TENANT, "2026-09-01T00:00:00.000Z", "user.login"),       # first day of the window
        make_event(TENANT, "2026-09-01T10:00:00.000Z", "user.login"),
        make_event(TENANT, "2026-09-30T12:00:00.000Z", "invoice.create"),   # newest day = window end
        make_event(TENANT, "2026-09-30T13:00:00.000Z", "invoice.create", result="failure"),  # failures count too
    ])

    summary = (await client.get("/v1/summary", headers=headers(TENANT))).json()

    assert (summary["from"], summary["to"]) == ("2026-09-01", "2026-09-30")
    assert len(summary["days"]) == 30
    assert summary["days"][0] == {"date": "2026-09-01", "counts": {"user.login": 2}}
    assert summary["days"][-1] == {"date": "2026-09-30", "counts": {"invoice.create": 2}}
    assert all(day["counts"] == {} for day in summary["days"][1:-1])  # quiet days present, empty


async def test_window_ends_on_newest_day_across_all_tenants(client):
    await ingest(client, [
        make_event(TENANT, "2026-09-10T08:00:00.000Z"),
        make_event("t_0141", "2026-10-05T08:00:00.000Z"),  # another tenant's newer event moves the window
    ])

    summary = (await client.get("/v1/summary", headers=headers(TENANT))).json()

    assert (summary["from"], summary["to"]) == ("2026-09-06", "2026-10-05")
    counts = {day["date"]: day["counts"] for day in summary["days"]}
    assert counts["2026-09-10"] == {"document.view": 1}
    assert counts["2026-10-05"] == {}  # the other tenant's event isn't counted here
