import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from app import cursor, db, reads
from app.models import Action, IngestBatch
from app.writes import insert_events

TENANT_RE = re.compile(r"^t_\d+$")


@asynccontextmanager
async def lifespan(app):
    await db.connect()
    yield
    await db.close()


app = FastAPI(title="Audit Log Service", lifespan=lifespan)


def tenant_from_header(x_tenant_id: Optional[str] = Header(None)) -> str:
    """The reading tenant comes only from this header, never from the query string or a cursor.

    Assumption: an auth gateway in front of this service authenticates the user and sets it.
    """
    if x_tenant_id is None or not TENANT_RE.match(x_tenant_id):
        raise HTTPException(400, "X-Tenant-ID header is required, e.g. X-Tenant-ID: t_0001")
    return x_tenant_id


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Query times without a timezone are taken as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@app.get("/health")
async def health():
    try:
        await db.pool().fetchval("SELECT 1")
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=503)
    return {"status": "ok"}


@app.post("/v1/events")
async def ingest(batch: IngestBatch):
    """Store a batch of events. Invalid batches never get here: FastAPI returns 422 naming the bad event's index.

    Safe to retry: events already stored are counted as duplicates, not errors.
    The reply is sent only after the transaction commits.
    """
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            inserted = await insert_events(conn, batch.events)
    received = len(batch.events)
    return {"received": received, "inserted": inserted, "duplicates": received - inserted}


@app.get("/v1/events")
async def list_events(
    tenant_id: str = Depends(tenant_from_header),
    action: Optional[Action] = None,
    from_: Optional[datetime] = Query(None, alias="from", description="inclusive"),
    to: Optional[datetime] = Query(None, description="exclusive"),
    limit: int = Query(50, ge=1, le=200),
    cursor_param: Optional[str] = Query(None, alias="cursor"),
):
    """One tenant's events, newest first, filtered by action and/or time range, one page at a time."""
    from_, to = as_utc(from_), as_utc(to)
    if from_ is not None and to is not None and from_ >= to:
        raise HTTPException(400, "'from' must be earlier than 'to'")
    filters = {
        "action": action,
        "from": from_.isoformat() if from_ else None,
        "to": to.isoformat() if to else None,
    }

    async with db.tenant_connection(tenant_id) as conn:
        if cursor_param is None:
            # First page: fix the set of events this listing will ever show
            snapshot, after = await conn.fetchval(reads.SNAPSHOT_SQL), None
        else:
            try:
                state = cursor.decode(cursor_param)
            except cursor.InvalidCursor as exc:
                raise HTTPException(400, str(exc))
            if state["tenant"] != tenant_id or state["filters"] != filters:
                raise HTTPException(400, "cursor doesn't belong to this tenant and these filters")
            snapshot = state["snapshot"]
            after = (datetime.fromisoformat(state["occurred_at"]), UUID(state["event_id"]))

        rows = await reads.list_events(conn, tenant_id, snapshot, limit, action, from_, to, after)

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = cursor.encode({
            "tenant": tenant_id,
            "filters": filters,
            "snapshot": snapshot,
            "occurred_at": last["occurred_at"].isoformat(),  # full precision, not the display format
            "event_id": str(last["event_id"]),
        })
    return {"data": [reads.event_to_json(row) for row in rows], "next_cursor": next_cursor}


@app.get("/v1/summary")
async def summary(tenant_id: str = Depends(tenant_from_header)):
    """Events per action per day for the last 30 days (ending on the newest day in the data)."""
    async with db.tenant_connection(tenant_id) as conn:
        return await reads.daily_summary(conn, tenant_id)
