# NOTES

## Brief facts
- Prod scale: ~800 tenants; top 5 ≈ 60% of events; ~20M events/day avg; peak ~3,000 events/s; retention 1 year
- Derived: ~231 events/s avg; ~7.3B rows/year
- Dataset: audit_events.ndjson.gz; ~2M lines; 173 MB gz / ~1 GB raw; occurred_at 2026-08-01 → 2026-09-30 UTC
- Dataset SHA-256: e968244bc33aaf0bc13c98d253f49819da1236620eae8cf51de31b7fb34d4dc1
- File order = arrival order, not occurred_at order
- Duplicates in file share event_id; DB must hold each event once
- "Last 30 days" = 30 days before newest event in the data
- Benchmarks: via API; median of 10 runs; page size 50; B1–B3 List t_0001, B4 Summary t_0001
- Grading gates: fresh clone → docker compose up → load script must work; DESIGN.md self-written
- Time budget: 5 hours total (brief allows 3 days)

## Dataset facts (scripts/explore_dataset.py, 2026-10-19, ~38 s run)
- SHA-256 matches
- Lines: 2,001,032; unique event_ids: 1,980,386; duplicate lines: 20,646 (~1%)
- Every duplicate byte-identical to its first copy → same occurred_at, same tenant_id
- Bad JSON lines: 0; rule violations: 0 (no missing/extra fields, all enums valid, all IPv4, all UTC ms timestamps)
- actor_email: set for every user, null for every service/api_key
- resource_type always = action prefix (invoice.delete → invoice)
- Tenants: 800; top 5 = 60.0% (t_0001 20.0%, t_0002 14.0%, t_0003 11.0%, t_0004 8.0%, t_0005 7.0%); t_0006 only 0.8%
- t_0001: 396,568 events → B2 page 2,001 exists
- t_0001 events in summary window (2026-09-01..2026-09-30): 198,615
- t_0001 invoice.delete in Sept 2026: 4,265 → B3 first page is full
- occurred_at range: 2026-08-01T00:00:11.379Z → 2026-09-30T23:59:47.830Z
- Lateness, first copies: 21.2% behind newest seen; p99 5.4 min; max 9.99 min → matches brief's 10 min
- Lateness, retries: p50 17 s; p99 9.7 min; max 19.2 min; 67 retries > 10 min late → dedup can't rely on a time window
- Actions: document.view 444,862 (most) … api_key.revoke 5,067 (least); all 22 present
- actor_type: user 1,486,303 / service 296,587 / api_key 197,496
- result: success 1,904,308 / failure 76,078 (3.8%)
- metadata: avg 196 bytes, max 330 bytes
- Explore script peak memory: ~420 MB (one hash per event_id)

## Decisions
1. DB: PostgreSQL 16, one events table, no partitioning at 2M rows; monthly partitions on occurred_at = production plan, described in DESIGN.md only
   - Rejected: ClickHouse (dedup only at background merges → constraint 1 harder); partitioning now (time)
2. events: 12 fields + received_at timestamptz default now() + ingest_seq bigint identity; event_id uuid PRIMARY KEY (= dedup rule); ip inet; metadata jsonb; occurred_at timestamptz; CHECKs on actor_type, action, resource_type, result
   - Rejected: separate dedup table (extra write per event; PK already does it)
   - Data check: duplicates identical → (event_id, occurred_at) unique key would also work once partitioned
3. Indexes, nothing else on events: (tenant_id, occurred_at DESC, event_id DESC) for plain list; (tenant_id, action, occurred_at DESC, event_id DESC) for action filter; time range uses the same two
   - Rejected: separate time-range index (range is a slice of the occurred_at column already in both)
4. Summary table: daily_action_counts(tenant_id, day date, action, count bigint, PK (tenant_id, day, action)) + index on day; day = UTC date of occurred_at
   - Rejected: GROUP BY on events per request (t_0001 has ~199k events in window; cost grows with tenant size)
5. One shared write statement for ingest + load: INSERT … ON CONFLICT (event_id) DO NOTHING RETURNING → add only returned rows to daily_action_counts (ON CONFLICT DO UPDATE count = count + excluded.count)
   - Effect: retries never double-count; late events land on their own day
   - Rejected: rebuilding counts with a periodic job (stale summary; second code path)
