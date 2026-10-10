from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from podcast_processor.word_boundary_refiner import WordBoundaryRefiner
from shared.test_utils import create_standard_test_config


def _build_response(
    *,
    content: str,
    finish_reason: str | None,
    prompt_tokens: int = 11,
    cached_prompt_tokens: int = 3,
    completion_tokens: int = 7,
    total_tokens: int = 18,
) -> MagicMock:
    choice = MagicMock()
    choice.message = MagicMock()
    choice.message.content = content
    choice.finish_reason = finish_reason

    response = MagicMock()
    response.choices = [choice]
    response.usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached_prompt_tokens),
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )
    return response


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [("length", "length"), ("stop", "format"), (None, "format")],
)
def test_parse_failure_reason_classification(
    finish_reason: str | None, expected: str
) -> None:
    assert WordBoundaryRefiner._parse_failure_reason(finish_reason) == expected


@pytest.mark.parametrize(
    ("finish_reason", "expected_error"),
    [("length", "parse_failed:length"), ("stop", "parse_failed:format")],
)
def test_refine_tags_parse_failures_with_finish_reason(
    finish_reason: str, expected_error: str, caplog: pytest.LogCaptureFixture
) -> None:
    refiner = WordBoundaryRefiner(config=create_standard_test_config())
    refiner._update_model_call = MagicMock()  # type: ignore[method-assign]

    response = _build_response(content="not valid json", finish_reason=finish_reason)
    all_segments = [
        {
            "sequence_num": 1,
            "start_time": 10.0,
            "end_time": 12.0,
            "text": "This episode is brought to you by",
        }
    ]

    with (
        patch(
            "podcast_processor.word_boundary_refiner.render_prompt_and_upsert_model_call",
            return_value=("prompt", 42),
        ),
        patch(
            "litellm.completion",
            return_value=response,
        ),
        caplog.at_level("DEBUG"),
    ):
        result = refiner.refine(
            ad_start=10.0,
            ad_end=12.0,
            confidence=0.9,
            all_segments=all_segments,
            post_id=99,
            first_seq_num=1,
            last_seq_num=1,
        )

    assert result.start_adjustment_reason == "heuristic_fallback"
    assert result.end_adjustment_reason == "unchanged"

    update_calls = cast(MagicMock, refiner._update_model_call).call_args_list
    assert update_calls[0].kwargs["status"] == "received_response"
    assert update_calls[0].kwargs["error_message"] is None
    assert update_calls[0].kwargs["usage"] == {
        "prompt_tokens": 11,
        "cached_prompt_tokens": 3,
        "completion_tokens": 7,
        "total_tokens": 18,
    }
    assert update_calls[1].kwargs["status"] == "success_heuristic"
    assert update_calls[1].kwargs["error_message"] == expected_error

    assert "Word boundary refine finish_reason=" in caplog.text
    assert f"no parseable JSON ({expected_error.split(':')[1]})" in caplog.text


def test_parse_json_recovers_truncated_fenced_payload() -> None:
    refiner = WordBoundaryRefiner(config=create_standard_test_config())

    truncated = """```json
{
  "refined_start_segment_seq": 2096,
  "refined_start_phrase": "if you're the purchasing",
  "refined_end_segment_seq": 2115,
  "refined_end_phrase": "thank you",
  "start_adjustment_reason": "start moved to sponsor lead in",
  "end_adjustment_reason": "end kept near return cue"
"""

    parsed = refiner._parse_json(truncated)

    assert parsed is not None
    assert parsed["refined_start_segment_seq"] == 2096
    assert parsed["refined_end_segment_seq"] == 2115
    assert parsed["refined_end_phrase"] == "thank you"


def test_parse_json_recovers_truncated_mid_key_prefix() -> None:
    refiner = WordBoundaryRefiner(config=create_standard_test_config())

    truncated = """{
  "refined_start_segment_seq": 288,
  "refined_start_phrase"""

    parsed = refiner._parse_json(truncated)

    assert parsed is not None
    assert parsed["refined_start_segment_seq"] == 288
    assert "refined_start_phrase" not in parsed


