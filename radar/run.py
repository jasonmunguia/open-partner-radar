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

# Principle 3, second layer. `degraded` only fires when a run HAPPENS and produces
# nothing — it is blind to a feature that stops running at all, which is exactly how
# ingest_yc sat at failures=0/degraded=False for 259h. Each feature declares how long
# its output may legitimately go untouched; staleness is evaluated at READ time
# (principle 1) so changing a cadence re-prices history with no migration.
SOURCE_MAX_AGE_H = {
    "ingest_yc": 60,      # Tier A cadence
    "ingest_a16z": 60,    # Tier A cadence
    "score": 36,
    "news_fetch": 36,
    "digest": 36,
    "signals": 36,
    "leads": 36,
}


def _cfg(name):
    """Load a config file, falling back to its shipped .example.

    The Bring-Your-Own-Context contract: config/ holds Synphony's thesis and never
    ships, so a fresh public clone has only config/<name>.example. Falling back to it
    means a cold clone RUNS — with example values it will obviously want to replace —
    instead of dying on a missing file it was never given.

    This was a real defect: on 2026-08-29 a cold clone failed a test because
    _retired_features() read sources.yaml, got FileNotFoundError, swallowed it, and
    reported nothing as retired. The BYOC half was untested against an actual export.
    """
    base = os.path.join(ROOT, "config", name)
    for path in (base, base + ".example"):
        if os.path.exists(path):
            with open(path) as fh:
                return yaml.safe_load(fh) or {}
    raise FileNotFoundError(
        f"config/{name} not found. Copy config/{name}.example to config/{name} "
        "and edit it — see ONBOARDING.md."
    )


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
        "max_age_hours": SOURCE_MAX_AGE_H.get(feature),
        **(extra or {}),
    }
    os.makedirs(os.path.dirname(HEALTH), exist_ok=True)
    json.dump(health, open(HEALTH, "w"), indent=2)
    return health[feature]


def health_report(now=None, retired=None):
    """Evaluate health at READ time, including staleness.

    Returns {feature: {...stored..., age_h, stale, unhealthy}}. `unhealthy` is the
    number to trust: it is true when a run went bad OR when no run has landed inside
    the feature's declared cadence. The stored `degraded` flag alone cannot see the
    second case, because a feature that raises before publishing never writes at all.
    """
    now = now if now is not None else time.time()
    if not os.path.exists(HEALTH):
        return {}
    try:
        with open(HEALTH) as fh:
            health = json.load(fh)
    except json.JSONDecodeError:
        return {}

    # Injectable so the staleness rules stay testable without a config file on disk.
    # Reading sources.yaml implicitly would make this function's behaviour depend on
    # hidden global state — and a health checker you cannot test in isolation is the
    # last thing this module should be.
    retired = _retired_features() if retired is None else frozenset(retired)
    out = {}
    for feature, entry in health.items():
        if not isinstance(entry, dict):
            continue
        max_age = entry.get("max_age_hours") or SOURCE_MAX_AGE_H.get(feature)
        age_h = (now - entry.get("last_run", 0)) / 3600.0
        is_retired = feature in retired
        # A retired source is not a broken one. Flagging a deliberately disabled lane as
        # STALE forever is how a health report becomes noise the reader learns to skip —
        # the same failure this whole health layer exists to prevent.
        stale = bool(max_age and age_h > max_age) and not is_retired
        out[feature] = {
            **entry,
            "age_h": round(age_h, 1),
            "max_age_hours": max_age,
            "retired": is_retired,
            "stale": stale,
            "unhealthy": (bool(entry.get("degraded")) or stale) and not is_retired,
        }
    return out


def _retired_features():
    """Feature names whose source is disabled in config — health must not flag these.

    Maps config source names to the health feature they publish under, so disabling a
    source in sources.yaml is the single switch that also silences its health alarm.
    """
    try:
        sources = _cfg("sources.yaml")
    except Exception:                                          # noqa: BLE001 — principle 4
        return frozenset()
    off = set()
    for tier in ("tier_a", "tier_b"):
        for src in sources.get(tier) or []:
            if not src.get("enabled"):
                off.add(f"ingest_{src.get('name', '')}")
    return frozenset(off)


