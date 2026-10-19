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
6. Ingest: POST /v1/events, body {"events": [...]}, 1–1,000 events; any invalid event → whole batch 422 with failing index; one transaction; reply {"received", "inserted", "duplicates"} only after commit; late and out-of-order events accepted; occurred_at > 1 h in the future rejected (decision 35)
   - Rejected: partial success (more complex response); async queue before DB (time; harder "stored" guarantee)
   - Changed from step 0 suggestion (per-event reject) → all-or-nothing, for time
7. Read-API tenant: required X-Tenant-ID header, assumed set by an auth gateway that already authenticated the user; every read query has WHERE tenant_id = $1
   - Rejected: per-tenant API keys (time)
   - Row-level security first rejected for time, later added as a 2nd layer → decision 38
8. List: GET /v1/events?action=&from=&to=&limit=&cursor=; order (occurred_at DESC, event_id DESC); from inclusive, to exclusive, ISO 8601; limit default 50, max 200
   - Rejected: ordering by arrival (brief: newest first = when it happened)
9. Pagination: keyset cursor = base64url JSON {last occurred_at, last event_id, snapshot ingest_seq, tenant, filters} + HMAC-SHA256 (secret from env, compose default); snapshot = ingest_seq sequence last value at page 1; every page adds ingest_seq <= snapshot; mismatched signature/tenant/filters → 400; new/late events show after refresh
   - Rejected: offset pagination (dupes/misses under inserts; deep pages slow)
