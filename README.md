# Audit Log Service

An audit log service for a multi-tenant SaaS platform. It has three HTTP APIs, all backed by PostgreSQL 16:

- **Ingest:** accepts batches of events.
- **List:** returns one tenant's events, newest first, with filters and stable pagination.
- **Summary:** returns events per action per day for the last 30 days.

A load script bulk-loads the provided 2M-event dataset. Design decisions are in [DESIGN.md](DESIGN.md).

## Prerequisites

- Docker with Compose v2 (`docker compose version`). Tested with Docker Desktop 29.8.2 and 3.82 GiB of memory given to Docker. About 4 GB is enough.
- Nothing else: Python, the database and the tests all run in containers.

## 1. Add the dataset

The dataset isn't in the repo. Put your copy at `data/audit_events.ndjson.gz`:

```bash
mkdir -p data
cp /path/to/audit_events.ndjson.gz data/
```

Check the download. The expected SHA-256 is `e968244bc33aaf0bc13c98d253f49819da1236620eae8cf51de31b7fb34d4dc1`.

```bash
shasum -a 256 data/audit_events.ndjson.gz   # macOS
sha256sum data/audit_events.ndjson.gz       # Linux
```

## 2. Start the service

```bash
docker compose up -d --build --wait
```

`--wait` returns once both containers are healthy, which takes about 10 seconds. This builds and starts two containers:

- `db`: PostgreSQL 16. The schema in `migrations/` is built into the image and applied on first start.
- `api`: FastAPI on port 8000. It starts once the database is healthy.

Check that the API is up:

```bash
curl localhost:8000/health
```

Expected: `{"status":"ok"}`.

## 3. Load the dataset

```bash
docker compose exec -T api python scripts/load.py < data/audit_events.ndjson.gz
```

This takes about **1.5 minutes** on the machine below: 1 min 32 s and 1 min 39 s in two runs.

- The file is piped in through stdin, so Docker never needs access to the project folder.
- Rows are written in batches of 10,000. Each batch is one SQL statement that skips any `event_id` already stored, so duplicates in the file are stored once.
- It prints progress, then a summary: lines read, inserted, duplicates, invalid lines, time taken and disk usage.
- Re-running it is safe; it inserts 0.

The data lives in a Docker volume. It survives `docker compose down`; `docker compose down -v` deletes it.

## 4. Call the APIs

The read APIs take the tenant from the `X-Tenant-ID` header. We assume an auth gateway in front of this service has already authenticated the user and sets this header.

**Ingest a batch.** The sample uses tenant `t_9999` and September 2026 times, so it doesn't affect the benchmark tenant or move the summary window:

```bash
curl -s -X POST localhost:8000/v1/events -H 'Content-Type: application/json' -d @- <<'EOF'
{"events": [
  {"event_id": "9f1c2a64-3b7e-4c1d-8a2f-0e5b6c7d8e01", "tenant_id": "t_9999", "actor_id": "usr_demo01", "actor_type": "user",
   "actor_email": "demo@example.com", "action": "invoice.create", "resource_type": "invoice", "resource_id": "inv_90001",
   "result": "success", "ip": "203.0.113.10", "metadata": {"amount": 120.5, "currency": "USD"}, "occurred_at": "2026-09-15T10:00:00.000Z"},
  {"event_id": "9f1c2a64-3b7e-4c1d-8a2f-0e5b6c7d8e02", "tenant_id": "t_9999", "actor_id": "key_demo02", "actor_type": "api_key",
   "actor_email": null, "action": "document.view", "resource_type": "document", "resource_id": "doc_777",
   "result": "success", "ip": "198.51.100.7", "metadata": {"request_id": "req_demo"}, "occurred_at": "2026-09-15T09:51:30.250Z"}
]}
EOF
```

The first call returns `{"received":2,"inserted":2,"duplicates":0}`. Run it again, as a retry would, and it returns `"inserted":0,"duplicates":2`.

