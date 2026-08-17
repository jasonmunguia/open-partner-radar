"""News-first discovery. The primary lane.

WHY THIS REPLACED THE DIRECTORY SWEEP (2026-08-15, the operator's correction):

Accelerator directories describe a company as it was at *application time*, not as it is.
Synphony's own Bookface entry still reads as "robots for strawberries", which stopped being
true months ago. Ranking companies off that text produces a stale catalog, and re-emailing a
catalog every morning is what made the digest useless.

the operator's actual workflow is to read robotics news and the wider internet and spot companies
worth partnering with. So news is the DISCOVERY mechanism; directories (Bookface, a16z) are
demoted to ENRICHMENT — good for batch, headcount and founders, never for what a company does.

DESIGN CONSTRAINT: none of these feeds may depend on the `yc` binary. That binary carries
the account holder's Bookface auth, it has 403'd since ~2026-08-08, and it takes Exa down with it. A
discovery lane that dies when someone else's token expires is not a discovery lane. Everything
below is free, keyless, and independently reachable.

Verified working 2026-08-15:
  - Google News RSS  — arbitrary query + `when:7d`, no key
  - HN Algolia       — search_by_date, 17k+ robotics stories, no key (HTTPS only; http 404s)
  - The Robot Report — RSS
  - IEEE Spectrum    — robotics topic RSS
"""
import hashlib
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) partner-radar/2.0"}

# Queries derived from the deployment-speed thesis, not from "robotics" generally.
# Each one targets a category that can shorten Synphony's path to a live deployment.
GOOGLE_NEWS_QUERIES = [
    "robot hand OR gripper OR end-effector launch startup",
    "humanoid robot price OR affordable OR low-cost launch",
    "teleoperation OR remote robot operation startup launch",
    "robot foundation model OR manipulation policy release",
    "tactile sensing OR force control robot hardware",
    "robot data collection device OR demonstration rig",
    "robotics startup pivot OR new product OR unveils",
    "industrial robot deployment factory manufacturing startup",
    "robotic arm startup funding seed",
    "physical AI startup launch",
    "dexterous manipulation robot startup",
    "robot simulation OR sim2real startup launch",
    # COMPETITOR / DEPLOYMENT-COMPANY LANE (added 2026-08-16).
    # The queries above hunt component vendors, so companies that DEPLOY robots as their own
    # product were structurally invisible: Example Competitor C — Travis Kalanick's company, $1.7B led by
    # a16z, targeting food and mining, which overlaps Synphony's primary lane — never once
    # appeared in 141 judged rows. Competitor intel is a first-class output (the operator
    # 2026-08-16), so it needs its own queries rather than hoping supplier queries catch it.
    "robotics company deploys robots factories customers",
    "robots as a service industrial deployment contract",
    "food processing OR meat plant automation robot deployment",
    "warehouse robot deployment company raises",
    "physical AI company automating labor factories",
]

HN_QUERIES = ["robotics", "robot arm", "humanoid robot", "manipulation policy", "teleoperation"]

STATIC_FEEDS = [
    ("therobotreport", "https://www.therobotreport.com/feed/"),
    ("ieee_spectrum", "https://spectrum.ieee.org/feeds/topic/robotics.rss"),
]

GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
HN_API = "https://hn.algolia.com/api/v1/search_by_date?query={q}&tags=story&hitsPerPage={n}"


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get(url, timeout=25, attempts=3):
    """GET with backoff. A feed that dies on one hiccup must not report as 'no news'."""
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
                return r.read()
        except Exception as ex:
            last = ex
            time.sleep(1.0 * (2 ** i))
    raise last


def _norm_url(url):
    """Canonical form for dedup: strip scheme, www, tracking params, trailing slash."""
    if not url:
        return ""
    u = re.sub(r"^https?://", "", url.strip()).rstrip("/")
    u = re.sub(r"^www\.", "", u)
    u = re.split(r"[?#]", u)[0]
    return u.lower()


def item_id(url, title):
    return hashlib.sha1(f"{_norm_url(url)}|{(title or '').lower()[:80]}".encode()).hexdigest()[:16]


