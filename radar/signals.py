"""Signals: timestamped events about companies, with type-specific decay.

Why this exists. `scored.jsonl` answers "which companies exist and how do they rank."
That is a directory, and a directory is equally stale everywhere — a row scored in
July reads exactly like a row scored this morning. Industry intel is a different
question: *what changed, and when.*

A signal is an event with a timestamp and a half-life. That single addition is what
lets the radar say "this got interesting last week" instead of handing back the same
ranked list every day.

Design, following the house principles:
  1. Store raw, score at read — signals persist immutably; decay is a pure function
     applied on load, so re-tuning a half-life re-prices all history with no migration.
  2. Assert yield per source — `ingest_signals` reports counts; the caller publishes.
  5. Dedup on canonical entity identity — signals key to the same `canonical_key`
     as company rows, so a signal found on X joins the YC row for the same company.
  7. Bounded state — signals older than RETENTION_DAYS are dropped at load.

Half-lives are not decoration. A hiring req is stale in six weeks because it gets
filled; a new plant is a deployment window that stays open for a year. Giving both
the same decay is the mistake that makes "recency" meaningless.
"""
from __future__ import annotations

import json
import math
import os
import re
import time

RETENTION_DAYS = 365

# type -> (base weight, half-life in days)
#
# Weights encode Synphony's deployment-speed objective, not generic newsworthiness.
# `hiring_automation` outranks `funding` deliberately: a company posting for an
# automation engineer has already decided to deploy and is staffing it, which is a
# nearer-term trigger than a raise that may fund anything.
SIGNAL_TYPES = {
    "hiring_automation": (10.0, 45),   # active deployment intent; req gets filled
    "facility":          (9.0, 270),   # new plant/line — greenfield window, stays open
    "funding":           (7.0, 180),   # budget exists; timing trigger
    "exec_move":         (6.0, 120),   # new budget owner (COO/VP Ops/automation lead)
    "partnership":       (5.0, 180),   # integrator tie-up — may signal a closed door
    "launch":            (4.0, 90),
    "press":             (1.5, 60),    # generic mention; weakest
}

DEFAULT_TYPE = "press"


def _norm_domain(url_or_domain: str) -> str:
    s = (url_or_domain or "").lower()
    s = re.sub(r"^https?://", "", s).strip("/")
    s = re.sub(r"^www\.", "", s).split("/")[0]
    return s


def entity_key(*, yc_id: str = "", website: str = "", name: str = "") -> str:
    """Mirror of ycfetch.canonical_key so signals and company rows collide correctly."""
    if yc_id:
        return f"yc:{yc_id}"
    site = _norm_domain(website)
    if site:
        return f"domain:{site}"
    return "name:" + (name or "").lower().strip()


def make_signal(*, kind, title, url, source, ts=None, entity_name="",
                yc_id="", website="", evidence=None, magnitude=1.0):
    """Build one normalized signal record.

    `magnitude` scales the base weight for signals that carry size — a $50M raise
    versus a $2M pre-seed, ten automation reqs versus one. Default 1.0 means
    "present, size unknown", never zero.
    """
    if kind not in SIGNAL_TYPES:
        kind = DEFAULT_TYPE
    key = entity_key(yc_id=yc_id, website=website, name=entity_name)
    ts = int(ts if ts is not None else time.time())
    sid = f"{key}|{kind}|{_norm_domain(url) or url}|{ts // 86400}"
    return {
        "id": sid,
        "entity_key": key,
        "entity_name": entity_name,
        "kind": kind,
        "ts": ts,
        "source": source,
        "title": (title or "")[:300],
        "url": url,
        "magnitude": float(magnitude),
        "evidence": (evidence or "")[:500],
    }


def decayed_weight(sig, now=None):
    """Exponential decay by the signal type's half-life. Pure, read-time (principle 1)."""
    now = now if now is not None else time.time()
    base, half_life = SIGNAL_TYPES.get(sig.get("kind"), SIGNAL_TYPES[DEFAULT_TYPE])
    age_days = max(0.0, (now - sig.get("ts", 0)) / 86400.0)
    return base * float(sig.get("magnitude", 1.0)) * math.pow(0.5, age_days / half_life)


def ingest_signals(rows, raw_dir, shard="signals.jsonl"):
    """Append new signals, deduped by id. Returns a yield report (principle 2).

    Append-only: an event that happened does not un-happen, so this never rewrites
    history — it only adds ids it has not seen.
    """
    os.makedirs(raw_dir, exist_ok=True)
    path = os.path.join(raw_dir, shard)

    existing = {}
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                existing[rec.get("id")] = rec

    # Dedup against BOTH what is already stored and what is earlier in this batch —
    # the same event routinely arrives twice in one run from two sources.
    seen_ids = set(existing)
    fresh = []
    for r in rows:
        rid = r.get("id")
        if not rid or rid in seen_ids:
            continue
        seen_ids.add(rid)
        fresh.append(r)

    if fresh:
        with open(path, "a") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in fresh)

    by_kind = {}
    for r in fresh:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    return {"received": len(rows), "new": len(fresh),
            "duplicate": len(rows) - len(fresh),
            "total_stored": len(existing) + len(fresh), "by_kind": by_kind}


def load_signals(raw_dir, shard="signals.jsonl", now=None, retention_days=RETENTION_DAYS):
    """Read signals back, dropping anything past retention (principle 7)."""
    now = now if now is not None else time.time()
    path = os.path.join(raw_dir, shard)
    if not os.path.exists(path):
        return []
    cutoff = now - retention_days * 86400
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ts", 0) >= cutoff:
                out.append(rec)
    return out


def heat(signals, now=None):
    """Roll signals up per entity into a decayed 'heat' score.

    Returns {entity_key: {...}} sorted-ready. `heat` is the sum of decayed weights,
    so three fresh signals beat one old one, and a company that went quiet six months
    ago falls off on its own without anybody pruning a list by hand.
    """
    now = now if now is not None else time.time()
    agg = {}
    for sig in signals:
        key = sig.get("entity_key")
        if not key:
            continue
        w = decayed_weight(sig, now)
        cur = agg.setdefault(key, {
            "entity_key": key, "entity_name": sig.get("entity_name", ""),
            "heat": 0.0, "count": 0, "kinds": {}, "latest_ts": 0, "signals": [],
        })
        cur["heat"] += w
        cur["count"] += 1
        cur["kinds"][sig["kind"]] = cur["kinds"].get(sig["kind"], 0) + 1
        if sig.get("ts", 0) > cur["latest_ts"]:
            cur["latest_ts"] = sig["ts"]
            cur["entity_name"] = sig.get("entity_name") or cur["entity_name"]
        cur["signals"].append(sig)

    for cur in agg.values():
        cur["heat"] = round(cur["heat"], 3)
        cur["signals"].sort(key=lambda s: s.get("ts", 0), reverse=True)
        cur["age_days"] = round((now - cur["latest_ts"]) / 86400.0, 1)
    return agg


def ranked(signals, now=None, limit=None, min_heat=0.0):
    """Entities ordered by heat — the 'what got interesting' list."""
    rows = sorted(heat(signals, now).values(), key=lambda r: r["heat"], reverse=True)
    rows = [r for r in rows if r["heat"] >= min_heat]
    return rows[:limit] if limit else rows
