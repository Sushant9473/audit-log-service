"""Constraint 4: a tenant must never see another tenant's events."""

import pytest

from tests.helpers import headers, ingest, make_event

A, B = "t_0301", "t_0302"


async def test_tenant_sees_only_its_own_events_in_list_and_summary(client):
    a_events = [make_event(A, action="invoice.delete") for _ in range(5)]
    b_events = [make_event(B, action="invoice.delete") for _ in range(3)]
    await ingest(client, a_events + b_events)  # one batch may mix tenants
    b_ids = {e["event_id"] for e in b_events}

    for params in ({}, {"action": "invoice.delete"}, {"from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"}):
        response = await client.get("/v1/events", headers=headers(B), params=params)
        assert {e["event_id"] for e in response.json()["data"]} == b_ids

    summary = (await client.get("/v1/summary", headers=headers(B))).json()
    assert sum(sum(day["counts"].values()) for day in summary["days"]) == 3


async def test_cursor_cannot_be_reused_by_another_tenant_or_edited(client):
    await ingest(client, [make_event(A) for _ in range(3)])
    cursor = (await client.get("/v1/events", headers=headers(A), params={"limit": 1})).json()["next_cursor"]

    stolen = await client.get("/v1/events", headers=headers(B), params={"limit": 1, "cursor": cursor})
    tampered = await client.get("/v1/events", headers=headers(A), params={"limit": 1, "cursor": "x" + cursor[1:]})

    assert stolen.status_code == 400
    assert tampered.status_code == 400


@pytest.mark.parametrize("path", ["/v1/events", "/v1/summary"])
@pytest.mark.parametrize("tenant_header", [None, "", "acme", "t_0001; DROP TABLE events"])
async def test_missing_or_malformed_tenant_header_is_rejected(client, path, tenant_header):
    response = await client.get(path, headers={} if tenant_header is None else {"X-Tenant-ID": tenant_header})
    assert response.status_code == 400