**List events.** Newest first, 50 per page by default.

No filter:

```bash
curl -s -H 'X-Tenant-ID: t_0001' 'localhost:8000/v1/events'
```

Action filter:

```bash
curl -s -H 'X-Tenant-ID: t_0001' 'localhost:8000/v1/events?action=invoice.delete'
```

Time range (`from` inclusive, `to` exclusive):

```bash
curl -s -H 'X-Tenant-ID: t_0001' 'localhost:8000/v1/events?from=2026-09-01T00:00:00Z&to=2026-10-01T00:00:00Z'
```

Both filters together:

```bash
curl -s -H 'X-Tenant-ID: t_0001' 'localhost:8000/v1/events?action=invoice.delete&from=2026-09-01T00:00:00Z&to=2026-10-01T00:00:00Z'
```

**Next page.** Pass the previous response's `next_cursor`. The filters must be the same as on the first page.

```bash
CURSOR=$(curl -s -H 'X-Tenant-ID: t_0001' 'localhost:8000/v1/events?limit=50' | sed -E 's/.*"next_cursor":"([^"]+)".*/\1/')
curl -s -H 'X-Tenant-ID: t_0001' "localhost:8000/v1/events?limit=50&cursor=$CURSOR"
```

**Summary.** Events per action per day:

```bash
curl -s -H 'X-Tenant-ID: t_0001' 'localhost:8000/v1/summary'
```

## 5. Run the tests

```bash
docker compose exec api pytest -q
```

There are 39 tests covering the four constraints (including retries that arrive while the original request is still running), validation and the summary. They create their own `audit_test` database, so the loaded data is never touched.

## 6. Run the benchmarks

```bash
docker compose exec api python scripts/bench.py
```

This needs the dataset loaded. It prints the B1–B4 table below, plus how B2's page was reached.

## Results

### Benchmarks

Tenant `t_0001`, page size 50. Each figure is the median of 10 runs after one uncounted warm-up request.

Times are measured by an HTTP client inside the api container, so they include HTTP and JSON handling, not just the SQL. They were taken right after the load, so the cache was warm.

| ID | Query | Median (ms) | Min (ms) | Max (ms) |
|---|---|---|---|---|
| B1 | List, no filters, first page | 2.3 | 2.2 | 2.4 |
| B2 | List, no filters, events 100,001–100,050 | 2.2 | 2.2 | 6.1 |
| B3 | List, `action=invoice.delete`, 1–30 Sept 2026, first page | 2.4 | 2.3 | 2.4 |
| B4 | Summary, last 30 days | 2.1 | 2.0 | 2.3 |

A second run gave the same medians within 0.1 ms.

**How B2 is reached.** Pagination is keyset-based: each cursor points just after the last row of the previous page, so there's no "jump to offset". The benchmark walks 2,000 pages once with `next_cursor` (5.4 s, 2.7 ms per page) to get the cursor for page 2,001. It then times 10 requests for that page. The page's first and last events match rows 100,001 and 100,050 of a SQL `ORDER BY occurred_at DESC, event_id DESC`. The cursor seeks straight into the index, so page 2,001 costs the same as page 1.

### Load

| | |
|---|---|
| Load time | 89.2 s in the script; 1 min 32 s wall clock, including `docker compose exec` (22,428 lines/s) |
| Lines read | 2,001,032 |
| Inserted | 1,980,386 |
| Duplicates skipped | 20,646 |
| Invalid lines | 0 |

### Disk usage after the load

| | Size |
|---|---|
| `events` table | 745 MB |
| `events` indexes (primary key + 2 list indexes) | 417 MB |
| `daily_action_counts` (summary table, with its indexes) | 58 MB |
| Whole database | 1,227 MB |

That's about 615 bytes per event, including indexes.

### Machine

| | |
|---|---|
| CPU | Apple M1, 8 cores |
| RAM | 8 GB |
| OS | macOS 27.0.1 |
| Docker | Docker Desktop 29.8.2: 8 CPUs, memory limit 3.82 GiB |

