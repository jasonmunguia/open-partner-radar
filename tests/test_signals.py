"""Signal decay, identity, and rollup.

The behavior worth protecting: a signal's value must fall with age at a rate that
depends on its TYPE. Uniform decay would make an open plant-expansion window look as
stale as a filled job req, which defeats the point of tracking signals at all.
"""
import time

import pytest

from radar import signals as S


NOW = 1_800_000_000


def _sig(kind, days_ago, **kw):
    return S.make_signal(kind=kind, title=f"{kind} event", url=f"https://ex.com/{kind}",
                         source="test", ts=NOW - days_ago * 86400,
                         entity_name="Acme", website="acme.com", **kw)


# ------------------------------------------------------------------ identity

def test_entity_key_prefers_yc_id():
    assert S.entity_key(yc_id="123", website="acme.com") == "yc:123"


def test_entity_key_normalizes_domain():
    for site in ("https://www.Acme.com/careers", "acme.com", "http://acme.com/"):
        assert S.entity_key(website=site) == "domain:acme.com"


def test_entity_key_falls_back_to_name():
    assert S.entity_key(name="  Acme Robotics ") == "name:acme robotics"


def test_signal_and_company_row_share_identity():
    """Principle 5 — a signal must collide with the company row for the same firm."""
    from radar.ycfetch import canonical_key
    row = {"source": "web", "website": "https://www.acme.com/"}
    assert canonical_key(row) == S.entity_key(website="https://www.acme.com/")


# --------------------------------------------------------------------- decay

def test_weight_halves_at_exactly_one_half_life():
    base, half = S.SIGNAL_TYPES["funding"]
    fresh = S.decayed_weight(_sig("funding", 0), now=NOW)
    aged = S.decayed_weight(_sig("funding", half), now=NOW)
    assert fresh == pytest.approx(base)
    assert aged == pytest.approx(base / 2, rel=1e-6)


def test_hiring_decays_faster_than_facility():
    """A filled req goes stale; a new plant stays a deployment window."""
    hiring = S.decayed_weight(_sig("hiring_automation", 90), now=NOW)
    facility = S.decayed_weight(_sig("facility", 90), now=NOW)
    assert facility > hiring, "facility must outlast hiring at 90 days"


def test_fresh_hiring_outranks_fresh_funding():
    """Deployment-speed lens: staffing an automation role beats a raise."""
    assert (S.decayed_weight(_sig("hiring_automation", 0), now=NOW)
            > S.decayed_weight(_sig("funding", 0), now=NOW))


def test_magnitude_scales_weight():
    small = S.decayed_weight(_sig("funding", 0, magnitude=1.0), now=NOW)
    big = S.decayed_weight(_sig("funding", 0, magnitude=5.0), now=NOW)
    assert big == pytest.approx(small * 5)


def test_unknown_kind_falls_back_to_press():
    sig = S.make_signal(kind="not_a_real_kind", title="t", url="u", source="s",
                        ts=NOW, website="acme.com")
    assert sig["kind"] == S.DEFAULT_TYPE


def test_future_timestamp_does_not_inflate_weight():
    base, _ = S.SIGNAL_TYPES["funding"]
    assert S.decayed_weight(_sig("funding", -30), now=NOW) == pytest.approx(base)


# ------------------------------------------------------------------- storage

def test_ingest_dedupes_on_repeat(tmp_path):
    rows = [_sig("funding", 1), _sig("funding", 1)]
    rep = S.ingest_signals(rows, str(tmp_path))
    assert rep["new"] == 1 and rep["duplicate"] == 1


def test_ingest_is_append_only_across_runs(tmp_path):
    S.ingest_signals([_sig("funding", 1)], str(tmp_path))
    rep = S.ingest_signals([_sig("funding", 1), _sig("launch", 1)], str(tmp_path))
    assert rep["new"] == 1
    assert rep["total_stored"] == 2


def test_load_drops_past_retention(tmp_path):
    S.ingest_signals([_sig("funding", 5), _sig("funding", 400)], str(tmp_path))
    kept = S.load_signals(str(tmp_path), now=NOW, retention_days=365)
    assert len(kept) == 1


def test_corrupt_line_does_not_break_load(tmp_path):
    S.ingest_signals([_sig("funding", 1)], str(tmp_path))
    with open(tmp_path / "signals.jsonl", "a") as fh:
        fh.write("{not json\n\n")
    assert len(S.load_signals(str(tmp_path), now=NOW)) == 1


# -------------------------------------------------------------------- rollup

def test_heat_sums_and_three_fresh_beat_one_old():
    hot = [_sig("press", 0), _sig("press", 0, magnitude=1.0), _sig("launch", 1)]
    cold = [_sig("funding", 900)]
    assert (S.heat(hot, now=NOW)["domain:acme.com"]["heat"]
            > S.heat(cold, now=NOW)["domain:acme.com"]["heat"])


def test_ranked_orders_by_heat_desc():
    a = S.make_signal(kind="hiring_automation", title="a", url="https://a.com/1",
                      source="t", ts=NOW, website="a.com", entity_name="A")
    b = S.make_signal(kind="press", title="b", url="https://b.com/1",
                      source="t", ts=NOW - 50 * 86400, website="b.com", entity_name="B")
    rows = S.ranked([a, b], now=NOW)
    assert [r["entity_key"] for r in rows] == ["domain:a.com", "domain:b.com"]


def test_heat_tracks_latest_and_age():
    rows = S.heat([_sig("press", 10), _sig("launch", 2)], now=NOW)
    assert rows["domain:acme.com"]["age_days"] == pytest.approx(2, abs=0.1)
    assert rows["domain:acme.com"]["count"] == 2


def test_min_heat_filters():
    assert S.ranked([_sig("press", 600)], now=NOW, min_heat=1.0) == []
