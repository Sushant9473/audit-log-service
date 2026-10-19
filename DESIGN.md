# Audit Log Service: Design

Setup, commands and all benchmark results are in [README.md](README.md).

The brief sets four constraints, numbered the same way in the code comments:

1. Retries: calling services retry, so the same event can arrive more than once. It must be stored once.
2. Late and out-of-order events: events can arrive up to 10 minutes late and in any order.
3. Stable paging: paging must not show an event twice or miss one while new events arrive.
4. Tenant isolation: a tenant must never see another tenant's events.

[scripts/explore_dataset.py](scripts/explore_dataset.py) reports the dataset facts the design depends on. All 20,646 repeated lines are byte-identical to their first copy. Repeats arrive up to 19.2 minutes after the original, and 67 of them are more than 10 minutes late. The load is skewed: the top 5 of 800 tenants hold 60% of events, and `t_0001` alone has 20%.

## 1. Database choice

We use PostgreSQL 16 on one node, with a single unpartitioned `events` table.

PostgreSQL gives exact, immediate deduplication (constraint 1). A primary key on `event_id` rejects a repeat in the same transaction that would store it, so when the API replies 200, the event is stored exactly once. Transactions also let the insert and the daily-count update succeed or fail together, so a crash between them can't leave the summary wrong.

Every List query has the same shape: one tenant, newest first, maybe one action, maybe a time range. A B-tree on `(tenant_id, [action,] occurred_at DESC, event_id DESC)` returns those rows already in order, so the scan stops after `limit + 1` rows with no sort. Row-level security adds the database's own check on constraint 4.

ClickHouse or another column store would be very good for the summary. But `ReplacingMergeTree` only removes duplicates during background merges, so reads would either need `FINAL` or show duplicates for a while, which makes constraint 1 eventual instead of exact.

We also left out partitioning for now. At 2M rows (1.2 GB) it adds DDL, partition upkeep and a composite primary key with no measurable gain. Section 6 has the production plan, which partitions by month.

## 2. Schema

The schema is in [migrations/001_schema.sql](migrations/001_schema.sql) and row-level security in [migrations/002_row_level_security.sql](migrations/002_row_level_security.sql).

### `events`

Each field from the brief gets its natural type: `uuid` for `event_id`, which is the primary key and the deduplication rule, `inet` for `ip`, `jsonb` for `metadata` and `timestamptz` for `occurred_at`. `CHECK` constraints repeat the brief's allowed values and require `actor_email` to be null unless `actor_type` is `user`, as a backstop to the API's validation. Two columns are ours: `received_at`, when the event was stored, and `ingest_seq`, an identity column that records arrival order for the List snapshot (section 3).

| Index | Serves |
|---|---|
| `events_pkey (event_id)` | Deduplication |
| `events_tenant_time_idx (tenant_id, occurred_at DESC, event_id DESC)` | List with no filter or only a time range (B1, B2) |
| `events_tenant_action_time_idx (tenant_id, action, occurred_at DESC, event_id DESC)` | List with an action filter, with or without a time range (B3) |

`event_id` goes last in both list indexes to break ties between events with the same `occurred_at`. That makes the order total, so a cursor names exactly one position. A time range is a slice of `occurred_at` inside either index, so it needs no index of its own, and `events` has no other indexes because each one costs write time and 74-107 bytes per row.

### `daily_action_counts`

```sql
tenant_id text, day date, action text, count bigint,
PRIMARY KEY (tenant_id, day, action)   -- Summary reads one tenant's days; writes upsert by this key
INDEX ON (day)                         -- finds the newest day across all tenants
```

`day` is the UTC date of `occurred_at`, and the statement that stores events also updates this table (section 4).

Without it, the Summary API would run a `GROUP BY` over `events` on every request, which gets slower as a tenant grows; `t_0001` already has 198,615 events in the 30-day window. The counts table has at most one row per tenant, day and action, so a summary reads at most 660 rows (30 days x 22 actions) whatever the event volume. A periodic rebuild or a materialized view would leave the summary stale between runs and be a second code path to keep correct.

The window is the 30 UTC days ending on the newest day in the data across all tenants, as the brief says. `newest_event_day()` in [app/reads.py](app/reads.py) finds that day with a backward index-only scan. We didn't use a rolling 720-hour window, because daily buckets can't be split and the response is per day.