def test_context_by_seq_window_uses_contiguous_window_with_padding() -> None:
    refiner = WordBoundaryRefiner(config=create_standard_test_config())
    all_segments = [
        {
            "sequence_num": seq,
            "start_time": float(seq),
            "end_time": float(seq) + 1.0,
            "text": f"Segment {seq}",
        }
        for seq in range(100)
    ]

    selected = refiner._context_by_seq_window(
        all_segments,
        first_seq_num=20,
        last_seq_num=80,
    )
    selected_seqs = [int(seg["sequence_num"]) for seg in selected]

    assert selected_seqs == list(range(12, 89))
    assert 50 in selected_seqs


def test_refine_start_uses_segment_seq_without_phrase() -> None:
    refiner = WordBoundaryRefiner(config=create_standard_test_config())
    all_segments = [
        {
            "sequence_num": 288,
            "start_time": 100.0,
            "end_time": 101.0,
            "text": "Segment text",
        }
    ]

    refined_start, changed, _reason, err = refiner._refine_start(
        ad_start=110.0,
        all_segments=all_segments,
        context_segments=[],
        start_segment_seq=288,
        start_phrase=None,
        start_word=None,
        start_occurrence=None,
        start_word_index=None,
        start_reason="",
    )

    assert err is None
    assert changed is True
    assert refined_start == 100.0


def test_refine_end_uses_segment_seq_without_phrase() -> None:
    refiner = WordBoundaryRefiner(config=create_standard_test_config())
    all_segments = [
        {
            "sequence_num": 345,
            "start_time": 200.0,
            "end_time": 205.0,
            "text": "Segment text",
        }
    ]

    refined_end, changed, _reason, err = refiner._refine_end(
        ad_end=204.0,
        all_segments=all_segments,
        context_segments=[],
        end_segment_seq=345,
        end_phrase=None,
        end_reason="",
    )

    assert err is None
    assert changed is True
    assert refined_end == 205.0


def test_estimate_phrase_time_prefers_exact_word_timestamps() -> None:
    refiner = WordBoundaryRefiner(config=create_standard_test_config())

    estimated = refiner._estimate_phrase_time(
        all_segments=[
            {
                "sequence_num": 12,
                "start_time": 100.0,
                "end_time": 110.0,
                "text": "this is brought to you by a sponsor today",
                "words": [
                    {"word": "this", "start": 100.0, "end": 100.4},
                    {"word": "is", "start": 100.4, "end": 100.8},
                    {"word": "brought", "start": 100.8, "end": 101.3},
                    {"word": "to", "start": 101.3, "end": 101.5},
                    {"word": "you", "start": 101.5, "end": 101.9},
                    {"word": "by", "start": 101.9, "end": 102.1},
                    {"word": "a", "start": 102.1, "end": 102.2},
                    {"word": "sponsor", "start": 104.75, "end": 105.4},
                    {"word": "today", "start": 105.4, "end": 106.0},
                ],
            }
        ],
        context_segments=[],
        preferred_segment_seq=12,
        phrase="sponsor today",
        direction="start",
    )

    assert estimated == 104.75


def test_estimate_word_time_prefers_exact_word_timestamps() -> None:
    refiner = WordBoundaryRefiner(config=create_standard_test_config())

    estimated = refiner._estimate_word_time(
        all_segments=[
            {
                "sequence_num": 15,
                "start_time": 200.0,
                "end_time": 210.0,
                "text": "alpha beta gamma delta",
                "words": [
                    {"word": "alpha", "start": 200.0, "end": 200.5},
                    {"word": "beta", "start": 200.5, "end": 201.0},
                    {"word": "gamma", "start": 205.25, "end": 205.8},
                    {"word": "delta", "start": 205.8, "end": 206.4},
                ],
            }
        ],
        segment_seq=15,
        word="gamma",
        occurrence="first",
        word_index=None,
    )

    assert estimated == 205.25


