from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import db
from app.models import IngestBatch
from app.writes import insert_events


@asynccontextmanager
async def lifespan(app):
    await db.connect()
    yield
    await db.close()


app = FastAPI(title="Audit Log Service", lifespan=lifespan)


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
