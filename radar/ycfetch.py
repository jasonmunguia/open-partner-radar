"""YC adapter. Batch-sharded ingestion with ground-truth yield assertion.

Principle 2: every shard is checked against a live count probe. The Amazon
`intern` vs `internship` bug (12 results instead of 1,536, silently, for weeks)
is impossible here because the API tells us how many records should exist
before we fetch them.

Access notes verified 2026-07-29:
  - `attach_csv` returns METADATA ONLY and writes no file. Do not use it.
    Records arrive as an inline CSV string in result.csv_results.
  - limit max 200, page 0-indexed. Batch shards are <=248 records, so no
    shard exceeds 2 pages and Algolia deep-pagination limits are never hit.
  - There is no plain company-name column; the name lives inside the `link`
    markdown field. URLs carry a leading "'" spreadsheet-safety prefix.
"""
import csv
import io
import json
import os
import re
import subprocess
import time

EXTRA_FIELDS = ",".join([
    "long_description", "website", "team_size", "current_location",
    "industry", "subindustry", "tags", "objectID", "isHiring",
    "active_founders.title", "batch_display_name",
])

MD_LINK = re.compile(r"\[(?P<text>[^\]]*)\]\((?P<url>[^)]*)\)")

# The yc CLI silently truncates stdout at ~64 KiB, mid-JSON-string, while still
# exiting 0 with "status": "success". Measured 2026-07-29 on an S25 shard:
#   limit=25 -> 27.8 KB ok | limit=50 -> 56.3 KB ok
#   limit=100 -> 66.9 KB TRUNCATED | limit=200 -> 69.6 KB TRUNCATED
# 40 keeps every page comfortably under the cap with long_description included.
# Do NOT raise this without re-measuring; the failure mode is corrupt data, not an error.
PAGE_LIMIT = 40


class YCError(RuntimeError):
    pass


def _parse_json_payload(stdout):
    """Parse yc output, tolerating status lines printed to stdout before the JSON.

    The CLI emits human-readable notices on stdout, not stderr — e.g.
    "Token expired, refreshing..." ahead of the payload. Parsing stdout naively breaks
    every time an access token rolls over, which is both intermittent and easy to
    misdiagnose as a data problem. Slice from the first brace instead.
    """
    start = stdout.find("{")
    if start == -1:
        raise YCError(f"no JSON in yc output; head={stdout[:200]!r}")
    try:
        return json.loads(stdout[start:])
    except json.JSONDecodeError as ex:
        raise YCError(f"unparseable yc output: {ex}; head={stdout[start:start + 200]!r}")