10. Summary: GET /v1/summary; anchor = newest day in daily_action_counts (all tenants); window = 30 UTC days ending on anchor day, inclusive; every day present, zeros included; success + failure counted
    - Rejected: rolling 720 h window (daily buckets can't split a day); per-tenant anchor (brief says "newest event in your data")
11. Load: scripts/load.py inside api container; dataset piped on stdin: docker compose exec -T api python scripts/load.py < data/audit_events.ndjson.gz; stream gzip; batches of 10,000 → shared insert_events() statement (unnest arrays, decision 16; the COPY-into-temp-table plan was dropped); bad lines skipped + counted; re-runnable
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
    - Evidence: test_concurrent_writes.py; with the event_id sort removed → DeadlockDetectedError in 3/3 runs; restored → pass
    - Rollup ORDER BY = safeguard only: removed → no deadlock in 5 runs (aggregate happened to emit groups in the same order; SQL doesn't promise that) → kept
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
29. Tests: separate database audit_test, dropped + recreated from all migrations/*.sql in order at start of every run; tables truncated before each test; app run in-process via httpx ASGITransport; command docker compose exec api pytest -q
    - Rejected: testing against the loaded data (would change counts and benchmark data); separate test image (time) → pytest, pytest-asyncio, httpx in requirements.txt
30. Test coverage: duplicates (retried batch, repeat inside batch, counts not inflated); concurrent retries (same batch from 10 callers at once → each event stored by exactly one request; 12 overlapping batches in random orders → no deadlock; 12 disjoint batches on the same 60 count rows → no lost increments); late + out of order across midnight (list order, day buckets); paging with 20 newer + 20 late events arriving mid-paging (no repeats, exactly the page-1 set, refresh shows all); ties on occurred_at; tenant isolation (list, filters, summary, stolen/edited cursor, bad header); 9 invalid-event cases + batch size 0 / 1001; offset timestamps stored as UTC; summary zero-fill, window edges, failures counted, anchor across tenants
31. Benchmarks: scripts/bench.py, httpx client inside api container → http://localhost:8000, one keep-alive connection; 1 warm-up + 10 timed runs each; times include HTTP + JSON, not just SQL
32. B2 reached by walking 2,000 pages with next_cursor once (keyset can't jump to an offset), then timing 10 requests of page 2,001 with that cursor
    - Rejected: adding an offset parameter just for B2 (offset = slow deep pages + unstable under inserts)
33. Start command docker compose up -d --build --wait; api healthcheck = python urllib GET /health (slim image has no curl) → command returns only when both containers answer
    - Rejected: telling the grader to wait or retry curl
34. README sample ingest = tenant t_9999, 2026-09-15 times (doesn't touch t_0001, doesn't move summary window); next-page example extracts next_cursor with sed (no jq/python needed on host)
35. Ingest + load reject occurred_at more than 1 h ahead of server time (MAX_FUTURE_SKEW in app/models.py)
    - Why: Summary window ends on newest day across all tenants → one event dated 2099 would blank every tenant's dashboard
    - Rejected: clamping the summary anchor to today (bad data still stored; anchor rule no longer "newest event in the data")
    - Rejected: tighter allowance like 5 min (callers with drifting clocks would lose events)
36. Size caps (app/models.py): metadata ≤ 16 KB as compact JSON; actor_id, resource_id ≤ 255 chars; actor_email ≤ 320; tenant_id ≤ 32 → max request ≈ 1,000 × ~16.5 KB ≈ 16.5 MB
    - Why: no cap = one caller could store huge rows (disk, memory, slow pages for that tenant)
    - Dataset check: metadata max 330 B → re-run of load with caps: 0 invalid lines (67.3 s, 29,746 lines/s)
    - Rejected: CHECK constraints in the DB as well (time; app is the only writer)
37. Ingest throughput benchmark: scripts/ingest_bench.py; reserved tenants t_9001–t_9005, Sept 2026 dates (summary window unchanged), deleted before + after; bodies built before the clock; several client processes
    - Rejected: one client process (httpx in 1 process saturates at ~900 req/s → measured the client, not the API; found by per-process CPU sampling: client 98%, uvicorn 34%)
    - Rejected: benchmarking against an empty DB (less realistic; real run writes into the 2M-row table)
38. Row-level security = 2nd layer for constraint 4 (migrations/002_row_level_security.sql)
    - Role tenant_reader (NOLOGIN, SELECT only); policies FOR SELECT on events + daily_action_counts: tenant_id = current_setting('app.tenant_id', true); unset → NULL → no rows
    - Reads: db.tenant_connection() = transaction + SET LOCAL ROLE tenant_reader + set_config('app.tenant_id', tenant, true); both end with the transaction (also on errors)
    - Writes (ingest, load) stay as table owner: one batch may mix tenants
    - Summary anchor via newest_event_day() SECURITY DEFINER: sees all tenants, returns only the date
    - Role created in a DO block that ignores duplicate_object (roles are server-wide; audit_test reuses it)
    - Limit: API logs in as superuser `audit` and only drops to tenant_reader per read → guards against a forgotten filter (bug), not against SQL injection (could RESET ROLE); production: separate non-superuser login roles for reader and writer
    - Rejected: RLS on writes (ingest batches span tenants); separate reader connection pool (more config, same effect)

## Assumptions
- event_id unique globally, not per tenant
- Duplicate with different payload → first write wins (none in dataset)
- Ingest callers = trusted internal services; one batch may mix tenants
- actor_email must be null unless actor_type = user; null allowed for user (brief type: string or null)
- Timestamps with non-Z offset accepted and converted to UTC
- Events > 10 min late (e.g. retries, up to 19 min in data) accepted; up to 1 h in the future accepted (clock skew)
- Summary anchor moves as new events arrive; at most 1 h ahead of server time (decision 35)
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
- Fresh-clone run (2026-10-19): git clone to temp folder "grader-check" (own compose project + volume), dataset copied in, every README command run as written
  - Found: curl /health right after docker compose up -d → connection reset (exit 56), uvicorn not ready → fixed with healthcheck + --wait (commit 501eb67), re-run from empty volume
  - docker compose build --no-cache OK (pinned pip versions install from scratch)
  - up -d --build --wait: healthy in 6.8 s; /health ok immediately
  - Load: 96.7 s in script, 1 min 39 s wall clock; 1,980,386 inserted / 20,646 duplicates / 0 invalid (same as first load)
  - Ingest sample: inserted 2, then duplicates 2; 4 list variants, cursor page 2, summary all OK
  - Tests (docker compose exec api pytest -q, no -T): 29 passed
  - Benchmarks: B1 2.3 / B2 2.2 / B3 2.4 / B4 2.0 ms median
  - docker compose restart → healthy, rows kept
  - git history: no data/, *.ndjson*, .env ever committed; 30 tracked files
- Ingest throughput, 1 API process (as shipped), into the 2M-row DB, concurrent callers:

  | Events per request | Callers | Events | Seconds | Events/s | p50 ms | p95 ms | Max ms |
  |---|---|---|---|---|---|---|---|
  | 1 | 32 | 20,000 | 11.9 | 1,683 | 13.7 | 40.0 | 405.4 |
  | 100 | 8 | 100,000 | 4.4 | 22,557 | 28.1 | 60.4 | 186.4 |
  | 1,000 | 4 | 200,000 | 9.1 | 21,971 | 127.6 | 386.3 | 876.5 |

- Ingest ranges across repeated runs: 1 event/request 1,683–2,267 events/s; batches of 100 or 1,000: 19k–37k events/s (Docker Desktop disk I/O variance; checkpoints during runs)
- 1 event/request is API-CPU-bound: uvicorn 96% CPU while client processes ~50% each
- 4 API processes (temporary, port 8001): 1 event/request 1,923/s, batches 19k–28k/s → no gain on this laptop (clients, 4 API processes and Postgres share 8 cores); can't show scaling on one machine
- fsync on the DB volume (pg_test_fsync): ~12,900 fdatasync/s (78 µs) → commit flushes not the limit
- vs prod: peak 3,000/s → batched ingest has 7–12× headroom on 1 process; 1 event/request is below peak on 1 process
- First (flawed) run with 1 client process: 866 / 26,916 / 37,225 events/s; client at 98% CPU → discarded
- Row-level security checks: tests/test_row_level_security.py (query with no WHERE sees only scoped tenant; role without tenant sees 0 rows; reader can't DELETE; scope gone after a failed request on all 10 pool connections; summary anchor still global)
- Mutation: List query's tenant filter removed → isolation tests still pass (RLS alone); RLS also bypassed → isolation test fails
- Plans as tenant_reader: same indexes (events_tenant_time_idx, events_tenant_action_time_idx, daily_action_counts_pkey); policy becomes a One-Time Filter, not a per-row check
- Benchmarks with RLS (warm, run 3): B1 3.0 / B2 3.1 / B3 2.9 / B4 2.4 ms median; before RLS 2.3 / 2.2 / 2.4 / 2.1 → ~0.5 ms = BEGIN + SET LOCAL ROLE + set_config + COMMIT round trips
- Benchmarks right after db restart (cold, run 1): 2.6 / 2.6 / 3.6 / 2.8 ms; B2 walk 9.5 s (4.8 ms/page)
- Fresh-clone run #2, final version (commit 56146bd, 2026-10-19): build --no-cache OK; up --wait healthy in 6.7 s; initdb ran 001 + 002 (policies, function)
  - Load: 127.9 s in script, 2 min 10 s wall (15,643 lines/s); 1,980,386 / 20,646 / 0; disk 745 MB table + 417 MB indexes + 53 MB counts = 1,222 MB DB
  - Slower than first load (89.2 s): extra validation (metadata size via json.dumps, future check) + run-to-run variance; validation-only re-run went 55.9 s → 67.3 s
  - Ingest x2, 4 list variants, cursor page, summary OK; tests 44 passed; B1–B4 2.7 / 2.6 / 2.7 / 2.4 ms; ingest 2,372 / 28,406 / 27,016 events/s; restart OK
- Tests: 29 passed in ~1.5 s (step 5); 35 passed in ~2.5 s after improvements 1–2 (future-date rule, concurrent writes); 39 passed in ~2.9 s after size caps; 44 passed in ~3.5 s after row-level security
- Mutation checks: snapshot filter removed → pagination test fails ("listing must be exactly what existed at page 1"); cursor tenant check removed → isolation test fails; code restored → 29 pass
- Main DB after test runs: 1,980,388 rows (untouched)
- Step 4 checks: 3 pages via next_cursor = SQL ORDER BY result, 0 overlap; cursor from other tenant / other filters / tampered / garbage → 400; summary 2026-09-15 for t_0001 = GROUP BY on events (7,728 events, 22 actions); 30 days returned

## DESIGN.md fact sheet (fragments only; write the doc yourself)

### Measured per row (1,980,388 rows, after load + VACUUM)
- events heap: 394 B
- events_pkey (uuid): 40 B
- events_tenant_time_idx: 74 B
- events_tenant_action_time_idx: 107 B
- 3 indexes: 221 B; table + indexes: 615 B
- daily_action_counts: 180 B/row incl. its 2 indexes; 335,309 rows for 61 days
- WAL: ~927 B per inserted event (100k-line load into audit_test; incl. full-page writes + VACUUM ANALYZE)

### Projected 1 year at 20M/day
- 7.3B rows
- table 2.88 TB + indexes 1.61 TB (pkey 294 GB, tenant_time 537 GB, tenant_action_time 780 GB) = 4.49 TB
- growth 12.3 GB/day; WAL 18.5 GB/day
- monthly partitions: 12/yr, 608M rows each, 240 GB table + 134 GB indexes
- daily partitions: 365/yr, 20M rows each, 7.9 GB table + 4.4 GB indexes
- daily_action_counts: ~2.0M rows/yr (~362 MB) at dataset density; ceiling 800 × 22 × 365 = 6.42M rows/yr (~1.16 GB)

### Write rate
- prod average 231 events/s; peak 3,000/s; peak/average 13×
- measured load: 22,428 lines/s on 1 connection, batches of 10,000, table growing 0 → 2M rows = 7.5× peak
- WAL at peak ≈ 2.8 MB/s
- prod daily volume = 616× dataset daily volume (32,465 events/day)
- measured Ingest API, 1 process: 1 event/request ~1,700–2,400 events/s (API CPU-bound, below 3,000/s peak); batches of 100–1,000: ~19k–37k events/s (7–12× peak)

### Tenant skew
- t_0001 = 20.0% → 4.0M events/day, 1.46B/yr, ~898 GB/yr incl. indexes (~107 GB of tenant_time index alone)
- top 5 = 60% → 12M/day; every tenant from t_0006 on ≤ 0.8%
- hottest summary row: t_0001 × document.view (22.5% of events) → ~898k increments/day, ~10/s average, ~135/s at peak if each event were its own transaction

### Reads
- B1–B4 medians 2.0–2.4 ms
- B2: 2,000 requests to reach, 5.4 s (2.7 ms/page); the page itself 2.2 ms
- summary reads 660 rows for t_0001 (30 days × 22 actions) whatever the event volume
- today: indexes 417 MB vs shared_buffers 512 MB vs Docker memory 3.82 GiB; 1 year: indexes 1.61 TB

### Topics for "what breaks first" (no conclusions)
- single-node write rate and WAL volume
- index size vs RAM; random uuid primary-key inserts; hot tenants' index pages
- hot tenants and partition size
- contention on hot daily_action_counts rows
- removing year-old data without partitions (20M rows/day)
- deep paging (B2-style walk)
- connection count (asyncpg pool max 10 per API process; Postgres default max_connections 100)

### DESIGN.md sections → items in this file to cover
1. Database choice → decision 1; decision 14
2. Schema → decisions 2, 3, 4, 18, 35, 36; dataset facts (duplicates identical, 0 rule violations)
3. Pagination → decisions 8, 9, 24, 25 (incl. edge case), 32
4. Write path → decisions 5, 6, 11, 16, 17, 19, 20, 37 (ingest throughput)
5. Constraints
   - 1 duplicates → decisions 2, 5, 16, 17; retries up to 19.2 min late; concurrent-retry tests + deadlock mutation (3/3)
   - 2 late/out of order → decisions 4, 5, 10; first copies ≤ 9.99 min late; tests
   - 3 stable paging → decisions 9, 25; paging test; mutation check
   - 4 tenant isolation → decisions 7, 22, 24, 28, 38 (row-level security + its limit); tests; mutation checks (filter removed → RLS still holds)
6. At production scale → this fact sheet; decision 1 (partitioning plan); decision 37 (1 event/request below peak → batching, more API processes); decision 38 limit (non-superuser roles)
7. Assumptions → Assumptions section; decisions 6 (changed to all-or-nothing), 7 (gateway), 10, 23, 35 (1 h clock skew)
8. AI usage → yours to write; facts: AI wrote code, tests, scripts, README and NOTES.md and proposed the decisions you approved; DESIGN.md written by you
