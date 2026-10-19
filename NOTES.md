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
11. Load: scripts/load.py inside api container (docker compose exec); stream gzip; batches of 10,000 → COPY into temp table → shared write statement; bad lines skipped + counted; re-runnable
    - Rejected: posting batches to the Ingest API (HTTP + JSON overhead; brief allows direct bulk load)
12. Schema setup: migrations/ mounted into /docker-entrypoint-initdb.d (runs on fresh volume); schema change → docker compose down -v
    - Rejected: migration tool like Alembic (time)

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
- (none yet)