def yc_call(payload, timeout=180):
    """Run one search.companies call. Raises YCError — never returns a silent empty."""
    proc = subprocess.run(
        ["yc", "tools", "run", "search.companies", "--input", json.dumps(payload), "--json"],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise YCError(f"yc exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    doc = _parse_json_payload(proc.stdout)
    result = doc.get("result") or {}
    if result.get("status") != "success":
        raise YCError(f"yc status={result.get('status')!r} body={str(result)[:300]}")
    return result


def batch_counts():
    """Ground truth: how many companies exist per batch, straight from the index."""
    result = yc_call({"limit": 0, "group_counts_by": "batch"})
    counts = result.get("counts") or {}
    if not counts:
        raise YCError("batch count probe returned no counts — index or auth problem")
    return {k: v for k, v in counts.items() if k != "Unspecified"}


def _clean(value):
    if value is None:
        return ""
    v = value.strip()
    # repeated-empty artifacts render as sequences of bare quotes
    if not v.strip('", '):
        return ""
    if v.startswith("'"):          # spreadsheet-safety prefix on URLs
        v = v[1:]
    return v


def _parse_rows(csv_text):
    """csv_results -> list of normalized dicts."""
    rows = []
    for raw in csv.DictReader(io.StringIO(csv_text)):
        link_field = raw.get("link") or ""
        m = MD_LINK.search(link_field)
        name = (m.group("text") if m else link_field).strip()
        bookface_url = m.group("url") if m else ""
        if not name:
            continue
        founders = [
            {"name": fm.group("text"), "bookface": fm.group("url")}
            for fm in MD_LINK.finditer(raw.get("active_founders.link") or "")
        ]
        rows.append({
            "source": "yc",
            "source_id": _clean(raw.get("id")) or _clean(raw.get("objectID")),
            "name": name,
            "bookface_url": bookface_url,
            "batch": _clean(raw.get("batch")),
            "status": _clean(raw.get("status")),
            "one_liner": _clean(raw.get("one_liner")),
            "long_description": _clean(raw.get("long_description")),
            "website": _clean(raw.get("website")),
            "team_size": _clean(raw.get("team_size")),
            "location": _clean(raw.get("current_location")),
            "industry": _clean(raw.get("industry")),
            "subindustry": _clean(raw.get("subindustry")),
            "tags": _clean(raw.get("tags")),
            "is_hiring": _clean(raw.get("isHiring")),
            "yc_partner": _clean(raw.get("partners")),
            "founders": founders,
        })
    return rows


def fetch_batch(batch):
    """All companies in one batch, paginated. Returns (rows, reported_total)."""
    rows, page, reported_total = [], 0, None
    while True:
        result = yc_call({
            "filters": {"batch": batch},
            "limit": PAGE_LIMIT,
            "page": page,
            "extra_fields": EXTRA_FIELDS,
        })
        if reported_total is None:
            reported_total = result.get("total_count")
        chunk = _parse_rows(result.get("csv_results") or "")

        # Second line of defence against the 64 KiB truncation: the response tells
        # us how many records it contains. If we parsed fewer, the payload was cut
        # in a way that still happened to yield valid JSON. Never accept it.
        declared = result.get("count")
        if isinstance(declared, int) and len(chunk) != declared:
            raise YCError(
                f"batch {batch} page {page}: parsed {len(chunk)} rows but response "
                f"declared {declared} — output truncated, lower PAGE_LIMIT")

        rows.extend(chunk)
        if len(chunk) < PAGE_LIMIT:
            break
        page += 1
        if page > 30:                      # hard stop; largest shard is 248/40 = 7 pages
            raise YCError(f"batch {batch}: pagination exceeded 30 pages")
        time.sleep(0.5)
    return rows, reported_total


def ingest(batches, raw_dir, tolerance=0.90):
    """Fetch every batch, assert yield against ground truth, write raw shards.

    Returns a report dict. A batch that under-delivers is recorded as a FAILURE
    with its numbers — it is never silently accepted as an empty result.
    """
    os.makedirs(raw_dir, exist_ok=True)
    truth = batch_counts()
    report = {"ok": [], "failed": [], "total_rows": 0, "truth": truth}

    for batch in batches:
        expected = truth.get(batch)
        try:
            rows, reported = fetch_batch(batch)
        except Exception as ex:
            report["failed"].append({"batch": batch, "expected": expected,
                                     "got": 0, "error": str(ex)[:300]})
            continue

        got = len(rows)
        if expected and got < expected * tolerance:
            report["failed"].append({
                "batch": batch, "expected": expected, "got": got,
                "reported_total": reported,
                "error": f"yield {got}/{expected} below {tolerance:.0%} floor",
            })
            # still persist what we got — raw is never thrown away
        else:
            report["ok"].append({"batch": batch, "expected": expected, "got": got})

        shard = os.path.join(raw_dir, f"{batch}.jsonl")
        with open(shard, "w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        report["total_rows"] += got

    return report


def load_raw(raw_dir):
    """Read every shard back. Dedup on canonical identity (principle 5)."""
    seen, rows = set(), []
    if not os.path.isdir(raw_dir):
        return rows
    for fname in sorted(os.listdir(raw_dir)):
        if not fname.endswith(".jsonl"):
            continue
        with open(os.path.join(raw_dir, fname)) as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = canonical_key(row)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
    return rows


def canonical_key(row):
    """Stable identity across sources: YC id if present, else registrable domain."""
    if row.get("source") == "yc" and row.get("source_id"):
        return f"yc:{row['source_id']}"
    site = (row.get("website") or "").lower()
    site = re.sub(r"^https?://", "", site).strip("/")
    site = re.sub(r"^www\.", "", site).split("/")[0]
    return f"domain:{site}" if site else "name:" + (row.get("name") or "").lower().strip()
