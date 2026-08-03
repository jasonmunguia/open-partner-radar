"""Scoring — a PURE function of a raw record plus the rubric.

Principle 1: nothing here is persisted alongside the raw data. Change the rubric
and every record re-prices on the next read, with no migration and no purge of
stale rows. internship-radar needed a `self-heal` block (poll.py:225) precisely
because it lacked this separation.
"""
import re

# Axis names come from the rubric, not from code. `I` is special-cased (it has a base value
# and negative groups); every other axis is a plain additive keyword axis. Adding or renaming
# an axis in rubric.yaml therefore needs no change here.
def _keyword_axes(rubric):
    return [k for k in (rubric.get("axes") or {}) if k != "I"]


def _blob(row):
    parts = [row.get("one_liner", ""), row.get("long_description", ""),
             row.get("tags", ""), row.get("subindustry", ""), row.get("industry", "")]
    return " ".join(p for p in parts if p).lower()


def _hits(text, terms):
    """Whole-token-ish matching. Guards against 'mes' inside 'mesh' / 'times'."""
    found = []
    for term in terms:
        t = term.lower().strip()
        if not t:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])"
        if re.search(pattern, text):
            found.append(t)
    return found


def _axis_score(text, axis_cfg):
    total, matched = 0.0, []
    for group in (axis_cfg.get("groups") or {}).values():
        found = _hits(text, group.get("terms") or [])
        if not found:
            continue
        weight = group.get("weight", 0)
        # first hit earns the group weight; extra hits add 25% once (diminishing)
        total += weight * (1.25 if len(found) > 1 else 1.0)
        matched.extend(found)
    cap = axis_cfg.get("cap", 100)
    return max(0, min(cap, round(total))), matched


def _internalizability(text, cfg):
    score = cfg.get("base", 45)
    matched = []
    for group in (cfg.get("groups") or {}).values():
        found = _hits(text, group.get("terms") or [])
        if not found:
            continue
        score += group.get("weight", 0) * (1.25 if len(found) > 1 else 1.0)
        matched.extend(found)
    return max(0, min(cfg.get("cap", 100), round(score))), matched


def _leverage(row, cfg):
    """Their stage vs ours. Small + recent + active = they need the logo more."""
    reasons = []
    full = cfg.get("team_size_full_points", 10)
    zero = cfg.get("team_size_zero_points", 60)
    try:
        team = int(float(row.get("team_size") or 0))
    except (TypeError, ValueError):
        team = 0

    if team <= 0:
        size_pts = 45                      # unknown: neutral, do not reward or punish
        reasons.append("team size unknown")
    elif team <= full:
        size_pts = 75
        reasons.append(f"{team}-person team")
    elif team >= zero:
        size_pts = 0
        reasons.append(f"{team} people — no asymmetry")
    else:
        span = max(1, zero - full)
        size_pts = round(75 * (zero - team) / span)
        reasons.append(f"{team}-person team")

    score = size_pts
    if row.get("batch") in (cfg.get("recent_batches") or []):
        score += cfg.get("recency_bonus", 25)
        reasons.append(f"recent batch {row.get('batch')}")
    if (row.get("status") or "").lower() not in ("active", ""):
        score -= cfg.get("inactive_penalty", 40)
        reasons.append(f"status {row.get('status')}")
    return max(0, min(100, round(score))), reasons


def _is_competitor(text, cfg):
    dep = _hits(text, cfg.get("deployment_terms") or [])
    setting = _hits(text, cfg.get("setting_terms") or [])
    if dep and setting:
        return True, dep[:3] + setting[:2]
    return False, []


def _match_list(name, entries):
    n = (name or "").lower()
    return next((e for e in entries or [] if e.lower() in n), None)


def _assign_tier(rules, scores, competitor):
    env = {"__builtins__": {}}
    env.update(scores)
    for rule in rules or []:
        when = rule.get("when")
        if when == "competitor":
            if competitor:
                return rule["tier"]
            continue
        if when == "always":
            return rule["tier"]
        try:
            if eval(when, env, {}):        # noqa: S307 — local config, restricted env
                return rule["tier"]
        except Exception:
            continue
    return "T6_PASS"


def score_row(row, rubric, exclusions=None):
    """Return the full scored record. Never mutates `row`."""
    exclusions = exclusions or {}
    text = _blob(row)
    scores, matched = {}, {}

    for axis in _keyword_axes(rubric):
        cfg = (rubric.get("axes") or {}).get(axis) or {}
        scores[axis], matched[axis] = _axis_score(text, cfg)

    scores["I"], matched["I"] = _internalizability(text, (rubric.get("axes") or {}).get("I") or {})
    scores["L"], leverage_reasons = _leverage(row, rubric.get("leverage") or {})

    competitor, comp_terms = _is_competitor(text, rubric.get("competitor") or {})
    tier = _assign_tier(rubric.get("tiers"), scores, competitor)

    # Thin-description guard: too little text to score, but a relevant tag. Absence of
    # evidence is not evidence of absence — send it to stage 2 for a web lookup instead of
    # discarding it. Rescues the newest 2-person shops, which are the highest-leverage
    # targets and the ones least likely to have written their page.
    thin_cfg = rubric.get("thin_description") or {}
    thin = False
    if tier == "T6_PASS" and thin_cfg:
        body = f"{row.get('one_liner', '')} {row.get('long_description', '')}".strip()
        tags_l = (row.get("tags") or "").lower()
        if len(body) <= thin_cfg.get("max_chars", 200) and any(
                t.lower() in tags_l for t in thin_cfg.get("require_any_tag") or []):
            tier, thin = thin_cfg.get("tier", "CANDIDATE_UNKNOWN"), True

    flags = []
    existing = _match_list(row.get("name"), exclusions.get("existing"))
    dnc = _match_list(row.get("name"), exclusions.get("do_not_contact"))
    crm = _match_list(row.get("name"), exclusions.get("crm_seen"))
    if existing:
        tier, _ = "T0_EXISTING", flags.append(f"existing relationship: {existing}")
    if dnc:
        tier, _ = "T5_WATCH", flags.append(f"do-not-contact: {dnc}")
    if crm:
        flags.append(f"already in CRM: {crm}")
    if competitor and not existing:
        flags.append("competitor profile — monitor, do not contact")

    return {
        **row,
        "scores": scores,
        "tier": tier,
        "competitor": competitor,
        "flags": flags,
        "why": {
            **{ax: matched[ax][:6] for ax in matched},
            "L": leverage_reasons,
            "competitor_terms": comp_terms,
        },
        # Stage-1 output is a QUEUE decision, not an alert decision. Only the
        # stage-2 LLM reviewer produces final tiers, so nothing is emailed from here.
        "stage2": tier in (rubric.get("stage2_queue") or []) and not (dnc or existing or crm),
    }


def score_all(rows, rubric, exclusions=None):
    return [score_row(r, rubric, exclusions) for r in rows]
