"""News -> signal classification and entity matching.

The matching tests matter more than the classification tests. Attaching a signal to
the WRONG company is worse than attaching it to none: it silently corrupts the
entity graph, and nothing downstream can detect it. This stack already ate that
failure once (substring tier-scoring, 2026-08-15 — 60 companies misscored), so
word-boundary matching is asserted explicitly here.
"""
import pytest

from radar import signal_news as N


def item(title, summary="", url="https://ex.com/a", publisher="pub"):
    return {"title": title, "summary": summary, "url": url, "publisher": publisher}


# ------------------------------------------------------------ classification

@pytest.mark.parametrize("title,expected", [
    ("Acme raises $12M Series A", "funding"),
    ("Ant Group Backs Hong Kong Robotics Startup Daimeng in New Funding", "funding"),
    ("Acme opens new plant in Ohio", "facility"),
    ("Acme breaks ground on manufacturing site", "facility"),
    ("Acme names Jane Doe as COO", "exec_move"),
    ("Acme partners with Fanuc on integration", "partnership"),
    ("Acme launches new gripper line", "launch"),
    ("Acme mentioned in industry roundup", "press"),
])
def test_classification(title, expected):
    assert N.classify(item(title))[0] == expected


def test_hiring_beats_generic_when_both_present():
    kind, _ = N.classify(item("Acme is hiring automation engineers for its new line"))
    assert kind == "hiring_automation"


def test_funding_magnitude_scales_with_round_size():
    small = N.funding_magnitude("raises $2M seed")
    big = N.funding_magnitude("raises $500M Series D")
    assert big > small
    assert 0.5 <= small <= 3.0 and 0.5 <= big <= 3.0


def test_funding_magnitude_handles_billions():
    assert N.funding_magnitude("raises $1.2B") > N.funding_magnitude("raises $50M")


def test_funding_magnitude_defaults_when_no_figure():
    assert N.funding_magnitude("raises an undisclosed round") == 1.0


# ----------------------------------------------------------------- matching

KNOWN = [
    {"name": "Chef Robotics", "website": "chefrobotics.ai", "source": "yc", "source_id": "1"},
    {"name": "Boston Dynamics", "website": "bostondynamics.com", "source": "web"},
    {"name": "Nori", "website": "nori.com", "source": "web"},
]


def test_matches_known_company():
    idx = N.build_name_index(KNOWN)
    row = N.match_entity("Chef Robotics raises $43M", idx)
    assert row["name"] == "Chef Robotics"


def test_does_not_match_substring_inside_another_word():
    """'Nori' must not match 'Noribachi' — the exact bug class that misscored 60 rows."""
    idx = N.build_name_index(KNOWN)
    assert N.match_entity("Noribachi announces new lighting plant", idx) is None


def test_prefers_longest_match():
    idx = N.build_name_index([
        {"name": "Boston", "website": "boston.com", "source": "web"},
        {"name": "Boston Dynamics", "website": "bostondynamics.com", "source": "web"},
    ])
    assert N.match_entity("Boston Dynamics unveils Atlas", idx)["name"] == "Boston Dynamics"


def test_corporate_suffixes_are_ignored():
    idx = N.build_name_index([{"name": "Acme, Inc.", "website": "acme.com", "source": "web"}])
    assert N.match_entity("Acme opens plant", idx) is not None


def test_very_short_names_are_not_indexed():
    """A 2-char name would match everywhere."""
    idx = N.build_name_index([{"name": "AI", "website": "ai.com", "source": "web"}])
    assert idx == {}


def test_unknown_company_returns_none():
    idx = N.build_name_index(KNOWN)
    assert N.match_entity("Totally Unheard Of Corp raises money", idx) is None


# ------------------------------------------------------------------- routing

def test_matched_signal_carries_entity_identity():
    sigs, _ = N.signals_from_news([item("Chef Robotics raises $43M Series B")], KNOWN)
    assert len(sigs) == 1
    assert sigs[0]["entity_key"] == "yc:1"
    assert sigs[0]["kind"] == "funding"
    assert sigs[0]["magnitude"] > 1.0


def test_unmatched_event_becomes_discovery_not_loss():
    _, unmatched = N.signals_from_news([item("Newco Foods opens new plant in Iowa")], KNOWN)
    assert len(unmatched) == 1
    assert unmatched[0]["kind"] == "facility"


def test_unmatched_generic_press_is_dropped_as_noise():
    sigs, unmatched = N.signals_from_news([item("Some company was mentioned")], KNOWN)
    assert sigs == [] and unmatched == []


def test_real_queue_headline_classifies_as_funding():
    """Verbatim row from data/news/queue.jsonl."""
    t = "Ant Group Backs Hong Kong Robotics Startup Daimeng in New Funding - asiabusinessoutlook.com"
    assert N.classify(item(t))[0] == "funding"
