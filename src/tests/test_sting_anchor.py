"""Tests for the deterministic sting anchor (backtest-validated policy).

The flagship cases mirror real golden-set episodes: post 2409's Odd
Lots break-2 start sat 13s into the ChatGPT ad head (middle-out cut);
post 2397's AI Daily Brief end swallowed the "Welcome back" resume;
post 2402's end left the Blitzy sign-off tail, recoverable only by a
later move on a 1.00-score return-sting detection.
"""

import json

import pytest

from podcast_processor.sting_anchor import (
    EndAnchorRule,
    FeedStingConfig,
    StartAnchorRule,
    StingPeak,
    apply_sting_anchors,
    feed_sting_config,
    load_sting_configs,
    normalized_xcorr,
    pick_peaks,
)


def boundary(start: float, end: float) -> dict:
    return {
        "orig_start": start,
        "orig_end": end,
        "refined_start": start,
        "refined_end": end,
        "confidence": 0.85,
    }


def peak(time: float, score: float) -> StingPeak:
    return StingPeak(time=time, score=score)


# Odd Lots: motif template, START only (offset +13.6, dead zone 5s).
OL_CONFIG = FeedStingConfig(
    template_path="odd_lots_motif.wav",
    threshold=0.7,
    start_rule=StartAnchorRule(
        offset=13.6,
        window_before=90.0,
        window_after=10.0,
        min_move=5.0,
        require_positive_break_start=True,
    ),
)

# AI Daily Brief: sting B template, START (regime-gated) + END roles.
ADB_CONFIG = FeedStingConfig(
    template_path="adb_sting_b.wav",
    threshold=0.7,
    start_rule=StartAnchorRule(
        offset=4.4,
        window_before=45.0,
        window_after=8.0,
        min_move=1.0,
        regime_gate=(6.0, 15.0),
    ),
    end_rule=EndAnchorRule(
        offset=-1.0,
        window_before=25.0,
        window_after=20.0,
        dead_zone=3.0,
        min_move=1.0,
        later_move_min_score=0.9,
        min_start_gap=15.0,
    ),
)


def test_ol_start_anchor_recovers_middle_out_start() -> None:
    # Post 2409 break 2: refined start 1792.2, motif onset 1763.8 (0.98).
    boundaries = [boundary(1500.0, 1700.0), boundary(1792.2, 1906.8)]
    result, adjustments = apply_sting_anchors(
        boundaries, OL_CONFIG, [peak(1763.8, 0.98)]
    )
    assert len(adjustments) == 1
    adj = adjustments[0]
    assert adj.block_index == 1
    assert adj.edge == "start"
    assert result[1]["refined_start"] == pytest.approx(1777.4)
    assert result[1]["refined_end"] == 1906.8
    assert result[1]["sting_anchor"] == {
        "role": "start",
        "score": 0.98,
        "onset": 1763.8,
        "original_start": 1792.2,
    }
    # Inputs are not mutated, and the other block is untouched.
    assert boundaries[1]["refined_start"] == 1792.2
    assert "sting_anchor" not in boundaries[1]
    assert result[0] == boundaries[0]


def test_ol_start_dead_zone_suppresses_small_move() -> None:
    # Implied move is 3.6s: inside the 5s dead zone (bed/mapping variance).
    boundaries = [boundary(1000.0, 1200.0)]
    result, adjustments = apply_sting_anchors(
        boundaries, OL_CONFIG, [peak(990.0, 0.97)]
    )
    assert adjustments == []
    assert result[0]["refined_start"] == 1000.0


def test_ol_start_window_excludes_far_peak() -> None:
    boundaries = [boundary(1000.0, 1200.0)]
    result, adjustments = apply_sting_anchors(
        boundaries, OL_CONFIG, [peak(880.0, 0.99)]
    )
    assert adjustments == []
    assert result[0]["refined_start"] == 1000.0


def test_ol_start_skips_episode_opening_break() -> None:
    # require_positive_break_start: a break starting at 0.0 is the
    # episode opening, where the motif role does not apply.
    boundaries = [boundary(0.0, 100.0)]
    result, adjustments = apply_sting_anchors(boundaries, OL_CONFIG, [peak(10.0, 0.99)])
    assert adjustments == []
    assert result[0]["refined_start"] == 0.0


def test_adb_start_fine_adjustment() -> None:
    boundaries = [boundary(1000.0, 1200.0)]
    result, adjustments = apply_sting_anchors(
        boundaries, ADB_CONFIG, [peak(997.0, 0.99)]
    )
    assert len(adjustments) == 1
    assert result[0]["refined_start"] == pytest.approx(1001.4)


