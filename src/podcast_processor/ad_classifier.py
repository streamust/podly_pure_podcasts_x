from __future__ import annotations

import logging
import math
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from jinja2 import Template
from pydantic import ValidationError

if TYPE_CHECKING:
    from litellm.exceptions import InternalServerError
from sqlalchemy import and_

from app.extensions import db
from app.memory_pressure import collect_incremental
from app.models import AudioSegment, Identification, ModelCall, Post, TranscriptSegment
from app.writer.batching import (
    batch_count_for,
    get_writer_batch_size,
    iter_writer_batches,
)
from app.writer.client import writer_client
from podcast_processor.boundary_refiner import BoundaryRefiner
from podcast_processor.cue_detector import CueDetector
from podcast_processor.llm_concurrency_limiter import (
    ConcurrencyContext,
    LLMConcurrencyLimiter,
    get_concurrency_limiter,
)
from podcast_processor.llm_model_call_utils import (
    LLMRequestTooLargeError,
    apply_service_tier,
    call_litellm_with_tier_retry,
    extract_litellm_usage,
)
from podcast_processor.model_output import (
    AdSegmentPredictionList,
    clean_and_parse_model_output,
)
from podcast_processor.prompt import (
    build_prompt_audio_markers,
    build_speaker_context_for_prompt,
    transcript_excerpt_for_prompt,
)
from podcast_processor.repeat_ad_finder import (
    RepeatAdCandidate,
    find_repeat_candidates_with_fallback,
    repeat_ad_detection_enabled,
    tokenize,
)
from podcast_processor.repeat_ad_refiner import RepeatAdRefiner
from podcast_processor.token_rate_limiter import (
    TokenRateLimiter,
    configure_rate_limiter_for_model,
)
from podcast_processor.transcribe import Segment, load_word_timestamps_by_sequence
from podcast_processor.word_boundary_refiner import WordBoundaryRefiner
from shared.config import Config, TestWhisperConfig
from shared.llm_utils import (
    model_uses_max_completion_tokens,
    normalize_completion_args_for_model,
)


class ClassifyParams:
    def __init__(
        self,
        system_prompt: str,
        user_prompt_template: Template,
        post: Post,
        num_segments_per_prompt: int,
        max_overlap_segments: int,
    ):
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template
        self.post = post
        self.num_segments_per_prompt = num_segments_per_prompt
        self.max_overlap_segments = max_overlap_segments


class ClassifyException(Exception):
    """Custom exception for classification errors."""


