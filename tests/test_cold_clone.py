"""The shipped config must actually score — a cold clone must never be silently inert.

2026-09-01 audit: the public edition's rubric.yaml.example carried none of the six keys
score.py reads. 312 of 312 real companies fell to T6_PASS, zero queued for review, exit 0.
Green pipeline, empty product. These tests load config/<name>.yaml if present (private tree)
else config/<name>.yaml.example (public tree), so they guard both editions.
"""
import os

import yaml

from radar import score as S

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCORER_KEYS = ("axes", "tiers", "leverage", "competitor", "thin_description", "stage2_queue")


def _cfg(name):
    for cand in (f"config/{name}", f"config/{name}.example"):
        path = os.path.join(ROOT, cand)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return yaml.safe_load(fh)
    raise FileNotFoundError(f"neither config/{name} nor its .example exists")


def test_rubric_has_every_key_the_scorer_reads():
    rubric = _cfg("rubric.yaml")
    missing = [k for k in SCORER_KEYS if k not in rubric]
    assert not missing, f"rubric lacks {missing} — score.py reads them; without them every row is T6_PASS"
    assert any("groups" in (a or {}) for a in rubric["axes"].values()), \
        "no axis has term groups — nothing can score above zero"


def test_sources_has_a_delivery_block():
    src = _cfg("sources.yaml")
    assert "delivery" in src and src["delivery"].get("to") and src["delivery"].get("sender_account"), \
        "digest.py reads delivery.to / delivery.sender_account; without them the send exits"


def test_perfect_fit_row_is_not_passed():
    rubric = _cfg("rubric.yaml")
    row = {
        "name": "Fixture Robotics",
        "one_liner": "Teleoperation and human-in-the-loop remote intervention for robot arms",
        "long_description": ("Remote operator takeover gives 100% uptime for manipulation "
                             "policies deployed on factory production lines. Tactile sensing "
                             "and force control for delicate insertion tasks."),
        "tags": "robotics, hardware",
        "team_size": 6,
        "batch": "S26",
        "status": "Active",
    }
    out = S.score_row(row, rubric, {})
    assert out["tier"] != "T6_PASS", f"perfect-fit row fell to T6_PASS: {out}"
    assert out["stage2"], "perfect-fit row was not queued for stage-2 review"
