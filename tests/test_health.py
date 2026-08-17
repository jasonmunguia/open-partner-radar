"""Regression tests for the 259-hour false green.

The bug, in production on 2026-08-16: health.json reported
    ingest_yc  produced=741  failures=0  degraded=False
while the lane's last successful run was 259 hours (10.8 days) earlier. Two
independent defects combined to hide it:

  1. `ingest()` raises YCError the instant Bookface auth breaks. That raise
     propagated out of `cmd_ingest` BEFORE `_publish_health` ran, so the stored
     entry was simply never updated — health kept describing the last run that
     worked.
  2. `degraded` is computed only from a run that actually happens. Nothing in the
     schema could express "this feature stopped running," so even a correct entry
     would have looked fine.

Fix 1 makes the failure publish. Fix 2 evaluates staleness at read time. Either
alone leaves a blind spot, so both are tested here.
"""
import json
import time

import pytest

from radar import run as R


@pytest.fixture
def health_file(tmp_path, monkeypatch):
    """Point the module at a throwaway health.json."""
    p = tmp_path / "health.json"
    monkeypatch.setattr(R, "HEALTH", str(p))
    return p


def _write(path, entry_name, **fields):
    path.write_text(json.dumps({entry_name: fields}))


def _write_many(path, **entries):
    """Write several features at once. `_write` replaces the whole file, so calling it
    twice silently keeps only the last entry — which is a trap when testing interactions
    between features."""
    path.write_text(json.dumps(entries))


# ---------------------------------------------------------------- staleness

def test_stale_feature_is_unhealthy_even_when_degraded_is_false(health_file):
    """THE regression test. Exactly the production row that lied."""
    _write(health_file, "ingest_yc",
           last_run=int(time.time()) - 259 * 3600,
           produced=741, failures=0, degraded=False, ever_produced=True,
           zero_streak=0, max_age_hours=60)

    rows = R.health_report(retired=frozenset())
    assert rows["ingest_yc"]["stale"] is True
    assert rows["ingest_yc"]["unhealthy"] is True, (
        "a lane dark for 259h must not report healthy"
    )
    assert rows["ingest_yc"]["age_h"] == pytest.approx(259, abs=1)


def test_fresh_feature_is_healthy(health_file):
    _write(health_file, "ingest_yc",
           last_run=int(time.time()) - 3600, produced=741, failures=0,
           degraded=False, ever_produced=True, zero_streak=0, max_age_hours=60)

    rows = R.health_report(retired=frozenset())
    assert rows["ingest_yc"]["stale"] is False
    assert rows["ingest_yc"]["unhealthy"] is False


def test_max_age_falls_back_to_declared_table(health_file):
    """An entry written before max_age_hours existed still gets evaluated."""
    _write(health_file, "ingest_yc",
           last_run=int(time.time()) - 200 * 3600,
           produced=10, failures=0, degraded=False, ever_produced=True, zero_streak=0)

    rows = R.health_report(retired=frozenset())
    assert rows["ingest_yc"]["max_age_hours"] == R.SOURCE_MAX_AGE_H["ingest_yc"]
    assert rows["ingest_yc"]["unhealthy"] is True


def test_degraded_run_is_unhealthy_even_when_fresh(health_file):
    _write(health_file, "digest", last_run=int(time.time()), produced=0,
           failures=22, degraded=True, ever_produced=True, zero_streak=1,
           max_age_hours=36)
    assert R.health_report()["digest"]["unhealthy"] is True


def test_missing_health_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "HEALTH", str(tmp_path / "nope.json"))
    assert R.health_report() == {}


def test_corrupt_health_file_does_not_raise(health_file):
    health_file.write_text("{not json")
    assert R.health_report() == {}


# ------------------------------------------------- failure must publish health

def _sources():
    return {"tier_a": [
        {"name": "yc", "enabled": True, "batches": ["W26", "S26"]},
        {"name": "a16z_speedrun", "enabled": False},
    ]}


