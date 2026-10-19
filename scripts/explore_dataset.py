#!/usr/bin/env python3
"""Stream the audit-event dataset once and report the facts the design depends on.

Usage:
    python3 scripts/explore_dataset.py data/audit_events.ndjson.gz

Standard library only, so it runs with any Python 3.9+ without installing anything.
It keeps one small hash per event_id in memory, never the events themselves.
"""

import gzip
import hashlib
import ipaddress
import json
import re
import sys
import time
from array import array
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

EXPECTED_SHA256 = "e968244bc33aaf0bc13c98d253f49819da1236620eae8cf51de31b7fb34d4dc1"

FIELDS = (
    "event_id", "tenant_id", "actor_id", "actor_type", "actor_email", "action",
    "resource_type", "resource_id", "result", "ip", "metadata", "occurred_at",
)
ACTOR_TYPES = {"user", "service", "api_key"}
ACTOR_ID_PREFIX = {"user": "usr_", "service": "svc_", "api_key": "key_"}
ACTIONS = {
    "user.login", "user.logout", "user.password_reset", "user.mfa_enabled",
    "user.invite", "user.role_change", "user.delete", "api_key.create",
    "api_key.revoke", "invoice.create", "invoice.update", "invoice.delete",
    "invoice.export", "payment.refund", "project.create", "project.update",
    "project.delete", "document.view", "document.download", "document.share",
    "settings.update", "webhook.create",
}
RESOURCE_TYPES = {"user", "api_key", "invoice", "payment", "project", "document", "settings", "webhook"}
RESULTS = {"success", "failure"}

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TENANT_RE = re.compile(r"^t_\d+$")

BIG_TENANT = "t_0001"
TEN_MINUTES_MS = 10 * 60 * 1000


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_occurred_at(value):
    """Return (milliseconds since epoch, UTC date), or (None, None) if it can't be parsed."""
    if not isinstance(value, str):
        return None, None
    try:
        if value.endswith("Z"):
            # Python 3.9's fromisoformat doesn't accept a trailing "Z"
            dt = datetime.fromisoformat(value[:-1]).replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                return None, None
            dt = dt.astimezone(timezone.utc)
    except ValueError:
        return None, None
    return round(dt.timestamp() * 1000), dt.date()


def find_violations(ev):
    """Yield a short name for every rule from the brief that this event breaks."""
    for field in FIELDS:
        if field not in ev:
            yield f"missing field: {field}"
    extra = set(ev) - set(FIELDS)
    if extra:
        yield "extra fields: " + ", ".join(sorted(extra))

    if not UUID_RE.match(str(ev.get("event_id"))):
        yield "event_id not a lowercase UUID"
    if not TENANT_RE.match(str(ev.get("tenant_id"))):
        yield "tenant_id not like t_<digits>"

    actor_type = ev.get("actor_type")
    if actor_type not in ACTOR_TYPES:
        yield "unknown actor_type"
    elif not str(ev.get("actor_id")).startswith(ACTOR_ID_PREFIX[actor_type]):
        yield "actor_id prefix doesn't match actor_type"
    email = ev.get("actor_email")
    if actor_type == "user" and email is None:
        yield "user with null actor_email"
    if actor_type != "user" and email is not None:
        yield "non-user with actor_email"

    action = ev.get("action")
    if action not in ACTIONS:
        yield "unknown action"
    if ev.get("resource_type") not in RESOURCE_TYPES:
        yield "unknown resource_type"
    elif action in ACTIONS and action.split(".")[0] != ev["resource_type"]:
        yield "resource_type doesn't match action prefix"
    if ev.get("result") not in RESULTS:
        yield "unknown result"

    try:
        ipaddress.IPv4Address(ev.get("ip"))
    except ValueError:
        yield "ip not IPv4"
    if not isinstance(ev.get("metadata"), dict):
        yield "metadata not a JSON object"
    if not TIMESTAMP_RE.match(str(ev.get("occurred_at"))):
        yield "occurred_at not like 2026-09-14T08:31:07.412Z"