def cmd_health():
    """Print the read-time health table. Exit 2 if anything is unhealthy."""
    rows = health_report()
    if not rows:
        print("no health.json yet — run ingest first", file=sys.stderr)
        return 1

    print(f"{'feature':<16} {'produced':>8} {'fails':>6} {'age':>8}  {'limit':>6}  status")
    bad = []
    for feature in sorted(rows):
        r = rows[feature]
        if r["unhealthy"]:
            reason = "STALE" if r["stale"] else "DEGRADED"
            bad.append(f"{feature} ({reason})")
        elif r.get("retired"):
            reason = "retired"
        else:
            reason = "ok"
        limit = f"{r['max_age_hours']}h" if r["max_age_hours"] else "—"
        print(f"{feature:<16} {r.get('produced', 0):>8} {r.get('failures', 0):>6} "
              f"{r['age_h']:>7.1f}h {limit:>7}  {reason}")

    if bad:
        print(f"\nUNHEALTHY: {', '.join(bad)}", file=sys.stderr)
        return 2
    print("\nall features healthy")
    return 0


def _ingest_yc(sources):
    """Fetch YC shards. Returns exit status. NEVER lets an exception escape without
    publishing health first.

    This is the fix for the 259-hour false green: `ingest()` calls `batch_counts()`,
    which raises YCError the moment auth breaks. That raise used to propagate straight
    out of cmd_ingest, skipping `_publish_health` entirely — so health.json kept the
    last SUCCESSFUL run's entry and reported failures=0, degraded=False while the
    lane had been dark for eleven days. An auth outage must be louder than a quiet
    day, not silent.
    """
    yc = next((s for s in sources["tier_a"] if s["name"] == "yc"), {})
    if not yc.get("enabled"):
        print("yc source disabled in config")
        return 0

    try:
        report = ingest(yc["batches"], RAW_YC)
    except Exception as ex:                                    # noqa: BLE001 — principle 4
        _publish_health("ingest_yc", 0, 1,
                        {"error": f"{type(ex).__name__}: {str(ex)[:300]}",
                         "batches_ok": 0, "batches_failed": len(yc.get("batches") or [])})
        print(f"  FAIL yc: {type(ex).__name__}: {ex}", file=sys.stderr)
        return 2

    for ok in report["ok"]:
        print(f"  ok    {ok['batch']:>4}  {ok['got']:>4}/{ok['expected']}")
    for bad in report["failed"]:
        print(f"  FAIL  {bad['batch']:>4}  {bad['got']:>4}/{bad['expected']}  {bad['error']}")

    h = _publish_health("ingest_yc", report["total_rows"], len(report["failed"]),
                        {"batches_ok": len(report["ok"]), "batches_failed": len(report["failed"])})
    print(f"\ningested {report['total_rows']} YC rows across {len(report['ok'])} healthy shards"
          f"{' — DEGRADED' if h['degraded'] else ''}")
    return 0 if not report["failed"] else 2


def _ingest_a16z(sources):
    """a16z speedrun talent network (public API, no auth). Independent of YC."""
    a16z_cfg = next((s for s in sources["tier_a"] if s["name"] == "a16z_speedrun"), {})
    if not a16z_cfg.get("enabled"):
        return 0
    try:
        from radar.sources_ext import fetch_a16z
        rows, errs = fetch_a16z(recent_only=True)
        raw_a16z = os.path.join(ROOT, "data", "raw", "a16z")
        os.makedirs(raw_a16z, exist_ok=True)
        with open(os.path.join(raw_a16z, "companies.jsonl"), "w") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in rows)
        _publish_health("ingest_a16z", len(rows), len(errs))
        print(f"ingested {len(rows)} a16z rows ({len(errs)} errors)")
        return 0 if not errs else 2
    except Exception as ex:                                    # noqa: BLE001 — principle 4
        _publish_health("ingest_a16z", 0, 1, {"error": str(ex)[:200]})
        print(f"  FAIL a16z: {ex}", file=sys.stderr)
        return 2