## API reference

### `POST /v1/events`: ingest a batch

The body is `{"events": [...]}`, with 1–1,000 events. Each event has the brief's 12 fields:

- `event_id`: a UUID
- `tenant_id`: like `t_0042`
- `actor_id`: starts with `usr_`, `svc_` or `key_` to match `actor_type`; at most 255 characters
- `actor_type`: `user`, `service` or `api_key`
- `actor_email`: must be `null` unless `actor_type` is `user`; at most 320 characters
- `action`: one of the brief's 22 actions
- `resource_type`: one of the brief's 8 resource types
- `resource_id`: at most 255 characters
- `result`: `success` or `failure`
- `ip`: an IPv4 address
- `metadata`: a JSON object, at most 16 KB
- `occurred_at`: ISO 8601 with a timezone, stored as UTC; at most 1 hour ahead of the server's clock

Unknown fields are rejected.

| Status | Meaning |
|---|---|
| 200 | `{"received": n, "inserted": n, "duplicates": n}`. Sent only after the batch is committed. Duplicates are events already stored (retries) or repeated within the batch. |
| 422 | At least one event is invalid, and **nothing** from the batch is stored. `detail[].loc` names the bad event, e.g. `["body", "events", 1, "ip"]`. |

### `GET /v1/events`: list one tenant's events

| Parameter | Meaning |
|---|---|
| `X-Tenant-ID` header | Required, e.g. `t_0001` |
| `action` | Optional, one of the 22 actions |
| `from` | Optional, ISO 8601, inclusive. Times without a timezone are read as UTC. |
| `to` | Optional, ISO 8601, exclusive. Same timezone rule. |
| `limit` | 1–200, default 50 |
| `cursor` | `next_cursor` from the previous page |

The response is `{"data": [event, ...], "next_cursor": "..." | null}`. Events are ordered by `occurred_at` descending, then `event_id` descending. `next_cursor` is `null` on the last page.

**Stable paging.** The first page records a snapshot. Every later page in that listing only shows events that were stored before the snapshot. Events that arrive while someone is paging, including late events that would sort into pages already shown, can't cause duplicates or gaps. To see new events, start a new listing without a cursor.

A cursor is signed, and it only works with the same tenant and the same filters it was created for.

| Status | Meaning |
|---|---|
| 200 | One page |
| 400 | Missing or malformed `X-Tenant-ID`; malformed, tampered or mismatched cursor; `from` not before `to` |
| 422 | Unknown `action`, `limit` outside 1–200, or a time that can't be parsed |

### `GET /v1/summary`: events per action per day

Takes the `X-Tenant-ID` header, which is required.

The window is the 30 UTC days ending on the day of the newest event across all tenants. For this dataset, that's 2026-09-01 to 2026-09-30. Failures are counted along with successes.

The response looks like this:

```json
{"from": "2026-09-01", "to": "2026-09-30",
 "days": [{"date": "2026-09-01", "counts": {"document.view": 1742, "user.login": 1163}}]}
```

All 30 days are always present. A day with no events has `"counts": {}`.

Returns 400 for a missing or malformed `X-Tenant-ID`.

### `GET /health`

Returns `{"status": "ok"}`, or 503 if the database can't be reached.

## Project layout

```
app/main.py          HTTP endpoints and request checks
app/models.py        Event schema and validation (shared by ingest and load)
app/writes.py        The single write path: insert, skip duplicates, update daily counts
app/reads.py         List and summary queries
app/cursor.py        Signed pagination cursors
migrations/          Database schema, built into the db image
scripts/load.py      Bulk loader
scripts/bench.py     B1–B4 benchmarks
scripts/explore_dataset.py  One-off dataset checks (standard library only)
tests/               Integration tests
```

## Stopping

```bash
docker compose down
```

This stops the containers and keeps the loaded data. Add `-v` to delete the data too.
