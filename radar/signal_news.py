"""Turn news items into typed signals.

Two jobs, deliberately separated because they fail differently:

  1. CLASSIFY — what kind of event is this headline describing? Rule-based and
     deterministic. Headlines are formulaic ("raises $12M", "opens new plant",
     "names COO"), so keyword rules get most of the way with zero token cost and
     full testability. Per the model-routing rule, bulk mechanical classification
     does not belong on an expensive model.

  2. MATCH — which company is it about? This is the hard half and the one worth
     being honest about. Pulling the subject out of "Ant Group Backs Hong Kong
     Robotics Startup Daimeng in New Funding" means knowing the *recipient* is
     Daimeng, not the investor. Rule-based extraction gets this wrong often enough
     that guessing would quietly poison the entity graph.

So we do not guess. A signal attaches to an entity only when the headline contains
a name we already know from the scored universe. Everything else lands in an
UNMATCHED queue — which is not a failure mode, it is a discovery feed: a funding
event for a company not yet in the radar is exactly the thing worth surfacing.
"""
from __future__ import annotations

import re

from radar.signals import make_signal

# Ordered: first match wins, so specific patterns precede generic ones.
RULES = [
    ("hiring_automation", re.compile(
        r"\b(hiring|job|jobs|opening|recruit\w*|is looking for|to hire|adds? (?:staff|roles))\b"
        r".{0,60}\b(automation|robotic|integrat\w+|controls?|maintenance)\b|"
        r"\b(automation|robotics)\b.{0,40}\b(engineer|technician|manager)\b.{0,30}\b(hiring|opening|job)\b",
        re.I)),
    ("facility", re.compile(
        r"\b(new|opens?|opening|expand\w*|breaks? ground|groundbreaking|builds?|building|"
        r"adds?)\b.{0,40}\b(plant|facility|factory|warehouse|distribution cent\w+|"
        r"production line|manufacturing site|fulfillment cent\w+)\b|"
        r"\b(plant|factory|facility)\b.{0,30}\b(expansion|opening)\b", re.I)),
    ("funding", re.compile(
        r"\b(raises?|raised|funding|secures?|secured|closes?|closed|backs?|backed|"
        r"investment|invests?|series [a-e]\b|seed round|pre-seed)\b", re.I)),
    ("exec_move", re.compile(
        r"\b(names?|appoints?|hires?|promotes?|joins? as|steps? down|departs?)\b"
        r".{0,40}\b(ceo|coo|cto|cfo|president|vp|vice president|head of|chief)\b|"
        r"\b(new|incoming)\b.{0,15}\b(ceo|coo|cto|vp of operations)\b", re.I)),
    ("partnership", re.compile(
        r"\b(partners?\w*|teams? up|collaborat\w+|joint venture|alliance|"
        r"signs? (?:a )?(?:deal|agreement)|selects?|deploys? with)\b", re.I)),
    ("launch", re.compile(
        r"\b(launch\w*|unveils?|introduces?|announces? (?:the )?(?:new|availability)|"
        r"debuts?|releases?|rolls? out|general availability)\b", re.I)),
]

# Rough dollar magnitude for funding headlines. A $200M round is a different event
# from a $2M pre-seed and should not carry identical weight.
_MONEY = re.compile(r"\$\s?([\d.]+)\s*([mb])(?:illion|n)?\b", re.I)


def funding_magnitude(text: str) -> float:
    """Scale a funding signal by round size. 1.0 when no figure is stated."""
    m = _MONEY.search(text or "")
    if not m:
        return 1.0
    try:
        amount = float(m.group(1))
    except ValueError:
        return 1.0
    if m.group(2).lower() == "b":
        amount *= 1000.0
    # Compress hard: $500M should outweigh $5M, but not by 100x.
    if amount <= 0:
        return 1.0
    return max(0.5, min(3.0, 0.5 + (amount ** 0.5) / 10.0))


def classify(item) -> tuple[str, float]:
    """(kind, magnitude) for one news item. Falls back to generic press."""
    text = f"{item.get('title', '')} {item.get('summary', '')}"
    for kind, pattern in RULES:
        if pattern.search(text):
            mag = funding_magnitude(text) if kind == "funding" else 1.0
            return kind, mag
    return "press", 1.0


def _norm_name(name: str) -> str:
    """Strip corporate suffixes and punctuation so 'Acme, Inc.' matches 'Acme'."""
    n = (name or "").lower().strip()
    n = re.sub(r"[^\w\s&-]", " ", n)
    n = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|technology|"
               r"robotics|labs|ai|holdings|group|gmbh|sa|bv|plc)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def build_name_index(known_rows):
    """{normalized name: row} for the companies already in the universe.

    Names shorter than 4 characters are dropped — a two-letter company name matches
    half the English language and would attach signals to the wrong firm.
    """
    idx = {}
    for row in known_rows:
        norm = _norm_name(row.get("name") or "")
        if len(norm) >= 4:
            idx.setdefault(norm, row)
    return idx


def match_entity(text: str, name_index):
    """Return the known row this headline is about, or None.

    Word-boundary matching only. Substring matching caused a real misscore in this
    stack before (tier-scoring, 2026-08-15: 60 companies misscored, 93 dropped), so
    it is not repeated here.
    """
    haystack = _norm_name(text)
    if not haystack:
        return None
    best = None
    for norm, row in name_index.items():
        if re.search(rf"(?<![\w]){re.escape(norm)}(?![\w])", haystack):
            # Prefer the longest match: "boston dynamics" over "boston".
            if best is None or len(norm) > len(best[0]):
                best = (norm, row)
    return best[1] if best else None


def signals_from_news(items, known_rows, source="news"):
    """(matched signals, unmatched discovery rows).

    Unmatched is a feature: a funding event for a company the radar has never seen
    is a lead, not an error.
    """
    index = build_name_index(known_rows)
    matched, unmatched = [], []

    for item in items:
        kind, magnitude = classify(item)
        title = item.get("title") or ""
        row = match_entity(f"{title} {item.get('summary', '')}", index)
        published = item.get("ts") or item.get("published_ts")

        if row is None:
            # Only non-generic events are worth a discovery slot; a bare "press"
            # mention of an unknown company is noise.
            if kind != "press":
                unmatched.append({
                    "kind": kind, "title": title, "url": item.get("url", ""),
                    "publisher": item.get("publisher", ""),
                    "summary": (item.get("summary") or "")[:280],
                })
            continue

        matched.append(make_signal(
            kind=kind, title=title, url=item.get("url", ""), source=source,
            ts=published, entity_name=row.get("name", ""),
            yc_id=row.get("source_id") if row.get("source") == "yc" else "",
            website=row.get("website", ""), magnitude=magnitude,
            evidence=f"{item.get('publisher', '')}: {(item.get('summary') or '')[:200]}",
        ))

    return matched, unmatched