def cmd_ingest():
    """Run every Tier A source. Sources are INDEPENDENT — one dying must not blind
    the others. Previously a16z sat after YC in a single flow, so the YC auth raise
    took a16z down with it: both features froze at the same timestamp for 259h.
    """
    sources = _cfg("sources.yaml")
    statuses = [_ingest_yc(sources), _ingest_a16z(sources)]
    return 2 if any(s != 0 for s in statuses) else 0


def cmd_score():
    rubric = _cfg("rubric.yaml")
    exclusions = _cfg("exclusions.yaml")
    rows = load_raw(RAW_YC) + load_raw(os.path.join(ROOT, "data", "raw", "a16z"))
    if not rows:
        _publish_health("score", 0, 1, {"note": "no raw rows — run ingest-a16z first"})
        print("no raw rows found; run `ingest-a16z` (keyless) or `ingest` first", file=sys.stderr)
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


NEWS_DIR = os.path.join(ROOT, "data", "news")


def cmd_news():
    """Fetch every news feed, drop anything already seen, queue the rest for the LLM judge.

    This is the primary discovery lane (see radar/news.py for why directories were demoted).
    Deliberately does NOT filter on keywords — the operator's instruction is that an LLM judges every
    item, because a keyword gate cannot tell "company shipped a gripper" from "listicle
    mentioning grippers", and it was the keyword stage that made the old digest useless.
    """
    from radar.news import fetch_all

    os.makedirs(NEWS_DIR, exist_ok=True)
    seen_path = os.path.join(NEWS_DIR, "seen.json")
    seen = {}
    if os.path.exists(seen_path):
        try:
            with open(seen_path) as fh:
                seen = json.load(fh)
        except json.JSONDecodeError:
            seen = {}

    items, errors, counts = fetch_all(window="7d")

    # Yield floor (principle 2). These feeds reliably return hundreds per week; a collapse to
    # near-zero means a feed changed shape, not that robotics went quiet.
    degraded = len(items) < 40
    fresh = [it for it in items if it["id"] not in seen]

    queue_path = os.path.join(NEWS_DIR, "queue.jsonl")
    with open(queue_path, "w") as fh:
        fh.writelines(json.dumps(it) + "\n" for it in fresh)
    for it in fresh:
        seen[it["id"]] = int(time.time())

    # Bounded state: 120-day window, declared at design time rather than retrofitted.
    cutoff = int(time.time()) - 120 * 86400
    seen = {k: v for k, v in seen.items() if isinstance(v, int) and v >= cutoff}
    with open(seen_path, "w") as fh:
        json.dump(seen, fh)

    _publish_health("news_fetch", len(items), len(errors),
                    {"fresh": len(fresh), "per_feed": counts, "below_floor": degraded})
    for e in errors[:5]:
        print(f"  [warn] {e}", file=sys.stderr)
    print(f"news: {len(items)} fetched, {len(fresh)} new -> {queue_path}"
          f"{'  — DEGRADED (below yield floor)' if degraded else ''}")
    print(f"  per-feed: {counts}")
    return 2 if (degraded or errors) else 0


def _read_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def cmd_signals():
    """Classify the news queue into typed signals and roll them up per entity.

    This is the layer that makes the radar answer 'what changed' rather than 'what
    exists'. Signals decay by type at read time, so a company that went quiet falls
    off on its own — no hand-pruned watchlist.
    """
    from radar.signal_news import signals_from_news
    from radar.signals import ingest_signals, load_signals, ranked

    raw_signals = os.path.join(ROOT, "data", "raw", "signals")
    news_items = _read_jsonl(os.path.join(ROOT, "data", "news", "queue.jsonl"))
    known = _read_jsonl(os.path.join(DERIVED, "scored.jsonl"))

    if not known:
        _publish_health("signals", 0, 1, {"note": "no scored.jsonl — run score first"})
        print("no scored universe to match against; run score first", file=sys.stderr)
        return 1

    matched, unmatched = signals_from_news(news_items, known)
    report = ingest_signals(matched, raw_signals)

    # Discovery queue: real events about companies the radar has never seen.
    disco_path = os.path.join(DERIVED, "signal_discoveries.jsonl")
    with open(disco_path, "w") as fh:
        fh.writelines(json.dumps(u) + "\n" for u in unmatched)

    _publish_health("signals", report["new"], 0,
                    {"news_in": len(news_items), "matched": len(matched),
                     "unmatched": len(unmatched), "by_kind": report["by_kind"],
                     "total_stored": report["total_stored"]})

    hot = ranked(load_signals(raw_signals), limit=15, min_heat=0.5)
    print(f"news items in : {len(news_items)}")
    print(f"matched       : {len(matched)} ({report['new']} new, {report['duplicate']} dup)")
    print(f"unmatched     : {len(unmatched)} -> {disco_path}")
    print(f"by kind       : {report['by_kind']}")
    if hot:
        print(f"\n{'entity':<34} {'heat':>7} {'sigs':>5} {'last':>7}  kinds")
        for r in hot:
            kinds = ",".join(f"{k}x{v}" for k, v in sorted(r["kinds"].items()))
            name = (r["entity_name"] or r["entity_key"])[:33]
            print(f"{name:<34} {r['heat']:>7.2f} {r['count']:>5} {r['age_days']:>6.1f}d  {kinds}")
    else:
        print("\nno entities above the heat floor yet")
    return 0


