"""The one write path into storage, used by both the Ingest API and the load script."""

import json

# One statement, one round trip per batch:
#   1. insert the events; a retried event_id hits the primary key and is skipped (constraint 1)
#   2. add only the rows that were really inserted to the per-day counts, so a retry never
#      double-counts, and a late event lands on its own day (constraint 2)
#   3. return how many were inserted
# Data-modifying CTEs always run, even though step 2's result isn't selected.
INSERT_EVENTS_SQL = """
WITH inserted AS (
    INSERT INTO events (event_id, tenant_id, actor_id, actor_type, actor_email, action,
                        resource_type, resource_id, result, ip, metadata, occurred_at)
    SELECT * FROM unnest($1::uuid[], $2::text[], $3::text[], $4::text[], $5::text[], $6::text[],
                         $7::text[], $8::text[], $9::text[], $10::inet[], $11::jsonb[], $12::timestamptz[])
    ON CONFLICT (event_id) DO NOTHING
    RETURNING tenant_id, action, occurred_at
), counted AS (
    INSERT INTO daily_action_counts AS d (tenant_id, day, action, count)
    SELECT tenant_id, (occurred_at AT TIME ZONE 'UTC')::date, action, count(*)
    FROM inserted
    GROUP BY 1, 2, 3
    ORDER BY 1, 2, 3  -- lock count rows in the same order in every transaction: no deadlocks
    ON CONFLICT (tenant_id, day, action) DO UPDATE SET count = d.count + EXCLUDED.count
)
SELECT count(*) FROM inserted
"""


async def insert_events(conn, events):
    """Store a batch of validated Events inside the caller's transaction. Returns how many were new."""
    # Keep the first copy of any event_id repeated inside the batch, then sort by event_id so
    # concurrent batches take primary-key locks in the same order (no deadlocks).
    unique = {}
    for event in events:
        unique.setdefault(event.event_id, event)
    rows = sorted(unique.values(), key=lambda e: e.event_id)

    return await conn.fetchval(
        INSERT_EVENTS_SQL,
        [e.event_id for e in rows],
        [e.tenant_id for e in rows],
        [e.actor_id for e in rows],
        [e.actor_type for e in rows],
        [e.actor_email for e in rows],
        [e.action for e in rows],
        [e.resource_type for e in rows],
        [e.resource_id for e in rows],
        [e.result for e in rows],
        [e.ip for e in rows],
        [json.dumps(e.metadata, separators=(",", ":")) for e in rows],
        [e.occurred_at for e in rows],
    )