### Validation

[app/models.py](app/models.py) defines one Pydantic `Event` model, shared by the Ingest API and the load script. Unknown fields are rejected (`extra="forbid"`), not dropped, because an audit log shouldn't lose data quietly. `occurred_at` must be an ISO 8601 string with a timezone and is converted to UTC. Field sizes are capped (`metadata` at 16 KB), which puts the largest valid request at about 16.5 MB.

An event can be at most 1 hour in the future. The summary window ends on the newest day across all tenants, so one event dated 2099 would empty every tenant's summary. Capping the summary's end date at today wouldn't help, since the bad data would still be stored. A tighter allowance, say 5 minutes, would lose events from callers with drifting clocks.

## 3. Pagination

`GET /v1/events` returns one tenant's events ordered by `occurred_at DESC, event_id DESC`, with optional `action`, `from` (inclusive) and `to` (exclusive) filters.

### Keyset position

After page 1, each query adds:

```sql
(occurred_at, event_id) < ($last_occurred_at, $last_event_id)
```

The row comparison matches the index order, so PostgreSQL puts it in the index condition and seeks straight to the position. Page 2,001 costs the same as page 1: in the benchmarks, B2 (events 100,001-100,050) has a 3.1 ms median against 3.0 ms for B1's first page.

We didn't use offset pagination. Rows shift as events arrive, which causes duplicates and gaps, and `OFFSET 100000` reads and discards 100,000 rows. Ordering by arrival was also out, because the brief's "newest first" means when the event happened.

### Snapshot

Page 1 reads the newest arrival number handed out so far:

```sql
SELECT last_value FROM events_ingest_seq_seq
```

Every page of that listing then adds `ingest_seq <= snapshot`, so events stored after page 1 never show up in it, whether they sort above everything or into pages already read or still to come. Later pages can't shift, and the listing is exactly the set of events that existed at page 1. Reading the sequence is O(1), whereas `max(ingest_seq)` would scan the table.

There's one edge case. A row gets its sequence number when it's inserted but only becomes visible when its transaction commits. A transaction with a number at or below the snapshot that commits after page 1 can still show up on a later page if it sorts below the cursor. It can never appear twice, because the cursor only moves forward.

### Cursor

The cursor is `base64url(JSON) + "." + base64url(HMAC-SHA256(JSON))`, keyed with `CURSOR_SECRET` ([app/cursor.py](app/cursor.py)). It holds the tenant, the filters, the snapshot, and the last row's `event_id` and `occurred_at`. The timestamp is kept to the microsecond, since a cursor rounded to the milliseconds in the response would skip or repeat rows.

The server checks the signature with `hmac.compare_digest` and rejects a cursor whose tenant or filters don't match the request, with 400 either way. The cursor is signed but not encrypted. The client has already seen those rows; it just mustn't be able to edit the position or raise the snapshot.

## 4. Write path

### One statement per batch

[app/writes.py](app/writes.py) sends the whole batch as 12 arrays in one statement:

```sql
WITH inserted AS (
    INSERT INTO events (...) SELECT * FROM unnest($1::uuid[], ..., $12::timestamptz[])
    ON CONFLICT (event_id) DO NOTHING
    RETURNING tenant_id, action, occurred_at
), counted AS (
    INSERT INTO daily_action_counts AS d (tenant_id, day, action, count)
    SELECT tenant_id, (occurred_at AT TIME ZONE 'UTC')::date, action, count(*)
    FROM inserted GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
    ON CONFLICT (tenant_id, day, action) DO UPDATE SET count = d.count + EXCLUDED.count
)
SELECT count(*) FROM inserted
```

A retried event returns nothing from `inserted`, so it adds nothing to the counts. A late event is counted under its own UTC day. Each batch is one round trip whatever its size, and since the statement text never changes, asyncpg prepares it once per connection.

### Avoiding deadlocks

Two concurrent batches with overlapping events could take primary-key locks in opposite orders and deadlock. Python sorts each batch by `event_id` first, so every transaction locks in the same order, and the `ORDER BY` in `counted` does the same for the count rows. With the sort removed, [tests/test_concurrent_writes.py](tests/test_concurrent_writes.py) failed with `DeadlockDetectedError` in 3 of 3 runs. We preferred this to catching deadlock errors and retrying.