def test_refine_reverts_invalid_start_only_partial_response() -> None:
    refiner = WordBoundaryRefiner(config=create_standard_test_config())
    refiner._update_model_call = MagicMock()  # type: ignore[method-assign]

    response = _build_response(
        content="""
{
  "refined_start_segment_seq": 1356,
  "refined_start_phrase": "we got to take",
  "refined_end_segment_seq": 0,
  "refined_end_phrase": null,
  "start_adjustment_reason": "Transition phrase marks the ad start.",
  "end_adjustment_reason": "Ad content continues past provided segments."
}
""",
        finish_reason="stop",
    )
    all_segments = [
        {
            "sequence_num": 1356,
            "start_time": 12.0,
            "end_time": 15.0,
            "text": "we got to take another quick pause for our sponsor",
        }
    ]

    with (
        patch(
            "podcast_processor.word_boundary_refiner.render_prompt_and_upsert_model_call",
            return_value=("prompt", 42),
        ),
        patch(
            "litellm.completion",
            return_value=response,
        ),
    ):
        result = refiner.refine(
            ad_start=10.0,
            ad_end=12.0,
            confidence=0.9,
            all_segments=all_segments,
            post_id=99,
            first_seq_num=1356,
            last_seq_num=1356,
        )

    assert result.refined_start == 10.0
    assert result.refined_end == 12.0
    assert result.start_adjustment_reason == "unchanged"
    assert (
        result.end_adjustment_reason == "Ad content continues past provided segments."
    )

    update_calls = cast(MagicMock, refiner._update_model_call).call_args_list
    assert update_calls[0].kwargs["status"] == "received_response"
    assert update_calls[1].kwargs["status"] == "success_heuristic"
    assert update_calls[1].kwargs["error_message"] == "start_out_of_window"
    assert update_calls[2].kwargs["status"] == "success"