def test_adb_start_regime_gate_vetoes_mid_size_move() -> None:
    # Post 2404's shape: the sting sits 11.8s before the ad (norm 4.4),
    # implying a 7.4s move -- inside the ambiguous 6-15s band.
    boundaries = [boundary(584.8, 700.0)]
    result, adjustments = apply_sting_anchors(
        boundaries, ADB_CONFIG, [peak(573.0, 0.99)]
    )
    assert adjustments == []
    assert result[0]["refined_start"] == 584.8


def test_adb_start_large_move_recovers_misplaced_boundary() -> None:
    # Post 2396: refined start 1215.078, sting onset 1183.3 (1.00).
    boundaries = [boundary(1215.078, 1400.0)]
    result, adjustments = apply_sting_anchors(
        boundaries, ADB_CONFIG, [peak(1183.3, 1.0)]
    )
    assert len(adjustments) == 1
    assert result[0]["refined_start"] == pytest.approx(1187.7)


def test_adb_end_earlier_move_recovers_swallowed_resume() -> None:
    # Post 2397: refined end 753.6, return sting onset 743.8 (0.99).
    boundaries = [boundary(555.0, 753.6)]
    result, adjustments = apply_sting_anchors(
        boundaries, ADB_CONFIG, [peak(743.8, 0.99)]
    )
    assert len(adjustments) == 1
    adj = adjustments[0]
    assert adj.edge == "end"
    assert result[0]["refined_end"] == pytest.approx(742.8)
    assert result[0]["sting_anchor"] == {
        "role": "end",
        "score": 0.99,
        "onset": 743.8,
        "original_end": 753.6,
    }


def test_adb_end_dead_zone_leaves_consistent_end() -> None:
    # Onset within 3s of the end: refiner already agrees with the sting.
    boundaries = [boundary(555.0, 753.6)]
    result, adjustments = apply_sting_anchors(
        boundaries, ADB_CONFIG, [peak(751.6, 0.99)]
    )
    assert adjustments == []
    assert result[0]["refined_end"] == 753.6


def test_adb_end_later_move_requires_high_score() -> None:
    # Post 2402's Blitzy tail: end 916.7, return onset 921.1; the target
    # (920.1) extends the cut -- allowed only at score >= 0.9.
    boundaries = [boundary(743.1, 916.7)]
    result, adjustments = apply_sting_anchors(
        boundaries, ADB_CONFIG, [peak(921.1, 0.85)]
    )
    assert adjustments == []
    assert result[0]["refined_end"] == 916.7

    result, adjustments = apply_sting_anchors(
        boundaries, ADB_CONFIG, [peak(921.1, 1.0)]
    )
    assert len(adjustments) == 1
    assert result[0]["refined_end"] == pytest.approx(920.1)


def test_adb_end_window_excludes_own_start_sting() -> None:
    # A short block: its start sting (902.0) must not be read as the
    # return sting; the end window starts at break_start + 15.
    boundaries = [boundary(900.0, 916.7)]
    result, adjustments = apply_sting_anchors(
        boundaries, ADB_CONFIG, [peak(902.0, 1.0)]
    )
    assert adjustments == []
    assert result[0]["refined_end"] == 916.7


def test_cluster_internal_edges_are_never_moved() -> None:
    # Two blocks 10s apart are one break: start role hits block 0's
    # start only, end role hits block 1's end only.
    boundaries = [boundary(1000.0, 1100.0), boundary(1110.0, 1200.0)]
    peaks = [peak(990.0, 0.99), peak(1190.0, 0.99)]
    result, adjustments = apply_sting_anchors(boundaries, ADB_CONFIG, peaks)
    assert {(a.block_index, a.edge) for a in adjustments} == {(0, "start"), (1, "end")}
    assert result[0]["refined_start"] == pytest.approx(994.4)
    assert result[0]["refined_end"] == 1100.0
    assert result[1]["refined_start"] == 1110.0
    assert result[1]["refined_end"] == pytest.approx(1189.0)


def test_noop_cases() -> None:
    boundaries = [boundary(1000.0, 1200.0)]
    # No config for the feed.
    result, adjustments = apply_sting_anchors(boundaries, None, [peak(990.0, 0.99)])
    assert adjustments == []
    assert result[0] == boundaries[0]
    # No detections at all (positive-only: absence is not evidence).
    result, adjustments = apply_sting_anchors(boundaries, OL_CONFIG, [])
    assert adjustments == []
    # Detection below the feed threshold (degraded sting instance).
    result, adjustments = apply_sting_anchors(
        boundaries, OL_CONFIG, [peak(990.0, 0.49)]
    )
    assert adjustments == []
    # No boundaries.
    result, adjustments = apply_sting_anchors([], OL_CONFIG, [peak(990.0, 0.99)])
    assert result == []
    assert adjustments == []