### Ingest API

`POST /v1/events` takes 1-1,000 events, all or nothing. Pydantic checks the whole batch before any SQL runs, so one invalid event gets a 422 with its position and nothing is stored. A valid batch runs in one transaction, and the 200 is sent only after the commit, with counts of events received, inserted and skipped as duplicates. A retry of a stored batch still gets 200.

Our first plan allowed partial success. We switched because per-event results make every caller handle a result per event. A queue in front of the database would make a 200 mean "queued" instead of "stored", and the throughput doesn't need one: on one API process, batched requests reach 19,000-37,000 events/s, 7-12x the 3,000/s production peak. Single-event requests only reach 1,700-2,300 events/s, with uvicorn at 96% CPU on per-request HTTP, JSON and validation.

### Load script

[scripts/load.py](scripts/load.py) validates each line with the same `Event` model and writes batches of 10,000 through the same `insert_events()`, committing each batch so a crashed load can be re-run. It writes to the database directly, as the brief allows, instead of paying HTTP and JSON overhead through the Ingest API. `COPY` into a temporary table would be faster, but it would be a second write path, and as the shared path it would create a temporary table per ingest request.

## 5. The four constraints

The 44 tests run inside the stack with `docker compose exec api pytest -q`, against their own `audit_test` database. A mutation check means we removed a protection on purpose to confirm the matching test fails without it.

### 1. Retries are stored once

The `event_id` primary key plus `ON CONFLICT DO NOTHING` skips a repeat instead of raising an error, and repeats inside one batch are collapsed before the insert. A retry can arrive while the original request is still running. PostgreSQL then makes the second insert wait on the first transaction's uncommitted key, and stores the event only if the first transaction rolls back.

Deduplication has no time window. Retries in the dataset arrive up to 19.2 minutes after the original, so a "seen in the last N minutes" check would miss some.

Tests: [tests/test_duplicates.py](tests/test_duplicates.py) covers retried batches and repeats within a batch. [tests/test_concurrent_writes.py](tests/test_concurrent_writes.py) sends the same batch from 10 callers at once and checks the counts against a `GROUP BY` over `events`.

### 2. Late and out-of-order events land in the right place

List sorts by `occurred_at`, so a late event lands in its place. Counts are keyed by the UTC day of `occurred_at`, so an event from 23:58 that arrives after midnight still counts toward its own day.

Test: [tests/test_late_events.py](tests/test_late_events.py) sends events from both sides of midnight in scrambled order and checks the list order and the day buckets.

### 3. Paging is stable while events arrive

This comes from the snapshot and keyset cursor in section 3.

Test: [tests/test_pagination.py](tests/test_pagination.py) adds 50 events while a 300-event listing is open and checks that the rest of the listing is exactly the original 300. Mutation check: removing the snapshot filter makes it fail.

### 4. A tenant sees only its own events

The tenant comes only from the `X-Tenant-ID` header, never from the query string or a cursor. Every read query filters on `tenant_id = $1` with bound parameters, and cursors are signed and bound to the tenant.

Row-level security ([migrations/002_row_level_security.sql](migrations/002_row_level_security.sql)) backs this up. Each read runs in a transaction that switches to the `tenant_reader` role with `SET LOCAL ROLE` and sets `app.tenant_id`. That role can only `SELECT`, and its policies show only rows for that tenant, or none if no tenant is set. Both settings end with the transaction, so a pooled connection is clean for the next request. Writes run as the table owner, because one batch may hold several tenants.

Tests: [tests/test_tenant_isolation.py](tests/test_tenant_isolation.py) and [tests/test_row_level_security.py](tests/test_row_level_security.py). Mutation check: with the tenant filter removed from the List query, the isolation tests still pass, because row-level security holds on its own.

One limit: the API connects as the superuser `audit`. Row-level security catches a query that forgets its tenant filter, but injected SQL could run `RESET ROLE`, so bound parameters are the defense against injection. Production would use separate non-superuser login roles.

## 6. At production scale

The brief's production numbers are 800 tenants with the top 5 holding about 60% of events, 20M events/day on average with peaks of 3,000/s, and 1-year retention. That's about 7.3 billion rows, 616 times the dataset's daily volume.