def test_get_context_uses_rust_when_flag_enabled_and_post_guid_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the Rust path is enabled and a post_guid is threaded through, the
    rust sidecar wrapper is invoked and its sequence_nums are re-hydrated with
    the original in-memory `words` arrays so downstream phrase resolution still
    sees the same shape it would have built locally."""
    refiner = WordBoundaryRefiner(config=create_standard_test_config())
    all_segments = [
        {
            "sequence_num": 100,
            "start_time": 0.0,
            "end_time": 1.0,
            "text": "a",
            "words": [{"word": "a", "start": 0.0, "end": 1.0}],
        },
        {
            "sequence_num": 101,
            "start_time": 1.0,
            "end_time": 2.0,
            "text": "b",
            "words": [{"word": "b", "start": 1.0, "end": 2.0}],
        },
    ]

    monkeypatch.setenv("PODLY_RUST_WORD_BOUNDARY_ENABLED", "true")

    captured: dict[str, object] = {}

    def fake_wb_context(**kwargs: object) -> list[dict[str, object]]:
        captured.update(kwargs)
        return [{"sequence_num": 101, "start_time": 1.0, "end_time": 2.0, "text": "b"}]

    with patch("shared.rust_sidecar.try_wb_context", side_effect=fake_wb_context):
        selected = refiner._get_context(
            ad_start=1.0,
            ad_end=2.0,
            all_segments=all_segments,
            first_seq_num=101,
            last_seq_num=101,
            post_guid="post-abc",
        )

    assert captured["post_guid"] == "post-abc"
    assert captured["first_seq"] == 101
    assert captured["last_seq"] == 101
    assert len(selected) == 1
    assert selected[0] is all_segments[1]
    assert "words" in selected[0]


def test_get_context_skips_rust_when_flag_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refiner = WordBoundaryRefiner(config=create_standard_test_config())
    monkeypatch.setenv("PODLY_RUST_WORD_BOUNDARY_ENABLED", "false")

    all_segments = [
        {"sequence_num": 1, "start_time": 0.0, "end_time": 1.0, "text": "x"}
    ]

    def boom(**_kwargs: object) -> None:
        raise AssertionError("Rust wrapper invoked while flag disabled")

    with patch("shared.rust_sidecar.try_wb_context", side_effect=boom):
        selected = refiner._get_context(
            ad_start=0.0,
            ad_end=1.0,
            all_segments=all_segments,
            first_seq_num=1,
            last_seq_num=1,
            post_guid="post-abc",
        )

    assert selected == all_segments


def test_get_context_skips_rust_without_post_guid() -> None:
    refiner = WordBoundaryRefiner(config=create_standard_test_config())
    all_segments = [
        {"sequence_num": 1, "start_time": 0.0, "end_time": 1.0, "text": "x"}
    ]

    def boom(**_kwargs: object) -> None:
        raise AssertionError("Rust wrapper invoked without post_guid")

    with patch("shared.rust_sidecar.try_wb_context", side_effect=boom):
        selected = refiner._get_context(
            ad_start=0.0,
            ad_end=1.0,
            all_segments=all_segments,
            first_seq_num=1,
            last_seq_num=1,
            post_guid=None,
        )

    assert selected == all_segments


def test_get_context_falls_back_when_rust_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refiner = WordBoundaryRefiner(config=create_standard_test_config())
    monkeypatch.setenv("PODLY_RUST_WORD_BOUNDARY_ENABLED", "true")

    all_segments = [
        {"sequence_num": 5, "start_time": 50.0, "end_time": 51.0, "text": "x"}
    ]

    with patch("shared.rust_sidecar.try_wb_context", return_value=None):
        selected = refiner._get_context(
            ad_start=50.0,
            ad_end=51.0,
            all_segments=all_segments,
            first_seq_num=5,
            last_seq_num=5,
            post_guid="post-abc",
        )

    assert selected == all_segments


def _llm_response_with_phrases() -> MagicMock:
    return _build_response(
        content="""
{
  "refined_start_segment_seq": 100,
  "refined_start_phrase": "brought to you by",
  "refined_end_segment_seq": 101,
  "refined_end_phrase": "thanks for listening",
  "start_adjustment_reason": "sponsor lead-in",
  "end_adjustment_reason": "sign-off"
}
""",
        finish_reason="stop",
    )


def test_refine_uses_rust_refine_from_llm_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the flag is on and a post_guid is threaded through, refine()
    short-circuits the Python `_refine_start` / `_refine_end` pair, using
    whatever Rust returns. Python keeps applying its cross-window guards and
    reason defaulting."""
    refiner = WordBoundaryRefiner(config=create_standard_test_config())
    refiner._update_model_call = MagicMock()  # type: ignore[method-assign]
    refiner._refine_start = MagicMock(  # type: ignore[method-assign]
        side_effect=AssertionError("python _refine_start must not be called")
    )
    refiner._refine_end = MagicMock(  # type: ignore[method-assign]
        side_effect=AssertionError("python _refine_end must not be called")
    )

    monkeypatch.setenv("PODLY_RUST_WORD_BOUNDARY_ENABLED", "true")
    response = _llm_response_with_phrases()
    all_segments = [
        {
            "sequence_num": 100,
            "start_time": 95.0,
            "end_time": 100.0,
            "text": "brought to you by",
        },
        {
            "sequence_num": 101,
            "start_time": 100.0,
            "end_time": 110.0,
            "text": "thanks for listening",
        },
    ]
    rust_payload = {
        "parse_status": "ok",
        "refined_start": 96.5,
        "refined_end": 108.25,
        "start_changed": True,
        "end_changed": True,
        "start_error": None,
        "end_error": None,
        "start_reason": "sponsor lead-in",
        "end_reason": "sign-off",
    }

    with (
        patch(
            "podcast_processor.word_boundary_refiner.render_prompt_and_upsert_model_call",
            return_value=("prompt", 42),
        ),
        patch("litellm.completion", return_value=response),
        patch(
            "shared.rust_sidecar.try_wb_refine_from_llm",
            return_value=rust_payload,
        ),
        patch("shared.rust_sidecar.try_wb_context", return_value=None),
    ):
        result = refiner.refine(
            ad_start=100.0,
            ad_end=110.0,
            confidence=0.9,
            all_segments=all_segments,
            post_id=42,
            post_guid="post-abc",
            first_seq_num=100,
            last_seq_num=101,
        )

    assert result.refined_start == 96.5
    assert result.refined_end == 108.25
    assert result.start_adjustment_reason == "sponsor lead-in"
    assert result.end_adjustment_reason == "sign-off"


