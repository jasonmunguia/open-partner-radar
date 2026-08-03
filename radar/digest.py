"""Daily 10am digest: companies + posts, with links, ready for the operator to act on.

Delivery is SMTP with an explicitly named account (principle 6) via ~/.claude/tools/mailer.py.
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
sys.path.insert(0, os.path.expanduser("~/.claude/tools"))

TIER_LABEL = {
    "T1_PARTNER_NOW": ("Partner now", "#0b7a3b"),
    "T2_ABSORB_TRACK": ("Absorb track", "#1558b0"),
    "CANDIDATE_ACCELERANT": ("Deployment accelerant", "#1558b0"),
    "CANDIDATE_HARDWARE": ("Cheap/free hardware", "#8a5a00"),
    "CANDIDATE_UNKNOWN": ("Needs a look — thin public info", "#6b6b6b"),
    "T3_CUSTOMER": ("Customer", "#6b3fa0"),
    "T4_CHANNEL": ("Channel", "#6b3fa0"),
    "T5_WATCH": ("Competitor — watch, do not contact", "#a11"),
}
# Order the email sections. Accelerants first: they move a deployment date.
SECTIONS = ["T1_PARTNER_NOW", "CANDIDATE_ACCELERANT", "CANDIDATE_HARDWARE",
            "T2_ABSORB_TRACK", "CANDIDATE_UNKNOWN", "T3_CUSTOMER", "T5_WATCH"]
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


def fetch_posts(limit_per_query=6):
    """Recent Launch YC posts + Bookface founder posts on robotics topics.

    Errors are collected and surfaced in the digest footer (principle 4) rather than
    disappearing into stderr.
    """
    posts, errors = [], []
    for entity in ("launches", "forum"):
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


def build_html(new_companies, new_posts, standing, errors):
    today = datetime.now().strftime("%A %-d %B")
    parts = [f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
font-size:14px;color:#111;max-width:760px;line-height:1.45">
<div style="font-size:12px;color:#888;margin-bottom:2px">Partner Radar · {today}</div>
<h2 style="margin:0 0 14px 0;font-size:19px">{len(new_companies)} new companies · {len(new_posts)} new posts</h2>"""]

    if not new_companies and not new_posts:
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
        parts.append('<div style="margin-top:22px;padding:10px;background:#fff6f6;'
                     'border-left:3px solid #a11;font-size:12px;color:#a11">'
                     '<b>Degraded this run</b><br>' + "<br>".join(errors[:8]) + '</div>')

    parts.append('<div style="margin-top:24px;font-size:11px;color:#aaa">'
                 'partner-radar · ~/Desktop/partner-radar · ranked by time-to-deployment</div></div>')
    return "\n".join(parts)


def main():
    dry = "--dry" in sys.argv
    sources = _cfg("sources.yaml")
    delivery = sources.get("delivery") or {}
    seen = _seen()

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

    posts, errors = fetch_posts()

    # Semantic web sweep: feature launches and pivots announced in the wild, not on any
    # launch page. This is the lane that surfaces non-accelerator companies — the
    # Microfactory class — which no batch source can reach.
    try:
        from radar.sources_ext import fetch_web_signals
        sigs, sig_errs = fetch_web_signals(days=14, per_query=6)
        posts.extend(sigs)
        errors.extend(sig_errs)
    except Exception as ex:
        errors.append(f"web_semantic: {str(ex)[:120]}")

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
    if not new_companies and not new_posts:
        standing = sorted(pool, key=lambda r: -(r.get("scores", {}).get("A", 0)))[:6]

    html = build_html(new_companies, new_posts, standing, errors)
    if dry:
        print(html)
        return 0

    from mailer import send
    subject = (f"Partner Radar — {len(new_companies)} companies, {len(new_posts)} posts"
               if (new_companies or new_posts) else "Partner Radar — quiet day")
    send(delivery.get("sender_account", "personal"),
         delivery.get("to", "you@example.com"), subject, html)

    seen["last_sent"] = int(time.time())
    os.makedirs(DERIVED, exist_ok=True)
    json.dump(seen, open(SEEN, "w"))

    health = json.load(open(HEALTH)) if os.path.exists(HEALTH) else {}
    health["digest"] = {"last_run": int(time.time()),
                        "produced": len(new_companies) + len(new_posts),
                        "companies": len(new_companies), "posts": len(new_posts),
                        "failures": len(errors), "degraded": bool(errors)}
    json.dump(health, open(HEALTH, "w"), indent=2)
    print(f"sent: {len(new_companies)} companies, {len(new_posts)} posts, {len(errors)} errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