class AdClassifier:
    """Handles the classification of ad segments in podcast transcripts."""

    def __init__(
        self,
        config: Config,
        logger: logging.Logger | None = None,
        model_call_query: Any | None = None,
        identification_query: Any | None = None,
        db_session: Any | None = None,
    ):
        self.config = config
        self.logger = logger or logging.getLogger("global_logger")
        self.model_call_query = model_call_query or ModelCall.query
        self.identification_query = identification_query or Identification.query
        self.db_session = db_session or db.session

        # Initialize rate limiter for the configured model
        self.rate_limiter: TokenRateLimiter | None
        if self.config.llm_enable_token_rate_limiting:
            tokens_per_minute = self.config.llm_max_input_tokens_per_minute
            if tokens_per_minute is None:
                # Use model-specific defaults
                self.rate_limiter = configure_rate_limiter_for_model(
                    self.config.llm_model
                )
            else:
                # Use custom limit
                from podcast_processor.token_rate_limiter import get_rate_limiter

                self.rate_limiter = get_rate_limiter(tokens_per_minute)
                self.logger.info(
                    f"Using custom token rate limit: {tokens_per_minute}/min"
                )
        else:
            self.rate_limiter = None
            self.logger.info("Token rate limiting disabled")

        # Initialize concurrency limiter for LLM API calls
        self.concurrency_limiter: LLMConcurrencyLimiter | None
        max_concurrent = getattr(self.config, "llm_max_concurrent_calls", 3)
        if max_concurrent > 0:
            self.concurrency_limiter = get_concurrency_limiter(max_concurrent)
            self.logger.info(
                f"LLM concurrency limiting enabled: max {max_concurrent} concurrent calls"
            )
        else:
            self.concurrency_limiter = None
            self.logger.info("LLM concurrency limiting disabled")

        # Set to True if any LLM response fails to parse during a single
        # ``classify()`` run. Reset on each new call. The orchestrator reads
        # this after classification finishes to decide whether to auto-retry
        # zero-ad runs (see PodcastProcessor zero-ads guard).
        self.had_parse_error = False
        self._adaptive_input_token_limit: int | None = None

        # Initialize cue detector for neighbor expansion
        self.cue_detector = CueDetector()

        # Initialize boundary refiner (conditionally based on config).
        # Either refiner type may be used; both expose a compatible `.refine()`.
        self.boundary_refiner: BoundaryRefiner | WordBoundaryRefiner | None = None
        if config.enable_boundary_refinement:
            if getattr(config, "enable_word_level_boundary_refinder", False):
                self.boundary_refiner = WordBoundaryRefiner(config, self.logger)
                self.logger.info("Word-level boundary refiner enabled")
            else:
                # Share the classifier's token bucket with the refiner: both
                # draw on the same provider per-minute token budget, so the
                # refiner must pace itself instead of firing unpaced right
                # after classification filled the window (was causing 429s).
                self.boundary_refiner = BoundaryRefiner(
                    config, self.logger, token_limiter=self.rate_limiter
                )
                self.logger.info("Boundary refinement enabled")
        else:
            self.logger.info("Boundary refinement disabled via config")

        # Repeat-ad detection (opt-in via ENABLE_REPEAT_AD_DETECTION). Catches
        # dynamically-inserted ads the classifier missed by matching detected
        # ads against the rest of the transcript and confirming with the LLM.
        self.repeat_ad_refiner: RepeatAdRefiner | None = None
        if repeat_ad_detection_enabled():
            self.repeat_ad_refiner = RepeatAdRefiner(config, self.logger)
            self.logger.info("Repeat-ad detection enabled")

    def classify(
        self,
        *,
        transcript_segments: list[TranscriptSegment],
        system_prompt: str,
        user_prompt_template: Template,
        post: Post,
    ) -> None:
        """
        Classifies transcript segments to identify ad segments.

        Args:
            transcript_segments: List of transcript segments to classify
            system_prompt: System prompt for the LLM
            user_prompt_template: User prompt template for the LLM
            post: Post containing the podcast to classify
        """
        # Reset the parse-error signal at the top of every classification run
        # so a flag from a prior post can't leak into this one's outcome.
        self.had_parse_error = False

        self.logger.info(
            f"Starting ad classification for post {post.id} with {len(transcript_segments)} segments."
        )

        if not transcript_segments:
            self.logger.info(
                f"No transcript segments to classify for post {post.id}. Skipping."
            )
            return

        classify_params = ClassifyParams(
            system_prompt=system_prompt,
            user_prompt_template=user_prompt_template,
            post=post,
            num_segments_per_prompt=self.config.processing.num_segments_to_input_to_prompt,
            max_overlap_segments=self.config.processing.max_overlap_segments,
        )

        total_segments = len(transcript_segments)

        try:
            current_index = 0
            next_overlap_segments: list[TranscriptSegment] = []
            max_iterations = (
                total_segments + 10
            )  # Safety limit to prevent infinite loops
            iteration_count = 0
            while current_index < total_segments and iteration_count < max_iterations:
                consumed_segments, next_overlap_segments = self._step(
                    classify_params,
                    next_overlap_segments,
                    current_index,
                    transcript_segments,
                )
                current_index += consumed_segments
                iteration_count += 1
                if consumed_segments == 0:
                    self.logger.error(
                        f"No progress made in iteration {iteration_count} for post {post.id}. "
                        "Breaking to avoid infinite loop."
                    )
                    break

            # Expand neighbors using bulk operations
            # NOTE: Use self.db_session.query() instead of self.identification_query
            # to ensure all operations use the same session consistently.
            ad_identifications = (
                self.db_session.query(Identification)
                .join(TranscriptSegment)
                .filter(
                    TranscriptSegment.post_id == post.id,
                    Identification.label == "ad",
                )
                .all()
            )

            if ad_identifications:
                # Get model_call from first identification
                model_call = (
                    ad_identifications[0].model_call if ad_identifications else None
                )
                if model_call:
                    created = self.expand_neighbors_bulk(
                        ad_identifications=ad_identifications,
                        model_call=model_call,
                        post_id=post.id,
                        window=5,
                    )
                    self.logger.info(
                        f"Created {created} neighbor identifications via bulk ops"
                    )

            # Pass 1.5: Detect repeated (dynamically-inserted) ads the
            # classifier missed. Runs before boundary refinement so the new ad
            # blocks get refined and persisted alongside the original ones.
            if self.repeat_ad_refiner is not None:
                self._detect_repeat_ads(transcript_segments, post)

            # Pass 2: Refine boundaries
            if self.boundary_refiner:
                self._refine_boundaries(transcript_segments, post)

        except ClassifyException as e:
            self.logger.error(f"Classification failed for post {post.id}: {e}")
            return

    def _step(
        self,
        classify_params: ClassifyParams,
        prev_overlap_segments: list[TranscriptSegment],
        current_index: int,
        transcript_segments: list[TranscriptSegment],
    ) -> tuple[int, list[TranscriptSegment]]:
        overlap_segments = self._apply_overlap_cap(prev_overlap_segments)
        remaining_segments = transcript_segments[current_index:]

        max_new_segments = classify_params.num_segments_per_prompt

        while True:
            (
                chunk_segments,
                user_prompt_str,
                consumed_segments,
                token_limit_trimmed,
            ) = self._build_chunk_payload(
                overlap_segments=overlap_segments,
                remaining_segments=remaining_segments,
                total_segments=transcript_segments,
                post=classify_params.post,
                system_prompt=classify_params.system_prompt,
                user_prompt_template=classify_params.user_prompt_template,
                max_new_segments=max_new_segments,
            )

            if not chunk_segments or consumed_segments <= 0:
                self.logger.error(
                    "No progress made while building classification chunk for post %s. "
                    "Stopping to avoid infinite loop.",
                    classify_params.post.id,
                )
                raise ClassifyException(
                    "No progress made while building classification chunk."
                )

            if token_limit_trimmed:
                self.logger.debug(
                    "Token limit trimming applied for post %s at transcript index %s. "
                    "Processing chunk with %s new segments across %s total segments.",
                    classify_params.post.id,
                    current_index,
                    consumed_segments,
                    len(chunk_segments),
                )

            try:
                identified_segments = self._process_chunk(
                    chunk_segments=chunk_segments,
                    system_prompt=classify_params.system_prompt,
                    user_prompt_str=user_prompt_str,
                    post=classify_params.post,
                )
                break
            except LLMRequestTooLargeError as exc:
                if consumed_segments <= 1:
                    raise
                learned_limit = max(1, int(exc.limit * 0.9))
                if self._adaptive_input_token_limit is None:
                    self._adaptive_input_token_limit = learned_limit
                else:
                    self._adaptive_input_token_limit = min(
                        self._adaptive_input_token_limit,
                        learned_limit,
                    )
                max_new_segments = max(
                    1,
                    min(
                        consumed_segments - 1,
                        int(consumed_segments * learned_limit / exc.requested),
                    ),
                )
                self.logger.warning(
                    "Provider rejected %s input segments for post %s (%s tokens "
                    "requested, TPM limit %s); rebuilding the chunk with at "
                    "most %s new segments and a %s-token input ceiling",
                    consumed_segments,
                    classify_params.post.id,
                    exc.requested,
                    exc.limit,
                    max_new_segments,
                    self._adaptive_input_token_limit,
                )

        next_overlap_segments = self._compute_next_overlap_segments(
            chunk_segments=chunk_segments,
            identified_segments=identified_segments,
            max_overlap_segments=classify_params.max_overlap_segments,
        )

        if next_overlap_segments:
            self.logger.debug(
                "Carrying forward %s overlap segments for post %s: %s",
                len(next_overlap_segments),
                classify_params.post.id,
                [seg.sequence_num for seg in next_overlap_segments],
            )

        return consumed_segments, next_overlap_segments

    def _process_chunk(
        self,
        *,
        chunk_segments: list[TranscriptSegment],
        system_prompt: str,
        post: Post,
        user_prompt_str: str,
    ) -> list[TranscriptSegment]:
        """Process a chunk of transcript segments for classification."""
        if not chunk_segments:
            return []

        first_seq_num = chunk_segments[0].sequence_num
        last_seq_num = chunk_segments[-1].sequence_num

        self.logger.info(
            f"Processing classification for post {post.id}, segments {first_seq_num}-{last_seq_num}."
        )

        model_call = self._get_or_create_model_call(
            post=post,
            first_seq_num=first_seq_num,
            last_seq_num=last_seq_num,
            user_prompt_str=user_prompt_str,
        )

        if not model_call:
            self.logger.error("ModelCall object is unexpectedly None. Skipping chunk.")
            return []

        if self._should_call_llm(model_call):
            self._perform_llm_call(
                model_call=model_call,
                system_prompt=system_prompt,
            )
        else:
            oversized_error = LLMRequestTooLargeError.from_message(
                model_call.error_message
            )
            if oversized_error is not None:
                raise oversized_error

        if model_call.status == "success" and model_call.response:
            return self._process_successful_response(
                model_call=model_call,
                current_chunk_db_segments=chunk_segments,
            )
        if model_call.status != "success":
            self.logger.info(
                f"LLM call for ModelCall {model_call.id} was not successful (status: {model_call.status}). No identifications to process."
            )
        return []

    def _build_chunk_payload(
        self,
        *,
        overlap_segments: list[TranscriptSegment],
        remaining_segments: list[TranscriptSegment],
        total_segments: list[TranscriptSegment],
        post: Post,
        system_prompt: str,
        user_prompt_template: Template,
        max_new_segments: int,
    ) -> tuple[list[TranscriptSegment], str, int, bool]:
        """Construct chunk data while enforcing overlap and token constraints."""
        if not remaining_segments:
            return ([], "", 0, False)

        capped_overlap = self._apply_overlap_cap(overlap_segments)
        new_segment_count = min(max_new_segments, len(remaining_segments))
        token_limit_trimmed = False

        while new_segment_count > 0:
            base_segments = remaining_segments[:new_segment_count]
            chunk_segments = self._combine_overlap_segments(
                overlap_segments=capped_overlap,
                base_segments=base_segments,
            )

            if not chunk_segments:
                return ([], "", 0, token_limit_trimmed)

            includes_start = (
                chunk_segments[0].id == total_segments[0].id
                if total_segments
                else False
            )
            includes_end = (
                chunk_segments[-1].id == total_segments[-1].id
                if total_segments
                else False
            )

            user_prompt_str = self._generate_user_prompt(
                current_chunk_db_segments=chunk_segments,
                all_transcript_segments=total_segments,
                post=post,
                user_prompt_template=user_prompt_template,
                includes_start=includes_start,
                includes_end=includes_end,
            )

            if (
                self.config.llm_max_input_tokens_per_call is not None
                or self._adaptive_input_token_limit is not None
            ) and not self._validate_token_limit(user_prompt_str, system_prompt):
                token_limit_trimmed = True
                if new_segment_count == 1:
                    self.logger.warning(
                        "Even single segment at transcript index %s exceeds token limit "
                        "for post %s. Proceeding with minimal chunk.",
                        base_segments[0].sequence_num,
                        post.id,
                    )
                    return (chunk_segments, user_prompt_str, new_segment_count, True)
                new_segment_count -= 1
                continue

            return (
                chunk_segments,
                user_prompt_str,
                new_segment_count,
                token_limit_trimmed,
            )

        return ([], "", 0, token_limit_trimmed)

    def _combine_overlap_segments(
        self,
        *,
        overlap_segments: list[TranscriptSegment],
        base_segments: list[TranscriptSegment],
    ) -> list[TranscriptSegment]:
        """Combine overlap and new segments while preserving order and removing duplicates."""
        combined: list[TranscriptSegment] = []
        seen_ids: set[int] = set()

        for segment in overlap_segments:
            if segment.id not in seen_ids:
                combined.append(segment)
                seen_ids.add(segment.id)

        for segment in base_segments:
            if segment.id not in seen_ids:
                combined.append(segment)
                seen_ids.add(segment.id)

        self.logger.debug(
            "Combined overlap (%s segments) and base (%s segments) into %s total segments. "
            "Overlap seq nums: %s, Base seq nums: %s",
            len(overlap_segments),
            len(base_segments),
            len(combined),
            [seg.sequence_num for seg in overlap_segments],
            [seg.sequence_num for seg in base_segments],
        )

        return combined

    def _compute_next_overlap_segments(
        self,
        *,
        chunk_segments: list[TranscriptSegment],
        identified_segments: list[TranscriptSegment],
        max_overlap_segments: int,
    ) -> list[TranscriptSegment]:
        """Determine which segments should be carried forward to the next chunk."""
        if max_overlap_segments <= 0 or not chunk_segments:
            return []

        # Baseline: carry ~50% of the chunk to guarantee overlap even without detections
        base_tail_count = max(1, math.ceil(len(chunk_segments) / 2))
        overlap_candidates = list(chunk_segments[-base_tail_count:])

        if identified_segments:
            # Preserve from earliest detected ad through the end of the chunk
            identified_ids = {seg.id for seg in identified_segments}
            earliest_index = None
            for i, seg in enumerate(chunk_segments):
                if seg.id in identified_ids:
                    earliest_index = i
                    break

            if earliest_index is not None:
                ad_tail = chunk_segments[earliest_index:]
                overlap_candidates = self._combine_overlap_segments(
                    overlap_segments=ad_tail,
                    base_segments=overlap_candidates,
                )

            # Conditional tail replay: always include the final ~15 seconds when ads are present
            tail_replay_segments = self._segments_covering_tail(
                chunk_segments=chunk_segments, seconds=15.0
            )
            overlap_candidates = self._combine_overlap_segments(
                overlap_segments=tail_replay_segments,
                base_segments=overlap_candidates,
            )

        capped = self._apply_overlap_cap(
            overlap_candidates, max_override=max_overlap_segments
        )
        self.logger.debug(
            "Carrying forward %s overlap segments: seq_nums %s (identified=%s)",
            len(capped),
            [seg.sequence_num for seg in capped],
            bool(identified_segments),
        )
        return capped

    def _apply_overlap_cap(
        self,
        overlap_segments: list[TranscriptSegment],
        max_override: int | None = None,
    ) -> list[TranscriptSegment]:
        """Ensure stored overlap obeys configured limits."""
        max_overlap = (
            self.config.processing.max_overlap_segments
            if max_override is None
            else max_override
        )
        if max_overlap <= 0 or not overlap_segments:
            if max_overlap <= 0 and overlap_segments:
                self.logger.debug(
                    "Discarding %s overlap segments because max_overlap_segments is %s.",
                    len(overlap_segments),
                    max_overlap,
                )
            return [] if max_overlap <= 0 else list(overlap_segments)

        if len(overlap_segments) <= max_overlap:
            self.logger.debug(
                "Overlap cap check: %s segments within limit of %s, no trimming needed",
                len(overlap_segments),
                max_overlap,
            )
            return list(overlap_segments)

        trimmed = overlap_segments[-max_overlap:]
        self.logger.debug(
            "Overlap cap enforcement: trimming from %s to %s segments (max=%s). "
            "Keeping seq_nums: %s",
            len(overlap_segments),
            len(trimmed),
            max_overlap,
            [seg.sequence_num for seg in trimmed],
        )
        return trimmed

    def _segments_covering_tail(
        self, *, chunk_segments: list[TranscriptSegment], seconds: float
    ) -> list[TranscriptSegment]:
        """Return the minimal set of segments covering the last `seconds` of audio."""
        if not chunk_segments:
            return []

        last_end_time = (
            chunk_segments[-1].end_time
            if chunk_segments[-1].end_time is not None
            else chunk_segments[-1].start_time
        )
        cutoff = last_end_time - seconds

        tail_segments: list[TranscriptSegment] = []
        for seg in reversed(chunk_segments):
            tail_segments.append(seg)
            if seg.start_time <= cutoff:
                break

        return list(reversed(tail_segments))

    def _validate_token_limit(self, user_prompt_str: str, system_prompt: str) -> bool:
        """Validate that the prompt doesn't exceed the configured token limit."""
        configured_limit = self.config.llm_max_input_tokens_per_call
        limits = [
            limit
            for limit in (configured_limit, self._adaptive_input_token_limit)
            if limit is not None
        ]
        if not limits:
            return True
        effective_limit = min(limits)

        # Create messages as they would be sent to the API
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt_str},
        ]

        # Count tokens (reuse the existing token counting logic from rate limiter)
        if self.rate_limiter:
            token_count = self.rate_limiter.count_tokens(
                messages, self.config.llm_model
            )
        else:
            # Fallback token estimation if no rate limiter
            total_chars = len(system_prompt) + len(user_prompt_str)
            token_count = total_chars // 4  # ~4 characters per token

        is_valid = token_count <= effective_limit

        if not is_valid:
            self.logger.debug(
                f"Prompt exceeds token limit: {token_count} > {effective_limit}"
            )
        else:
            self.logger.debug(
                f"Prompt within token limit: {token_count} <= {effective_limit}"
            )

        return is_valid

    def _prepare_api_call(
        self, model_call_obj: ModelCall, system_prompt: str
    ) -> dict[str, Any] | None:
        """Prepare API call arguments and validate token limits."""
        # Prepare messages for the API call
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": model_call_obj.prompt},
        ]

        # Use rate limiter to wait if necessary and track token usage
        if self.rate_limiter:
            self.rate_limiter.wait_if_needed(messages, model_call_obj.model_name)

            # Get usage stats for logging
            usage_stats = self.rate_limiter.get_usage_stats()
            self.logger.info(
                f"Token usage: {usage_stats['current_usage']}/{usage_stats['limit']} "
                f"({usage_stats['usage_percentage']:.1f}%) for ModelCall {model_call_obj.id}"
            )

        # Final validation: Check per-call token limit before making API call
        if (
            self.config.llm_max_input_tokens_per_call is not None
            or self._adaptive_input_token_limit is not None
        ):
            if not self._validate_token_limit(model_call_obj.prompt, system_prompt):
                limits = [
                    limit
                    for limit in (
                        self.config.llm_max_input_tokens_per_call,
                        self._adaptive_input_token_limit,
                    )
                    if limit is not None
                ]
                effective_limit = min(limits)
                error_msg = (
                    f"Prompt for ModelCall {model_call_obj.id} exceeds configured "
                    f"token limit of {effective_limit}. "
                    f"Consider reducing num_segments_to_input_to_prompt."
                )
                self.logger.error(error_msg)
                if model_call_obj.id is not None:
                    res = writer_client.update(
                        "ModelCall",
                        model_call_obj.id,
                        {"status": "failed", "error_message": error_msg},
                        wait=True,
                    )
                    if not res or not res.success:
                        raise RuntimeError(
                            getattr(res, "error", "Failed to update ModelCall")
                        )
                    # Update local object to reflect database state
                    model_call_obj.status = "failed"
                    model_call_obj.error_message = error_msg
                return None

        # Prepare completion arguments
        completion_args = {
            "model": model_call_obj.model_name,
            "messages": messages,
            "timeout": self.config.openai_timeout,
        }

        # Use max_completion_tokens for newer OpenAI models (o1, gpt-5, gpt-4o variants)
        # OpenAI deprecated max_tokens for these models in favor of max_completion_tokens
        # Check if this is a model that requires max_completion_tokens
        # This includes: gpt-5, gpt-4o variants, o1 series, and latest chatgpt models
        uses_max_completion_tokens = model_uses_max_completion_tokens(
            model_call_obj.model_name
        )

        # Debug logging to help diagnose model parameter issues
        self.logger.info(
            f"Model: '{model_call_obj.model_name}', using max_completion_tokens: {uses_max_completion_tokens}"
        )

        if uses_max_completion_tokens:
            completion_args["max_completion_tokens"] = self.config.openai_max_tokens
        else:
            # For older models and non-OpenAI models, use max_tokens
            completion_args["max_tokens"] = self.config.openai_max_tokens

        completion_args["response_format"] = {"type": "json_object"}

        normalize_completion_args_for_model(completion_args)
        apply_service_tier(completion_args, self.config)
        return completion_args

    def _generate_user_prompt(
        self,
        *,
        current_chunk_db_segments: list[TranscriptSegment],
        all_transcript_segments: list[TranscriptSegment],
        post: Post,
        user_prompt_template: Template,
        includes_start: bool,
        includes_end: bool,
    ) -> str:
        """Generate the user prompt string for the LLM."""
        temp_pydantic_segments_for_prompt = [
            Segment(
                start=db_seg.start_time,
                end=db_seg.end_time,
                text=db_seg.text,
                speaker_label=getattr(db_seg, "speaker_label", None),
            )
            for db_seg in current_chunk_db_segments
        ]
        all_prompt_segments = [
            Segment(
                start=db_seg.start_time,
                end=db_seg.end_time,
                text=db_seg.text,
                speaker_label=getattr(db_seg, "speaker_label", None),
            )
            for db_seg in all_transcript_segments
        ]
        audio_markers = self._load_chunk_audio_markers(
            post=post,
            current_chunk_db_segments=current_chunk_db_segments,
        )

        return user_prompt_template.render(
            podcast_title=post.title,
            podcast_topic=post.description if post.description else "",
            speaker_context=build_speaker_context_for_prompt(all_prompt_segments),
            transcript=transcript_excerpt_for_prompt(
                segments=temp_pydantic_segments_for_prompt,
                includes_start=includes_start,
                includes_end=includes_end,
                audio_markers=audio_markers,
            ),
        )

    def _load_chunk_audio_markers(
        self,
        *,
        post: Post,
        current_chunk_db_segments: list[TranscriptSegment],
    ) -> list[Any]:
        if not current_chunk_db_segments or getattr(post, "id", None) is None:
            return []

        chunk_start = float(current_chunk_db_segments[0].start_time)
        chunk_end = float(current_chunk_db_segments[-1].end_time)

        try:
            audio_segments = (
                self.db_session.query(AudioSegment)
                .filter(
                    AudioSegment.post_id == post.id,
                    AudioSegment.end_time >= chunk_start,
                    AudioSegment.start_time <= chunk_end,
                )
                .order_by(AudioSegment.start_time.asc())
                .all()
            )
        except Exception:  # noqa: BLE001
            self.logger.warning(
                "Failed to load INA audio markers for prompt on post %s",
                post.id,
                exc_info=True,
            )
            return []

        if not isinstance(audio_segments, list):
            return []

        clipped_segments: list[Any] = []
        for audio_segment in audio_segments:
            start_time = getattr(audio_segment, "start_time", None)
            end_time = getattr(audio_segment, "end_time", None)
            if start_time is None or end_time is None:
                continue

            try:
                clipped_start = max(float(start_time), chunk_start)
                clipped_end = min(float(end_time), chunk_end)
            except TypeError, ValueError:
                continue

            if clipped_end <= clipped_start:
                continue

            clipped_segments.append(
                SimpleNamespace(
                    start_time=clipped_start,
                    end_time=clipped_end,
                    label=getattr(audio_segment, "label", None),
                )
            )

        return build_prompt_audio_markers(clipped_segments)

    def _get_or_create_model_call(
        self,
        *,
        post: Post,
        first_seq_num: int,
        last_seq_num: int,
        user_prompt_str: str,
    ) -> ModelCall | None:
        """Get an existing ModelCall or create a new one via writer."""
        model = self.config.llm_model
        result = writer_client.action(
            "upsert_model_call",
            {
                "post_id": post.id,
                "model_name": model,
                "first_segment_sequence_num": first_seq_num,
                "last_segment_sequence_num": last_seq_num,
                "prompt": user_prompt_str,
            },
            wait=True,
        )
        if not result or not result.success:
            raise RuntimeError(getattr(result, "error", "Failed to upsert ModelCall"))

        model_call_id = (result.data or {}).get("model_call_id")
        if model_call_id is None:
            raise RuntimeError("Writer did not return model_call_id")

        model_call = self.db_session.get(ModelCall, int(model_call_id))
        if model_call is None:
            raise RuntimeError(f"ModelCall {model_call_id} not found after upsert")
        return model_call

    def _should_call_llm(self, model_call: ModelCall) -> bool:
        """Determine if an LLM call should be made."""
        return model_call.status not in ("success", "failed_permanent")

    def _perform_llm_call(self, *, model_call: ModelCall, system_prompt: str) -> None:
        """Perform the LLM call for classification.

        Re-raises on failure. `_call_model` already handles transient errors
        with its own retry loop; anything that escapes it (non-retryable
        exception, retries exhausted) is a genuine failure that must fail the
        whole job. Swallowing it here would produce a zero-ad run marked
        successful, leaving ads in the output.
        """
        self.logger.info(
            f"Calling LLM for ModelCall {model_call.id} (post {model_call.post_id}, segments {model_call.first_segment_sequence_num}-{model_call.last_segment_sequence_num})."
        )
        try:
            if isinstance(self.config.whisper, TestWhisperConfig):
                self._handle_test_mode_call(model_call)
            else:
                self._call_model(model_call_obj=model_call, system_prompt=system_prompt)
        except Exception as e:
            # `_call_model` already logged the exception with traceback before
            # re-raising, so don't dump it a second time -- just leave a short
            # breadcrumb so the failure is visible at this layer too.
            self.logger.error(
                "LLM call failed for ModelCall %s; failing classification: %s",
                model_call.id,
                e,
            )
            raise

    def _handle_test_mode_call(self, model_call: ModelCall) -> None:
        """Handle LLM call in test mode."""
        self.logger.info("Test mode: Simulating successful LLM call for classify.")
        test_response = AdSegmentPredictionList(ad_segments=[]).model_dump_json()
        res = writer_client.update(
            "ModelCall",
            model_call.id,
            {
                "response": test_response,
                "status": "success",
                "error_message": None,
                "retry_attempts": 1,
            },
            wait=True,
        )
        if not res or not res.success:
            raise RuntimeError(getattr(res, "error", "Failed to update ModelCall"))
        # Update local object to reflect database state
        model_call.status = "success"
        model_call.response = test_response
        model_call.error_message = None

    def _process_successful_response(
        self,
        *,
        model_call: ModelCall,
        current_chunk_db_segments: list[TranscriptSegment],
    ) -> list[TranscriptSegment]:
        """Process a successful LLM response and create Identification records."""
        self.logger.info(
            f"LLM call for ModelCall {model_call.id} was successful. Parsing response."
        )
        try:
            prediction_list = clean_and_parse_model_output(model_call.response)
            created_identification_count, matched_segments = (
                self._create_identifications(
                    prediction_list=prediction_list,
                    current_chunk_db_segments=current_chunk_db_segments,
                    model_call=model_call,
                )
            )

            if created_identification_count > 0:
                self.logger.info(
                    f"Created {created_identification_count} new Identification records for ModelCall {model_call.id}."
                )
            return matched_segments
        except (ValidationError, AssertionError) as e:
            self.logger.error(
                f"Error processing LLM response for ModelCall {model_call.id}: {e}",
                exc_info=True,
            )
            # Surface the parse failure to the orchestrator. Combined with a
            # final ad-window count of 0 this drives the optional auto-retry
            # path; on its own it's just diagnostic.
            self.had_parse_error = True
        return []

    def _create_identifications(
        self,
        *,
        prediction_list: AdSegmentPredictionList,
        current_chunk_db_segments: list[TranscriptSegment],
        model_call: ModelCall,
    ) -> tuple[int, list[TranscriptSegment]]:
        """Create Identification records from the prediction list."""
        to_insert: list[dict[str, Any]] = []
        matched_segments: list[TranscriptSegment] = []
        processed_segment_ids: set[int] = set()
        content_type = prediction_list.content_type

        for pred in prediction_list.ad_segments:
            adjusted_confidence = self._adjust_confidence(
                base_confidence=pred.confidence,
                content_type=content_type,
            )

            if adjusted_confidence < self.config.output.min_confidence:
                self.logger.info(
                    f"Ad prediction offset {pred.segment_offset:.2f} for post {model_call.post_id} ignored due to low confidence: {pred.confidence:.2f} (min: {self.config.output.min_confidence})"
                )
                continue

            matched_segment = self._find_matching_segment(
                segment_offset=pred.segment_offset,
                current_chunk_db_segments=current_chunk_db_segments,
            )

            if not matched_segment:
                self.logger.warning(
                    f"Could not find matching TranscriptSegment for ad prediction offset {pred.segment_offset:.2f} in post {model_call.post_id}, chunk {model_call.first_segment_sequence_num}-{model_call.last_segment_sequence_num}. Confidence: {pred.confidence:.2f}"
                )
                continue

            if matched_segment.id in processed_segment_ids:
                continue

            processed_segment_ids.add(matched_segment.id)
            matched_segments.append(matched_segment)

            if self._segment_has_ad_identification(matched_segment.id):
                self.logger.debug(
                    "Segment %s for post %s already has an ad identification; skipping new record.",
                    matched_segment.id,
                    model_call.post_id,
                )
                continue

            to_insert.append(
                {
                    "transcript_segment_id": matched_segment.id,
                    "model_call_id": model_call.id,
                    "label": "ad",
                    "confidence": adjusted_confidence,
                }
            )

            self._maybe_add_preroll_context(
                matched_segment=matched_segment,
                current_chunk_db_segments=current_chunk_db_segments,
                model_call=model_call,
                processed_segment_ids=processed_segment_ids,
                matched_segments=matched_segments,
                base_confidence=adjusted_confidence,
                to_insert=to_insert,
            )

        if not to_insert:
            return 0, matched_segments

        inserted = self._insert_identifications_batched(to_insert)
        return inserted, matched_segments

    def _adjust_confidence(
        self, *, base_confidence: float, content_type: str | None
    ) -> float:
        """Demote confidence for self-promo/educational contexts."""
        if not content_type:
            return base_confidence

        if content_type in {"educational/self_promo", "technical_discussion"}:
            return max(0.0, base_confidence - 0.25)
        if content_type == "transition":
            return max(0.0, base_confidence - 0.1)
        return base_confidence

    def _maybe_add_preroll_context(
        self,
        *,
        matched_segment: TranscriptSegment,
        current_chunk_db_segments: list[TranscriptSegment],
        model_call: ModelCall,
        processed_segment_ids: set[int],
        matched_segments: list[TranscriptSegment],
        base_confidence: float,
        to_insert: list[dict[str, Any]],
    ) -> int:
        """If an ad is detected within the first 45s, include up to 3 preceding intro segments."""
        if matched_segment.start_time > 45.0:
            return 0

        created = 0
        matched_index = current_chunk_db_segments.index(matched_segment)
        start_index = max(0, matched_index - 3)
        for seg in current_chunk_db_segments[start_index:matched_index]:
            if seg.id in processed_segment_ids:
                continue
            if self._segment_has_ad_identification(seg.id):
                continue

            processed_segment_ids.add(seg.id)
            matched_segments.append(seg)
            to_insert.append(
                {
                    "transcript_segment_id": seg.id,
                    "model_call_id": model_call.id,
                    "label": "ad",
                    "confidence": max(
                        base_confidence, self.config.output.min_confidence
                    ),
                }
            )
            created += 1

        if created:
            self.logger.debug(
                "Pre-roll look-back added %s intro segments before %s (post %s)",
                created,
                matched_segment.sequence_num,
                model_call.post_id,
            )
        return created

    def _find_matching_segment(
        self,
        *,
        segment_offset: float,
        current_chunk_db_segments: list[TranscriptSegment],
    ) -> TranscriptSegment | None:
        """Find the TranscriptSegment that matches the given segment offset."""
        min_diff = float("inf")
        matched_segment = None
        for ts_segment in current_chunk_db_segments:
            diff = abs(ts_segment.start_time - segment_offset)
            if diff < min_diff and diff < 0.5:  # Tolerance of 0.5 seconds
                matched_segment = ts_segment
                min_diff = diff
        return matched_segment

    def _segment_has_ad_identification(self, transcript_segment_id: int) -> bool:
        """Check if a transcript segment already has an ad identification.

        NOTE: Uses self.db_session.query() for session consistency.
        """
        return (
            self.db_session.query(Identification)
            .filter_by(
                transcript_segment_id=transcript_segment_id,
                label="ad",
            )
            .first()
            is not None
        )

    def _is_retryable_error(self, error: Exception) -> bool:
        """Determine if an error should be retried.

        Delegates to the shared LLMErrorClassifier so timeouts, rate limits,
        and 5xx responses all use one definition. A previous narrower version
        of this method omitted timeout patterns, which caused a single
        ``litellm.Timeout`` from the ad classifier to mark the call
        ``failed_permanent`` on attempt 1 and crash the whole processing job.
        """
        from podcast_processor.llm_error_classifier import LLMErrorClassifier

        return LLMErrorClassifier.is_retryable_error(error)

    def _build_success_payload(
        self,
        *,
        response: Any,
        raw_response_content: str,
        retry_attempts_value: int,
        model_call_id: int | None,
        attempt_service_tier: str | None,
    ) -> dict[str, Any]:
        """Assemble the writer payload for a successful ModelCall update,
        including per-provider token usage when reported. Kept separate from
        ``_call_model`` so the latter stays under the branch-count budget.
        """
        usage = extract_litellm_usage(response)
        payload: dict[str, Any] = {
            "response": raw_response_content,
            "status": "success",
            "error_message": None,
            "retry_attempts": retry_attempts_value,
            "service_tier": attempt_service_tier,
        }
        for field in (
            "prompt_tokens",
            "cached_prompt_tokens",
            "completion_tokens",
            "total_tokens",
        ):
            if usage.get(field) is not None:
                payload[field] = usage[field]
        if usage.get("total_tokens") is not None:
            self.logger.info(
                "ModelCall %s token usage: prompt=%s cached_prompt=%s completion=%s total=%s (tier=%s)",
                model_call_id,
                usage.get("prompt_tokens"),
                usage.get("cached_prompt_tokens"),
                usage.get("completion_tokens"),
                usage.get("total_tokens"),
                attempt_service_tier or "default",
            )

        # Persist USD cost computed against LiteLLM's price table while we
        # still have litellm loaded in this short-lived worker. The web
        # process reads the column directly and never imports litellm.
        from podcast_processor.llm_model_call_utils import compute_estimated_cost_usd

        cost = compute_estimated_cost_usd(
            model_name=getattr(self.config, "llm_model", "") or "",
            service_tier=attempt_service_tier,
            prompt=None,
            usage=usage,
            logger=self.logger,
            log_prefix=f"ModelCall {model_call_id}",
        )
        if cost is not None:
            payload["estimated_cost_usd"] = cost
        return payload

    def _call_model(  # noqa: PLR0912
        self,
        model_call_obj: ModelCall,
        system_prompt: str,
        max_retries: int | None = None,
    ) -> str | None:
        """Call the LLM model with retry logic."""
        # Use configured retry count if not specified
        retry_count = (
            max_retries
            if max_retries is not None
            else getattr(self.config, "llm_max_retry_attempts", 3)
        )

        last_error: Exception | None = None
        raw_response_content = None
        original_retry_attempts = (
            0
            if model_call_obj.retry_attempts is None
            else model_call_obj.retry_attempts
        )

        for attempt in range(retry_count):
            retry_attempts_value = original_retry_attempts + attempt + 1
            current_attempt_num = attempt + 1
            self.logger.info(
                f"Calling model {model_call_obj.model_name} for ModelCall {model_call_obj.id} (attempt {current_attempt_num}/{retry_count})"
            )

            try:
                # Prepare API call and validate token limits first so the
                # pending-status writer update can also persist the resolved
                # service_tier for this attempt.
                completion_args = self._prepare_api_call(model_call_obj, system_prompt)
                if completion_args is None:
                    return None  # Token limit exceeded

                requested_service_tier = completion_args.get("service_tier")

                # Persist retry attempt + pending status (+ tier) via writer
                if model_call_obj.id is not None:
                    pending_res = writer_client.update(
                        "ModelCall",
                        model_call_obj.id,
                        {
                            "status": "pending",
                            "retry_attempts": retry_attempts_value,
                            "service_tier": requested_service_tier,
                            # The backoff (if any) is over; this attempt is in
                            # flight, not waiting.
                            "next_retry_at": None,
                        },
                        wait=True,
                    )
                    if not pending_res or not pending_res.success:
                        raise RuntimeError(
                            getattr(pending_res, "error", "Failed to update ModelCall")
                        )
                    model_call_obj.service_tier = requested_service_tier

                from litellm.types.utils import Choices

                # Use concurrency limiter if available
                if self.concurrency_limiter:
                    with ConcurrencyContext(self.concurrency_limiter, timeout=30.0):
                        response = call_litellm_with_tier_retry(
                            completion_args,
                            config=self.config,
                            logger=self.logger,
                            model_call_id=model_call_obj.id,
                        )
                else:
                    response = call_litellm_with_tier_retry(
                        completion_args,
                        config=self.config,
                        logger=self.logger,
                        model_call_id=model_call_obj.id,
                    )

                # The helper removes Flex after an exhausted-tier fallback and
                # also honors OpenAI's response-reported tier.
                attempt_service_tier = completion_args.get("service_tier")

                response_first_choice = response.choices[0]
                assert isinstance(response_first_choice, Choices)
                content = response_first_choice.message.content
                assert content is not None
                raw_response_content = content

                success_payload = self._build_success_payload(
                    response=response,
                    raw_response_content=raw_response_content,
                    retry_attempts_value=retry_attempts_value,
                    model_call_id=model_call_obj.id,
                    attempt_service_tier=attempt_service_tier,
                )
                success_res = writer_client.update(
                    "ModelCall",
                    model_call_obj.id,
                    success_payload,
                    wait=True,
                )
                if not success_res or not success_res.success:
                    raise RuntimeError(
                        getattr(success_res, "error", "Failed to update ModelCall")
                    )
                # Update local object to reflect database state
                model_call_obj.status = "success"
                model_call_obj.response = raw_response_content
                model_call_obj.error_message = None
                self.logger.info(
                    f"Model call {model_call_obj.id} successful on attempt {current_attempt_num}."
                )
                return raw_response_content

            except Exception as e:
                if isinstance(e, LLMRequestTooLargeError):
                    error_message = str(e)
                    fail_res = writer_client.update(
                        "ModelCall",
                        model_call_obj.id,
                        {
                            "status": "failed_permanent",
                            "error_message": error_message,
                        },
                        wait=True,
                    )
                    if not fail_res or not fail_res.success:
                        raise RuntimeError(
                            getattr(fail_res, "error", "Failed to update ModelCall")
                        ) from e
                    model_call_obj.status = "failed_permanent"
                    model_call_obj.error_message = error_message
                    raise
                last_error = e
                if self._is_retryable_error(e):
                    # Preserve the classifier's gradual retry backoff after an
                    # exhausted Flex-to-standard cycle. Do not sleep after the
                    # final configured attempt.
                    if attempt == retry_count - 1:
                        break
                    self._handle_retryable_error(
                        model_call_obj=model_call_obj,
                        error=e,
                        attempt=attempt,
                        current_attempt_num=current_attempt_num,
                    )
                else:
                    self.logger.error(
                        f"Non-retryable LLM error for ModelCall {model_call_obj.id} (attempt {current_attempt_num}): {e}",
                        exc_info=True,
                    )
                    fail_res = writer_client.update(
                        "ModelCall",
                        model_call_obj.id,
                        {"status": "failed_permanent", "error_message": str(e)},
                        wait=True,
                    )
                    if not fail_res or not fail_res.success:
                        raise RuntimeError(
                            getattr(fail_res, "error", "Failed to update ModelCall")
                        ) from e
                    # Update local object to reflect database state
                    model_call_obj.status = "failed_permanent"
                    model_call_obj.error_message = str(e)
                    raise  # Re-raise non-retryable exceptions immediately

        # If we get here, all retries were exhausted
        self._handle_retry_exhausted(model_call_obj, retry_count, last_error)

        if last_error:
            raise last_error
        raise RuntimeError(
            f"Maximum retries ({retry_count}) exceeded for ModelCall {model_call_obj.id}."
        )

    def _handle_retryable_error(
        self,
        *,
        model_call_obj: ModelCall,
        error: InternalServerError | Exception,
        attempt: int,
        current_attempt_num: int,
    ) -> None:
        """Handle a retryable error during LLM call.

        Flips the row to `status="retrying"` with an attempt-annotated error
        message so the debug UI shows "retrying (2/5): rate limit" instead of
        sitting silently at `pending` for the duration of the backoff sleep.
        """
        self.logger.error(
            f"LLM retryable error for ModelCall {model_call_obj.id} (attempt {current_attempt_num}): {error}"
        )
        retry_count = getattr(self.config, "llm_max_retry_attempts", 3)
        error_message = f"Retrying ({current_attempt_num}/{retry_count}): {error}"

        # Compute the backoff before flipping the row so the retrying update can
        # carry the deadline — the jobs UI uses it to show "retrying in Ns".
        error_str = str(error).lower()
        if any(
            term in error_str
            for term in ["rate_limit_error", "ratelimiterror", "429", "rate limit"]
        ):
            # For rate limiting, use a gradual backoff starting at 30 seconds.
            wait_time = 30 * (2**attempt)
            self.logger.info(
                f"Rate limit detected. Waiting {wait_time}s before retry for ModelCall {model_call_obj.id}."
            )
        else:
            # For other errors, use shorter exponential backoff: 1, 2, 4 seconds
            wait_time = (2**attempt) * 1
            self.logger.info(
                f"Waiting {wait_time}s before next retry for ModelCall {model_call_obj.id}."
            )
        next_retry_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(
            seconds=wait_time
        )

        res = writer_client.update(
            "ModelCall",
            model_call_obj.id,
            {
                "status": "retrying",
                "error_message": error_message,
                "next_retry_at": next_retry_at,
            },
            wait=True,
        )
        if not res or not res.success:
            raise RuntimeError(getattr(res, "error", "Failed to update ModelCall"))
        # Update local object to reflect database state
        model_call_obj.status = "retrying"
        model_call_obj.error_message = error_message

        time.sleep(wait_time)

    def _handle_retry_exhausted(
        self,
        model_call_obj: ModelCall,
        max_retries: int,
        last_error: Exception | None,
    ) -> None:
        """Handle the case when all retries are exhausted."""
        self.logger.error(
            f"Failed to call model for ModelCall {model_call_obj.id} after {max_retries} attempts."
        )
        if last_error:
            error_message = str(last_error)
        else:
            error_message = f"Maximum retries ({max_retries}) exceeded without a specific InternalServerError."

        res = writer_client.update(
            "ModelCall",
            model_call_obj.id,
            {"status": "failed_retries", "error_message": error_message},
            wait=True,
        )
        if not res or not res.success:
            raise RuntimeError(getattr(res, "error", "Failed to update ModelCall"))
        # Update local object to reflect database state
        model_call_obj.status = "failed_retries"
        model_call_obj.error_message = error_message

    def _get_segments_bulk(
        self, post_id: int, sequence_numbers: list[int]
    ) -> dict[int, TranscriptSegment]:
        """Fetch multiple segments in one query.

        NOTE: Must use self.db_session.query() instead of TranscriptSegment.query
        to ensure we use the same session. Using TranscriptSegment.query
        (the Flask-SQLAlchemy scoped session) can lead to SQLite lock issues
        when another query on self.db_session is mid-transaction.
        """
        segments = (
            self.db_session.query(TranscriptSegment)
            .filter(
                and_(
                    TranscriptSegment.post_id == post_id,
                    TranscriptSegment.sequence_num.in_(sequence_numbers),
                )
            )
            .all()
        )
        return {seg.sequence_num: seg for seg in segments}

    def _get_existing_ids_bulk(
        self, post_id: int, model_call_id: int
    ) -> set[tuple[int, int, str]]:
        """Fetch all existing identifications as a set for O(1) lookup.

        NOTE: Uses self.db_session.query() for session consistency.
        """
        ids = (
            self.db_session.query(Identification)
            .join(TranscriptSegment)
            .filter(
                and_(
                    TranscriptSegment.post_id == post_id,
                    Identification.model_call_id == model_call_id,
                )
            )
            .all()
        )
        return {(i.transcript_segment_id, i.model_call_id, i.label) for i in ids}

    def _create_identifications_bulk(
        self, identifications: list[dict[str, Any]]
    ) -> int:
        """Bulk insert identifications"""
        return self._insert_identifications_batched(identifications)

    def _insert_identifications_batched(
        self, identifications: list[dict[str, Any]]
    ) -> int:
        if not identifications:
            return 0

        batch_size = get_writer_batch_size()
        total_batches = batch_count_for(len(identifications), batch_size=batch_size)
        inserted = 0
        self.logger.info(
            "[IDENTIFICATION_WRITE] rows=%s batches=%s batch_size=%s",
            len(identifications),
            total_batches,
            batch_size,
        )
        for batch_index, batch in enumerate(
            iter_writer_batches(identifications, batch_size=batch_size), start=1
        ):
            res = writer_client.action(
                "insert_identifications",
                {"identifications": list(batch)},
                wait=True,
            )
            if not res or not res.success:
                raise RuntimeError(
                    getattr(res, "error", "Failed to insert identifications")
                )
            inserted += int((res.data or {}).get("inserted") or 0)
            collect_incremental(
                f"ad classifier identification batch {batch_index}/{total_batches}",
                self.logger,
            )
        return inserted

    def _replace_identifications_batched(
        self,
        *,
        delete_ids: list[int],
        new_identifications: list[dict[str, Any]],
    ) -> int:
        res = writer_client.action(
            "replace_identifications",
            {"delete_ids": delete_ids, "new_identifications": []},
            wait=True,
        )
        if not res or not res.success:
            raise RuntimeError(
                getattr(res, "error", "Failed to delete replaced identifications")
            )
        return self._insert_identifications_batched(new_identifications)

    def expand_neighbors_bulk(
        self,
        ad_identifications: list[Identification],
        model_call: ModelCall,
        post_id: int,
        window: int = 5,
    ) -> int:
        """Expand neighbors using bulk operations (3 queries instead of 900)"""

        # PHASE 1: Bulk data collection (2 queries)

        # Collect all sequence numbers we need
        sequence_numbers = set()
        for ident in ad_identifications:
            base_seq = ident.transcript_segment.sequence_num
            for offset in range(-window, window + 1):
                sequence_numbers.add(base_seq + offset)

        # Query 1: Bulk fetch segments
        segments_by_seq = self._get_segments_bulk(post_id, list(sequence_numbers))

        # Query 2: Bulk fetch existing identifications
        existing = self._get_existing_ids_bulk(post_id, model_call.id)

        # PHASE 2: In-memory processing (0 queries)

        to_create = []
        for ident in ad_identifications:
            base_seq = ident.transcript_segment.sequence_num

            for offset in range(-window, window + 1):
                if offset == 0:
                    continue

                neighbor_seq = base_seq + offset
                seg = segments_by_seq.get(neighbor_seq)
                if not seg:
                    continue

                # Check if already exists (O(1) lookup)
                key = (seg.id, model_call.id, "ad")
                if key in existing:
                    continue

                text = seg.text or ""
                signals = self.cue_detector.analyze(text)
                has_strong_cue = (
                    signals["url"]
                    or signals["promo"]
                    or signals["phone"]
                    or signals["cta"]
                )
                is_transition = signals["transition"]
                is_self_promo = signals["self_promo"]

                gap_seconds = abs(
                    (seg.start_time or 0.0)
                    - (ident.transcript_segment.start_time or 0.0)
                )

                if not self._should_expand_neighbor(
                    has_strong_cue=has_strong_cue,
                    is_transition=is_transition,
                    gap_seconds=gap_seconds,
                ):
                    continue

                confidence = self._neighbor_confidence(
                    has_strong_cue=has_strong_cue,
                    is_transition=is_transition,
                    is_self_promo=is_self_promo,
                    gap_seconds=gap_seconds,
                )

                to_create.append(
                    {
                        "transcript_segment_id": seg.id,
                        "model_call_id": model_call.id,
                        "label": "ad",
                        "confidence": confidence,
                    }
                )
                existing.add(key)  # Avoid duplicates in this batch

        # PHASE 3: Bulk insert (1 query)

        if to_create:
            return self._create_identifications_bulk(to_create)
        return 0

    def _should_expand_neighbor(
        self,
        *,
        has_strong_cue: bool,
        is_transition: bool,
        gap_seconds: float,
    ) -> bool:
        if not self.config.enable_boundary_refinement:
            return has_strong_cue

        if has_strong_cue or is_transition:
            return True

        return gap_seconds <= 10.0

    @staticmethod
    def _neighbor_confidence(
        *,
        has_strong_cue: bool,
        is_transition: bool,
        is_self_promo: bool,
        gap_seconds: float,
    ) -> float:
        confidence = 0.72 if is_transition else 0.75
        if has_strong_cue:
            confidence = 0.85 if gap_seconds <= 10.0 else 0.8
        if is_self_promo:
            confidence = max(0.5, confidence - 0.25)
        return confidence

    def _detect_repeat_ads(
        self, transcript_segments: list[TranscriptSegment], post: Post
    ) -> None:
        """Find and confirm dynamically-inserted repeats of detected ads.

        For each distinct ad already detected, deterministically locate matching
        spans elsewhere in the transcript, ask the LLM to confirm each candidate
        is advertisement content, and write ad identifications for confirmations
        that clear the output confidence threshold. Boundary tightening of the
        new blocks is left to the ``_refine_boundaries`` pass that runs next.
        """
        if self.repeat_ad_refiner is None:
            return

        identifications = (
            self.db_session.query(Identification)
            .join(TranscriptSegment)
            .filter(TranscriptSegment.post_id == post.id, Identification.label == "ad")
            .all()
        )
        blocks = self._group_into_blocks(identifications)
        if not blocks:
            return

        min_confidence = float(self.config.output.min_confidence)
        seg_payloads = [
            {
                "sequence_num": s.sequence_num,
                "start_time": s.start_time,
                "end_time": s.end_time,
                "text": s.text,
            }
            for s in transcript_segments
        ]
        seg_by_seq = {int(s.sequence_num): s for s in transcript_segments}

        block_meta: list[dict[str, Any]] = []
        detected_ranges: list[tuple[int, int]] = []
        for block in blocks:
            segs = [
                i.transcript_segment
                for i in block["identifications"]
                if i.transcript_segment is not None
            ]
            if not segs:
                continue
            seqs = [int(s.sequence_num) for s in segs]
            first_seq, last_seq = min(seqs), max(seqs)
            detected_ranges.append((first_seq, last_seq))
            text = " ".join(
                s.text or "" for s in sorted(segs, key=lambda x: x.sequence_num)
            )
            block_meta.append(
                {
                    "first_seq": first_seq,
                    "last_seq": last_seq,
                    "text": text,
                    "confidence": float(block.get("confidence", 0.0) or 0.0),
                }
            )

        # Dedupe by normalized text so an ad inserted N times is searched once.
        seen_signatures: set[str] = set()
        found_seqs: set[int] = set()
        new_identifications: list[dict[str, Any]] = []

        for meta in block_meta:
            if meta["confidence"] < min_confidence:
                continue
            signature = " ".join(tokenize(meta["text"]))
            if not signature or signature in seen_signatures:
                continue
            seen_signatures.add(signature)

            candidates = find_repeat_candidates_with_fallback(
                seg_payloads,
                post_guid=getattr(post, "guid", None),
                target_first_seq=meta["first_seq"],
                target_last_seq=meta["last_seq"],
                exclude_ranges=detected_ranges,
                logger=self.logger,
            )

            for cand in candidates:
                cand_seqs = range(cand.first_seq, cand.last_seq + 1)
                if any(seq in found_seqs for seq in cand_seqs):
                    continue
                rows = self._confirm_and_record_repeat(
                    candidate=cand,
                    reference_text=meta["text"],
                    confidence_hint=meta["confidence"],
                    seg_by_seq=seg_by_seq,
                    post=post,
                    min_confidence=min_confidence,
                )
                if rows:
                    found_seqs.update(cand_seqs)
                    new_identifications.extend(rows)

        if new_identifications:
            created = self._create_identifications_bulk(new_identifications)
            self.logger.info(
                "Repeat-ad detection created %s identifications across %s "
                "confirmed repeat segments for post %s",
                created,
                len(found_seqs),
                post.id,
            )

    def _confirm_and_record_repeat(
        self,
        *,
        candidate: RepeatAdCandidate,
        reference_text: str,
        confidence_hint: float,
        seg_by_seq: dict[int, TranscriptSegment],
        post: Post,
        min_confidence: float,
    ) -> list[dict[str, Any]]:
        """Confirm one candidate with the LLM; return identification rows if accepted."""
        assert self.repeat_ad_refiner is not None
        window_segs = [
            seg_by_seq[seq]
            for seq in range(candidate.first_seq, candidate.last_seq + 1)
            if seq in seg_by_seq
        ]
        if not window_segs:
            return []

        candidate_payload = [
            {"start_time": s.start_time, "text": s.text} for s in window_segs
        ]
        confirmation = self.repeat_ad_refiner.confirm(
            reference_text=reference_text,
            candidate_segments=candidate_payload,
            candidate_first_seq=candidate.first_seq,
            confidence_hint=confidence_hint,
            post_id=post.id,
        )
        if (
            not confirmation.is_ad
            or confirmation.model_call_id is None
            or confirmation.confidence < min_confidence
        ):
            return []

        return [
            {
                "transcript_segment_id": s.id,
                "model_call_id": confirmation.model_call_id,
                "label": "ad",
                "confidence": confirmation.confidence,
            }
            for s in window_segs
        ]

    def _refine_boundaries(
        self, transcript_segments: list[TranscriptSegment], post: Post
    ) -> None:
        """Apply boundary refinement to detected ads.

        NOTE: Uses self.db_session.query() for session consistency.
        """
        if not self.boundary_refiner:
            return

        # Latest refined boundaries for downstream audio cuts. Overwrites prior
        # values for the post ("latest successful" semantics).
        refined_boundaries: list[dict[str, Any]] = []
        post_row = self._safe_get_post_row(post) or post
        words_by_sequence = load_word_timestamps_by_sequence(
            getattr(post_row, "transcript_word_timestamps", None)
        )

        # Get ad identifications
        identifications = (
            self.db_session.query(Identification)
            .join(TranscriptSegment)
            .filter(TranscriptSegment.post_id == post.id, Identification.label == "ad")
            .all()
        )

        # Group into ad blocks
        ad_blocks = self._group_into_blocks(identifications)

        for block in ad_blocks:
            # Skip low confidence or very short blocks
            if block["confidence"] < 0.6 or (block["end"] - block["start"]) < 15.0:
                continue

            # Refine
            seq_nums = [
                ident.transcript_segment.sequence_num
                for ident in block["identifications"]
                if ident.transcript_segment is not None
            ]

            refinement = self.boundary_refiner.refine(
                ad_start=block["start"],
                ad_end=block["end"],
                confidence=block["confidence"],
                all_segments=[
                    self._segment_payload_for_boundary_refinement(
                        s,
                        words_by_sequence.get(int(s.sequence_num)),
                    )
                    for s in transcript_segments
                ],
                post_id=post.id,
                first_seq_num=min(seq_nums) if seq_nums else None,
                last_seq_num=max(seq_nums) if seq_nums else None,
            )

            # Apply refinement: delete old identifications, create new ones
            # Note: Get model_call from block identifications
            model_call = (
                block["identifications"][0].model_call
                if block["identifications"]
                else None
            )
            if model_call:
                self._apply_refinement(
                    block, refinement, transcript_segments, post, model_call
                )

                refined_boundaries.append(
                    {
                        "orig_start": float(block["start"]),
                        "orig_end": float(block["end"]),
                        "refined_start": float(refinement.refined_start),
                        "refined_end": float(refinement.refined_end),
                        "confidence": float(block.get("confidence", 0.0) or 0.0),
                    }
                )

        # Store latest refined boundaries on the post so audio processing can cut
        # using refined timestamps (including word-level refined start times).
        # Clear the value when we have no refined boundaries so stale data doesn't
        # affect future audio cuts.
        try:
            res = writer_client.update(
                "Post",
                post.id,
                {
                    "refined_ad_boundaries": refined_boundaries or None,
                    "refined_ad_boundaries_updated_at": datetime.now(UTC).replace(
                        tzinfo=None
                    ),
                },
                wait=True,
            )
            if not res or not res.success:
                raise RuntimeError(
                    getattr(res, "error", "Failed to update refined ad boundaries")
                )
        except Exception as exc:  # noqa: BLE001
            # Best-effort: cutting can fall back to segment-derived windows.
            self.logger.warning(
                "Failed to persist refined ad boundaries for post %s: %s",
                post.id,
                exc,
            )

    @staticmethod
    def _segment_payload_for_boundary_refinement(
        segment: TranscriptSegment,
        words: list[Any] | None,
    ) -> dict[str, Any]:
        payload = {
            "sequence_num": segment.sequence_num,
            "start_time": segment.start_time,
            "text": segment.text,
            "end_time": segment.end_time,
        }
        if words:
            payload["words"] = words
        return payload

    def _safe_get_post_row(self, post: Post) -> Post | None:
        try:
            return self.db_session.get(Post, post.id)
        except Exception:  # noqa: BLE001
            return None

    def _group_into_blocks(
        self, identifications: list[Identification]
    ) -> list[dict[str, Any]]:
        """Group adjacent identifications into ad blocks"""
        if not identifications:
            return []

        identifications = sorted(
            identifications, key=lambda i: i.transcript_segment.start_time
        )
        blocks: list[dict[str, Any]] = []
        current: list[Identification] = []

        for ident in identifications:
            if (
                not current
                or ident.transcript_segment.start_time
                - current[-1].transcript_segment.end_time
                <= 10.0
            ):
                current.append(ident)
            else:
                blocks.append(self._create_block(current))
                current = [ident]

        if current:
            blocks.append(self._create_block(current))

        return blocks

    def _create_block(self, identifications: list[Identification]) -> dict[str, Any]:
        return {
            "start": min(i.transcript_segment.start_time for i in identifications),
            "end": max(i.transcript_segment.end_time for i in identifications),
            "confidence": sum(i.confidence for i in identifications)
            / len(identifications),
            "identifications": identifications,
        }

    def _apply_refinement(
        self,
        block: dict[str, Any],
        refinement: Any,
        transcript_segments: list[TranscriptSegment],
        post: Post,
        model_call: ModelCall,
    ) -> None:
        """Update identifications based on refined boundaries"""
        delete_ids = [
            i.id
            for i in block.get("identifications", [])
            if getattr(i, "id", None) is not None
        ]

        new_identifications: list[dict[str, Any]] = []
        for seg in transcript_segments:
            seg_start = float(seg.start_time or 0.0)
            seg_end = float(seg.end_time or seg_start)
            # Keep segments that overlap the refined window. This preserves the
            # containing segment when refined boundaries fall mid-segment.
            if seg_start <= float(refinement.refined_end) and seg_end >= float(
                refinement.refined_start
            ):
                new_identifications.append(
                    {
                        "transcript_segment_id": seg.id,
                        "model_call_id": model_call.id,
                        "label": "ad",
                        "confidence": block["confidence"],
                    }
                )

        self._replace_identifications_batched(
            delete_ids=delete_ids,
            new_identifications=new_identifications,
        )
