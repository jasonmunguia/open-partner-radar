"""Non-YC sources: a16z speedrun talent network, and semantic X/web discovery via Exa.

a16z API (verified 2026-07-30): https://speedrun-talent-network.com/api/v1/
  - Public, no auth. Spec at /api/v1/openapi.json. Endpoints: /jobs, /companies,
    /companies/{slug}, /collections, /stats/hiring.
  - /companies returns 879 records, 100/page, `page` is 0-based.
  - Requires a User-Agent header (403 without one) and a CA bundle (stock python3 on this
    Mac has none — use certifi, same fix mailer.py uses).
  - Fields: slug, name, url, tier (a16z 280 / speedrun 36 / market 563), cohort
    (SR001..SR007, GF1 — populated for only 43 records), open_roles, location,
    industries, blurb, logo.
  - Batch-age note: only speedrun cohorts carry a date. a16z portfolio and market companies
    have cohort=None, so the 9-month rule cannot be applied from this API alone — they are
    tagged `age_unknown` and the rerank must check a founded date before promoting them.

X / web discovery: the operator's requirement is that a company's pertinent news is often a
*feature launch or pivot* announced only on X, never on a launch page. Semantic search
catches those; keyword feed-scraping does not.
"""
import json
import os
import ssl
import subprocess
import time
import urllib.request

A16Z_BASE = "https://speedrun-talent-network.com/api/v1"
UA = {"User-Agent": "partner-radar/1.0", "Accept": "application/json"}

# Cohorts within ~9 months of 2026-07-30. SR006 began late Jan 2026; SR007 runs
# 27 Jul - 11 Oct 2026. SR005 and earlier fall outside the window.
RECENT_COHORTS = {"SR006", "SR007"}

# a16z industry tags worth keeping for the Synphony lens.
RELEVANT_INDUSTRIES = {"American Dynamism", "Deep Tech", "Infra", "AI"}


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get(url, timeout=30, attempts=4):
    """GET with backoff. The API rate-limits under bursts; without retry a single 429
    kills the whole source for the run and reports as 'zero results', which is the
    failure mode this project exists to avoid."""
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
                return json.loads(r.read())
        except Exception as ex:
            last = ex
            time.sleep(1.5 * (2 ** i))       # 1.5s, 3s, 6s, 12s
    raise last


def fetch_a16z(recent_only=True):
    """All a16z-network companies, normalised to the partner-radar row schema."""
    rows, page, errors = [], 0, []
    while page < 20:
        try:
            data = _get(f"{A16Z_BASE}/companies?page={page}&source=partner-radar")
        except Exception as ex:
            errors.append(f"a16z page {page}: {str(ex)[:120]}")
            break
        chunk = data.get("companies") or []
        if not chunk:
            break
        for c in chunk:
            cohort = c.get("cohort")
            industries = c.get("industries") or []
            # Age gate: cohort-dated records must be recent; undated ones are kept but
            # flagged, and only when their industry is plausibly relevant (otherwise the
            # 563-company "market" tier floods everything).
            if recent_only:
                if cohort and cohort not in RECENT_COHORTS:
                    continue
                if not cohort and not (set(industries) & RELEVANT_INDUSTRIES):
                    continue
            rows.append({
                "source": "a16z",
                "source_id": c.get("slug") or "",
                "name": c.get("name") or "",
                "bookface_url": c.get("url") or "",
                "batch": cohort or "",
                "status": "Active",
                "one_liner": c.get("blurb") or "",
                "long_description": c.get("blurb") or "",
                "website": "",
                "team_size": "",
                "location": c.get("location") or "",
                "industry": ", ".join(industries),
                "subindustry": "",
                "tags": ", ".join(industries + [c.get("tier") or ""]),
                "is_hiring": "true" if (c.get("open_roles") or 0) > 0 else "false",
                "yc_partner": "",
                "founders": [],
                "a16z_tier": c.get("tier") or "",
                "open_roles": c.get("open_roles") or 0,
                "age_unknown": not bool(cohort),
            })
        total_pages = data.get("total_pages") or 0
        page += 1
        if page >= total_pages:
            break
        time.sleep(0.2)
    return rows, errors


# ---------------------------------------------------------------------------------------
# Semantic X / web discovery via Exa (through the yc CLI's web tool)
# ---------------------------------------------------------------------------------------

X_QUERIES = [
    "robotics startup launched new robot arm or gripper product",
    "robotics company pivot to industrial manufacturing deployment",
    "humanoid robot startup announces low cost hardware",
    "robot foundation model or manipulation policy release",
    "teleoperation or remote robot operation product launch",
    "tactile sensing or force control robotics hardware launch",
    "robot data collection device or demonstration rig launch",
    "factory automation robot deployment announcement",
]


def _yc_web(payload, timeout=180):
    proc = subprocess.run(
        ["yc", "tools", "run", "web", "--input", json.dumps(payload), "--json"],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:200])
    # The CLI prints notices ("Token expired, refreshing...") to stdout ahead of the JSON.
    start = proc.stdout.find("{")
    if start == -1:
        raise RuntimeError(f"no JSON in yc output: {proc.stdout[:160]!r}")
    result = (json.loads(proc.stdout[start:]) or {}).get("result") or {}
    if result.get("status") != "success":
        raise RuntimeError(f"status={result.get('status')}")
    return result


def fetch_x_signals(days=14, per_query=8, include_x=True):
    """Semantic sweep for feature launches and pivots — the things announced only on X.

    Not a keyword feed scrape. the operator's point: a company's relevant event is often a new
    feature or a pivot posted to X and nowhere else, and only semantic search finds those.
    """
    signals, errors, seen = [], [], set()
    for q in X_QUERIES:
        payload = {"action": "search", "query": q, "num_results": per_query,
                   "highlights": True,
                   "start_published_date": time.strftime(
                       "%Y-%m-%d", time.gmtime(time.time() - days * 86400))}
        if include_x:
            # include_domains is a COMMA-SEPARATED STRING, not an array — passing a list
            # returns 422 "Invalid URL". Exa's `category: "tweet"` is documented in the
            # tool description but the API now rejects it ("no longer supported"), so
            # domain restriction is the working path. Verified 2026-07-30.
            payload["include_domains"] = "x.com,twitter.com"
        try:
            res = _yc_web(payload)
        except Exception as ex:
            errors.append(f"x/{q[:34]}: {str(ex)[:100]}")
            continue
        for r in (res.get("results") or []):
            url = r.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            hl = r.get("highlights") or []
            signals.append({
                "kind": "x" if ("x.com" in url or "twitter.com" in url) else "web",
                "title": (r.get("title") or url)[:160],
                "url": url,
                "id": url,
                "published": (r.get("published_date") or "")[:10],
                "blurb": (hl[0] if hl else (r.get("text") or ""))[:220].replace("\n", " "),
                "query": q,
            })
    return signals, errors


def fetch_web_signals(days=14, per_query=8):
    """Same semantic sweep, unrestricted to X — catches blogs, newsrooms, Show HN, etc."""
    return fetch_x_signals(days=days, per_query=per_query, include_x=False)
