import logging
from dataclasses import dataclass
from typing import Any

from app.extensions import db
from app.models import AudioSegment, Identification, ModelCall, Post, TranscriptSegment
from app.writer.client import writer_client
from podcast_processor.ad_merger import AdMerger
from podcast_processor.audio import clip_segments_with_fade, get_audio_duration_ms
from shared.audio_segment_utils import (
    bridge_ad_windows_with_audio,
    expand_episode_edge_ad_windows_with_audio,
    extract_audio_windows,
    extract_edge_audio_windows,
)
from shared.config import Config

ATOMIC_AD_BLOCK_GAP_SECONDS = 10.0
REFINED_BOUNDARY_MATCH_TOLERANCE_SECONDS = 0.75
EPISODE_EDGE_FRAGMENT_WINDOW_SECONDS = 30.0
SHORT_EDGE_FRAGMENT_MERGE_GAP_SECONDS = 20.0
MIN_NEIGHBOR_AD_DURATION_FOR_EDGE_MERGE_SECONDS = 15.0


@dataclass(frozen=True)
class TimeWindow:
    start: float
    end: float


@dataclass(frozen=True)
class RefinedBoundary:
    orig_start: float
    orig_end: float
    refined_start: float
    refined_end: float


class AudioProcessor:
    """Handles audio processing and ad segment removal from podcast files."""

    def __init__(
        self,
        config: Config,
        logger: logging.Logger | None = None,
        identification_query: Any | None = None,
        transcript_segment_query: Any | None = None,
        model_call_query: Any | None = None,
        db_session: Any | None = None,
    ):
        self.logger = logger or logging.getLogger("global_logger")
        self.config = config
        self._identification_query_provided = identification_query is not None
        self.identification_query = identification_query or Identification.query
        self.transcript_segment_query = (
            transcript_segment_query or TranscriptSegment.query
        )
        self.model_call_query = model_call_query or ModelCall.query
        self.db_session = db_session or db.session
        self.ad_merger = AdMerger()

    def get_ad_segments(self, post: Post) -> list[tuple[float, float]]:
        """
        Retrieves ad segments from the database for a given post.

        NOTE: Uses self.db_session.query() instead of self.identification_query
        to ensure all operations use the same session consistently.

        Args:
            post: The Post object to retrieve ad segments for

        Returns:
            A list of tuples containing start and end times (in seconds) of ad segments
        """
        rust_segments = self._try_rust_get_ad_segments(post)
        if rust_segments is not None:
            self.logger.info(
                f"Rust ad-merge produced {len(rust_segments)} ad windows for post {post.id}"
            )
            return rust_segments

        self.logger.info(f"Retrieving ad segments from database for post {post.id}.")

        query = (
            self.identification_query
            if self._identification_query_provided
            else self.db_session.query(Identification)
        )

        ad_identifications = (
            query.join(
                TranscriptSegment,
                Identification.transcript_segment_id == TranscriptSegment.id,
            )
            .join(ModelCall, Identification.model_call_id == ModelCall.id)
            .filter(
                TranscriptSegment.post_id == post.id,
                Identification.label == "ad",
                Identification.confidence >= self.config.output.min_confidence,
                ModelCall.status
                == "success",  # Only consider identifications from successful LLM calls
            )
            .all()
        )

        if not ad_identifications:
            self.logger.info(
                f"No ad segments found meeting criteria for post {post.id}."
            )
            return []

        # Get full segment objects with text for content analysis
        # Filter out any identifications with missing segments (DB integrity check)
        ad_segments_with_text = []
        valid_identifications = []
        for ident in ad_identifications:
            segment = ident.transcript_segment
            if segment:
                ad_segments_with_text.append(segment)
                valid_identifications.append(ident)
            else:
                # This should ideally not happen if DB integrity is maintained
                self.logger.warning(
                    f"Identification {ident.id} for post {post.id} refers to a missing TranscriptSegment {ident.transcript_segment_id}. Skipping."
                )

        if not ad_segments_with_text:
            self.logger.info(
                f"No valid ad segments with transcript data for post {post.id}."
            )
            return []

        # Content-aware merge
        ad_groups = self.ad_merger.merge(
            ad_segments=ad_segments_with_text,
            identifications=valid_identifications,
            max_gap=float(self.config.output.min_ad_segment_separation_seconds),
            min_content_gap=12.0,
        )

        self.logger.info(
            f"Merged {len(ad_segments_with_text)} segments into {len(ad_groups)} groups for post {post.id}"
        )

        refined_boundaries = self._load_refined_boundaries(post)
        ad_segments_times = [
            self._cut_window_for_ad_group(group, refined_boundaries)
            for group in ad_groups
        ]
        bridgeable_audio_windows = self._get_bridgeable_audio_windows(post)
        if bridgeable_audio_windows:
            ad_segments_times = bridge_ad_windows_with_audio(
                ad_segments_times,
                bridgeable_audio_windows,
            )
        edge_audio_windows = self._get_edge_expansion_audio_windows(post)
        if edge_audio_windows and not self._has_transcript_content_before_first_ad(
            post,
            ad_segments_times,
            valid_identifications,
        ):
            ad_segments_times = expand_episode_edge_ad_windows_with_audio(
                ad_segments_times,
                edge_audio_windows,
                edge_window_seconds=EPISODE_EDGE_FRAGMENT_WINDOW_SECONDS,
            )
        ad_segments_times.sort(key=lambda x: x[0])
        return ad_segments_times

    def _try_rust_get_ad_segments(self, post: Post) -> list[tuple[float, float]] | None:
        from shared.processing_paths import get_instance_dir
        from shared.rust_sidecar import rust_ad_merge_enabled, try_merge_ad_segments

        if not rust_ad_merge_enabled():
            return None

        if self._identification_query_provided:
            # Tests inject a custom query; the Rust path queries SQLite directly,
            # so let it fall through to the Python implementation.
            return None

        try:
            db_path = get_instance_dir() / "sqlite3.db"
            return try_merge_ad_segments(
                db_path=db_path,
                post_guid=post.guid,
                min_confidence=float(self.config.output.min_confidence),
                max_gap=float(self.config.output.min_ad_segment_separation_seconds),
                enable_boundary_refinement=bool(
                    getattr(self.config, "enable_boundary_refinement", False)
                ),
            )
        except Exception:
            self.logger.exception(
                "Rust ad-merge bootstrap failed for post %s; falling back",
                post.id,
            )
            return None

    def _get_bridgeable_audio_windows(self, post: Post) -> list[tuple[float, float]]:
        try:
            audio_segments = (
                self.db_session.query(AudioSegment)
                .filter(AudioSegment.post_id == post.id)
                .order_by(AudioSegment.start_time.asc())
                .all()
            )
        except Exception:  # noqa: BLE001
            self.logger.warning(
                "Failed to load INA audio segments while building cut windows for post %s",
                post.id,
                exc_info=True,
            )
            return []

        return extract_audio_windows(audio_segments)

    def _get_edge_expansion_audio_windows(
        self, post: Post
    ) -> list[tuple[float, float]]:
        try:
            audio_segments = (
                self.db_session.query(AudioSegment)
                .filter(AudioSegment.post_id == post.id)
                .order_by(AudioSegment.start_time.asc())
                .all()
            )
        except Exception:  # noqa: BLE001
            self.logger.warning(
                "Failed to load INA audio segments while expanding edge ad windows for post %s",
                post.id,
                exc_info=True,
            )
            return []

        return extract_edge_audio_windows(audio_segments)

    def _has_transcript_content_before_first_ad(
        self,
        post: Post,
        ad_segments: list[tuple[float, float]],
        ad_identifications: list[Identification],
    ) -> bool:
        if not ad_segments:
            return False

        first_start = min(start for start, _ in ad_segments)
        if first_start > EPISODE_EDGE_FRAGMENT_WINDOW_SECONDS:
            return False

        ad_segment_ids = {
            ident.transcript_segment_id
            for ident in ad_identifications
            if ident.transcript_segment_id is not None
        }
        try:
            preceding_segments = (
                self.db_session.query(TranscriptSegment)
                .filter(
                    TranscriptSegment.post_id == post.id,
                    TranscriptSegment.start_time < first_start,
                )
                .all()
            )
        except Exception:  # noqa: BLE001
            self.logger.warning(
                "Failed to load leading transcript segments while expanding edge ad windows for post %s",
                post.id,
                exc_info=True,
            )
            return True

        return any(segment.id not in ad_segment_ids for segment in preceding_segments)

    def _load_refined_boundaries(self, post: Post) -> list[RefinedBoundary]:
        if not getattr(self.config, "enable_boundary_refinement", False):
            return []

        post_row = self._safe_get_post_row(post)
        refined = getattr(post_row, "refined_ad_boundaries", None) if post_row else None
        return self._parse_refined_boundaries(refined)

    def _cut_window_for_ad_group(
        self,
        group: Any,
        refined_boundaries: list[RefinedBoundary],
    ) -> tuple[float, float]:
        atomic_blocks = self._atomic_ad_blocks_for_group(group)
        if not atomic_blocks:
            return (float(group.start_time), float(group.end_time))

        projected_blocks = [
            self._project_atomic_block(block, refined_boundaries)
            for block in atomic_blocks
        ]
        return (
            min(block.start for block in projected_blocks),
            max(block.end for block in projected_blocks),
        )

    @staticmethod
    def _atomic_ad_blocks_for_group(group: Any) -> list[TimeWindow]:
        segments = sorted(
            list(getattr(group, "segments", []) or []),
            key=lambda segment: float(getattr(segment, "start_time", 0.0) or 0.0),
        )
        if not segments:
            return []

        blocks: list[TimeWindow] = []
        current_start = float(segments[0].start_time)
        current_end = float(segments[0].end_time)

        for segment in segments[1:]:
            segment_start = float(segment.start_time)
            segment_end = float(segment.end_time)
            if segment_start - current_end <= ATOMIC_AD_BLOCK_GAP_SECONDS:
                current_end = max(current_end, segment_end)
                continue

            blocks.append(TimeWindow(start=current_start, end=current_end))
            current_start = segment_start
            current_end = segment_end

        blocks.append(TimeWindow(start=current_start, end=current_end))
        return blocks

    @staticmethod
    def _project_atomic_block(
        block: TimeWindow,
        refined_boundaries: list[RefinedBoundary],
    ) -> TimeWindow:
        matched = AudioProcessor._refined_boundaries_matching_block(
            block, refined_boundaries
        )
        if not matched:
            return block
        # Union every refined window overlapping this block. Keeping only the
        # single best-overlap match used to silently drop correctly refined
        # ad windows that shared one atomic transcript block, leaving ad
        # audio uncut.
        return TimeWindow(
            start=min(refined.refined_start for refined in matched),
            end=max(refined.refined_end for refined in matched),
        )

    @staticmethod
    def _refined_boundary_overlap(
        block: TimeWindow,
        refined: RefinedBoundary,
    ) -> float:
        return min(
            block.end + REFINED_BOUNDARY_MATCH_TOLERANCE_SECONDS,
            refined.orig_end + REFINED_BOUNDARY_MATCH_TOLERANCE_SECONDS,
        ) - max(
            block.start - REFINED_BOUNDARY_MATCH_TOLERANCE_SECONDS,
            refined.orig_start - REFINED_BOUNDARY_MATCH_TOLERANCE_SECONDS,
        )

    @classmethod
    def _refined_boundaries_matching_block(
        cls,
        block: TimeWindow,
        refined_boundaries: list[RefinedBoundary],
    ) -> list[RefinedBoundary]:
        return [
            refined
            for refined in refined_boundaries
            if cls._refined_boundary_overlap(block, refined) > 0.0
        ]

    def _safe_get_post_row(self, post: Post) -> Post | None:
        try:
            return self.db_session.get(Post, post.id)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _parse_refined_boundaries(
        refined: Any,
    ) -> list[RefinedBoundary]:
        if not refined or not isinstance(refined, list):
            return []

        parsed: list[RefinedBoundary] = []
        for item in refined:
            if not isinstance(item, dict):
                continue

            orig_start_raw = item.get("orig_start")
            orig_end_raw = item.get("orig_end")
            refined_start_raw = item.get("refined_start")
            refined_end_raw = item.get("refined_end")
            if (
                orig_start_raw is None
                or orig_end_raw is None
                or refined_start_raw is None
                or refined_end_raw is None
            ):
                continue

            try:
                orig_start = float(orig_start_raw)
                orig_end = float(orig_end_raw)
                refined_start = float(refined_start_raw)
                refined_end = float(refined_end_raw)
            except Exception:  # noqa: BLE001
                continue

            if orig_end <= orig_start or refined_end <= refined_start:
                continue

            parsed.append(
                RefinedBoundary(
                    orig_start=orig_start,
                    orig_end=orig_end,
                    refined_start=refined_start,
                    refined_end=refined_end,
                )
            )

        return parsed

    def merge_ad_segments(
        self,
        *,
        duration_ms: int,
        ad_segments: list[tuple[float, float]],
        min_ad_segment_length_seconds: float,
        min_ad_segment_separation_seconds: float,
    ) -> list[tuple[int, int]]:
        """
        Merges nearby ad segments and filters out segments that are too short.

        Args:
            duration_ms: Duration of the audio in milliseconds
            ad_segments: List of ad segments as (start, end) tuples in seconds
            min_ad_segment_length_seconds: Minimum length of an ad segment to retain
            min_ad_segment_separation_seconds: Minimum separation between segments before merging

        Returns:
            List of merged ad segments as (start, end) tuples in milliseconds
        """
        audio_duration_seconds = duration_ms / 1000.0

        self.logger.info(
            f"Creating new audio with ads segments removed between: {ad_segments}"
        )
        if not ad_segments:
            return []

        ad_segments = sorted(ad_segments)

        last_segment = self._get_last_segment_if_near_end(
            ad_segments,
            audio_duration_seconds=audio_duration_seconds,
            min_separation=min_ad_segment_separation_seconds,
        )

        ad_segments = self._merge_short_episode_edge_segments(
            ad_segments,
            audio_duration_seconds=audio_duration_seconds,
            min_length=min_ad_segment_length_seconds,
        )
        ad_segments = self._merge_close_segments(
            ad_segments, min_separation=min_ad_segment_separation_seconds
        )
        ad_segments = self._filter_short_segments(
            ad_segments, min_length=min_ad_segment_length_seconds
        )
        ad_segments = self._restore_last_segment_if_needed(ad_segments, last_segment)
        ad_segments = self._extend_last_segment_to_end_if_needed(
            ad_segments,
            audio_duration_seconds=audio_duration_seconds,
            min_separation=min_ad_segment_separation_seconds,
        )

        self.logger.info(f"Joined ad segments into: {ad_segments}")
        return [(int(start * 1000), int(end * 1000)) for start, end in ad_segments]

    def _get_last_segment_if_near_end(
        self,
        ad_segments: list[tuple[float, float]],
        *,
        audio_duration_seconds: float,
        min_separation: float,
    ) -> tuple[float, float] | None:
        if not ad_segments:
            return None
        if (audio_duration_seconds - ad_segments[-1][1]) < min_separation:
            return ad_segments[-1]
        return None

    def _merge_short_episode_edge_segments(
        self,
        ad_segments: list[tuple[float, float]],
        *,
        audio_duration_seconds: float,
        min_length: float,
    ) -> list[tuple[float, float]]:
        if len(ad_segments) < 2:
            return ad_segments

        merged = list(ad_segments)
        leading_duration = merged[0][1] - merged[0][0]
        leading_gap = merged[1][0] - merged[0][1]
        following_duration = merged[1][1] - merged[1][0]
        if (
            merged[0][0] <= EPISODE_EDGE_FRAGMENT_WINDOW_SECONDS
            and leading_duration < min_length
            and following_duration >= MIN_NEIGHBOR_AD_DURATION_FOR_EDGE_MERGE_SECONDS
            and leading_gap <= SHORT_EDGE_FRAGMENT_MERGE_GAP_SECONDS
        ):
            merged[1] = (merged[0][0], merged[1][1])
            merged.pop(0)

        if len(merged) < 2:
            return merged

        trailing_duration = merged[-1][1] - merged[-1][0]
        trailing_gap = merged[-1][0] - merged[-2][1]
        previous_duration = merged[-2][1] - merged[-2][0]
        if (
            audio_duration_seconds - merged[-1][1]
            <= EPISODE_EDGE_FRAGMENT_WINDOW_SECONDS
            and trailing_duration < min_length
            and previous_duration >= MIN_NEIGHBOR_AD_DURATION_FOR_EDGE_MERGE_SECONDS
            and trailing_gap <= SHORT_EDGE_FRAGMENT_MERGE_GAP_SECONDS
        ):
            merged[-2] = (merged[-2][0], merged[-1][1])
            merged.pop()

        return merged

    def _merge_close_segments(
        self,
        ad_segments: list[tuple[float, float]],
        *,
        min_separation: float,
    ) -> list[tuple[float, float]]:
        merged = list(ad_segments)
        i = 0
        while i < len(merged) - 1:
            if merged[i][1] + min_separation >= merged[i + 1][0]:
                merged[i] = (merged[i][0], merged[i + 1][1])
                merged.pop(i + 1)
            else:
                i += 1
        return merged

    def _filter_short_segments(
        self,
        ad_segments: list[tuple[float, float]],
        *,
        min_length: float,
    ) -> list[tuple[float, float]]:
        return [s for s in ad_segments if (s[1] - s[0]) >= min_length]

    def _restore_last_segment_if_needed(
        self,
        ad_segments: list[tuple[float, float]],
        last_segment: tuple[float, float] | None,
    ) -> list[tuple[float, float]]:
        if last_segment is None:
            return ad_segments
        if not ad_segments or ad_segments[-1] != last_segment:
            return [*ad_segments, last_segment]
        return ad_segments

    def _extend_last_segment_to_end_if_needed(
        self,
        ad_segments: list[tuple[float, float]],
        *,
        audio_duration_seconds: float,
        min_separation: float,
    ) -> list[tuple[float, float]]:
        if not ad_segments:
            return ad_segments
        if (audio_duration_seconds - ad_segments[-1][1]) < min_separation:
            return [*ad_segments[:-1], (ad_segments[-1][0], audio_duration_seconds)]
        return ad_segments

    def process_audio(
        self,
        post: Post,
        output_path: str,
        *,
        input_audio_path: str | None = None,
    ) -> list[tuple[int, int]]:
        """
        Process the podcast audio by removing ad segments.

        Args:
            post: The Post object containing the podcast to process
            output_path: Path where the processed audio file should be saved
        Returns:
            The merged ad segments that were removed, as millisecond windows.
        """
        ad_segments = self.get_ad_segments(post)
        source_audio_path = input_audio_path or post.unprocessed_audio_path

        duration_ms = get_audio_duration_ms(source_audio_path)
        if duration_ms is None:
            raise ValueError(
                f"Could not determine duration for audio: {source_audio_path}"
            )

        merged_ad_segments = self.merge_ad_segments(
            duration_ms=duration_ms,
            ad_segments=ad_segments,
            min_ad_segment_length_seconds=float(
                self.config.output.min_ad_segment_length_seconds
            ),
            min_ad_segment_separation_seconds=float(
                self.config.output.min_ad_segement_separation_seconds
            ),
        )

        clip_segments_with_fade(
            in_path=source_audio_path,
            ad_segments_ms=merged_ad_segments,
            fade_ms=self.config.output.fade_ms,
            out_path=output_path,
        )

        processed_duration_ms = get_audio_duration_ms(output_path)
        if processed_duration_ms is None:
            self.logger.warning(
                "Could not determine processed audio duration for post %s at %s; "
                "falling back to source duration",
                post.id,
                output_path,
            )
            processed_duration_ms = duration_ms

        # Persist the final MP3 runtime so downstream RSS/stats reflect ad-removed
        # audio rather than the source episode length.
        post.duration = processed_duration_ms / 1000.0
        post.processed_audio_path = output_path
        result = writer_client.update(
            "Post",
            post.id,
            {"processed_audio_path": output_path, "duration": post.duration},
            wait=True,
        )
        if not result or not result.success:
            raise RuntimeError(getattr(result, "error", "Failed to update post"))
        try:
            self.db_session.expire(post)
        except Exception:  # noqa: BLE001
            pass

        self.logger.info(
            f"Audio processing complete for post {post.id}, saved to {output_path}"
        )
        return merged_ad_segments
