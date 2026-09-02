"""The email must show PARTNER / ABSORB / WATCH / INTEL as headed sections, in that order.

Set by the operator 2026-09-02 after the skill had promised headed sections since v4 (2026-08-16)
while build_html ran the cards together with only a per-card label.
"""
import re

from radar import digest as D


def _rec(tier, name):
    return {"company": name, "tier": tier, "what_happened": "did a thing",
            "why_it_matters": "it matters", "source_url": f"https://example.com/{name}",
            "company_url": f"https://{name}.example", "published": "2026-09-01"}


def test_each_tier_gets_a_headed_section_in_email_order():
    news = [_rec("INTEL", "d"), _rec("PARTNER", "a"), _rec("WATCH", "c"), _rec("ABSORB", "b")]
    html = D.build_html([], [], [], [], news=news)
    heads = re.findall(r"<h3[^>]*>([^<]+)", html)
    labels = [D.TIER_LABEL[t][0] for t in ("PARTNER", "ABSORB", "WATCH", "INTEL")]
    found = [h.strip() for h in heads if any(h.strip().startswith(l) for l in labels)]
    assert found == labels, f"sections out of order or missing: {found}"
    for t in ("PARTNER", "ABSORB", "WATCH", "INTEL"):
        assert D.SECTION_INTRO[t] in html, f"{t} section has no rule line"


def test_cards_land_under_their_own_section():
    html = D.build_html([], [], [], [], news=[_rec("WATCH", "rival"), _rec("PARTNER", "vendor")])
    assert html.index(D.TIER_LABEL["PARTNER"][0]) < html.index("vendor") < \
        html.index(D.TIER_LABEL["WATCH"][0]) < html.index("rival")
