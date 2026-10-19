"""Bulk-load the audit-event dataset into Postgres.

Usage, from the repo root with the stack running:
    docker compose exec -T api python scripts/load.py < data/audit_events.ndjson.gz

The gzipped file is read from stdin (or from a path given as the first argument), one line at
a time, so it never sits in memory. Every line is validated with the same Event model as the
Ingest API and written with the same insert_events() statement, so duplicates are handled the
same way a retried event would be. Each batch commits on its own, so re-running is always safe.
"""

import argparse
import asyncio
import gzip
import os
import sys
import time
from collections import Counter

import asyncpg
from pydantic import ValidationError

from app.models import Event
from app.writes import insert_events

SIZES_SQL = """
SELECT pg_table_size('events')                        AS events_table,
       pg_indexes_size('events')                      AS events_indexes,
       pg_total_relation_size('daily_action_counts')  AS daily_action_counts,
       pg_database_size(current_database())           AS whole_database
"""


def mb(size_bytes):
    return f"{size_bytes / 1024 / 1024:,.0f} MB ({size_bytes:,} bytes)"


async def write_batch(conn, batch):
    async with conn.transaction():
        return await insert_events(conn, batch)


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", nargs="?", default="-", help="gzipped NDJSON file; '-' or omitted = stdin")
    parser.add_argument("--batch-size", type=int, default=10_000)
    args = parser.parse_args()

    if args.path == "-":
        if sys.stdin.isatty():
            sys.exit("Pipe the dataset in: docker compose exec -T api python scripts/load.py < data/audit_events.ndjson.gz")
        source = sys.stdin.buffer
    else:
        source = open(args.path, "rb")

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    started = time.time()
    lines_read = inserted = skipped = 0
    skip_reasons = Counter()
    batch = []

    with gzip.open(source, "rb") as lines:
        for raw in lines:
            lines_read += 1
            try:
                batch.append(Event.model_validate_json(raw))
            except ValidationError as exc:
                skipped += 1
                first_error = exc.errors()[0]
                skip_reasons[f"{'.'.join(map(str, first_error['loc']))}: {first_error['msg']}"] += 1

            if len(batch) >= args.batch_size:
                inserted += await write_batch(conn, batch)
                batch = []
            if lines_read % 100_000 == 0:
                elapsed = time.time() - started
                print(f"{lines_read:>10,} lines | {inserted:,} inserted | {elapsed:6.1f} s | {lines_read / elapsed:,.0f} lines/s", flush=True)

    if batch:
        inserted += await write_batch(conn, batch)
    load_seconds = time.time() - started

    # Valid lines that weren't inserted were already stored: repeats in the file, or an earlier run.
    duplicates = lines_read - skipped - inserted
    print("\n== Load finished ==")
    print(f"lines read:          {lines_read:,}")
    print(f"inserted:            {inserted:,}")
    print(f"duplicates skipped:  {duplicates:,}")
    print(f"invalid lines:       {skipped:,}")
    for reason, count in skip_reasons.most_common(10):
        print(f"    {count:,} × {reason}")
    print(f"elapsed:             {load_seconds:.1f} s ({lines_read / load_seconds:,.0f} lines/s)")

    # Bulk inserts leave planner statistics and the visibility map stale; refresh both now
    # instead of waiting for autovacuum, so queries get good plans and index-only scans.
    vacuum_started = time.time()
    await conn.execute("VACUUM (ANALYZE) events")
    await conn.execute("VACUUM (ANALYZE) daily_action_counts")
    print(f"VACUUM ANALYZE:      {time.time() - vacuum_started:.1f} s")

    sizes = await conn.fetchrow(SIZES_SQL)
    rows = await conn.fetchval("SELECT count(*) FROM events")
    print("\n== Disk usage ==")
    print(f"events rows:         {rows:,}")
    for name, size in sizes.items():
        print(f"{name + ':':<21}{mb(size)}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
