"""Story-level dedup: the same event from different outlets must collapse to one item.

Regression target (2026-08-15): a 7-day sweep returned the Mimic Robotics hand launch from
both SiliconANGLE and TechBriefs, and the Anthropic/Decart story from two outlets. URL dedup
cannot see these — different publishers, different URLs, same event — so the operator got the same
story twice in one email.

The expensive direction is over-merging: showing a duplicate is a papercut, silently hiding a
distinct company is a miss. These tests pin both sides of that boundary.
"""
import pytest

from radar.news import _is_same_story, _story_tokens, corpus_generic

# Stand-in for what corpus_generic derives from a real batch: the domain's own stopwords.
GENERIC = frozenset("robot robots robotics robotic humanoid funding raises startup".split())


def _same(a, b, threshold=0.6, generic=GENERIC):
    return _is_same_story(_story_tokens(a), [_story_tokens(b)],
                          threshold=threshold, generic=generic)


class TestStoryTokens:
    def test_drops_stopwords_and_short_words(self):
        tokens = _story_tokens("The New Robot Launches In A Factory")
        assert "the" not in tokens and "new" not in tokens and "launches" not in tokens
        assert "robot" in tokens and "factory" in tokens

    def test_case_and_punctuation_insensitive(self):
        assert _story_tokens("Mimic Robotics' M1 Hand!") == _story_tokens("mimic robotics m1 hand")

    def test_empty_title_is_empty_set(self):
        assert _story_tokens("") == frozenset()
        assert _story_tokens(None) == frozenset()


class TestSameStoryDetection:
    def test_identical_headlines_match(self):
        assert _same("Tacta Systems Unveils Robotic Hands",
                     "Tacta Systems Unveils Robotic Hands")

    def test_same_event_different_outlet_wording_matches(self):
        # Real pair observed in the 2026-08-15 sweep.
        assert _same(
            "mimic Robotics Launches M1 Hand and U1 Exoskeleton to Bring Dexterity",
            "mimic Robotics launches the M1 Hand and U1 Exoskeleton for dexterity")

    def test_distinct_companies_do_not_match(self):
        # The expensive failure: these must stay separate.
        assert not _same("Tacta Systems Unveils Robotic Hands For High-Volume Assembly",
                         "Mimic Robotics Launches M1 Hand and U1 Exoskeleton")

    def test_same_topic_different_company_does_not_match(self):
        assert not _same("Figure AI raises funding for humanoid robots",
                         "Apptronik raises funding for humanoid robots")

    def test_empty_tokens_never_match(self):
        assert not _is_same_story(frozenset(), [_story_tokens("anything at all here")])

    def test_no_prior_items_never_matches(self):
        assert not _is_same_story(_story_tokens("Some Robotics Headline"), [])

    @pytest.mark.parametrize("threshold,expected", [(0.3, True), (0.95, False)])
    def test_threshold_governs_strictness(self, threshold, expected):
        got = _same("Robot hand startup ships gripper hardware",
                    "Robot hand startup ships gripper hardware to factories today",
                    threshold=threshold)
        assert got is expected


class TestSymmetry:
    def test_comparison_is_symmetric(self):
        a = "Kailong High Tech Pivots from Emission Control to Embodied AI"
        b = "Kailong pivots from emission control into embodied AI"
        assert _same(a, b) == _same(b, a)


class TestCorpusGeneric:
    """DF-derived stopwords. Replaces a hand-maintained list, which would rot."""

    def test_returns_empty_below_min_titles(self):
        assert corpus_generic(["robot hand launch", "robot arm launch"]) == frozenset()

    def test_flags_tokens_above_document_frequency_ceiling(self):
        # "robot" in every title, "mimic" in one -> only "robot" is generic.
        titles = [f"robot story number {i} about things" for i in range(30)]
        titles.append("mimic exoskeleton dexterity breakthrough")
        generic = corpus_generic(titles, max_df=0.5)
        assert "robot" in generic
        assert "mimic" not in generic and "exoskeleton" not in generic

    def test_generic_gate_prevents_cross_company_merge(self):
        """The regression this whole mechanism exists for."""
        titles = [f"company{i} raises funding for humanoid robots" for i in range(30)]
        generic = corpus_generic(titles, max_df=0.5)
        assert not _same("Figure AI raises funding for humanoid robots",
                         "Apptronik raises funding for humanoid robots", generic=generic)

    def test_without_generic_set_falls_back_to_plain_jaccard(self):
        # Documents the small-batch behaviour: no DF signal, so overlap alone decides.
        assert _same("Figure AI raises funding for humanoid robots",
                     "Apptronik raises funding for humanoid robots", generic=frozenset())
