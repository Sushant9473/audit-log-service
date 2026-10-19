from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import db


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