# Words that carry no story identity. Kept small on purpose: an aggressive stoplist makes
# unrelated headlines collide, which is a worse failure than showing one story twice.
_STOP = frozenset("""a an the and or of for to in on at by with from as is are was were be
been its it this that these those new news said says report reports launch launches
launched announce announces announced up out over into after before amid via""".split())


def _story_tokens(title):
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return frozenset(w for w in words if len(w) > 2 and w not in _STOP)


def corpus_generic(titles, max_df=0.08, min_titles=25):
    """Tokens so common in THIS batch that they carry no story identity.

    Document frequency, not a hand-written list. In a robotics news sweep, "robot",
    "humanoid", "funding" and "raises" appear in hundreds of headlines — they are the
    corpus's own stopwords, and a curated list of them would rot as coverage shifts.
    Deriving them from the batch is self-tuning and needs no maintenance.

    Below `min_titles` the estimate is noise, so it returns empty and callers fall back to
    plain Jaccard — the small-batch case cannot over-merge much anyway.
    """
    if not titles or len(titles) < min_titles:
        return frozenset()
    df = {}
    for t in titles:
        for tok in _story_tokens(t):
            df[tok] = df.get(tok, 0) + 1
    ceiling = max_df * len(titles)
    return frozenset(tok for tok, n in df.items() if n > ceiling)


def _is_same_story(tokens, kept_tokens, threshold=0.6, generic=frozenset()):
    """Same-event detection: high overlap AND at least one distinctive token in common.

    The same event reaches us from several outlets with different wording — URL dedup
    cannot see that, so the operator gets one story twice. Jaccard (intersection over union) is
    the right similarity here: symmetric, and length-insensitive enough that a terse
    headline and a wordy one about the same event still match.

    But Jaccard alone is not sufficient, and the failing case is instructive:

        "Figure AI raises funding for humanoid robots"
        "Apptronik raises funding for humanoid robots"   -> 0.667, merged. WRONG.

    Two different companies, one event *type*. The generic vocabulary outvoted the only
    tokens that mattered (the names). So we additionally require the shared tokens to
    contain something NOT generic — because in this domain the company name is the story
    identity and the event verb is not.

    Over-merging silently hides a distinct company; under-merging shows a duplicate. The
    second-guess is far cheaper, so both gates are deliberately conservative.
    """
    for prior in kept_tokens:
        union = tokens | prior
        if not union:
            continue
        shared = tokens & prior
        if len(shared) / len(union) < threshold:
            continue
        if generic and not (shared - generic):
            continue          # overlap is entirely generic vocabulary — different stories
        return True
    return False


def _strip_html(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def _parse_rss(xml_bytes, source):
    """Minimal RSS/Atom reader. Returns normalized items."""
    out = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out
    # RSS 2.0 <item>, then Atom <entry>
    nodes = root.iter("item")
    for node in nodes:
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        desc = _strip_html(node.findtext("description") or "")
        pub = (node.findtext("pubDate") or "").strip()
        # Google News nests the real publisher in <source url="...">
        pub_dom = ""
        src_el = node.find("source")
        if src_el is not None:
            pub_dom = (src_el.get("url") or "").strip()
        if not title or not link:
            continue
        out.append({
            "feed": source,
            "title": title,
            "url": link,
            "publisher": _norm_url(pub_dom) or _norm_url(link).split("/")[0],
            "published": pub,
            "summary": desc[:400],
            "id": item_id(link, title),
        })
    return out


DISCOVERED_QUERIES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "discovered_queries.yaml")


def load_queries():
    """Preset queries plus any the coverage critic has added.

    The critic (an LLM step, see the skill) notices categories the preset list never asks
    for and appends them here. Merging at load time means the funnel widens permanently
    without a code change, and the hand-written list in this file stays the stable floor —
    a bad critic addition can be deleted from the YAML without touching the baseline.
    """
    queries = list(GOOGLE_NEWS_QUERIES)
    try:
        import yaml
        with open(DISCOVERED_QUERIES) as fh:
            extra = (yaml.safe_load(fh) or {}).get("queries") or []
        for q in extra:
            if isinstance(q, str) and q.strip() and q.strip() not in queries:
                queries.append(q.strip())
    except FileNotFoundError:
        pass
    except Exception:                       # noqa: BLE001 — a broken YAML must not kill the run
        pass
    return queries