def test_refine_falls_back_when_rust_refine_from_llm_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the flag is on but Rust returns None (sidecar failed / bad payload),
    refine() must fall back to the Python `_parse_json` + `_refine_start` +
    `_refine_end` path instead of raising."""
    refiner = WordBoundaryRefiner(config=create_standard_test_config())
    refiner._update_model_call = MagicMock()  # type: ignore[method-assign]

    monkeypatch.setenv("PODLY_RUST_WORD_BOUNDARY_ENABLED", "true")
    response = _llm_response_with_phrases()
    all_segments = [
        {
            "sequence_num": 100,
            "start_time": 95.0,
            "end_time": 100.0,
            "text": "brought to you by a sponsor",
        },
        {
            "sequence_num": 101,
            "start_time": 100.0,
            "end_time": 110.0,
            "text": "thanks for listening folks",
        },
    ]

    with (
        patch(
            "podcast_processor.word_boundary_refiner.render_prompt_and_upsert_model_call",
            return_value=("prompt", 42),
        ),
        patch("litellm.completion", return_value=response),
        patch("shared.rust_sidecar.try_wb_refine_from_llm", return_value=None),
        patch("shared.rust_sidecar.try_wb_context", return_value=None),
    ):
        result = refiner.refine(
            ad_start=100.0,
            ad_end=110.0,
            confidence=0.9,
            all_segments=all_segments,
            post_id=42,
            post_guid="post-abc",
            first_seq_num=100,
            last_seq_num=101,
        )

    # The Python path resolves "brought to you by" inside segment 100 (heuristic
    # word-time interpolation) and "thanks for listening" inside 101. The exact
    # numbers don't matter for the assertion — what matters is that refine()
    # produced a valid window without raising.
    assert result.refined_end > result.refined_start
    assert result.start_adjustment_reason
    assert result.end_adjustment_reason


def test_refine_falls_back_when_rust_refine_from_llm_reports_parse_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`parse_status: failed` from Rust mirrors today's `parse_failed` heuristic
    path: refine() must short-circuit to `_fallback(ad_start, ad_end)` and
    mark the model_call as `success_heuristic` with the parse_failed code."""
    refiner = WordBoundaryRefiner(config=create_standard_test_config())
    refiner._update_model_call = MagicMock()  # type: ignore[method-assign]
    refiner._refine_start = MagicMock(  # type: ignore[method-assign]
        side_effect=AssertionError("python _refine_start must not be called")
    )
    refiner._refine_end = MagicMock(  # type: ignore[method-assign]
        side_effect=AssertionError("python _refine_end must not be called")
    )

    monkeypatch.setenv("PODLY_RUST_WORD_BOUNDARY_ENABLED", "true")
    response = _build_response(content="garbage", finish_reason="stop")
    all_segments = [
        {
            "sequence_num": 100,
            "start_time": 95.0,
            "end_time": 100.0,
            "text": "brought to you by",
        }
    ]
    rust_payload = {
        "parse_status": "failed",
        "refined_start": 100.0,
        "refined_end": 110.0,
        "start_changed": False,
        "end_changed": False,
        "start_error": None,
        "end_error": None,
        "start_reason": "",
        "end_reason": "",
    }

    with (
        patch(
            "podcast_processor.word_boundary_refiner.render_prompt_and_upsert_model_call",
            return_value=("prompt", 42),
        ),
        patch("litellm.completion", return_value=response),
        patch(
            "shared.rust_sidecar.try_wb_refine_from_llm",
            return_value=rust_payload,
        ),
        patch("shared.rust_sidecar.try_wb_context", return_value=None),
    ):
        result = refiner.refine(
            ad_start=100.0,
            ad_end=110.0,
            confidence=0.9,
            all_segments=all_segments,
            post_id=42,
            post_guid="post-abc",
            first_seq_num=100,
            last_seq_num=100,
        )

    assert result.refined_start == 100.0
    assert result.refined_end == 110.0
    assert result.start_adjustment_reason == "heuristic_fallback"

    update_calls = cast(MagicMock, refiner._update_model_call).call_args_list
    assert update_calls[-1].kwargs["status"] == "success_heuristic"
    assert update_calls[-1].kwargs["error_message"] == "parse_failed:format"