def fields_that_differ(path, event_ids):
    """Second pass, only run if some repeats aren't byte-identical: which fields change between copies."""
    copies = defaultdict(list)
    with gzip.open(path, "rb") as f:
        for raw in f:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("event_id") in event_ids:
                copies[ev["event_id"]].append(ev)
    differing = Counter()
    for versions in copies.values():
        first = versions[0]
        for other in versions[1:]:
            for field in set(first) | set(other):
                if first.get(field) != other.get(field):
                    differing[field] += 1
    return differing


def percentile(sorted_values, p):
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * p))]


def human_ms(ms):
    return f"{ms / 1000:.1f} s ({ms / 60000:.2f} min)"


def main(path):
    started = time.time()

    print("== File ==")
    actual_sha = sha256_of(path)
    print(f"SHA-256 matches expected: {actual_sha == EXPECTED_SHA256} ({actual_sha})")

    first_line_hash = {}           # event_id -> hash of the first line seen with that id
    duplicate_lines = 0
    differing_duplicates = set()   # event_ids whose repeat is not byte-identical to the first copy
    bad_json_lines = 0
    # Per line: newest occurred_at seen so far minus this line's. Kept apart for first copies
    # and repeats, because a retry can arrive long after the original.
    lateness_first_ms, lateness_repeat_ms = array("q"), array("q")
    newest_so_far = None

    tenants, actions, actor_types, results, resource_types = Counter(), Counter(), Counter(), Counter(), Counter()
    violations, violation_example_line = Counter(), {}
    big_tenant_day_action = Counter()   # (date, action) -> events, for BIG_TENANT only
    min_ts = max_ts = None
    metadata_bytes_total = metadata_bytes_max = 0

    line_no = 0
    with gzip.open(path, "rb") as f:
        for line_no, raw in enumerate(f, 1):
            raw = raw.rstrip(b"\r\n")
            try:
                ev = json.loads(raw)
            except ValueError:
                bad_json_lines += 1
                continue
            if not isinstance(ev, dict):
                bad_json_lines += 1
                continue

            ts_ms, day = parse_occurred_at(ev.get("occurred_at"))
            if ts_ms is not None and (newest_so_far is None or ts_ms > newest_so_far):
                newest_so_far = ts_ms

            event_id = ev.get("event_id")
            line_hash = hash(raw)
            if event_id in first_line_hash:
                duplicate_lines += 1
                if first_line_hash[event_id] != line_hash:
                    differing_duplicates.add(event_id)
                if ts_ms is not None:
                    lateness_repeat_ms.append(newest_so_far - ts_ms)
                continue
            first_line_hash[event_id] = line_hash
            if ts_ms is not None:
                lateness_first_ms.append(newest_so_far - ts_ms)

            # Everything below counts each event once.
            tenant = ev.get("tenant_id")
            tenants[tenant] += 1
            actions[ev.get("action")] += 1
            actor_types[ev.get("actor_type")] += 1
            results[ev.get("result")] += 1
            resource_types[ev.get("resource_type")] += 1

            if ts_ms is not None:
                min_ts = ts_ms if min_ts is None else min(min_ts, ts_ms)
                max_ts = ts_ms if max_ts is None else max(max_ts, ts_ms)
                if tenant == BIG_TENANT:
                    big_tenant_day_action[(day, ev.get("action"))] += 1

            metadata_size = len(json.dumps(ev.get("metadata"), separators=(",", ":")))
            metadata_bytes_total += metadata_size
            metadata_bytes_max = max(metadata_bytes_max, metadata_size)

            for violation in find_violations(ev):
                violations[violation] += 1
                violation_example_line.setdefault(violation, line_no)

            if line_no % 500_000 == 0:
                print(f"  ...{line_no:,} lines read", file=sys.stderr)

    unique = len(first_line_hash)

    print("\n== Lines and duplicates ==")
    print(f"lines: {line_no:,}")
    print(f"lines that aren't a JSON object: {bad_json_lines:,}")
    print(f"unique event_ids: {unique:,}")
    print(f"duplicate lines (repeat of an earlier event_id): {duplicate_lines:,}")
    print(f"event_ids whose repeat is NOT byte-identical: {len(differing_duplicates):,}")
    if differing_duplicates:
        sample = set(list(differing_duplicates)[:200])
        differing = fields_that_differ(path, sample)
        if differing:
            print(f"  fields that differ (sample of {len(sample)} ids): {dict(differing.most_common())}")
        else:
            print("  same content, only formatting/key order differs")

    print("\n== Tenants ==")
    print(f"tenants: {len(tenants):,}")
    top10 = tenants.most_common(10)
    for tenant, count in top10:
        print(f"  {tenant}: {count:,} ({count / unique:.1%})")
    top5_share = sum(count for _, count in top10[:5]) / unique
    print(f"top 5 tenants' share of events: {top5_share:.1%}")
    big_total = tenants[BIG_TENANT]
    print(f"{BIG_TENANT} events: {big_total:,} (B2 needs >= 100,050: {'yes' if big_total >= 100_050 else 'NO'})")

    print("\n== Values (unique events) ==")
    for name, counter in (("action", actions), ("actor_type", actor_types),
                          ("result", results), ("resource_type", resource_types)):
        print(f"{name}: " + ", ".join(f"{k}={v:,}" for k, v in counter.most_common()))

    print("\n== Time ==")
    to_iso = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")
    print(f"occurred_at min: {to_iso(min_ts)}")
    print(f"occurred_at max: {to_iso(max_ts)}")
    anchor = datetime.fromtimestamp(max_ts / 1000, timezone.utc).date()
    window_start = anchor - timedelta(days=29)
    in_window = sum(n for (day, _), n in big_tenant_day_action.items() if window_start <= day <= anchor)
    print(f"summary window (30 UTC days ending on newest day): {window_start} .. {anchor}")
    print(f"{BIG_TENANT} events in that window: {in_window:,}")
    sept_start, sept_end = datetime(2026, 9, 1).date(), datetime(2026, 9, 30).date()
    b3 = sum(n for (day, action), n in big_tenant_day_action.items()
             if action == "invoice.delete" and sept_start <= day <= sept_end)
    print(f"{BIG_TENANT} invoice.delete on 2026-09-01..2026-09-30: {b3:,}")

    print("\n== Lateness (newest occurred_at seen so far in file order minus this line's) ==")
    for label, values in (("first copies", lateness_first_ms), ("repeats (retries)", lateness_repeat_ms)):
        ordered = sorted(values)
        behind = sum(1 for ms in ordered if ms > 0)
        over_10_min = sum(1 for ms in ordered if ms > TEN_MINUTES_MS)
        print(f"{label}: {len(ordered):,} lines, {behind:,} behind the newest seen so far ({behind / len(ordered):.1%})")
        print(f"  p50 {human_ms(percentile(ordered, 0.50))} | p99 {human_ms(percentile(ordered, 0.99))}"
              f" | p99.9 {human_ms(percentile(ordered, 0.999))} | max {human_ms(ordered[-1])}")
        print(f"  more than 10 minutes behind: {over_10_min:,}")

    print("\n== Rule violations (unique events) ==")
    if not violations:
        print("none")
    for violation, count in violations.most_common():
        print(f"  {violation}: {count:,} (first at line {violation_example_line[violation]:,})")

    print("\n== Metadata ==")
    print(f"average size: {metadata_bytes_total / unique:.0f} bytes, max: {metadata_bytes_max:,} bytes")

    print(f"\nelapsed: {time.time() - started:.1f} s")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 scripts/explore_dataset.py data/audit_events.ndjson.gz")
    main(sys.argv[1])
