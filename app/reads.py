"""Read queries for the List and Summary APIs. Every query filters on one tenant_id (constraint 4)."""

import json
from datetime import timedelta, timezone

# The newest ingest_seq handed out so far. A listing only shows rows at or below this number, so
# events stored after page 1 (including late ones that sort into pages already seen) can't shift
# later pages: no duplicates, no gaps (constraint 3). Reading the sequence is O(1), unlike max().
SNAPSHOT_SQL = "SELECT last_value FROM events_ingest_seq_seq"

EVENT_COLUMNS = """event_id, tenant_id, actor_id, actor_type, actor_email, action,
                   resource_type, resource_id, result, ip, metadata, occurred_at"""


async def list_events(conn, tenant_id, snapshot, limit, action=None, from_=None, to=None, after=None):
    """Fetch up to limit + 1 events, newest first; the extra row tells the caller there's a next page.

    `after` is (occurred_at, event_id) of the previous page's last row (keyset pagination).
    """
    conditions = ["tenant_id = $1", "ingest_seq <= $2"]
    params = [tenant_id, snapshot]

    def placeholder(value):
        # Values always travel as query parameters; only "$n" goes into the SQL text.
        params.append(value)
        return f"${len(params)}"

    if action is not None:
        conditions.append(f"action = {placeholder(action)}")
    if from_ is not None:
        conditions.append(f"occurred_at >= {placeholder(from_)}")
    if to is not None:
        conditions.append(f"occurred_at < {placeholder(to)}")
    if after is not None:
        # Row comparison = "strictly after the last row in (occurred_at DESC, event_id DESC) order"
        conditions.append(f"(occurred_at, event_id) < ({placeholder(after[0])}::timestamptz, {placeholder(after[1])}::uuid)")

    sql = f"""
        SELECT {EVENT_COLUMNS}
        FROM events
        WHERE {' AND '.join(conditions)}
        ORDER BY occurred_at DESC, event_id DESC
        LIMIT {placeholder(limit + 1)}
    """
    return await conn.fetch(sql, *params)


def format_timestamp(value):
    """2026-09-14T08:31:07.412Z, the brief's format."""
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def event_to_json(row):
    return {
        "event_id": str(row["event_id"]),
        "tenant_id": row["tenant_id"],
        "actor_id": row["actor_id"],
        "actor_type": row["actor_type"],
        "actor_email": row["actor_email"],
        "action": row["action"],
        "resource_type": row["resource_type"],
        "resource_id": row["resource_id"],
        "result": row["result"],
        "ip": str(row["ip"]),
        "metadata": json.loads(row["metadata"]),
        "occurred_at": format_timestamp(row["occurred_at"]),
    }


async def daily_summary(conn, tenant_id, days=30):
    """Events per action per UTC day for the `days` days ending on the newest day in the data."""
    # Newest day across all tenants (brief: "the newest event in your data"). Row-level security
    # hides other tenants' rows here, so this goes through a function that may look at all of
    # them but returns only the date (migrations/002). It uses the day index.
    anchor = await conn.fetchval("SELECT newest_event_day()")
    if anchor is None:
        return {"from": None, "to": None, "days": []}
    start = anchor - timedelta(days=days - 1)

    rows = await conn.fetch(
        """
        SELECT day, action, count
        FROM daily_action_counts
        WHERE tenant_id = $1 AND day BETWEEN $2 AND $3
        ORDER BY day, action
        """,
        tenant_id, start, anchor,
    )

    # Every day in the window is present, so the chart has no gaps; quiet days get empty counts.
    counts_by_day = {start + timedelta(days=i): {} for i in range(days)}
    for row in rows:
        counts_by_day[row["day"]][row["action"]] = row["count"]
    return {
        "from": start.isoformat(),
        "to": anchor.isoformat(),
        "days": [{"date": day.isoformat(), "counts": counts} for day, counts in counts_by_day.items()],
    }