def test_yc_auth_failure_publishes_health(health_file, monkeypatch, capsys):
    """A raise inside ingest() must land in health.json, not just stderr."""
    def boom(*_a, **_k):
        raise RuntimeError("yc status='forbidden' body=403")

    monkeypatch.setattr(R, "ingest", boom)
    status = R._ingest_yc(_sources())

    assert status == 2
    written = json.loads(health_file.read_text())["ingest_yc"]
    assert written["failures"] == 1
    assert written["produced"] == 0
    assert "403" in written["error"]
    assert "RuntimeError" in written["error"]


def test_yc_failure_does_not_stop_a16z(health_file, monkeypatch):
    """Sources are independent. YC dying must not blind a16z — that cascade froze
    both features at the same timestamp for 259h."""
    def boom(*_a, **_k):
        raise RuntimeError("auth dead")

    monkeypatch.setattr(R, "ingest", boom)

    calls = []
    def fake_a16z(sources):
        calls.append(True)
        return 0

    monkeypatch.setattr(R, "_ingest_a16z", fake_a16z)
    monkeypatch.setattr(R, "_cfg", lambda _n: _sources())

    status = R.cmd_ingest()
    assert calls == [True], "a16z must still run after YC fails"
    assert status == 2, "overall ingest still reports failure"


def test_disabled_yc_is_not_a_failure(health_file, monkeypatch):
    srcs = {"tier_a": [{"name": "yc", "enabled": False, "batches": []}]}
    assert R._ingest_yc(srcs) == 0


# ---------------------------------------------------------------- cmd exit code

def test_cmd_health_exits_2_when_unhealthy(health_file, capsys):
    # news_fetch, not ingest_yc: yc is retired in config, so cmd_health would
    # correctly call it healthy and this test would prove nothing.
    _write(health_file, "news_fetch",
           last_run=int(time.time()) - 259 * 3600, produced=741, failures=0,
           degraded=False, ever_produced=True, zero_streak=0, max_age_hours=60)
    assert R.cmd_health() == 2
    assert "STALE" in capsys.readouterr().out


def test_cmd_health_exits_0_when_healthy(health_file, capsys):
    _write(health_file, "news_fetch",
           last_run=int(time.time()) - 3600, produced=741, failures=0,
           degraded=False, ever_produced=True, zero_streak=0, max_age_hours=60)
    assert R.cmd_health() == 0
    assert "all features healthy" in capsys.readouterr().out


# ---------------------------------------------------------------- retirement

def test_retired_source_is_not_flagged_stale(health_file):
    """A deliberately disabled lane is not a broken one.

    YC was retired 2026-08-15 (its auth belonged to another person and kept expiring).
    Reporting it STALE forever would train the reader to skip the health section — the
    exact failure this health layer exists to prevent.
    """
    _write(health_file, "ingest_yc",
           last_run=int(time.time()) - 275 * 3600,
           produced=741, failures=0, degraded=False, ever_produced=True,
           zero_streak=0, max_age_hours=60)

    rows = R.health_report(retired=frozenset({"ingest_yc"}))
    assert rows["ingest_yc"]["retired"] is True
    assert rows["ingest_yc"]["stale"] is False
    assert rows["ingest_yc"]["unhealthy"] is False


def test_retirement_does_not_mask_other_features(health_file):
    """Retiring one source must not silence a genuinely broken one."""
    _write_many(
        health_file,
        ingest_yc={"last_run": int(time.time()) - 275 * 3600, "produced": 741,
                   "failures": 0, "degraded": False, "ever_produced": True,
                   "zero_streak": 0, "max_age_hours": 60},
        news_fetch={"last_run": int(time.time()) - 99 * 3600, "produced": 0,
                    "failures": 3, "degraded": True, "ever_produced": True,
                    "zero_streak": 2, "max_age_hours": 36},
    )

    rows = R.health_report(retired=frozenset({"ingest_yc"}))
    assert rows["ingest_yc"]["unhealthy"] is False
    assert rows["news_fetch"]["unhealthy"] is True


def test_retired_set_derived_from_disabled_config_sources():
    """Disabling a source in sources.yaml is the single switch that also silences it."""
    retired = R._retired_features()
    assert "ingest_yc" in retired, "yc is disabled in config, so it must read as retired"
    assert "ingest_a16z" not in retired, "a16z is enabled and must stay monitored"
