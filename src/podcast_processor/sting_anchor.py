"""Deterministic sting anchoring for refined ad boundaries.

Some shows stamp every ad break with the same short production sting.
That waveform is bit-identical across episodes, so normalized
cross-correlation against a per-feed template finds the sting onsets in
the original audio with sub-second precision -- a much stronger signal
than the refiner's text-only guess when a boundary lands far from the
break it belongs to. This module encodes the policy that a backtest
over 54 refined boundaries from 16 ear-reviewed episodes validated
(zero harms on ear-verified-good cuts):

* Breaks are the refined blocks grouped at <20s gaps (the backtest's
  grouping); only a break's outer edges are ever moved -- cluster
  internal edges are left alone.
* START role: the candidate is the template peak nearest the break
  start inside a configured window; the target is onset + offset
  (offset is per-show: the sting plays under the transition, so the
  true ad start follows the onset by a show-specific delay). Moves
  under the role's minimum are suppressed (dead zone). An optional
  two-regime gate additionally vetoes mid-size moves: small moves are
  fine adjustments and large moves recover a clearly misplaced
  boundary, but the band between them is where an anchor cannot tell a
  misplaced boundary from a different transition shape.
* END role: the candidate is the peak nearest the break end inside a
  window that starts after the break's start (so a break's own start
  sting is never mistaken for its return sting). If the end already
  sits within the dead zone of the onset, the refiner is consistent
  with the sting and nothing moves. The target is onset + offset
  (negative: the cut lands just before the sting so the resume keeps
  it). Later moves (extending the cut) are the false-positive-risk
  direction and require a high detection score; earlier moves only
  need the feed's detection threshold.
* Positive-only: no usable detection, no change. A missing or degraded
  sting (voiceover over it drops correlation below threshold on some
  episodes) is a silent no-op, never an inference -- the INA snap
  guard remains the always-on fallback underneath.

Per-feed configuration (template path, threshold, role parameters)
comes from a JSON file -- see ``sting_config.example.json`` -- pointed
to by the ``PODLY_STING_CONFIG`` env var. Nothing is configured for a
feed, nothing happens for that feed.

Every adjustment is returned to the caller, which records it on the
stored boundary under the "sting_anchor" key (a dict, or a list of two
dicts when both edges of one block moved) and logs it, so a later
"why did this cut land here?" can be attributed exactly: classifier
block (orig_start/orig_end) -> refiner (sting_anchor.original_*) ->
anchor (refined_*).

Detection decodes the episode and the template with ffmpeg to mono
8kHz audio and computes the normalized cross-correlation with an FFT
(numpy). It runs once per episode; results are cached per audio file.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy

SAMPLE_RATE = 8000
PEAK_FLOOR = 0.5
PEAK_CAP = 60
BREAK_GAP_SECONDS = 20.0
DEFAULT_THRESHOLD = 0.7
CONFIG_ENV_VAR = "PODLY_STING_CONFIG"


@dataclass(frozen=True)
class StingPeak:
    """One template detection: onset (seconds) and correlation score."""

    time: float
    score: float


@dataclass(frozen=True)
class StartAnchorRule:
    """START-edge anchoring parameters (backtest defaults)."""

    offset: float
    window_before: float = 45.0
    window_after: float = 8.0
    min_move: float = 1.0
    regime_gate: tuple[float, float] | None = None
    require_positive_break_start: bool = False


@dataclass(frozen=True)
class EndAnchorRule:
    """END-edge anchoring parameters (backtest defaults)."""

    offset: float
    window_before: float = 25.0
    window_after: float = 20.0
    dead_zone: float = 3.0
    min_move: float = 1.0
    later_move_min_score: float = 0.9
    min_start_gap: float = 15.0


@dataclass(frozen=True)
class FeedStingConfig:
    """Per-feed sting configuration: one template, up to two roles."""

    template_path: str
    threshold: float = DEFAULT_THRESHOLD
    start_rule: StartAnchorRule | None = None
    end_rule: EndAnchorRule | None = None


@dataclass(frozen=True)
class StingAdjustment:
    """A single applied anchor move, for logging and the audit key."""

    block_index: int
    edge: str  # "start" or "end"
    score: float
    onset: float
    original: float
    adjusted: float


def _group_break_indices(boundaries: list[dict[str, Any]]) -> list[list[int]]:
    """Group boundary indices into breaks (blocks <20s apart merge)."""
    groups: list[list[int]] = []
    current_end: float | None = None
    for index, boundary in enumerate(boundaries):
        start = float(boundary["refined_start"])
        end = float(boundary["refined_end"])
        if (
            groups
            and current_end is not None
            and start - current_end < BREAK_GAP_SECONDS
        ):
            groups[-1].append(index)
            current_end = max(current_end, end)
        else:
            groups.append([index])
            current_end = end
    return groups


def _nearest_peak(
    peaks: list[StingPeak], lo: float, hi: float, target: float
) -> StingPeak | None:
    candidates = [p for p in peaks if lo <= p.time <= hi]
    if not candidates:
        return None
    return min(candidates, key=lambda p: abs(p.time - target))


def _propose_start_adjustment(
    rule: StartAnchorRule,
    peaks: list[StingPeak],
    block_index: int,
    break_start: float,
) -> StingAdjustment | None:
    if rule.require_positive_break_start and break_start <= 0.0:
        return None
    peak = _nearest_peak(
        peaks,
        break_start - rule.window_before,
        break_start + rule.window_after,
        break_start,
    )
    if peak is None:
        return None
    target = peak.time + rule.offset
    move = abs(target - break_start)
    in_regime_band = (
        rule.regime_gate is not None
        and rule.regime_gate[0] < move < rule.regime_gate[1]
    )
    if move < rule.min_move or in_regime_band:
        return None
    return StingAdjustment(
        block_index=block_index,
        edge="start",
        score=peak.score,
        onset=peak.time,
        original=break_start,
        adjusted=target,
    )


def _propose_end_adjustment(
    rule: EndAnchorRule,
    peaks: list[StingPeak],
    block_index: int,
    break_start: float,
    break_end: float,
) -> StingAdjustment | None:
    window_lo = max(
        break_start + rule.min_start_gap,
        break_end - rule.window_before,
    )
    peak = _nearest_peak(peaks, window_lo, break_end + rule.window_after, break_end)
    if peak is None or abs(break_end - peak.time) <= rule.dead_zone:
        return None
    target = peak.time + rule.offset
    later_move = target > break_end
    if abs(target - break_end) < rule.min_move or (
        later_move and peak.score < rule.later_move_min_score
    ):
        return None
    return StingAdjustment(
        block_index=block_index,
        edge="end",
        score=peak.score,
        onset=peak.time,
        original=break_end,
        adjusted=target,
    )


def _propose_for_break(
    config: FeedStingConfig,
    peaks: list[StingPeak],
    boundaries: list[dict[str, Any]],
    group: list[int],
) -> list[StingAdjustment]:
    first, last = group[0], group[-1]
    break_start = float(boundaries[first]["refined_start"])
    break_end = float(boundaries[last]["refined_end"])
    proposals: list[StingAdjustment] = []
    if config.start_rule is not None:
        adj = _propose_start_adjustment(config.start_rule, peaks, first, break_start)
        if adj is not None:
            proposals.append(adj)
    if config.end_rule is not None:
        adj = _propose_end_adjustment(
            config.end_rule, peaks, last, break_start, break_end
        )
        if adj is not None:
            proposals.append(adj)
    return proposals


def _record_adjustments(
    result: list[dict[str, Any]], adjustments: list[StingAdjustment]
) -> None:
    """Write adjustments into the boundary copies + the audit key."""
    for adj in adjustments:
        boundary = result[adj.block_index]
        if adj.edge == "start":
            boundary["refined_start"] = adj.adjusted
            audit: dict[str, Any] = {
                "role": "start",
                "score": adj.score,
                "onset": adj.onset,
                "original_start": adj.original,
            }
        else:
            boundary["refined_end"] = adj.adjusted
            audit = {
                "role": "end",
                "score": adj.score,
                "onset": adj.onset,
                "original_end": adj.original,
            }
        existing = boundary.get("sting_anchor")
        if existing is None:
            boundary["sting_anchor"] = audit
        elif isinstance(existing, list):
            existing.append(audit)
        else:
            boundary["sting_anchor"] = [existing, audit]


def apply_sting_anchors(
    boundaries: list[dict[str, Any]],
    config: FeedStingConfig | None,
    peaks: list[StingPeak],
) -> tuple[list[dict[str, Any]], list[StingAdjustment]]:
    """Apply the sting anchor policy to a post's refined boundaries.

    ``boundaries`` are the dicts about to be stored on the post (each
    has ``refined_start`` / ``refined_end``); ``peaks`` are the feed
    template's detections on the episode's original audio. Returns a
    new boundary list (inputs are not mutated) and the adjustments
    that were applied. All proposals are computed from the original
    boundary snapshot before any of them is applied, matching the
    backtest's evaluation.
    """
    result = [dict(b) for b in boundaries]
    adjustments: list[StingAdjustment] = []
    if not boundaries or config is None:
        return result, adjustments
    usable = [p for p in peaks if p.score >= config.threshold]
    if not usable:
        return result, adjustments

    for group in _group_break_indices(boundaries):
        adjustments.extend(_propose_for_break(config, usable, boundaries, group))
    _record_adjustments(result, adjustments)
    return result, adjustments


# ---------------------------------------------------------------------------
# Detection: ffmpeg decode + FFT normalized cross-correlation (numpy).
# ---------------------------------------------------------------------------


def decode_audio_mono_8k(path: str) -> numpy.ndarray:
    """Decode an audio file to mono 8kHz samples (float64, s16le scale)."""
    import numpy as np

    ffmpeg = os.environ.get("FFMPEG_BIN", "ffmpeg")
    proc = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "s16le",
            "-",
        ],
        capture_output=True,
        check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float64)


def normalized_xcorr(template: numpy.ndarray, signal: numpy.ndarray) -> numpy.ndarray:
    """Normalized cross-correlation of ``template`` over ``signal``.

    Mirrors the backtest sweep exactly: both series are mean-subtracted
    (the signal globally), the numerator is the valid-mode correlation
    and the denominator is each window's energy times the template's.
    Returns one score per template position (len(signal)-len(template)+1).
    """
    import numpy as np

    t = np.asarray(template, dtype=np.float64)
    s = np.asarray(signal, dtype=np.float64)
    if len(t) == 0 or len(s) < len(t):
        return np.zeros(0, dtype=np.float64)
    t = t - t.mean()
    s = s - s.mean()
    n, m = len(s), len(t)
    size = 1 << (n + m - 1).bit_length()
    spec_signal = np.fft.rfft(s, size)
    spec_template = np.fft.rfft(t, size)
    corr = np.fft.irfft(spec_signal * np.conj(spec_template), size)[: n - m + 1]
    del spec_signal, spec_template
    energy_prefix = np.concatenate([[0.0], np.cumsum(s * s)])
    window_energy = energy_prefix[m:] - energy_prefix[:-m]
    return corr / (np.sqrt(window_energy * np.sum(t * t)) + 1e-12)


def pick_peaks(
    scores: numpy.ndarray,
    template_length: int,
    floor: float = PEAK_FLOOR,
    cap: int = PEAK_CAP,
) -> list[StingPeak]:
    """Greedy non-maximum suppression over correlation scores.

    Peaks are taken strongest-first; a candidate within one template
    length of an already-picked peak is suppressed (same suppression
    radius as the backtest sweep).
    """
    import numpy as np

    order = np.argsort(scores)[::-1]
    picked: list[tuple[int, float]] = []
    for index in order:
        value = float(scores[index])
        if value < floor or len(picked) >= cap:
            break
        position = int(index)
        if all(abs(position - p) >= template_length for p, _ in picked):
            picked.append((position, value))
    picked.sort()
    return [StingPeak(time=pos / SAMPLE_RATE, score=value) for pos, value in picked]


_DETECTION_CACHE: dict[tuple[str, int, int, str, float], list[StingPeak]] = {}


def detect_sting_peaks(
    audio_path: str,
    template_path: str,
    floor: float = PEAK_FLOOR,
) -> list[StingPeak]:
    """Detect a sting template's onsets in an episode's audio.

    Cached per (audio file, template, floor): classification may run
    the refinement funnel more than once for a post, and detection is
    the expensive step. One episode is held in memory at a time.
    """
    stat = os.stat(audio_path)
    key = (audio_path, stat.st_mtime_ns, stat.st_size, template_path, floor)
    cached = _DETECTION_CACHE.get(key)
    if cached is not None:
        return cached
    template = decode_audio_mono_8k(template_path)
    signal = decode_audio_mono_8k(audio_path)
    if len(signal) < len(template):
        peaks: list[StingPeak] = []
    else:
        scores = normalized_xcorr(template, signal)
        peaks = pick_peaks(scores, len(template), floor)
    del signal
    if len(_DETECTION_CACHE) > 16:
        _DETECTION_CACHE.clear()
    _DETECTION_CACHE[key] = peaks
    return peaks


# ---------------------------------------------------------------------------
# Per-feed JSON config (PODLY_STING_CONFIG). No config-store keys: Podly
# settings are schema-versioned through the Rust writer, so a file keeps
# this feature self-contained (the snap guard's lesson).
# ---------------------------------------------------------------------------

_CONFIG_CACHE: dict[tuple[str, int], dict[int, FeedStingConfig]] = {}


def _parse_start_rule(raw: dict[str, Any]) -> StartAnchorRule:
    regime = raw.get("regime_gate")
    return StartAnchorRule(
        offset=float(raw["offset"]),
        window_before=float(raw.get("window_before", 45.0)),
        window_after=float(raw.get("window_after", 8.0)),
        min_move=float(raw.get("min_move", 1.0)),
        regime_gate=(float(regime[0]), float(regime[1])) if regime else None,
        require_positive_break_start=bool(
            raw.get("require_positive_break_start", False)
        ),
    )


def _parse_end_rule(raw: dict[str, Any]) -> EndAnchorRule:
    return EndAnchorRule(
        offset=float(raw["offset"]),
        window_before=float(raw.get("window_before", 25.0)),
        window_after=float(raw.get("window_after", 20.0)),
        dead_zone=float(raw.get("dead_zone", 3.0)),
        min_move=float(raw.get("min_move", 1.0)),
        later_move_min_score=float(raw.get("later_move_min_score", 0.9)),
        min_start_gap=float(raw.get("min_start_gap", 15.0)),
    )


def load_sting_configs(config_path: str | None = None) -> dict[int, FeedStingConfig]:
    """Load per-feed sting configs from a JSON file.

    ``config_path`` defaults to the ``PODLY_STING_CONFIG`` env var; an
    unset/empty path yields no configs (feature inert). Template paths
    are resolved relative to the config file's directory. Raises
    ValueError on a malformed file -- callers treat config like the
    guard itself: best-effort, never a gate.
    """
    path = config_path or os.environ.get(CONFIG_ENV_VAR)
    if not path:
        return {}
    stat = os.stat(path)
    key = (path, stat.st_mtime_ns)
    if key in _CONFIG_CACHE:
        return _CONFIG_CACHE[key]
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    feeds_raw = data.get("feeds")
    if not isinstance(feeds_raw, dict):
        raise ValueError(f"sting config {path}: missing 'feeds' object")
    base_dir = Path(path).resolve().parent
    feeds: dict[int, FeedStingConfig] = {}
    for feed_id_raw, entry in feeds_raw.items():
        template = Path(str(entry["template"]))
        if not template.is_absolute():
            template = base_dir / template
        feeds[int(feed_id_raw)] = FeedStingConfig(
            template_path=str(template),
            threshold=float(entry.get("threshold", DEFAULT_THRESHOLD)),
            start_rule=_parse_start_rule(entry["start"]) if "start" in entry else None,
            end_rule=_parse_end_rule(entry["end"]) if "end" in entry else None,
        )
    if len(_CONFIG_CACHE) > 4:
        _CONFIG_CACHE.clear()
    _CONFIG_CACHE[key] = feeds
    return feeds


def feed_sting_config(
    feed_id: int, config_path: str | None = None
) -> FeedStingConfig | None:
    """The sting config for one feed, or None when unconfigured."""
    return load_sting_configs(config_path).get(int(feed_id))