def fetch_google_news(queries=None, window="7d", per_query=None):
    """Fetch each query. `per_query` (a dict) collects yield-per-query if supplied.

    Per-query yield is the DETERMINISTIC half of coverage management: a query returning
    zero week after week is dead weight, and counting needs no model. What counting cannot
    do is notice a category that was never queried at all — that absence is the LLM's job
    (see the weekly coverage critic in the skill).
    """
    queries = queries or load_queries()
    items, errors = [], []
    for q in queries:
        url = GOOGLE_NEWS.format(q=urllib.parse.quote(f"{q} when:{window}"))
        try:
            got = _parse_rss(_get(url), "google_news")
            items.extend(got)
            if per_query is not None:
                per_query[q] = len(got)
        except Exception as ex:
            errors.append(f"google_news[{q[:32]}]: {str(ex)[:90]}")
            if per_query is not None:
                per_query[q] = -1          # -1 distinguishes "errored" from "found nothing"
        time.sleep(0.3)
    return items, errors


def fetch_hn(queries=None, per_query=20):
    queries = queries or HN_QUERIES
    items, errors = [], []
    for q in queries:
        try:
            raw = _get(HN_API.format(q=urllib.parse.quote(q), n=per_query))
            data = json.loads(raw)
        except Exception as ex:
            errors.append(f"hn[{q[:24]}]: {str(ex)[:90]}")
            continue
        for h in data.get("hits", []):
            url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
            title = h.get("title") or ""
            if not title:
                continue
            items.append({
                "feed": "hackernews",
                "title": title,
                "url": url,
                "publisher": _norm_url(url).split("/")[0],
                "published": h.get("created_at") or "",
                "summary": (h.get("story_text") or "")[:400],
                "id": item_id(url, title),
                "hn_points": h.get("points") or 0,
            })
        time.sleep(0.3)
    return items, errors


def fetch_static_feeds(feeds=None):
    feeds = feeds or STATIC_FEEDS
    items, errors = [], []
    for name, url in feeds:
        try:
            items.extend(_parse_rss(_get(url), name))
        except Exception as ex:
            errors.append(f"{name}: {str(ex)[:90]}")
        time.sleep(0.3)
    return items, errors


def fetch_all(window="7d"):
    """Every feed, deduped on canonical URL. Returns (items, errors, per_feed_counts).

    Errors are RETURNED, never swallowed to stderr — the digest prints them so a dead feed
    is visible as a failure rather than looking like a quiet news day.
    """
    all_items, errors = [], []
    query_yield = {}
    for fn, kwargs in ((fetch_google_news, {"window": window, "per_query": query_yield}),
                       (fetch_hn, {}),
                       (fetch_static_feeds, {})):
        try:
            got, errs = fn(**kwargs)
        except Exception as ex:
            errors.append(f"{fn.__name__}: {str(ex)[:120]}")
            continue
        all_items.extend(got)
        errors.extend(errs)

    # Two-stage dedup. URL catches the identical link; story-similarity catches the same
    # event reported by different outlets, which URL dedup structurally cannot see.
    generic = corpus_generic([it["title"] for it in all_items])
    seen, deduped, counts, kept_tokens = set(), [], {}, []
    merged = 0
    for it in all_items:
        key = _norm_url(it["url"])
        if not key or key in seen:
            continue
        tokens = _story_tokens(it["title"])
        if tokens and _is_same_story(tokens, kept_tokens, generic=generic):
            merged += 1
            continue
        seen.add(key)
        kept_tokens.append(tokens)
        deduped.append(it)
        counts[it["feed"]] = counts.get(it["feed"], 0) + 1
    if merged:
        counts["_merged_duplicate_stories"] = merged
    counts["_query_yield"] = query_yield
    return deduped, errors, counts


def dead_queries(query_yield, runs_history=None, threshold=0):
    """Queries that returned nothing this run. Pure counting — no model involved.

    Reported, never auto-removed: a query can legitimately go quiet for a week, and
    silently dropping it would shrink coverage without anyone deciding to.
    """
    return sorted(q for q, n in (query_yield or {}).items() if n <= threshold and n >= 0)