Each event measured 615 bytes after the load: 394 in the table and 221 across the three indexes. At production volume:

| | 1 year at 20M events/day |
|---|---|
| `events` table | 2.88 TB |
| `events` indexes | 1.61 TB: primary key 294 GB, tenant+time 537 GB, tenant+action+time 780 GB |
| Total, and daily growth | 4.49 TB; +12.3 GB/day |
| WAL | 18.5 GB/day; about 2.8 MB/s at peak |
| `t_0001` alone (20%) | 1.46 billion rows/year, about 898 GB including indexes |

### What breaks first

Roughly in order, if this build ran in production unchanged:

1. **One API process with single-event callers, at the first peak.** At one event per request a process handles 1,700-2,300 events/s, below the 3,000/s peak. The API keeps no state, so the fix is to run several processes and ask callers to batch.

2. **Database connections, as soon as API processes multiply.** Each pool holds up to 10 connections against PostgreSQL's default `max_connections` of 100. PgBouncer in transaction mode fixes this, and tenant scoping still works because `SET LOCAL` lasts only for the transaction. asyncpg's prepared statements need PgBouncer's prepared-statement support (1.21 or later).

3. **The primary-key index outgrowing memory, within weeks to months.** `event_id` is a random UUID, so every insert and duplicate check lands on a random leaf page of `events_pkey`, which grows 0.8 GB/day. Once it no longer fits in memory, each insert waits on a random disk read. The list indexes avoid this because inserts land at the newest end of each tenant's range.

4. **Hot summary rows, at peak, if callers don't batch.** Every transaction that stores a `t_0001` `document.view` event updates the same counts row and holds its lock until commit, about 135 transactions/s on one row at peak. Batching makes it one update per batch, and a small `shard` column in the count key could spread each count over several rows.

5. **Retention, from day 366.** Without partitions, retention means deleting 20M rows a day from a 4.5 TB table, with WAL for every row and vacuum over three very large indexes. Partitioning a multi-terabyte table later would be a large migration, so it belongs in the first production schema.

Deep paging, the Summary API and the snapshot read don't get slower with volume, and 2.8 MB/s of WAL at peak is well within what one PostgreSQL node can write.

### Production plan

`events` would be partitioned by month on `occurred_at`, giving 12-13 partitions of about 608M rows each. PostgreSQL requires the partition key in unique constraints, so the primary key becomes `(event_id, occurred_at)`. That still catches retries, because a retry is the same event with the same `occurred_at`; a "retry" with a different `occurred_at` would be stored twice, which we accept. Retention becomes dropping a partition once all its rows are over a year old, and inserts only touch the current partition's primary key. List queries keep their shape, since a time filter skips whole partitions, and `ingest_seq` comes from one shared sequence, so the snapshot still works.

Other changes:

- Run several API processes behind PgBouncer (items 1 and 2).
- Add a streaming replica with automatic failover, plus base backups and archived WAL. Restoring 4.5 TB takes hours, so in practice the replica is the recovery path.
- Give the API separate non-superuser login roles for writes and reads, and authenticate tenants at the gateway.
- Smaller items: cap request bodies at about 17 MB at the gateway, and switch to weekly partitions if the current month's 24 GB primary key outgrows memory.

## 7. Assumptions

- `event_id` is unique globally. If a repeat has different content, the first write wins; the dataset has none.
- Ingest callers are trusted internal services, so the Ingest API has no authentication and a batch may mix tenants. An auth gateway authenticates users and sets `X-Tenant-ID` for the read APIs.
- Events of any lateness are accepted; the brief's 10 minutes isn't treated as a cut-off.
- "Last 30 days" means the 30 UTC days ending on the day of the newest event across all tenants.
- B3's "1-30 Sept 2026" is `from=2026-09-01T00:00:00Z` up to but not including `to=2026-10-01T00:00:00Z`. B2 is page 2,001, events 100,001-100,050.
- Designed but not built: partitioning, retention, authentication, separate database roles, high availability and a UI.

## 8. AI usage

The project was written with an AI coding assistant, including the code, tests, benchmark and load scripts, README and the working notes. The assistant proposed the design decisions, and the author approved each one. The measurements came from running the scripts in this repository on the machine described in the README.