def test_threshold_override_admits_weaker_peak() -> None:
    config = FeedStingConfig(
        template_path="tpl.wav",
        threshold=0.6,
        start_rule=StartAnchorRule(
            offset=13.6, window_before=90.0, window_after=10.0, min_move=5.0
        ),
    )
    boundaries = [boundary(1000.0, 1200.0)]
    result, adjustments = apply_sting_anchors(boundaries, config, [peak(970.0, 0.65)])
    assert len(adjustments) == 1
    assert result[0]["refined_start"] == pytest.approx(983.6)


def test_normalized_xcorr_matches_brute_force() -> None:
    import numpy as np

    rng = np.random.default_rng(7)
    signal = rng.normal(0.0, 1.0, 400)
    template = rng.normal(0.0, 1.0, 30)
    scores = normalized_xcorr(template, signal)

    t = template - template.mean()
    s = signal - signal.mean()
    expected = []
    for k in range(len(s) - len(t) + 1):
        window = s[k : k + len(t)]
        expected.append(
            float(window @ t) / (np.sqrt(float(window @ window) * float(t @ t)) + 1e-12)
        )
    assert scores == pytest.approx(np.array(expected), abs=1e-9)


def test_pick_peaks_finds_planted_template_and_suppresses_neighbours() -> None:
    import numpy as np

    rng = np.random.default_rng(42)
    sample_rate = 8000
    template = rng.normal(0.0, 1.0, 2 * sample_rate)
    signal = rng.normal(0.0, 0.05, 30 * sample_rate)
    planted = 12345
    signal[planted : planted + len(template)] += template
    scores = normalized_xcorr(template, signal)
    peaks = pick_peaks(scores, len(template), floor=0.5)
    assert peaks
    best = max(peaks, key=lambda p: p.score)
    assert best.time == pytest.approx(planted / sample_rate, abs=1.0 / sample_rate)
    assert best.score > 0.9
    # NMS: no two peaks within one template length of each other.
    import itertools

    times = [p.time for p in peaks]
    for a, b in itertools.pairwise(times):
        assert b - a >= len(template) / sample_rate


def test_load_sting_configs_resolves_relative_template(tmp_path) -> None:
    (tmp_path / "tpl.wav").write_bytes(b"RIFF")
    config_path = tmp_path / "sting.json"
    config_path.write_text(
        json.dumps(
            {
                "feeds": {
                    "2": {
                        "template": "tpl.wav",
                        "start": {"offset": 4.4, "regime_gate": [6.0, 15.0]},
                        "end": {"offset": -1.0},
                    },
                    "1": {"template": "tpl.wav", "start": {"offset": 13.6}},
                }
            }
        )
    )
    configs = load_sting_configs(str(config_path))
    assert set(configs) == {1, 2}
    adb = configs[2]
    assert adb.template_path == str(tmp_path / "tpl.wav")
    assert adb.threshold == 0.7
    assert adb.start_rule is not None
    assert adb.start_rule.regime_gate == (6.0, 15.0)
    assert adb.end_rule is not None
    assert adb.end_rule.later_move_min_score == 0.9
    assert configs[1].end_rule is None
    assert feed_sting_config(2, str(config_path)) == adb
    assert feed_sting_config(99, str(config_path)) is None


def test_load_sting_configs_unset_env_is_empty(monkeypatch) -> None:
    monkeypatch.delenv("PODLY_STING_CONFIG", raising=False)
    assert load_sting_configs() == {}


def test_load_sting_configs_missing_offset_is_an_error(tmp_path) -> None:
    config_path = tmp_path / "bad.json"
    config_path.write_text(
        json.dumps({"feeds": {"2": {"template": "t.wav", "start": {}}}})
    )
    with pytest.raises((KeyError, ValueError)):
        load_sting_configs(str(config_path))


# Odd Lots with the early cut: skip the motif when the span between
# the early and standard targets holds no non-ad speech.
OL_EARLY_CONFIG = FeedStingConfig(
    template_path="odd_lots_motif.wav",
    threshold=0.7,
    start_rule=StartAnchorRule(
        offset=13.6,
        window_before=90.0,
        window_after=10.0,
        min_move=5.0,
        require_positive_break_start=True,
        early_offset=1.0,
    ),
)


