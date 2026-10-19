"""Constraint 1: calling services retry, so the same event can arrive more than once."""

from tests.helpers import ingest, make_event, stored_rows, total_daily_counts


async def test_retried_batch_is_stored_once(client):
    batch = [make_event(), make_event(), make_event()]

    first = await ingest(client, batch)
    retry = await ingest(client, batch)

    assert first == {"received": 3, "inserted": 3, "duplicates": 0}
    assert retry == {"received": 3, "inserted": 0, "duplicates": 3}
    assert await stored_rows() == 3
    assert await total_daily_counts() == 3  # the retry didn't inflate the summary


async def test_duplicate_inside_one_batch_is_stored_once(client):
    event = make_event()

    result = await ingest(client, [event, make_event(), event])

    assert result == {"received": 3, "inserted": 2, "duplicates": 1}
    assert await stored_rows() == 2
    assert await total_daily_counts() == 2