6. Ingest: POST /v1/events, body {"events": [...]}, 1–1,000 events; any invalid event → whole batch 422 with failing index; one transaction; reply {"received", "inserted", "duplicates"} only after commit; late, out-of-order, future-dated events accepted
   - Rejected: partial success (more complex response); async queue before DB (time; harder "stored" guarantee)
   - Changed from step 0 suggestion (per-event reject) → all-or-nothing, for time
7. Read-API tenant: required X-Tenant-ID header, assumed set by an auth gateway that already authenticated the user; every read query has WHERE tenant_id = $1
   - Rejected: per-tenant API keys (time); row-level security (time; production hardening)
8. List: GET /v1/events?action=&from=&to=&limit=&cursor=; order (occurred_at DESC, event_id DESC); from inclusive, to exclusive, ISO 8601; limit default 50, max 200
   - Rejected: ordering by arrival (brief: newest first = when it happened)
9. Pagination: keyset cursor = base64url JSON {last occurred_at, last event_id, snapshot ingest_seq, tenant, filters} + HMAC-SHA256 (secret from env, compose default); snapshot = ingest_seq sequence last value at page 1; every page adds ingest_seq <= snapshot; mismatched signature/tenant/filters → 400; new/late events show after refresh
   - Rejected: offset pagination (dupes/misses under inserts; deep pages slow)
