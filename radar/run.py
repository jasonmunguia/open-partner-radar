"""Orchestrator: ingest -> score -> report, with feature-level health.

Principles 3 and 4: every derived feature publishes an output count, and no
caught exception ends at stderr. `refresh_funded()` in internship-radar died on
a NameError every single run for its entire life while heartbeat reported
`dark_sources: []` — because health watched sources, not features.

Usage:
  python3 -m radar.run ingest       # fetch YC shards, assert yield
  python3 -m radar.run score        # rebuild derived/scored.jsonl from raw/
  python3 -m radar.run report       # print tier counts + top T1/T2
"""
import json
import os
import sys
import time

import yaml

from radar import score as scoring
from radar.ycfetch import ingest, load_raw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_YC = os.path.join(ROOT, "data", "raw", "yc")
DERIVED = os.path.join(ROOT, "data", "derived")
HEALTH = os.path.join(ROOT, "data", "health.json")

TIER_ORDER = ["CANDIDATE_ACCELERANT", "CANDIDATE_HARDWARE", "CANDIDATE_CUSTOMER",
              "CANDIDATE_CHANNEL", "T1_PARTNER_NOW", "T2_ABSORB_TRACK", "T3_CUSTOMER",
              "T4_CHANNEL", "T5_WATCH", "T0_COALITION", "T0_EXISTING", "T6_PASS"]
REPORT_TIERS = ["CANDIDATE_ACCELERANT", "CANDIDATE_HARDWARE"]
# Which axis ranks each reported tier — the primary reason a company is in that tier.
TIER_SORT_AXIS = {"CANDIDATE_ACCELERANT": "A", "CANDIDATE_HARDWARE": "H",
                  "CANDIDATE_CUSTOMER": "D", "CANDIDATE_CHANNEL": "C"}


def _cfg(name):
    with open(os.path.join(ROOT, "config", name)) as fh:
        return yaml.safe_load(fh)


def _publish_health(feature, produced, failures, extra=None):
    """Every feature declares what it produced. Zero-with-history is a failure."""
    health = {}
    if os.path.exists(HEALTH):
        try:
            health = json.load(open(HEALTH))
        except json.JSONDecodeError:
            health = {}
    prior = health.get(feature, {})
    ever_produced = bool(prior.get("ever_produced")) or produced > 0
    streak = 0 if produced > 0 else prior.get("zero_streak", 0) + 1
    health[feature] = {
        "last_run": int(time.time()),
        "produced": produced,
        "failures": failures,
        "zero_streak": streak,
        "ever_produced": ever_produced,
        "degraded": bool(ever_produced and streak >= 2) or failures > 0,
        **(extra or {}),
    }
    os.makedirs(os.path.dirname(HEALTH), exist_ok=True)
    json.dump(health, open(HEALTH, "w"), indent=2)
    return health[feature]


def cmd_ingest():
    sources = _cfg("sources.yaml")
    yc = next(s for s in sources["tier_a"] if s["name"] == "yc")
    if not yc.get("enabled"):
        print("yc source disabled in config")
        return 1

    report = ingest(yc["batches"], RAW_YC)
    for ok in report["ok"]:
        print(f"  ok    {ok['batch']:>4}  {ok['got']:>4}/{ok['expected']}")
    for bad in report["failed"]:
        print(f"  FAIL  {bad['batch']:>4}  {bad['got']:>4}/{bad['expected']}  {bad['error']}")

    h = _publish_health("ingest_yc", report["total_rows"], len(report["failed"]),
                        {"batches_ok": len(report["ok"]), "batches_failed": len(report["failed"])})
    print(f"\ningested {report['total_rows']} YC rows across {len(report['ok'])} healthy shards"
          f"{' — DEGRADED' if h['degraded'] else ''}")

    # --- a16z speedrun talent network (public API, no auth) --------------------------------
    a16z_cfg = next((s for s in sources["tier_a"] if s["name"] == "a16z_speedrun"), {})
    if a16z_cfg.get("enabled"):
        try:
            from radar.sources_ext import fetch_a16z
            rows, errs = fetch_a16z(recent_only=True)
            raw_a16z = os.path.join(ROOT, "data", "raw", "a16z")
            os.makedirs(raw_a16z, exist_ok=True)
            with open(os.path.join(raw_a16z, "companies.jsonl"), "w") as fh:
                for r in rows:
                    fh.write(json.dumps(r) + "\n")
            _publish_health("ingest_a16z", len(rows), len(errs))
            print(f"ingested {len(rows)} a16z rows ({len(errs)} errors)")
        except Exception as ex:
            _publish_health("ingest_a16z", 0, 1, {"error": str(ex)[:200]})
            print(f"  FAIL a16z: {ex}", file=sys.stderr)

    return 0 if not report["failed"] else 2


def cmd_score():
    rubric = _cfg("rubric.yaml")
    exclusions = _cfg("exclusions.yaml")
    rows = load_raw(RAW_YC) + load_raw(os.path.join(ROOT, "data", "raw", "a16z"))
    if not rows:
        _publish_health("score", 0, 1, {"note": "no raw rows — run ingest first"})
        print("no raw rows found; run `ingest` first", file=sys.stderr)
        return 2

    scored = scoring.score_all(rows, rubric, exclusions)
    os.makedirs(DERIVED, exist_ok=True)
    out = os.path.join(DERIVED, "scored.jsonl")
    with open(out, "w") as fh:
        for rec in scored:
            fh.write(json.dumps(rec) + "\n")

    queued = sum(1 for r in scored if r["stage2"])
    _publish_health("score", len(scored), 0, {"stage2_queued": queued})
    print(f"scored {len(scored)} companies -> {out}  ({queued} queued for stage-2 review)")
    return 0


def _load_scored():
    path = os.path.join(DERIVED, "scored.jsonl")
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


def cmd_report(limit=25):
    scored = _load_scored()
    if not scored:
        print("nothing scored yet", file=sys.stderr)
        return 2

    counts = {}
    for r in scored:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1
    print(f"universe: {len(scored)} companies\n")
    for tier in TIER_ORDER:
        if counts.get(tier):
            print(f"  {tier:<16} {counts[tier]:>4}")

    for tier in REPORT_TIERS:
        axis = TIER_SORT_AXIS.get(tier, "A")
        picks = [r for r in scored if r["tier"] == tier]
        picks.sort(key=lambda r: (r["scores"].get(axis, 0), r["scores"]["L"]), reverse=True)
        print(f"\n{'=' * 78}\n{tier} — top {min(limit, len(picks))} of {len(picks)}\n{'=' * 78}")
        for r in picks[:limit]:
            s = r["scores"]
            print(f"\n{r['name']}  [{r['batch']}]  {r.get('location', '?')}  "
                  f"team={r.get('team_size') or '?'}")
            print("  " + " ".join(f"{k}{v}" for k, v in sorted(s.items()))
                  + f"   {r.get('website', '')}")
            print(f"  {r.get('one_liner', '')[:110]}")
            if r["why"].get(axis):
                print(f"  {axis}: {', '.join(r['why'][axis][:5])}")
            if r["flags"]:
                print(f"  flags: {'; '.join(r['flags'])}")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    table = {"ingest": cmd_ingest, "score": cmd_score, "report": cmd_report}
    if cmd not in table:
        print(f"unknown command {cmd!r}; use ingest|score|report", file=sys.stderr)
        return 1
    return table[cmd]()


if __name__ == "__main__":
    sys.exit(main())
