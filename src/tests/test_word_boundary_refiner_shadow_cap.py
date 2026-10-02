"""Tests for the shadow-mode shrink cap on WordBoundaryRefiner.

The cap is log-only while SHADOW_CAP_ENFORCE is False: these tests pin the
decision math, the log record shape, and the guarantee that shadow mode
never modifies the refinement it observes.
"""

import json
from pathlib import Path

import podcast_processor.word_boundary_refiner as wbr
from podcast_processor.word_boundary_refiner import WordBoundaryRefiner
from shared.test_utils import create_standard_test_config


def _record(**over):
    kwargs = dict(
        ad_start=742.0,
        ad_end=917.4,
        refined_start=742.0,
        refined_end=917.4,
        post_id=2380,
        post_guid="472602a9-test",
        start_reason="r",
        end_reason="r",
    )
    kwargs.update(over)
    return WordBoundaryRefiner._shadow_cap_record(**kwargs)


def test_shadow_cap_quiet_when_no_shrink():
    assert _record() is None


def test_shadow_cap_quiet_at_exactly_cap():
    # Boundary: strictly greater-than engages.
    assert _record(refined_start=742.0 + 60.0) is None
    assert _record(refined_end=917.4 - 60.0) is None


def test_shadow_cap_engages_on_start_shrink_devday_case():
    # Production DevDay numbers: block 742.0-917.4 refined to start 873.3.
    rec = _record(refined_start=873.3)
    assert rec is not None
    assert rec["event"] == "shadow_cap_would_clamp"
    assert rec["start_shrink_s"] == 131.3
    assert rec["clamped_start"] == 742.0 + 60.0
    assert rec["clamped_end"] == 917.4  # end untouched
    assert rec["enforced"] is False


def test_shadow_cap_engages_on_end_shrink():
    rec = _record(refined_end=917.4 - 66.1)
    assert rec is not None
    assert rec["end_shrink_s"] == 66.1
    assert rec["clamped_end"] == 917.4 - 60.0
    assert rec["clamped_start"] == 742.0  # start untouched


def test_shadow_cap_record_does_not_mutate_inputs():
    inputs = dict(
        ad_start=742.0,
        ad_end=917.4,
        refined_start=873.3,
        refined_end=900.0,
        post_id=2380,
        post_guid="g",
        start_reason="r",
        end_reason="r",
    )
    before = dict(inputs)
    WordBoundaryRefiner._shadow_cap_record(**inputs)
    assert inputs == before


def test_shadow_cap_log_writes_valid_jsonl(tmp_path, monkeypatch):
    log = tmp_path / "shadow.jsonl"
    monkeypatch.setattr(wbr, "SHADOW_CAP_LOG_PATH", str(log))
    refiner = WordBoundaryRefiner(config=create_standard_test_config())
    rec = _record(refined_start=873.3)
    assert rec is not None
    refiner._write_shadow_cap_log(rec)
    lines = Path(log).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["event"] == "shadow_cap_would_clamp"
    assert parsed["post_id"] == 2380


def test_shadow_cap_log_never_raises(tmp_path, monkeypatch):
    # Unwritable path must not break refinement.
    monkeypatch.setattr(wbr, "SHADOW_CAP_LOG_PATH", "/proc/nope/shadow.jsonl")
    refiner = WordBoundaryRefiner(config=create_standard_test_config())
    rec = _record(refined_start=873.3)
    assert rec is not None
    refiner._write_shadow_cap_log(rec)  # must not raise