10. Summary: GET /v1/summary; anchor = newest day in daily_action_counts (all tenants); window = 30 UTC days ending on anchor day, inclusive; every day present, zeros included; success + failure counted
    - Rejected: rolling 720 h window (daily buckets can't split a day); per-tenant anchor (brief says "newest event in your data")
11. Load: scripts/load.py inside api container; dataset piped on stdin: docker compose exec -T api python scripts/load.py < data/audit_events.ndjson.gz; stream gzip; batches of 10,000 → COPY into temp table → shared write statement; bad lines skipped + counted; re-runnable
    - Rejected: posting batches to the Ingest API (HTTP + JSON overhead; brief allows direct bulk load)
    - Rejected: bind-mounting ./data (Docker Desktop on macOS was blocked from ~/Desktop: "mkdir /host_mnt/Users/sushant/Desktop: operation not permitted"; same risk on grader's machine)
12. Schema setup: migrations/ copied into a db image (db.Dockerfile, FROM postgres:16) at /docker-entrypoint-initdb.d; runs once on a fresh volume; schema change → docker compose down -v
    - Rejected: bind-mounting migrations/ (same macOS block); migration tool like Alembic (time)
13. Compose: no folder mounts at all; db port not published to host (psql via docker compose exec db psql -U audit); api on :8000; db healthcheck pg_isready over TCP so api starts only after schema init
    - Rejected: publishing 5432 (clashes with a local Postgres on grader's machine)
14. Postgres settings for ~4 GB Docker: shared_buffers 512MB, work_mem 32MB, maintenance_work_mem 512MB, max_wal_size 2GB, shm_size 256mb
15. GET /health runs SELECT 1; 503 if DB unreachable
16. One write function app/writes.py insert_events() for ingest + load; one SQL statement per batch: INSERT … SELECT FROM unnest(12 arrays) ON CONFLICT DO NOTHING RETURNING → rollup upsert → count
    - Rejected: COPY into temp table for load (second code path; a temp table per ingest request = catalog churn at 3,000 ev/s)
17. No deadlocks between concurrent batches: rows sorted by event_id before insert; rollup rows upserted ORDER BY tenant, day, action → locks always taken in the same order
    - Rejected: retrying on deadlock errors
18. Validation (app/models.py, Pydantic): unknown fields rejected (extra="forbid"); occurred_at must be an ISO 8601 string with timezone, stored as UTC; actor_id prefix must match actor_type (usr_/svc_/key_); actor_email null unless user; batch 1–1,000 events else 422 (not 413)
    - Rejected: silently ignoring unknown fields (audit log shouldn't drop data quietly)
19. Ingest reply always 200 after commit, even if all duplicates; duplicates = received − inserted (repeats inside a batch included)
20. Load: each batch commits on its own (crash mid-load → just re-run); VACUUM (ANALYZE) after load so stats + visibility map are fresh (index-only scans)
    - Rejected: waiting for autovacuum (benchmarks right after load would get stale plans)
21. Dockerfile sets PYTHONPATH=/app so scripts/*.py can import app
22. Read-API errors: missing/bad X-Tenant-ID → 400; bad cursor → 400; from >= to → 400; unknown action, limit outside 1–200 → 422 (FastAPI validation)
23. Query times (from/to) without timezone → taken as UTC
24. Cursor = base64url(JSON).base64url(HMAC-SHA256); holds tenant, filters (action, from, to as UTC ISO), snapshot, last occurred_at (full µs precision), last event_id; limit not in cursor (may change between pages)
    - Checked against request: tenant + filters must match → else 400; signature checked first with hmac.compare_digest
25. Snapshot = SELECT last_value FROM events_ingest_seq_seq (O(1))
    - Rejected: max(ingest_seq) (no index on ingest_seq → full scan)
    - Edge case: a transaction holding a seq ≤ snapshot that commits after page 1 may appear on a later page or not at all in that listing; never twice; it wasn't visible when the listing began
    - ON CONFLICT DO NOTHING still uses up sequence numbers → gaps (last_value 3,960,938 after 2 loads); harmless
26. List response {"data": [...], "next_cursor": str | null}; events = the 12 brief fields, occurred_at as 2026-09-14T08:31:07.412Z; next_cursor null on last page (limit + 1 rows fetched to know)
27. Summary response {"from", "to", "days": [{"date", "counts": {action: n}}]}; all 30 days present; actions with 0 omitted inside a day; empty DB → from/to null
28. List SQL built from optional conditions; only "$n" placeholders go into SQL text, values always bound parameters (no injection)
29. Tests: separate database audit_test, dropped + recreated from migrations/001_schema.sql at start of every run; tables truncated before each test; app run in-process via httpx ASGITransport; command docker compose exec api pytest -q
    - Rejected: testing against the loaded data (would change counts and benchmark data); separate test image (time) → pytest, pytest-asyncio, httpx in requirements.txt
30. Test coverage: duplicates (retried batch, repeat inside batch, counts not inflated); late + out of order across midnight (list order, day buckets); paging with 20 newer + 20 late events arriving mid-paging (no repeats, exactly the page-1 set, refresh shows all); ties on occurred_at; tenant isolation (list, filters, summary, stolen/edited cursor, bad header); 9 invalid-event cases + batch size 0 / 1001; offset timestamps stored as UTC; summary zero-fill, window edges, failures counted, anchor across tenants
31. Benchmarks: scripts/bench.py, httpx client inside api container → http://localhost:8000, one keep-alive connection; 1 warm-up + 10 timed runs each; times include HTTP + JSON, not just SQL
32. B2 reached by walking 2,000 pages with next_cursor once (keyset can't jump to an offset), then timing 10 requests of page 2,001 with that cursor
    - Rejected: adding an offset parameter just for B2 (offset = slow deep pages + unstable under inserts)

## Assumptions
- event_id unique globally, not per tenant
- Duplicate with different payload → first write wins (none in dataset)
- Ingest callers = trusted internal services; one batch may mix tenants
- actor_email must be null unless actor_type = user; null allowed for user (brief type: string or null)
- Timestamps with non-Z offset accepted and converted to UTC
- Events > 10 min late (e.g. retries, up to 19 min in data) and future-dated events accepted
- Summary anchor moves as new events arrive; a future-dated event moves it too
- B3 "1–30 Sept 2026" = from 2026-09-01T00:00:00Z (incl.) to 2026-10-01T00:00:00Z (excl.)
- B2 = page 2,001 (events 100,001–100,050); timed = only that page's request; the 2,000-page walk reported separately
- Benchmarks warm (1 warm-up request); client inside api container → localhost:8000
- Disk usage reported = events table + its indexes + daily_action_counts + whole DB
- Retention (1 year) = design only, not implemented
- No UI; production scale explained, not built
- Demo/README ingest uses tenant t_9999 + Sept 2026 timestamps so it doesn't move the summary window

## Measurements
- Docker Desktop: Docker 29.8.2, Compose v5.6.0; 8 CPUs, 3.82 GiB memory limit (4,106,604,544 bytes)
- Step 2 checks: fresh volume → schema created by initdb; /health ok; same event_id inserted twice → INSERT 0 1 then INSERT 0 0; service actor with email → rejected by events_email_only_for_users
- Full load (fresh DB, batch 10,000): 89.2 s in script; 1 min 32 s wall clock incl. docker exec; 22,428 lines/s (25k/s at start → 22k/s at end as indexes grow)
- Full load result: 2,001,032 lines; 1,980,386 inserted; 20,646 duplicates skipped; 0 invalid; VACUUM ANALYZE 1.8 s
- Re-run of load: 0 inserted; 55.9 s; counts unchanged
- Disk after load: events table 745 MB (781,041,664 B); events indexes 417 MB (436,805,632 B, PK + 2 list indexes); daily_action_counts 58 MB incl. indexes (60,489,728 B; 335,307 rows); whole DB 1,227 MB (1,286,175,767 B)
- Per row: ~394 B table + ~221 B indexes ≈ 615 B/event
- Rollup check: sum(daily_action_counts.count) = count(events) = 1,980,386; 0 buckets differ from GROUP BY over events
- Demo ingest: 2 events, tenant t_9999, 2026-09-15 → events now 1,980,388; retry → inserted 0, duplicates 2; bad IP at index 1 → 422 loc ["body","events",1,"ip"], nothing stored
- EXPLAIN (ANALYZE, BUFFERS), t_0001, warm:
  - B1 page 1: Index Scan events_tenant_time_idx; 0.29 ms; 53 buffers; no Sort
  - Cursor at row 100,000 (B2 shape): Index Scan events_tenant_time_idx, ROW(occurred_at, event_id) < ROW(...) inside Index Cond; 0.64 ms; no Sort
  - action=invoice.delete + Sept (B3): Index Scan events_tenant_action_time_idx, all 4 conditions in Index Cond; 1.05 ms; no Sort
  - Summary anchor max(day): Index Only Scan Backward daily_action_counts_day_idx; 0.06 ms
  - Summary window: Bitmap scan on daily_action_counts_pkey + Sort of 660 rows; 2.4 ms
  - ingest_seq <= snapshot = Filter on fetched rows (not in index); no Seq Scan anywhere
  - Generic prepared-statement plans (asyncpg) use the same indexes
- Ad-hoc HTTP timings from host (not the benchmark): page 1 43 ms (first call), pages 2–3 6–11 ms, filtered 8.8 ms, summary 5.8 ms
- Benchmarks, run 1 (2026-10-19, warm, after load + VACUUM ANALYZE), page size 50, t_0001:

  | ID | Query | Median (ms) | Min (ms) | Max (ms) |
  |---|---|---|---|---|
  | B1 | List, no filters, first page | 2.3 | 2.2 | 2.4 |
  | B2 | List, no filters, events 100,001–100,050 (page 2,001) | 2.2 | 2.2 | 6.1 |
  | B3 | List, action=invoice.delete, 1–30 Sept 2026, first page | 2.4 | 2.3 | 2.4 |
  | B4 | Summary, last 30 days | 2.1 | 2.0 | 2.3 |

- Benchmarks, run 2 (same conditions): B1 2.3 / B2 2.2 / B3 2.3 / B4 2.0 ms median → stable within 0.1 ms
- B2 walk: 2,000 pages (100,000 events) in 5.4 s ≈ 2.7 ms per page
- B2 check: page 2,001 first event bbb9e108-… (2026-09-15T18:54:17.459Z) = SQL row_number 100,001; last 7fe5830c-… (18:35:13.685Z) = row_number 100,050
- Host: Apple M1, 8 cores (8 physical), 8 GiB RAM, macOS 27.0.1
- Docker Desktop 29.8.2, aarch64: 8 CPUs, memory limit 3.82 GiB (4,106,604,544 bytes)
- Container memory after benchmarks: db 739 MiB, api 46 MiB
- Tests: 29 passed in ~1.5 s
- Mutation checks: snapshot filter removed → pagination test fails ("listing must be exactly what existed at page 1"); cursor tenant check removed → isolation test fails; code restored → 29 pass
- Main DB after test runs: 1,980,388 rows (untouched)
- Step 4 checks: 3 pages via next_cursor = SQL ORDER BY result, 0 overlap; cursor from other tenant / other filters / tampered / garbage → 400; summary 2026-09-15 for t_0001 = GROUP BY on events (7,728 events, 22 actions); 30 days returned
