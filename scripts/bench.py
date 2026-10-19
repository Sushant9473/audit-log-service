"""Benchmarks B1–B4 from the brief: median of 10 runs each, page size 50, tenant t_0001.

Usage, with the stack running and the dataset loaded:
    docker compose exec api python scripts/bench.py

Times are measured by an HTTP client inside the api container, so they include HTTP and
JSON handling, not just the SQL. Each benchmark gets one warm-up request that isn't counted.
"""

import statistics
import time

import httpx

API = "http://localhost:8000"
TENANT = "t_0001"
PAGE_SIZE = 50
RUNS = 10


def timed_get(client, path, params):
    started = time.perf_counter()
    response = client.get(path, params=params)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    return response.json(), elapsed_ms


def measure(client, path, params):
    timed_get(client, path, params)  # warm-up, not counted
    return [timed_get(client, path, params)[1] for _ in range(RUNS)]


def cursor_for_page_2001(client):
    """Events 100,001–100,050 are page 2,001. Keyset pagination can't jump to an offset,
    so walk 2,000 pages once with next_cursor; then B2 times requests for that one page."""
    params, events_passed = {"limit": PAGE_SIZE}, 0
    started = time.perf_counter()
    for _ in range(2000):
        body, _ = timed_get(client, "/v1/events", params)
        events_passed += len(body["data"])
        params = {"limit": PAGE_SIZE, "cursor": body["next_cursor"]}
    walk_seconds = time.perf_counter() - started
    assert events_passed == 100_000, events_passed
    return params, walk_seconds


def main():
    with httpx.Client(base_url=API, headers={"X-Tenant-ID": TENANT}, timeout=30) as client:
        b2_params, walk_seconds = cursor_for_page_2001(client)
        page_2001, _ = timed_get(client, "/v1/events", b2_params)

        b3_params = {"limit": PAGE_SIZE, "action": "invoice.delete",
                     "from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"}
        results = [
            ("B1", "List t_0001, no filters, first page", measure(client, "/v1/events", {"limit": PAGE_SIZE})),
            ("B2", "List t_0001, no filters, events 100,001–100,050 (page 2,001)", measure(client, "/v1/events", b2_params)),
            ("B3", "List t_0001, action=invoice.delete, 1–30 Sept 2026, first page", measure(client, "/v1/events", b3_params)),
            ("B4", "Summary t_0001, last 30 days", measure(client, "/v1/summary", {})),
        ]

    print("| ID | Query | Median (ms) | Min (ms) | Max (ms) |")
    print("|---|---|---|---|---|")
    for bench_id, label, times in results:
        print(f"| {bench_id} | {label} | {statistics.median(times):.1f} | {min(times):.1f} | {max(times):.1f} |")

    print(f"\nB2 cursor: walked 2,000 pages ({2000 * PAGE_SIZE:,} events) with next_cursor in {walk_seconds:.1f} s "
          f"({walk_seconds / 2000 * 1000:.1f} ms per page); then timed page 2,001 on its own.")
    first, last = page_2001["data"][0], page_2001["data"][-1]
    print(f"Page 2,001: {len(page_2001['data'])} events; first {first['event_id']} at {first['occurred_at']}, "
          f"last {last['event_id']} at {last['occurred_at']}")


if __name__ == "__main__":
    main()
