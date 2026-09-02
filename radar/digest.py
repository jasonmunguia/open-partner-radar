"""Daily 10am digest: companies + posts, with links, ready for the operator to act on.

Delivery is SMTP with an explicitly named account (principle 6) via tools/mailer.py in this repo.
No self-send, no GitHub-issue relay — those cost the internship radar six rewrites.

Dedup is on canonical entity identity (principle 5), so a company that appears in YC and
again via Exa is emailed once, ever.

Usage:
  python3 -m radar.digest          # build + send
  python3 -m radar.digest --dry    # print HTML to stdout, send nothing
"""
import csv
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DERIVED = os.path.join(ROOT, "data", "derived")
SEEN = os.path.join(DERIVED, "digest_seen.json")
HEALTH = os.path.join(ROOT, "data", "health.json")
# The mailer ships in-repo (tools/); the operator-level copy is a fallback for old installs.
sys.path.insert(0, os.path.expanduser("~/.claude/tools"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

# v4 taxonomy (the operator 2026-08-16). One criterion: is the company's technology INSIDE
# Synphony's core competency (models, fine-tuning for tasks, deployment) or outside it?
#   inside  -> ABSORB   integrate, deploy, then build the capability in-house
#   outside -> PARTNER  buy and keep buying; arms, sensors, teleop are not our specialty
# See scripts/migrate_tiers_v4.py for why the old T1/T2 split was incoherent.
TIER_LABEL = {
    "PARTNER": ("Partner — buy it, don't build it", "#0b7a3b"),
    "ABSORB": ("Absorb — integrate, then rebuild in-house", "#1558b0"),
    "WATCH": ("Competitor — intel only, never contact", "#a11"),
    "INTEL": ("Industry intel", "#6b3fa0"),
    # Stage-1 candidate tiers, still emitted by the keyword prefilter.
    "CANDIDATE_ACCELERANT": ("Deployment accelerant", "#1558b0"),
    "CANDIDATE_HARDWARE": ("Cheap/free hardware", "#8a5a00"),
    "CANDIDATE_UNKNOWN": ("Needs a look — thin public info", "#6b6b6b"),
}
# Email order. Partners first (they unblock a deployment), then absorb targets, then
# competitor intel — which the operator reads deliberately, not as leftovers.
SECTIONS = ["PARTNER", "ABSORB", "WATCH", "INTEL",
            "CANDIDATE_ACCELERANT", "CANDIDATE_HARDWARE", "CANDIDATE_UNKNOWN"]
POST_QUERIES = ["robotics", "robot arm", "manipulation", "humanoid", "teleoperation",
                "industrial automation", "manufacturing robot"]


def _cfg(name):
    with open(os.path.join(ROOT, "config", name)) as fh:
        return yaml.safe_load(fh)


def _load_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _seen():
    if os.path.exists(SEEN):
        try:
            return json.load(open(SEEN))
        except json.JSONDecodeError:
            pass
    return {"companies": {}, "posts": {}, "last_sent": 0}


def _yc(payload, timeout=120):
    proc = subprocess.run(
        ["yc", "tools", "run", "search", "--input", json.dumps(payload), "--json"],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:200])
    # CLI prints notices ("Token expired, refreshing...") to stdout before the JSON.
    start = proc.stdout.find("{")
    if start == -1:
        raise RuntimeError(f"no JSON in yc output: {proc.stdout[:160]!r}")
    result = (json.loads(proc.stdout[start:]) or {}).get("result") or {}
    if result.get("status") != "success":
        raise RuntimeError(f"status={result.get('status')}")
    return result


BOOKFACE_RAW = os.path.join(ROOT, "data", "raw", "bookface")
BOOKFACE_MAX_AGE_H = 48


def load_bookface_posts(max_age_h=BOOKFACE_MAX_AGE_H):
    """Read Bookface posts from raw shards written by the operator-local Bookface capture
    script (scripts/arc_bookface.py in the private tree; it is tied to one browser profile and
    does not ship in the public edition — this lane is OPTIONAL).

    Deliberately a FILE read, not a browser call. The Arc reader needs Arc running
    with a debug port and Bookface open; the digest runs from cron at 7am when that
    is not guaranteed. Splitting them means a closed browser degrades one lane
    instead of killing the email — and it matches the repo's raw/derived split
    (principle 1: store raw, read later).

    Staleness is reported, never hidden. A shard nobody refreshed is a dark lane,
    which is the exact failure the health work this week existed to surface.
    """
    posts, errors = [], []
    if not os.path.isdir(BOOKFACE_RAW):
        # Never configured, not broken: an optional lane that was never set up must not paint
        # a healthy install red (the 2026-09-01 cold-clone audit saw exactly that).
        return posts, []

    now = time.time()
    for fname in sorted(os.listdir(BOOKFACE_RAW)):
        if not fname.endswith(".jsonl"):
            continue
        path = os.path.join(BOOKFACE_RAW, fname)
        age_h = (now - os.path.getmtime(path)) / 3600.0
        if age_h > max_age_h:
            errors.append(f"bookface/{fname[:-6]}: stale, {int(age_h)}h old "
                          f"(limit {max_age_h}h) — refresh the Bookface capture")
            continue
        posts.extend(_load_jsonl(path))
    return posts, errors


def fetch_posts(limit_per_query=6):
    """Legacy Launch-YC lane via the `yc` CLI.

    Kept only for the `launches` entity. The `forum` entity moved to Bookface-over-Arc
    (load_bookface_posts) because this CLI authenticates as a different person and its
    token has been dead since 2026-08-05 — 2 entities x 11 queries produced exactly the
    22 failures that flagged the digest degraded every run.
    """
    posts, errors = [], []
    for entity in ("launches",):
        for query in POST_QUERIES:
            try:
                res = _yc({"entity": entity, "query": query,
                           "limit": limit_per_query, "extra_fields": "url,indexed_at"})
            except Exception as ex:
                errors.append(f"{entity}/{query}: {str(ex)[:90]}")
                continue
            for row in csv.DictReader(io.StringIO(res.get("csv_results") or "")):
                link = row.get("link") or ""
                m = re.search(r"\[([^\]]*)\]\(([^)]*)\)", link)
                title = (m.group(1) if m else link).strip()
                url = m.group(2) if m else (row.get("url") or "")
                if not title or not url:
                    continue
                posts.append({"kind": entity, "title": title, "url": url,
                              "id": row.get("objectID") or url,
                              "blurb": (row.get("one_liner") or row.get("searchable_title")
                                        or "")[:160]})
    return posts, errors


def _news_card(rec):
    """The primary email unit: a news event, why it matters, and TWO links.

    the operator's requirement (2026-08-15): always link to the source the item was found in AND to
    the company's own website. The source proves where it came from; the company site is where
    he actually goes to evaluate.
    """
    company = rec.get("company") or "?"
    tier = rec.get("tier") or ""
    label, colour = TIER_LABEL.get(tier, (tier.replace("_", " ").title(), "#333"))
    what = rec.get("what_happened") or ""
    why = rec.get("why_it_matters") or ""
    ask = rec.get("validation_question") or ""
    known = rec.get("known")
    links = []
    if rec.get("company_url"):
        links.append(f'<a href="{rec["company_url"]}" style="color:#1558b0"><b>{company} site</b></a>')
    if rec.get("source_url"):
        src = rec.get("source_publisher") or "source"
        links.append(f'<a href="{rec["source_url"]}" style="color:#666">{src}</a>')
    meta = " · ".join(x for x in [rec.get("published", "")[:16],
                                  f"I={rec['I']}" if rec.get("I") is not None else "",
                                  "already tracked" if known else ""] if x)
    return f"""
<div style="margin:0 0 20px 0;padding:12px 14px;border-left:3px solid {colour}">
  <div style="font-size:15px;font-weight:600">{company}
    <span style="font-weight:400;font-size:12px;color:{colour}"> · {label}</span></div>
  <div style="margin:5px 0;font-size:14px;color:#222">{what}</div>
  <div style="margin:5px 0;font-size:14px;color:#0b7a3b"><b>Why:</b> {why}</div>
  {f'<div style="margin:4px 0;font-size:13px;color:#8a5a00"><b>Ask them:</b> {ask}</div>' if ask else ''}
  <div style="font-size:12px;color:#888;margin-top:6px">{' · '.join(links)}
    {f'<span style="color:#bbb"> — {meta}</span>' if meta else ''}</div>
</div>"""


def _normalize(rec):
    """Reconcile the two record shapes.

    Stage 1 (radar/score.py) writes {scores:{A,H,D,C,I,L}}. The stage-2 reranker writes
    `I` at top level plus `stage1_scores`, and omits canonical_key. Rather than force the
    reranker into a rigid schema, absorb both shapes here — the digest is the only consumer.
    """
    out = dict(rec)
    scores = dict(out.get("scores") or out.get("stage1_scores") or {})
    if out.get("I") is not None:
        scores["I"] = out["I"]
    out["scores"] = scores
    return out


def _card(rec):
    s = rec.get("scores") or {}
    site = rec.get("website") or ""
    bf = rec.get("bookface_url") or ""
    links = []
    if site:
        links.append(f'<a href="{site}" style="color:#1558b0">site</a>')
    if bf:
        links.append(f'<a href="{bf}" style="color:#1558b0">bookface</a>')
    meta = " · ".join(x for x in [rec.get("batch"),
                                  f"{rec.get('team_size')} people" if rec.get("team_size") else "",
                                  rec.get("location") or ""] if x)
    why = rec.get("why_it_matters") or rec.get("one_liner") or ""
    angle = rec.get("angle") or ""
    ask = rec.get("validation_question") or ""
    scoreline = " ".join(f"{k}{v}" for k, v in sorted(s.items())) if s else ""
    return f"""
<div style="margin:0 0 18px 0;padding:12px 14px;border-left:3px solid #ddd">
  <div style="font-size:15px;font-weight:600">{rec.get('name','?')}
    <span style="font-weight:400;color:#666;font-size:13px"> — {meta}</span></div>
  <div style="margin:4px 0 6px 0;font-size:14px;color:#222">{why}</div>
  {f'<div style="margin:4px 0;font-size:13px;color:#0b7a3b"><b>Angle:</b> {angle}</div>' if angle else ''}
  {f'<div style="margin:4px 0;font-size:13px;color:#8a5a00"><b>Ask them:</b> {ask}</div>' if ask else ''}
  <div style="font-size:12px;color:#888">{' · '.join(links)}{('  |  ' + scoreline) if scoreline else ''}</div>
</div>"""


SIGNAL_LABEL = {
    "hiring_automation": ("Hiring — automation", "#0a7"),
    "facility": ("New facility", "#0a7"),
    "funding": ("Funding", "#1558b0"),
    "exec_move": ("Exec move", "#845"),
    "partnership": ("Partnership", "#845"),
    "launch": ("Launch", "#555"),
    "press": ("Press", "#888"),
}


def _health_banner(unhealthy):
    """Surface dark lanes IN THE EMAIL (principle 4).

    ingest_yc reported healthy for 259h while dead. A health check nobody reads is
    the same as no health check, so the failure now arrives in the inbox rather than
    waiting to be discovered in a JSON file.
    """
    if not unhealthy:
        return ""
    lines = []
    for name, r in unhealthy:
        if r["stale"]:
            why = f"stale — last ran {int(r['age_h'])}h ago (limit {r['max_age_hours']}h)"
        else:
            why = f"{r.get('failures', 0)} failures this run"
        lines.append(f"<b>{name}</b> — {why}")
    rows = "<br>".join(lines)
    return ('<div style="margin:0 0 16px 0;padding:10px;background:#fff6f6;'
            'border-left:3px solid #a11;font-size:12px;color:#a11">'
            f'<b>⚠ {len(unhealthy)} lane(s) not healthy</b> — findings below are incomplete<br>'
            f'{rows}</div>')


def _signal_section(hot):
    """Heat-ranked entities: what got interesting, not what merely exists."""
    if not hot:
        return ""
    out = [('<h3 style="margin:22px 0 8px 0;font-size:15px">What changed '
            '<span style="color:#999;font-weight:400">(decayed by recency)</span></h3>')]
    for r in hot:
        kinds = " · ".join(
            f'<span style="color:{SIGNAL_LABEL.get(k, (k, "#888"))[1]}">'
            f'{SIGNAL_LABEL.get(k, (k, "#888"))[0]}{"" if v == 1 else f" ×{v}"}</span>'
            for k, v in sorted(r["kinds"].items(), key=lambda kv: -kv[1]))
        latest = r["signals"][0] if r.get("signals") else {}
        name = r.get("entity_name") or r.get("entity_key", "")
        out.append(
            f'<div style="margin:0 0 11px 0">'
            f'<b style="font-size:14px">{name}</b> '
            f'<span style="color:#999;font-size:12px">heat {r["heat"]:.1f} · '
            f'{r["count"]} signal{"" if r["count"] == 1 else "s"} · '
            f'{r["age_days"]:.0f}d ago</span><br>'
            f'<span style="font-size:11px;text-transform:uppercase">{kinds}</span><br>'
            f'<a href="{latest.get("url", "")}" style="color:#1558b0;font-size:13px">'
            f'{latest.get("title", "")[:130]}</a></div>')
    return "\n".join(out)


def _discovery_section(discoveries):
    """Real events about companies the radar has never seen — the lead feed."""
    if not discoveries:
        return ""
    out = [('<h3 style="margin:22px 0 8px 0;font-size:15px">New to the radar '
            '<span style="color:#999;font-weight:400">(unmatched — worth a look)</span></h3>')]
    for d in discoveries[:8]:
        label, colour = SIGNAL_LABEL.get(d["kind"], (d["kind"], "#888"))
        out.append(
            f'<div style="margin:0 0 8px 0;font-size:13px">'
            f'<span style="font-size:11px;color:{colour};text-transform:uppercase">{label}</span><br>'
            f'<a href="{d.get("url", "")}" style="color:#1558b0">{d.get("title", "")[:130]}</a>'
            f'</div>')
    return "\n".join(out)


def build_html(new_companies, new_posts, standing, errors, news=None,
               hot=None, discoveries=None, unhealthy=None):
    news = news or []
    today = datetime.now().strftime("%A %-d %B")
    bits = []
    if hot:
        bits.append(f"{len(hot)} moving")
    if news:
        bits.append(f"{len(news)} from the news")
    if new_companies:
        bits.append(f"{len(new_companies)} new companies")
    headline = " · ".join(bits) or "quiet day"
    parts = [f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
font-size:14px;color:#111;max-width:760px;line-height:1.45">
<div style="font-size:12px;color:#888;margin-bottom:2px">Partner Radar · {today}</div>
<h2 style="margin:0 0 14px 0;font-size:19px">{headline}</h2>""",
             _health_banner(unhealthy or []),
             _signal_section(hot or []),
             _discovery_section(discoveries or [])]

    # News is the primary lane — what moved, why it matters, source + company link.
    if news:
        for rec in news:
            parts.append(_news_card(rec))

    if not news and not new_companies and not new_posts:
        parts.append('<p style="color:#666">Nothing new since the last run. '
                     'Standing shortlist below so the thread stays warm.</p>')

    by_tier = {}
    for rec in new_companies:
        by_tier.setdefault(rec.get("tier", "CANDIDATE_UNKNOWN"), []).append(rec)

    for tier in SECTIONS:
        rows = by_tier.get(tier)
        if not rows:
            continue
        label, colour = TIER_LABEL.get(tier, (tier, "#333"))
        parts.append(f'<h3 style="margin:20px 0 8px 0;font-size:15px;color:{colour}">'
                     f'{label} <span style="color:#999;font-weight:400">({len(rows)})</span></h3>')
        parts.extend(_card(r) for r in rows)

    if new_posts:
        parts.append('<h3 style="margin:22px 0 8px 0;font-size:15px">Posts &amp; launches</h3>')
        for p in new_posts:
            tag = {"launches": "Launch YC", "forum": "Bookface",
                   "x": "X", "web": "Web"}.get(p["kind"], p["kind"])
            if p.get("published"):
                tag += f" · {p['published']}"
            parts.append(
                f'<div style="margin:0 0 10px 0;font-size:14px">'
                f'<span style="font-size:11px;color:#888;text-transform:uppercase">{tag}</span><br>'
                f'<a href="{p["url"]}" style="color:#1558b0;font-weight:600">{p["title"]}</a>'
                f'<div style="color:#555;font-size:13px">{p["blurb"]}</div></div>')

    if standing:
        parts.append('<h3 style="margin:22px 0 8px 0;font-size:15px;color:#666">'
                     'Standing shortlist</h3>')
        parts.extend(_card(r) for r in standing)

    if errors:
        # Collapse identical failures. One dead upstream produces one error per query, which
        # rendered as eight identical "403 forbidden" lines and buried the actual signal.
        tallies = {}
        for e in errors:
            msg = e.split(": ", 1)[-1].strip()
            tallies[msg] = tallies.get(msg, 0) + 1
        lines = [f"{m} <span style='color:#c88'>(&times;{n})</span>" if n > 1 else m
                 for m, n in sorted(tallies.items(), key=lambda kv: -kv[1])[:6]]
        parts.append('<div style="margin-top:22px;padding:10px;background:#fff6f6;'
                     'border-left:3px solid #a11;font-size:12px;color:#a11">'
                     f'<b>Degraded this run</b> — {len(errors)} failures<br>'
                     + "<br>".join(lines) + '</div>')

    parts.append('<div style="margin-top:24px;font-size:11px;color:#aaa">'
                 'partner-radar · ~/Desktop/partner-radar · ranked by time-to-deployment</div></div>')
    return "\n".join(parts)


def main():
    dry = "--dry" in sys.argv
    sources = _cfg("sources.yaml")
    delivery = sources.get("delivery") or {}
    seen = _seen()

    # --- Primary lane: LLM-judged news -----------------------------------------------------
    judged = _load_jsonl(os.path.join(ROOT, "data", "news", "judged.jsonl"))
    seen.setdefault("news", {})
    news = []
    for rec in judged:
        key = rec.get("source_url") or rec.get("company")
        if not key or key in seen["news"] or rec.get("tier") in ("T6_PASS", "T0_EXISTING"):
            continue
        news.append(rec)
        seen["news"][key] = int(time.time())
    news.sort(key=lambda r: SECTIONS.index(r["tier"]) if r.get("tier") in SECTIONS else 99)
    news = news[:20]

    reranked = _load_jsonl(os.path.join(DERIVED, "reranked.jsonl"))
    scored = _load_jsonl(os.path.join(DERIVED, "scored.jsonl"))
    pool = reranked or [r for r in scored if r.get("stage2")]

    new_companies = []
    for rec in pool:
        rec = _normalize(rec)
        # Dedup key carries the STAGE. A company sent as a provisional prefilter card must
        # still be sendable once as a final reranked card — the reranked version is the
        # whole point (it has the tier, the I score, why_it_matters and the angle).
        # Keying on identity alone silently suppressed all 30 reranked rows on 2026-07-31.
        stage = "final" if rec.get("reranked_at") else "prov"
        base = rec.get("canonical_key") or f"{rec.get('source', 'yc')}:{rec.get('source_id')}"
        key = f"{base}:{stage}"
        if key in seen["companies"] or rec.get("tier") in ("T0_EXISTING", "T6_PASS"):
            continue
        new_companies.append(rec)
        seen["companies"][key] = int(time.time())
    # Highest-signal first, capped so the email stays readable.
    # Sort by section, THEN by the axis that put the company in that section, THEN leverage.
    # Sorting by section alone showed whichever shard happened to be read first — the first
    # dry run led with an ITAR-compliance company and a git-worktree tool.
    primary = {"CANDIDATE_HARDWARE": "H", "T3_CUSTOMER": "D", "T4_CHANNEL": "C"}
    def rank(r):
        tier = r.get("tier")
        s = r.get("scores") or {}
        return (SECTIONS.index(tier) if tier in SECTIONS else 99,
                -s.get(primary.get(tier, "A"), 0),
                -s.get("L", 0))
    new_companies.sort(key=rank)
    new_companies = new_companies[:30]

    # POSTS LANE RETIRED 2026-08-15. Both fetchers here (`fetch_posts` for Launch YC /
    # Bookface, and Exa's `fetch_web_signals`) route through the `yc` binary, which carries
    # the account holder's auth. Since that token broke they produced 22 identical 403s on every single
    # run — a permanent red banner that trained the reader to ignore the failure section,
    # which is worse than having no section at all.
    #
    # Nothing is lost: news.py covers the same discovery ground through four keyless feeds
    # (Google News RSS, HN Algolia, The Robot Report, IEEE Spectrum) and does it better,
    # because it reaches non-accelerator companies that Bookface never contained.
    # RESTORED 2026-08-16 via Bookface-over-Arc. The lane above was retired because the
    # only path to it was the `yc` binary carrying someone else's expired token. Reading
    # Bookface through the Arc Space that is already signed in needs no credential at all,
    # so the lane comes back without the permanent red banner that justified killing it.
    posts, errors = load_bookface_posts()
    new_posts = []
    for p in posts:
        if p["id"] in seen["posts"]:
            continue
        new_posts.append(p)
        seen["posts"][p["id"]] = int(time.time())
    new_posts = new_posts[:25]

    # Never send a blank email: if nothing is new, carry the top standing candidates so the
    # channel stays useful and the operator doesn't learn to ignore it.
    standing = []
    if not news and not new_companies and not new_posts:
        standing = sorted(pool, key=lambda r: -(r.get("scores", {}).get("A", 0)))[:6]

    # --- Signals lane: what CHANGED, decayed by recency -------------------------------------
    hot, discoveries, unhealthy = [], [], []
    try:
        from radar.run import health_report
        from radar.signals import load_signals, ranked
        hot = ranked(load_signals(os.path.join(ROOT, "data", "raw", "signals")),
                     limit=10, min_heat=0.5)
        discoveries = _load_jsonl(os.path.join(DERIVED, "signal_discoveries.jsonl"))
        # A lane that stopped running must reach the inbox, not sit in a JSON file.
        unhealthy = sorted(
            ((name, r) for name, r in health_report().items() if r.get("unhealthy")),
            key=lambda kv: kv[0])
    except Exception as ex:                                    # noqa: BLE001 — principle 4
        errors.append(f"signals: {type(ex).__name__}: {str(ex)[:90]}")

    html = build_html(new_companies, new_posts, standing, errors, news=news,
                      hot=hot, discoveries=discoveries, unhealthy=unhealthy)
    if dry:
        print(html)
        return 0

    from mailer import send
    top = news[0]["company"] if news else None
    warn = f"⚠{len(unhealthy)} " if unhealthy else ""
    if hot:
        lead = hot[0].get("entity_name") or "movement"
        subject = f"{warn}Partner Radar — {len(hot)} moving, incl. {lead}"
    elif news:
        subject = f"{warn}Partner Radar — {len(news)} finds" + (f", incl. {top}" if top else "")
    elif new_companies or new_posts:
        subject = f"{warn}Partner Radar — {len(new_companies)} companies, {len(new_posts)} posts"
    else:
        subject = f"{warn}Partner Radar — quiet day"
    sender, to = delivery.get("sender_account"), delivery.get("to")
    if not (sender and to):
        # Loud, not a silent fallback to a hardcoded address: a missing delivery block used to
        # mail the author's placeholder inbox and report success.
        raise SystemExit("config/sources.yaml has no delivery.sender_account / delivery.to — "
                         "register a sender with `python3 tools/mailer.py add <name> <address>` "
                         "and set both keys (README: Operator coupling)")
    send(sender, to, subject, html)

    seen["last_sent"] = int(time.time())
    os.makedirs(DERIVED, exist_ok=True)
    json.dump(seen, open(SEEN, "w"))

    health = json.load(open(HEALTH)) if os.path.exists(HEALTH) else {}
    health["digest"] = {"last_run": int(time.time()),
                        "produced": len(new_companies) + len(new_posts),
                        "companies": len(new_companies), "posts": len(new_posts),
                        "failures": len(errors), "degraded": bool(errors)}
    json.dump(health, open(HEALTH, "w"), indent=2)
    # Report the PRIMARY lane first. This line counted only companies and posts, so the
    # 06:00 run that emailed 25 freshly-judged news items logged "sent: 0 companies,
    # 0 posts" — a system misreporting its own main output, which is the exact failure
    # class the health work exists to prevent.
    print(f"sent: {len(news)} news, {len(new_companies)} companies, "
          f"{len(new_posts)} posts, {len(errors)} errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