def cmd_leads():
    """Harvest co-mentioned companies from judged news into a research queue.

    WHY THIS EXISTS. Discovery was 100% preset: 24 hardcoded queries decided what the judge
    ever saw, so a company outside that funnel was invisible no matter how good the judging
    got. Example Competitor C — Travis Kalanick's $1.7B a16z-led company targeting food, Synphony's primary
    lane — appeared in 0 of 141 judged rows for exactly that reason. Not a judgment failure;
    a coverage failure.

    Articles name comparables constantly ("Example Competitor C, like Figure and Physical Intelligence...").
    The judge already has the full article text in context, so emitting those names costs
    ~20 output tokens and no extra call — the AI half is free. Everything here is the script
    half: dedup against what we already know, and queue the rest.

    Deliberately NOT rule-based extraction from raw text. `signal_news.py` documents why:
    "Ant Group Backs Hong Kong Robotics Startup Daimeng" needs the *recipient*, and getting
    that wrong poisons the entity graph. An LLM that has read the article knows; a regex does not.
    """
    judged = os.path.join(ROOT, "data", "news", "judged.jsonl")
    leads_path = os.path.join(ROOT, "data", "news", "leads.jsonl")
    if not os.path.exists(judged):
        print("no judged.jsonl yet")
        return 0

    known = set()
    for path in (os.path.join(DERIVED, "reranked.jsonl"), judged, leads_path):
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for key in ("name", "company"):
                    if rec.get(key):
                        known.add(rec[key].strip().lower())

    fresh, seen_now = [], set()
    with open(judged) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for name in rec.get("companies_mentioned") or []:
                clean = (name or "").strip()
                low = clean.lower()
                if not clean or low in known or low in seen_now:
                    continue
                seen_now.add(low)
                fresh.append({"name": clean,
                              "found_via": rec.get("company"),
                              "source_url": rec.get("source_url"),
                              "queued_at": int(time.time())})

    if fresh:
        with open(leads_path, "a") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in fresh)

    _publish_health("leads", len(fresh), 0, {"known_universe": len(known)})
    print(f"leads: {len(fresh)} new co-mentioned companies -> {leads_path}")
    for r in fresh[:10]:
        print(f"  • {r['name']}  (via {r['found_via']})")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    # Per-source entry points exist so the scheduler can gate each source on its OWN
    # artifact age. `ingest` (all sources) stays for manual use.
    table = {"ingest": cmd_ingest, "score": cmd_score, "report": cmd_report,
             "news": cmd_news, "health": cmd_health, "signals": cmd_signals,
             "leads": cmd_leads,
             "ingest-yc": lambda: _ingest_yc(_cfg("sources.yaml")),
             "ingest-a16z": lambda: _ingest_a16z(_cfg("sources.yaml"))}
    if cmd not in table:
        print(f"unknown command {cmd!r}; use {'|'.join(sorted(table))}", file=sys.stderr)
        return 1
    return table[cmd]()


if __name__ == "__main__":
    sys.exit(main())