def test_early_cut_fires_when_span_clear() -> None:
    # Refined start 120.0, motif onset 100.0: standard target 113.6
    # (move 6.4, past the 5s dead zone). Span (101.0, 113.6) holds no
    # speech at all, so the early target 101.0 wins.
    boundaries = [boundary(120.0, 200.0)]
    result, adjustments = apply_sting_anchors(
        boundaries, OL_EARLY_CONFIG, [peak(100.0, 1.0)], [], []
    )
    assert len(adjustments) == 1
    adj = adjustments[0]
    assert adj.early is True
    assert result[0]["refined_start"] == pytest.approx(101.0)
    assert result[0]["sting_anchor"] == {
        "role": "start",
        "score": 1.0,
        "onset": 100.0,
        "original_start": 120.0,
        "early": True,
    }


def test_early_cut_blocked_by_content_speech() -> None:
    # Unlabeled speech in the span: keep the standard target.
    boundaries = [boundary(120.0, 200.0)]
    tsegs = [(105.0, 110.0, "none")]
    result, adjustments = apply_sting_anchors(
        boundaries, OL_EARLY_CONFIG, [peak(100.0, 1.0)], tsegs, []
    )
    assert len(adjustments) == 1
    assert adjustments[0].early is False
    assert result[0]["refined_start"] == pytest.approx(113.6)
    assert "early" not in result[0]["sting_anchor"]


def test_early_cut_allowed_with_ad_labeled_speech() -> None:
    # The only speech in the span is ad-labeled: cutting it is the
    # point, so the early target applies.
    boundaries = [boundary(120.0, 200.0)]
    tsegs = [(102.0, 112.0, "ad")]
    result, adjustments = apply_sting_anchors(
        boundaries, OL_EARLY_CONFIG, [peak(100.0, 1.0)], tsegs, []
    )
    assert len(adjustments) == 1
    assert adjustments[0].early is True
    assert result[0]["refined_start"] == pytest.approx(101.0)


def test_early_cut_blocked_by_untranscribed_ina_speech() -> None:
    # No transcript coverage in the span but INA hears speech: unknown
    # talk, not safe to cut.
    boundaries = [boundary(120.0, 200.0)]
    result, adjustments = apply_sting_anchors(
        boundaries, OL_EARLY_CONFIG, [peak(100.0, 1.0)], [], [(105.0, 110.0)]
    )
    assert len(adjustments) == 1
    assert adjustments[0].early is False
    assert result[0]["refined_start"] == pytest.approx(113.6)


def test_early_cut_allows_ina_speech_inside_ad_coverage() -> None:
    # INA speech that sits inside ad-labeled transcript coverage is the
    # same ad speech: the early target applies.
    boundaries = [boundary(120.0, 200.0)]
    tsegs = [(100.0, 115.0, "ad")]
    ina = [(105.0, 120.0)]
    result, adjustments = apply_sting_anchors(
        boundaries, OL_EARLY_CONFIG, [peak(100.0, 1.0)], tsegs, ina
    )
    assert len(adjustments) == 1
    assert adjustments[0].early is True
    assert result[0]["refined_start"] == pytest.approx(101.0)


def test_early_cut_fails_safe_without_segment_data() -> None:
    # early_offset configured but no segment data available: the
    # standard target applies, never a blind early cut.
    boundaries = [boundary(120.0, 200.0)]
    result, adjustments = apply_sting_anchors(
        boundaries, OL_EARLY_CONFIG, [peak(100.0, 1.0)]
    )
    assert len(adjustments) == 1
    assert adjustments[0].early is False
    assert result[0]["refined_start"] == pytest.approx(113.6)


def test_no_early_offset_is_parity() -> None:
    # Without early_offset (e.g. the ADB configs), passing segment data
    # changes nothing: stock behavior, early never set.
    boundaries = [boundary(120.0, 200.0)]
    result, adjustments = apply_sting_anchors(
        boundaries, OL_CONFIG, [peak(100.0, 1.0)], [], []
    )
    assert len(adjustments) == 1
    assert adjustments[0].early is False
    assert result[0]["refined_start"] == pytest.approx(113.6)
    assert "early" not in result[0]["sting_anchor"]


def test_load_sting_configs_parses_early_offset(tmp_path) -> None:
    cfg = {
        "feeds": {
            "1": {
                "template": "motif.wav",
                "threshold": 0.7,
                "start": {"offset": 13.6, "early_offset": 1.0},
            }
        }
    }
    path = tmp_path / "sting_config.json"
    path.write_text(json.dumps(cfg))
    (tmp_path / "motif.wav").write_bytes(b"RIFF")
    configs = load_sting_configs(str(path))
    assert configs[1].start_rule is not None
    assert configs[1].start_rule.early_offset == 1.0