def test_refine_logs_warning_when_rust_reports_salvaged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When Rust falls back to the partial-fields salvage path, Python must
    surface a WARN so log-based observability doesn't regress when the
    bundled subcommand absorbs the parser."""
    refiner = WordBoundaryRefiner(config=create_standard_test_config())
    refiner._update_model_call = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setenv("PODLY_RUST_WORD_BOUNDARY_ENABLED", "true")

    response = _llm_response_with_phrases()
    all_segments = [
        {
            "sequence_num": 100,
            "start_time": 95.0,
            "end_time": 100.0,
            "text": "brought to you by",
        }
    ]
    rust_payload = {
        "parse_status": "salvaged",
        "refined_start": 96.0,
        "refined_end": 109.0,
        "start_changed": True,
        "end_changed": True,
        "start_error": None,
        "end_error": None,
        "start_reason": "x",
        "end_reason": "y",
    }

    with (
        patch(
            "podcast_processor.word_boundary_refiner.render_prompt_and_upsert_model_call",
            return_value=("prompt", 42),
        ),
        patch("litellm.completion", return_value=response),
        patch(
            "shared.rust_sidecar.try_wb_refine_from_llm",
            return_value=rust_payload,
        ),
        patch("shared.rust_sidecar.try_wb_context", return_value=None),
        caplog.at_level("WARNING"),
    ):
        refiner.refine(
            ad_start=100.0,
            ad_end=110.0,
            confidence=0.9,
            all_segments=all_segments,
            post_id=42,
            post_guid="post-abc",
            first_seq_num=100,
            last_seq_num=100,
        )

    assert "recovered partial fields via Rust salvage" in caplog.text.lower() or any(
        "salvage" in r.message.lower() for r in caplog.records
    )


def test_refine_skips_rust_refine_from_llm_without_post_guid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refiner = WordBoundaryRefiner(config=create_standard_test_config())
    refiner._update_model_call = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setenv("PODLY_RUST_WORD_BOUNDARY_ENABLED", "true")

    response = _llm_response_with_phrases()
    all_segments = [
        {
            "sequence_num": 100,
            "start_time": 95.0,
            "end_time": 100.0,
            "text": "brought to you by",
        },
        {
            "sequence_num": 101,
            "start_time": 100.0,
            "end_time": 110.0,
            "text": "thanks for listening",
        },
    ]

    def boom(**_kwargs: object) -> None:
        raise AssertionError("try_wb_refine_from_llm invoked without post_guid")

    with (
        patch(
            "podcast_processor.word_boundary_refiner.render_prompt_and_upsert_model_call",
            return_value=("prompt", 42),
        ),
        patch("litellm.completion", return_value=response),
        patch("shared.rust_sidecar.try_wb_refine_from_llm", side_effect=boom),
        patch("shared.rust_sidecar.try_wb_context", return_value=None),
    ):
        result = refiner.refine(
            ad_start=100.0,
            ad_end=110.0,
            confidence=0.9,
            all_segments=all_segments,
            post_id=42,
            post_guid=None,
            first_seq_num=100,
            last_seq_num=101,
        )

    assert result.refined_end > result.refined_start
