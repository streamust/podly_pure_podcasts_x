import concurrent.futures
import json
import logging
import os
import shutil
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Template
from sqlalchemy import case, func
from sqlalchemy.orm import object_session

from app.extensions import db
from app.models import ModelCall, Post, ProcessingJob, TranscriptSegment
from app.runtime_config import config as runtime_config
from app.writer.client import writer_client
from podcast_processor.ad_classifier import AdClassifier
from podcast_processor.audio import (
    clip_segments_exact,
    get_audio_duration_ms,
    overlay_beeps_with_ducking,
)
from podcast_processor.audio_processor import AudioProcessor
from podcast_processor.chapter_ad_detector import (
    ChapterAdDetector,
    ChapterDetectionError,
)
from podcast_processor.chapter_fallback import (
    generate_chapters_from_transcript,
    generate_topic_chapters_from_transcript_with_llm,
    refine_description_chapters_with_word_refiner,
    refine_generated_chapter_titles_with_llm,
    refine_transcript_chapters_with_word_refiner,
    resolve_llm_path_chapters,
)
from podcast_processor.chapter_filter import parse_filter_strings
from podcast_processor.chapter_writer import (
    fill_chapter_gaps,
    recalculate_chapter_times,
    write_adjusted_chapters,
)
from podcast_processor.ina_client import AudioSegmentResult, analyze_audio
from podcast_processor.podcast_downloader import PodcastDownloader, sanitize_title
from podcast_processor.processing_status_manager import ProcessingStatusManager
from podcast_processor.profanity_filter import (
    DEFAULT_PROFANITY_TERMS,
    PROFANITY_MERGE_GAP_MS,
    extract_profanity_windows,
)
from podcast_processor.prompt import (
    DEFAULT_SYSTEM_PROMPT_PATH,
    DEFAULT_USER_PROMPT_TEMPLATE_PATH,
)
from podcast_processor.transcription_manager import TranscriptionManager
from shared.config import Config
from shared.processing_paths import (
    ProcessingPaths,
    find_existing_processed_audio_path,
    get_job_unprocessed_path,
    get_srv_root,
    paths_from_unprocessed_path,
)

logger = logging.getLogger("global_logger")


def _serialize_chapters_for_output(
    chapters: list[Any], processed_audio_path: str
) -> list[dict[str, Any]]:
    """Return the chapter list serialized for chapter_data, forced to span the
    full processed audio file. Padding chapters to [0, audio_duration_ms] keeps
    UI display, RSS metadata, and MP3 chapter tags in agreement: a chapter set
    that stops short of the file is mis-tagged in some players and confusing in
    the UI ("first chapter starts at 00:02").
    """
    try:
        duration_ms = get_audio_duration_ms(processed_audio_path) or 0
    except Exception:  # noqa: BLE001 - probe is best-effort
        duration_ms = 0
    filled = fill_chapter_gaps(list(chapters), duration_ms)
    return [
        {
            "title": ch.title,
            "start_time": round(ch.start_time_ms / 1000.0, 1),
            "end_time": round(ch.end_time_ms / 1000.0, 1),
        }
        for ch in filled
    ]


@dataclass(frozen=True)
class ProfanityBleepResult:
    audio_path: str | None
    windows_ms: list[tuple[int, int]]


def get_post_processed_audio_path(post: Post) -> ProcessingPaths | None:
    """
    Generate the processed audio path based on the post's unprocessed audio path.
    Returns None if unprocessed_audio_path is not set.
    """
    unprocessed_path = post.unprocessed_audio_path
    if not unprocessed_path or not isinstance(unprocessed_path, str):
        logger.warning(f"Post {post.id} has no unprocessed_audio_path.")
        return None

    title = post.feed.title
    if not title or not isinstance(title, str):
        logger.warning(f"Post {post.id} has no feed title.")
        return None

    return paths_from_unprocessed_path(unprocessed_path, title)


def get_post_processed_audio_path_cached(
    post: Post, feed_title: str
) -> ProcessingPaths | None:
    """
    Generate the processed audio path using cached feed title to avoid ORM access.
    Returns None if unprocessed_audio_path is not set.
    """
    unprocessed_path = post.unprocessed_audio_path
    if not unprocessed_path or not isinstance(unprocessed_path, str):
        logger.warning(f"Post {post.id} has no unprocessed_audio_path.")
        return None

    if not feed_title or not isinstance(feed_title, str):
        logger.warning(f"Post {post.id} has no feed title.")
        return None

    return paths_from_unprocessed_path(unprocessed_path, feed_title)


class PodcastProcessor:
    """
    Main coordinator for podcast processing workflow.
    Delegates to specialized components for transcription, ad classification, and audio processing.
    """

    lock_lock = threading.Lock()
    locks: dict[str, threading.Lock] = {}  # Now keyed by post GUID instead of file path

    def __init__(
        self,
        config: Config,
        logger: logging.Logger | None = None,
        transcription_manager: TranscriptionManager | None = None,
        ad_classifier: AdClassifier | None = None,
        audio_processor: AudioProcessor | None = None,
        status_manager: ProcessingStatusManager | None = None,
        db_session: Any | None = None,
        downloader: PodcastDownloader | None = None,
    ) -> None:
        super().__init__()
        self.logger = logger or logging.getLogger("global_logger")
        self.output_dir = str(get_srv_root())
        self.config: Config = config
        self.db_session = db_session or db.session

        # Initialize downloader
        self.downloader = downloader or PodcastDownloader(logger=self.logger)

        # Initialize status manager
        self.status_manager = status_manager or ProcessingStatusManager(
            self.db_session, self.logger
        )

        # litellm is loaded lazily here (not at module top) so that paths which
        # never use LLM features (e.g. chapter-only) don't pay the ~120 MB
        # litellm import cost.
        import litellm

        litellm.api_base = self.config.openai_base_url
        litellm.api_key = self.config.llm_api_key

        # Initialize components with default implementations if not provided
        if transcription_manager is None:
            self.transcription_manager = TranscriptionManager(self.logger, config)
        else:
            self.transcription_manager = transcription_manager

        if ad_classifier is None:
            self.ad_classifier = AdClassifier(config)
        else:
            self.ad_classifier = ad_classifier

        if audio_processor is None:
            self.audio_processor = AudioProcessor(config=config, logger=self.logger)
        else:
            self.audio_processor = audio_processor

        # In-memory marker: when the zero-ads guard triggers an auto-retry,
        # the current (failed) job is still mid-flight. Its finalization
        # would otherwise delete the unprocessed audio file via
        # ``_remove_unprocessed_audio``, which would force the retry to
        # re-download — dangerous for dynamic-ad-insertion feeds where
        # different bytes arrive each request and the saved transcripts
        # would no longer line up. Holding the post guid here tells
        # ``_remove_unprocessed_audio`` to skip cleanup so the retry can
        # reuse the exact file the transcripts were made from. Cleared
        # after the next ``process()`` call.
        self._suppress_unprocessed_cleanup_for_guid: str | None = None

    def process(  # noqa: PLR0912
        self,
        post: Post,
        job_id: str,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> str:
        """
        Process a podcast by downloading, transcribing, identifying ads, and removing ad segments.
        Updates the existing job record for tracking progress.

        Args:
            post: The Post object containing the podcast to process
            job_id: Job ID of the existing job to update (required)
            cancel_callback: Optional callback to check for cancellation

        Returns:
            Path to the processed audio file
        """
        job = self.db_session.get(ProcessingJob, job_id)
        if not job:
            raise ProcessorException(f"Job with ID {job_id} not found")

        # Cache job and post attributes early to avoid ORM access after expire_all()
        # This includes relationship access like post.feed.title
        cached_post_guid = post.guid
        cached_post_id = post.id
        cached_post_title = post.title
        cached_feed_title = post.feed.title
        cached_job_id = job.id
        cached_current_step = job.current_step
        cached_ad_detection_strategy = getattr(
            post.feed, "ad_detection_strategy", "llm"
        )
        cached_chapter_filter_strings = getattr(
            post.feed, "chapter_filter_strings", None
        )
        cached_enable_llm_chapter_fallback_tagging = (
            self._resolve_llm_chapter_fallback_tagging_enabled(
                getattr(post, "feed", None),
                ad_detection_strategy=cached_ad_detection_strategy,
            )
        )
        cached_enable_profanity_bleeping = self._resolve_profanity_bleeping_enabled(
            getattr(post, "feed", None)
        )

        try:
            self.logger.debug(
                "processor.process enter: job_id=%s post_guid=%s job_bound=%s",
                job_id,
                getattr(post, "guid", None),
                object_session(job) is not None,
            )
            # Update job to running status
            self.status_manager.update_job_status(
                job, "running", 0, "Starting processing"
            )

            # Validate post
            if not post.whitelisted:
                raise ProcessorException(
                    f"Post with GUID {cached_post_guid} not whitelisted"
                )

            # Zero-ads guard auto-retry: when the worker dequeues a job
            # carrying ``auto_retry_attempted=True`` it's a fresh retry for a
            # post whose prior run produced 0 ads + a classifier parse error.
            # Without this cleanup, _check_existing_processed_audio (below)
            # would find the prior run's processed mp3 on disk and exit
            # immediately, no-oping the retry. Cleanup wipes the bad
            # processed file + classifier outputs while preserving the
            # transcripts so Whisper isn't re-billed.
            if getattr(job, "auto_retry_attempted", False):
                self.logger.info(
                    "[ZERO_ADS_GUARD] Job %s is an auto-retry; clearing "
                    "prior classification artifacts before reprocessing.",
                    job.id,
                )
                try:
                    writer_client.action(
                        "prepare_post_for_auto_retry",
                        {"post_id": post.id},
                        wait=True,
                    )
                    self.db_session.refresh(post)
                except Exception:
                    self.logger.exception(
                        "Failed to prepare post %s for auto-retry; "
                        "continuing anyway (early-exit may fire).",
                        post.id,
                    )

            # Check if processed audio already exists (database or disk)
            if self._check_existing_processed_audio(post):
                self.status_manager.update_job_status(
                    job, "completed", job.total_steps or 4, "Processing complete", 100.0
                )
                return str(post.processed_audio_path)

            simulated_path = self._simulate_developer_processing(
                post,
                job,
                cached_post_guid,
                cached_post_title,
                cached_feed_title,
                cached_job_id,
            )
            if simulated_path:
                return simulated_path

            # Step 1: Download (if needed)
            self._handle_download_step(
                post, job, cached_post_guid, cached_post_title, cached_job_id
            )
            self._raise_if_cancelled(job, 1, cancel_callback)

            # Get processing paths and acquire lock
            processed_audio_path = self._acquire_processing_lock(
                post, job, cached_post_guid, cached_job_id, cached_feed_title
            )

            try:
                if os.path.exists(processed_audio_path):
                    self.logger.info(f"Audio already processed: {post}")
                    # Update the database with the processed audio path
                    self._remove_unprocessed_audio(post)
                    result = writer_client.update(
                        "Post",
                        post.id,
                        {
                            "processed_audio_path": processed_audio_path,
                            "unprocessed_audio_path": None,
                        },
                        wait=True,
                    )
                    if not result or not result.success:
                        raise RuntimeError(
                            getattr(result, "error", "Failed to update post")
                        )
                    self.status_manager.update_job_status(
                        job,
                        "completed",
                        job.total_steps or 4,
                        "Processing complete",
                        100.0,
                    )
                    return processed_audio_path

                # Perform the main processing steps
                self._perform_processing_steps(
                    post,
                    job,
                    processed_audio_path,
                    cancel_callback,
                    cached_ad_detection_strategy,
                    cached_chapter_filter_strings,
                    cached_enable_llm_chapter_fallback_tagging,
                    cached_enable_profanity_bleeping,
                )

                self.logger.info(f"Processing podcast: {post} complete")
                self._notify_processing_succeeded(
                    post_guid=cached_post_guid,
                    post_title=cached_post_title,
                    feed_title=cached_feed_title,
                    ad_windows_count=getattr(job, "ad_windows_count", None),
                )
                return processed_audio_path
            finally:
                # Release lock using cached GUID without touching ORM state after potential rollback
                try:
                    if cached_post_guid is not None:
                        lock = PodcastProcessor.locks.get(cached_post_guid)
                        if lock is not None and lock.locked():
                            lock.release()
                except Exception:  # noqa: BLE001
                    # Best-effort lock release; avoid masking original exceptions
                    pass

        except ProcessorException as e:
            error_msg = str(e)
            if "Processing job in progress" in error_msg:
                # Not a genuine failure (a concurrent run holds the lock); update
                # status but don't notify -- it's noise.
                self.status_manager.update_job_status(
                    job,
                    "failed",
                    cached_current_step,
                    "Another processing job is already running for this episode",
                )
            else:
                self.status_manager.update_job_status(
                    job, "failed", cached_current_step, error_msg
                )
                self._notify_processing_failed(
                    post_guid=cached_post_guid,
                    post_id=cached_post_id,
                    post_title=cached_post_title,
                    feed_title=cached_feed_title,
                    step_name=self._step_label(cached_current_step),
                    error_message=error_msg,
                    job_id=cached_job_id,
                )
            raise

        except Exception as e:
            self.logger.error(
                "processor.process unexpected error: job_id=%s %s",
                job_id,
                e,
                exc_info=True,
            )
            self.status_manager.update_job_status(
                job, "failed", cached_current_step, f"Unexpected error: {e!s}"
            )
            self._notify_processing_failed(
                post_guid=cached_post_guid,
                post_id=cached_post_id,
                post_title=cached_post_title,
                feed_title=cached_feed_title,
                step_name=self._step_label(cached_current_step),
                error_message=f"Unexpected error: {e!s}",
                job_id=cached_job_id,
            )
            raise

    @staticmethod
    def _step_label(step: int | None) -> str | None:
        labels = {
            0: "Queued",
            1: "Downloading episode",
            2: "Transcribing audio",
            3: "Identifying ads",
            4: "Processing audio",
        }
        if step is None:
            return None
        return labels.get(step)

    def _notify_processing_failed(
        self,
        *,
        post_guid: str,
        post_id: int | None,
        post_title: str | None,
        feed_title: str | None,
        step_name: str | None,
        error_message: str,
        job_id: str | None,
    ) -> None:
        """Best-effort failure notification via Apprise. Never raises."""
        try:
            from app.notifications import notification_service

            notification_service.notify_processing_failed(
                post_guid=post_guid,
                post_id=post_id,
                post_title=post_title,
                feed_title=feed_title,
                step_name=step_name,
                error_message=error_message,
                job_id=job_id,
            )
        except Exception:
            self.logger.exception(
                "Failed to dispatch processing-failed notification for post %s",
                post_guid,
            )

    def _notify_processing_succeeded(
        self,
        *,
        post_guid: str,
        post_title: str | None,
        feed_title: str | None,
        ad_windows_count: int | None,
    ) -> None:
        """Best-effort success notification via Apprise. Never raises."""
        try:
            from app.notifications import notification_service

            notification_service.notify_processing_succeeded(
                post_guid=post_guid,
                post_title=post_title,
                feed_title=feed_title,
                ad_windows_count=ad_windows_count,
            )
        except Exception:
            self.logger.exception(
                "Failed to dispatch processing-succeeded notification for post %s",
                post_guid,
            )

    def _acquire_processing_lock(
        self,
        post: Post,
        job: ProcessingJob,
        post_guid: str,
        job_id: str,
        feed_title: str,
    ) -> str:
        """
        Acquire processing lock for the post and return the processed audio path.
        Lock is now based on post GUID for better granularity and reliability.

        Args:
            post: The Post object to process
            job: The ProcessingJob for tracking
            post_guid: Cached post GUID to avoid ORM access
            job_id: Cached job ID to avoid ORM access
            feed_title: Cached feed title to avoid ORM access

        Returns:
            Path to the processed audio file

        Raises:
            ProcessorException: If lock cannot be acquired or paths are invalid
        """
        # Get processing paths
        working_paths = get_post_processed_audio_path_cached(post, feed_title)
        if working_paths is None:
            raise ProcessorException("Processed audio path not found")

        processed_audio_path = str(working_paths.post_processed_audio_path)

        # Use post GUID as lock key instead of file path for better granularity
        lock_key = post_guid

        # Acquire lock (this is where we cancel existing jobs if we can get the lock)
        locked = False
        with PodcastProcessor.lock_lock:
            if lock_key not in PodcastProcessor.locks:
                PodcastProcessor.locks[lock_key] = threading.Lock()
                PodcastProcessor.locks[lock_key].acquire(blocking=False)
                locked = True

        if not locked and not PodcastProcessor.locks[lock_key].acquire(blocking=False):
            raise ProcessorException("Processing job in progress")

        # Cancel existing jobs since we got the lock
        self.status_manager.cancel_existing_jobs(post_guid, job_id)

        self.make_dirs(working_paths)
        return processed_audio_path

    def _ina_enabled(self) -> bool:
        raw_value = os.environ.get("INA_ENABLED", "")
        return raw_value.strip().lower() in {"1", "true", "yes", "on"}

    def _ina_base_url(self) -> str | None:
        raw_value = os.environ.get("INA_BASE_URL")
        if raw_value is None:
            return None
        base_url = raw_value.strip()
        return base_url or None

    def _ina_timeout_sec(self) -> int:
        raw_value = os.environ.get("INA_TIMEOUT_SEC")
        if raw_value is None:
            return 3600
        try:
            return max(1, int(raw_value))
        except TypeError, ValueError:
            self.logger.warning(
                "Invalid INA_TIMEOUT_SEC=%r; defaulting to 3600 seconds",
                raw_value,
            )
            return 3600

    def _start_optional_ina_analysis(
        self,
        post: Post,
    ) -> tuple[
        concurrent.futures.ThreadPoolExecutor | None,
        concurrent.futures.Future[list[AudioSegmentResult]] | None,
    ]:
        unprocessed_audio_path = getattr(post, "unprocessed_audio_path", None)
        if not self._ina_enabled():
            return None, None
        if not unprocessed_audio_path or not isinstance(unprocessed_audio_path, str):
            return None, None
        if not self._ina_base_url():
            self.logger.warning(
                "INA_ENABLED is true but INA_BASE_URL is not set; skipping INA analysis"
            )
            return None, None

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            self._run_ina_analysis,
            int(post.id),
            unprocessed_audio_path,
        )
        return executor, future

    def _await_optional_ina_analysis(
        self,
        executor: concurrent.futures.ThreadPoolExecutor | None,
        future: concurrent.futures.Future[list[AudioSegmentResult]] | None,
    ) -> None:
        if future is None:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=False)
            return

        try:
            future.result(timeout=self._ina_timeout_sec() + 30)
        except Exception as exc:
            self.logger.warning("INA analysis failed: %s", exc, exc_info=True)
        finally:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=False)

    def _run_ina_analysis(
        self,
        post_id: int,
        audio_path: str,
    ) -> list[AudioSegmentResult]:
        self.logger.info("[INA] Starting INA analysis for post %s", post_id)
        model_call_id: int | None = None
        try:
            # Clear any stale ina rows from a prior run before upserting. The
            # final UPDATE to set last_segment_sequence_num=len(results)-1
            # would otherwise collide with a previous run's identically-keyed
            # ModelCall row (unique on post_id+first+last+model_name).
            writer_client.action(
                "delete_model_calls_for_post_by_model_name",
                {
                    "post_id": post_id,
                    "model_name": "ina:speech_music_noise",
                },
                wait=True,
            )

            upsert_res = writer_client.action(
                "upsert_model_call",
                {
                    "post_id": post_id,
                    "model_name": "ina:speech_music_noise",
                    "first_segment_sequence_num": 0,
                    "last_segment_sequence_num": 0,
                    "prompt": "INA speech segmenter analysis",
                },
                wait=True,
            )
            if upsert_res and upsert_res.success:
                model_call_id = (upsert_res.data or {}).get("model_call_id")

            base_url = self._ina_base_url()
            if base_url is None:
                raise RuntimeError(
                    "INA_BASE_URL is required when INA analysis is enabled"
                )

            results, raw_response = analyze_audio(
                audio_path=audio_path,
                base_url=base_url,
                timeout=self._ina_timeout_sec(),
            )

            write_res = writer_client.action(
                "replace_audio_segments",
                {
                    "post_id": post_id,
                    "segments": [
                        {
                            "label": result.label,
                            "start_time": result.start_time,
                            "end_time": result.end_time,
                        }
                        for result in results
                    ],
                    "model_call_id": model_call_id,
                },
                wait=True,
            )
            if not write_res or not write_res.success:
                raise RuntimeError(
                    getattr(write_res, "error", "Failed to persist INA audio segments")
                )

            if model_call_id is not None:
                update_res = writer_client.update(
                    "ModelCall",
                    int(model_call_id),
                    {
                        "status": "success",
                        "response": raw_response,
                        "error_message": None,
                        "first_segment_sequence_num": 0,
                        "last_segment_sequence_num": max(len(results) - 1, 0),
                    },
                    wait=True,
                )
                # Silent writer failures would leave the placeholder row
                # stuck at status="pending" forever; surface them so the
                # outer except branch can mark the call failed_permanent.
                if not update_res or not update_res.success:
                    raise RuntimeError(
                        getattr(
                            update_res,
                            "error",
                            "Failed to finalize INA ModelCall update",
                        )
                    )

            self.logger.info(
                "[INA] INA analysis complete for post %s: %s segments",
                post_id,
                len(results),
            )
            return results
        except Exception as exc:
            if model_call_id is not None:
                try:
                    writer_client.action(
                        "mark_model_call_failed",
                        {
                            "model_call_id": int(model_call_id),
                            "error_message": str(exc),
                            "status": "failed_permanent",
                        },
                        wait=True,
                    )
                except Exception:
                    self.logger.warning(
                        "[INA] Failed to mark model call %s as failed",
                        model_call_id,
                        exc_info=True,
                    )
            raise

    def _perform_processing_steps(
        self,
        post: Post,
        job: ProcessingJob,
        processed_audio_path: str,
        cancel_callback: Callable[[], bool] | None = None,
        ad_detection_strategy: str = "llm",
        chapter_filter_strings: str | None = None,
        enable_llm_chapter_fallback_tagging: bool | None = None,
        enable_profanity_bleeping: bool = False,
    ) -> None:
        """
        Perform the main processing steps based on the ad detection strategy.

        Args:
            post: The Post object to process
            job: The ProcessingJob for tracking
            processed_audio_path: Path where the processed audio will be saved
            cancel_callback: Optional callback to check for cancellation
            ad_detection_strategy: "llm", "chapter", or "chapter_insert"
            chapter_filter_strings: Comma-separated filter strings for chapter strategy
        """
        if enable_profanity_bleeping and ad_detection_strategy == "chapter":
            raise ProcessorException(
                "Profanity bleeping requires transcripts and is not supported for "
                "chapter-based ad removal"
            )

        ina_executor, ina_future = self._start_optional_ina_analysis(post)
        try:
            if ad_detection_strategy == "chapter":
                self._perform_chapter_based_processing(
                    post,
                    job,
                    processed_audio_path,
                    cancel_callback,
                    chapter_filter_strings,
                )
            elif ad_detection_strategy == "chapter_insert":
                if enable_profanity_bleeping:
                    self._perform_chapter_insertion_only_processing(
                        post,
                        job,
                        processed_audio_path,
                        cancel_callback,
                        enable_profanity_bleeping,
                    )
                else:
                    self._perform_chapter_insertion_only_processing(
                        post,
                        job,
                        processed_audio_path,
                        cancel_callback,
                    )
            else:
                self._perform_llm_based_processing(
                    post,
                    job,
                    processed_audio_path,
                    cancel_callback,
                    enable_llm_chapter_fallback_tagging,
                    enable_profanity_bleeping,
                )
        finally:
            self._await_optional_ina_analysis(ina_executor, ina_future)

    def _resolve_llm_chapter_fallback_tagging_enabled(
        self,
        feed: Any | None,
        *,
        ad_detection_strategy: str,
    ) -> bool:
        if ad_detection_strategy == "chapter_insert":
            return True

        feed_override = (
            getattr(feed, "enable_llm_chapter_fallback_tagging", None)
            if feed is not None
            else None
        )
        if feed_override is not None:
            return bool(feed_override)

        return bool(getattr(self.config, "enable_llm_chapter_fallback_tagging", False))

    def _resolve_profanity_bleeping_enabled(self, feed: Any | None) -> bool:
        if feed is None:
            return False
        return bool(getattr(feed, "enable_profanity_bleeping", False))

    def _resolve_chapter_full_block_text(self, feed: Any | None) -> bool:
        """Per-feed override wins when set; otherwise the global setting."""
        feed_override = (
            getattr(feed, "chapter_full_block_text", None) if feed is not None else None
        )
        if feed_override is not None:
            return bool(feed_override)
        return bool(getattr(self.config, "chapter_full_block_text", False))

    def _word_level_boundary_refiner_enabled(self) -> bool:
        return bool(
            getattr(self.config, "enable_boundary_refinement", False)
            and getattr(self.config, "enable_word_level_boundary_refinder", False)
        )

    def _load_words_by_sequence(
        self,
        post: Post | None,
        rich_transcript_segments: list[Any] | None,
    ) -> dict[int, list[Any]]:
        """Build a ``{sequence_num: [WordTimestamp, ...]}`` map for chapter
        word-boundary refinement.

        Prefers the in-memory rich segments (freshest) and falls back to the
        word timestamps persisted on the post. Returns an empty dict when no
        word timestamps are available, in which case the LLM chapter refiner
        degrades to its non-exact path.
        """
        from podcast_processor.transcribe import (
            load_word_timestamps_by_sequence,
            serialize_segment_word_timestamps,
        )

        if rich_transcript_segments:
            try:
                payload = serialize_segment_word_timestamps(rich_transcript_segments)
            except Exception:  # noqa: BLE001
                payload = None
            if payload:
                return load_word_timestamps_by_sequence(payload)

        raw_payload = (
            getattr(post, "transcript_word_timestamps", None) if post else None
        )
        return load_word_timestamps_by_sequence(raw_payload)

    def _has_saved_transcript_word_timestamps(self, post: Post | None) -> bool:
        if post is None:
            return False

        raw_payload = getattr(post, "transcript_word_timestamps", None)
        if not isinstance(raw_payload, list):
            return False

        for segment_payload in raw_payload:
            if not isinstance(segment_payload, dict):
                continue
            words = segment_payload.get("words")
            if isinstance(words, list) and words:
                return True

        return False

    def _prepare_profanity_bleeped_audio(
        self,
        *,
        source_audio_path: str | None,
        processed_audio_path: str,
        rich_transcript_segments: list[Any] | None,
        saved_bleep_windows_ms: list[tuple[int, int]] | None = None,
        enable_profanity_bleeping: bool,
        use_vbr: bool = False,
    ) -> ProfanityBleepResult:
        if not enable_profanity_bleeping:
            return ProfanityBleepResult(source_audio_path, [])
        if not source_audio_path:
            raise ProcessorException(
                "No unprocessed audio available for profanity bleeping"
            )

        windows_ms = list(saved_bleep_windows_ms or [])
        if not windows_ms and saved_bleep_windows_ms is None:
            runtime_whisper = getattr(runtime_config, "whisper", None)
            if getattr(runtime_whisper, "whisper_type", None) != "remote":
                raise ProcessorException(
                    "Profanity bleeping currently requires remote transcription with "
                    "a WhisperX-compatible endpoint"
                )

            output_config = getattr(self.config, "output", None)
            pad_start_ms = int(
                getattr(output_config, "bleep_padding_start_ms", 150) or 0
            )
            pad_end_ms = int(getattr(output_config, "bleep_padding_end_ms", 150) or 0)
            windows_ms, saw_word_timestamps = self._extract_profanity_windows(
                rich_transcript_segments or [],
                pad_start_ms=pad_start_ms,
                pad_end_ms=pad_end_ms,
            )
            if not saw_word_timestamps:
                raise ProcessorException(
                    "Profanity bleeping requires WhisperX word timestamps, but the "
                    "current transcription response did not include them"
                )

        if not windows_ms:
            return ProfanityBleepResult(source_audio_path, [])

        temp_dir = str(Path(processed_audio_path).parent)
        with tempfile.NamedTemporaryFile(
            suffix=".mp3",
            prefix="bleeped_source_",
            delete=False,
            dir=temp_dir,
        ) as temp_file:
            temp_path = temp_file.name

        overlay_beeps_with_ducking(
            windows_ms,
            source_audio_path,
            temp_path,
            use_vbr=use_vbr,
        )
        return ProfanityBleepResult(temp_path, windows_ms)

    def _extract_profanity_windows(
        self,
        rich_transcript_segments: list[Any],
        *,
        pad_start_ms: int,
        pad_end_ms: int,
    ) -> tuple[list[tuple[int, int]], bool]:
        """Run profanity-window extraction. Tries Rust sidecar first, then falls
        back to the Python implementation.
        """
        from shared.rust_sidecar import (
            rust_profanity_enabled,
            try_extract_profanity_windows,
        )

        # Flatten word list and detect whether word timestamps exist. Both the
        # Rust path (which receives flat words) and the Python fallback need
        # this signal, so compute it once here regardless of which path runs.
        words: list[dict[str, Any]] = []
        saw_word_timestamps = False
        for segment in rich_transcript_segments:
            seg_words = getattr(segment, "words", None) or []
            if seg_words:
                saw_word_timestamps = True
            for w in seg_words:
                word_text = getattr(w, "word", None)
                start = getattr(w, "start", None)
                end = getattr(w, "end", None)
                if word_text is None or start is None or end is None:
                    continue
                words.append(
                    {
                        "word": str(word_text),
                        "start": float(start),
                        "end": float(end),
                    }
                )

        if rust_profanity_enabled() and saw_word_timestamps:
            rust_windows = try_extract_profanity_windows(
                words=words,
                profanity_terms=sorted(DEFAULT_PROFANITY_TERMS),
                pad_start_ms=pad_start_ms,
                pad_end_ms=pad_end_ms,
                merge_gap_ms=PROFANITY_MERGE_GAP_MS,
            )
            if rust_windows is not None:
                return rust_windows, saw_word_timestamps

        return extract_profanity_windows(
            rich_transcript_segments,
            pad_start_ms=pad_start_ms,
            pad_end_ms=pad_end_ms,
        )

    def _serialize_bleep_windows(
        self, windows_ms: list[tuple[int, int]]
    ) -> list[dict[str, float]]:
        return [
            {
                "start_time": round(start_ms / 1000.0, 3),
                "end_time": round(end_ms / 1000.0, 3),
            }
            for start_ms, end_ms in windows_ms
            if end_ms > start_ms
        ]

    def _load_saved_bleep_windows(
        self, post: Post | None
    ) -> tuple[bool, list[tuple[int, int]]]:
        if post is None:
            return False, []

        raw_windows = getattr(post, "bleep_windows", None)
        if raw_windows is None:
            return False, []
        if not isinstance(raw_windows, list):
            return True, []

        windows_ms: list[tuple[int, int]] = []
        for item in raw_windows:
            if not isinstance(item, dict):
                continue

            start_raw = item.get("start_time")
            end_raw = item.get("end_time")
            if start_raw is None or end_raw is None:
                continue

            try:
                start_ms = max(0, round(float(start_raw) * 1000.0))
                end_ms = max(start_ms, round(float(end_raw) * 1000.0))
            except Exception:  # noqa: BLE001
                continue

            if end_ms > start_ms:
                windows_ms.append((start_ms, end_ms))

        return True, windows_ms

    def _cleanup_temp_audio_path(
        self,
        temp_audio_path: str | None,
        *,
        original_audio_path: str | None,
    ) -> None:
        if (
            not temp_audio_path
            or temp_audio_path == original_audio_path
            or not os.path.exists(temp_audio_path)
        ):
            return

        try:
            os.remove(temp_audio_path)
        except OSError:
            self.logger.warning(
                "Failed to remove temporary audio file %s",
                temp_audio_path,
                exc_info=True,
            )

    def _evaluate_zero_ads_guard(
        self,
        post: Post,
        job: ProcessingJob,
        *,
        ad_windows_count: int,
        had_classification_parse_error: bool,
    ) -> None:
        """Record the run's final ad-window count and react to a zero-ads
        outcome.

        Always:
          - persists ``ad_windows_count`` on the job (visible to the UI)
          - persists ``had_classification_parse_error`` on the job when set
          - logs a WARNING when the LLM run produced zero ad windows

        Conditional (LLM run, zero ads, parse error, setting enabled, no
        prior retry): marks ``auto_retry_attempted`` on the current job and
        enqueues a fresh pending job for the same post. The worker picks it
        up on the next tick; the prior job stays in place (with the badge)
        so the operator can see the failure context.
        """
        try:
            writer_client.action(
                "record_ad_windows_count",
                {"job_id": job.id, "count": ad_windows_count},
                wait=True,
            )
        except Exception:
            self.logger.exception(
                "Failed to record ad_windows_count=%s for job %s",
                ad_windows_count,
                getattr(job, "id", None),
            )

        if had_classification_parse_error:
            try:
                writer_client.action(
                    "mark_classification_parse_error",
                    {"job_id": job.id},
                    wait=True,
                )
            except Exception:
                self.logger.exception(
                    "Failed to mark had_classification_parse_error for job %s",
                    getattr(job, "id", None),
                )

        if ad_windows_count > 0:
            return

        feed = getattr(post, "feed", None)
        strategy = getattr(feed, "ad_detection_strategy", "llm") or "llm"
        # The chapter strategies legitimately produce zero windows for
        # episodes that have no advertisement chapters tagged. The guard is
        # only meaningful for the LLM strategy where zero usually means the
        # model fumbled.
        if strategy != "llm":
            return

        self.logger.warning(
            "[ZERO_ADS_GUARD] Post %s (job %s) completed with 0 ad windows "
            "(had_parse_error=%s, auto_retry_attempted=%s). Review whether "
            "this is a genuine ad-free episode or a classification miss.",
            getattr(post, "id", None),
            getattr(job, "id", None),
            had_classification_parse_error,
            getattr(job, "auto_retry_attempted", False),
        )

        if not had_classification_parse_error:
            return

        if getattr(job, "auto_retry_attempted", False):
            self.logger.info(
                "[ZERO_ADS_GUARD] Skipping auto-retry for post %s: a prior "
                "retry has already been attempted.",
                getattr(post, "id", None),
            )
            return

        output_cfg = getattr(self.config, "output", None)
        auto_retry_enabled = bool(
            getattr(output_cfg, "auto_retry_zero_ads_on_parse_error", False)
        )
        if not auto_retry_enabled:
            self.logger.info(
                "[ZERO_ADS_GUARD] Auto-retry disabled in settings; leaving "
                "post %s as-is (set output.auto_retry_zero_ads_on_parse_error "
                "to true to enable).",
                getattr(post, "id", None),
            )
            return

        self.logger.warning(
            "[ZERO_ADS_GUARD] Auto-requeuing post %s once due to "
            "classification parse error + zero ad windows.",
            getattr(post, "id", None),
        )
        # Hold the post guid so this run's finalization doesn't delete the
        # unprocessed audio file before the retry job can reuse it. Vital
        # for DAI feeds (Megaphone et al.) where re-downloading would yield
        # different bytes and break transcript alignment.
        self._suppress_unprocessed_cleanup_for_guid = post.guid
        try:
            writer_client.action(
                "mark_auto_retry_attempted",
                {"job_id": job.id},
                wait=True,
            )
            writer_client.action(
                "create_job",
                {
                    "job_data": {
                        "post_guid": post.guid,
                        "status": "pending",
                        "current_step": 0,
                        "total_steps": 4,
                        "progress_percentage": 0.0,
                        "step_name": "Queued (auto-retry)",
                        "jobs_manager_run_id": getattr(
                            job, "jobs_manager_run_id", None
                        ),
                        # Propagate the guard onto the *retry* job too. If
                        # the retry also produces zero ads + parse error,
                        # its own evaluation sees this flag and skips
                        # enqueueing yet another retry. Without this, the
                        # flag only lives on the failed job we just marked
                        # and a malformed retry could loop indefinitely.
                        "auto_retry_attempted": True,
                    }
                },
                wait=True,
            )
        except Exception:
            self.logger.exception(
                "Failed to enqueue zero-ads auto-retry for post %s",
                getattr(post, "id", None),
            )

    def _make_transcribe_progress_callback(
        self,
        job: ProcessingJob,
        *,
        step: int,
        label: str,
        progress_base: float,
        progress_span: float = 25.0,
    ) -> Callable[[int, int], None]:
        """Build a chunk-progress callback that updates the job's
        ``step_name`` and ``progress_percentage`` after each whisper chunk
        completes.

        ``progress_base`` is the percentage to display before the first chunk
        finishes (matches the value passed to the initial update_job_status
        call). ``progress_span`` is how much of the bar this transcription
        owns — defaults to 25% (one full stage).
        """

        def _on_chunk(chunks_done: int, total_chunks: int) -> None:
            if total_chunks <= 0:
                return
            done = max(0, min(chunks_done, total_chunks))
            sub_label = (
                label if total_chunks <= 1 else f"{label} (chunk {done}/{total_chunks})"
            )
            progress = progress_base + progress_span * (done / total_chunks)
            try:
                self.status_manager.update_job_status(
                    job, "running", step, sub_label, progress
                )
            except Exception:
                # Sub-progress reporting must never break the actual job.
                self.logger.exception(
                    "Failed to publish transcription chunk progress for job %s",
                    getattr(job, "id", None),
                )

        return _on_chunk

    def _transcribe_for_processing(
        self,
        post: Post,
        *,
        include_word_timestamps: bool,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> tuple[list[Any], list[Any] | None]:
        transcribe_for_processing = getattr(
            self.transcription_manager,
            "transcribe_for_processing",
            None,
        )
        if callable(transcribe_for_processing):
            result = transcribe_for_processing(
                post,
                include_word_timestamps=include_word_timestamps,
                progress_callback=progress_callback,
            )
            if isinstance(result, tuple) and len(result) == 2:
                return result

        return (
            self.transcription_manager.transcribe(
                post, progress_callback=progress_callback
            ),
            None,
        )

    def _perform_llm_based_processing(
        self,
        post: Post,
        job: ProcessingJob,
        processed_audio_path: str,
        cancel_callback: Callable[[], bool] | None = None,
        enable_llm_chapter_fallback_tagging: bool | None = None,
        enable_profanity_bleeping: bool = False,
    ) -> None:
        """
        Perform LLM-based ad detection: transcription, classification, and audio processing.
        """
        needs_word_timestamps_for_refiner = self._word_level_boundary_refiner_enabled()
        has_saved_transcript_word_timestamps = (
            self._has_saved_transcript_word_timestamps(
                post if needs_word_timestamps_for_refiner else None
            )
        )
        has_saved_bleep_windows, saved_bleep_windows_ms = (
            self._load_saved_bleep_windows(post if enable_profanity_bleeping else None)
        )
        # Step 2: Transcribe audio. The LLM pipeline has a dedicated
        # chapter-generation step (5), so declare the larger step count up
        # front — other strategies keep the default 4.
        self.status_manager.update_job_status(
            job, "running", 2, "Transcribing audio", 50.0, total_steps=5
        )
        transcribe_progress = self._make_transcribe_progress_callback(
            job, step=2, label="Transcribing audio", progress_base=50.0
        )
        transcript_segments, rich_transcript_segments = self._transcribe_for_processing(
            post,
            progress_callback=transcribe_progress,
            include_word_timestamps=(
                (enable_profanity_bleeping and not has_saved_bleep_windows)
                or (
                    needs_word_timestamps_for_refiner
                    and not has_saved_transcript_word_timestamps
                )
            ),
        )
        self._raise_if_cancelled(job, 2, cancel_callback)
        unprocessed_audio_path = (
            str(post.unprocessed_audio_path) if post.unprocessed_audio_path else None
        )
        post_description = post.description

        # Step 3: Classify ad segments
        self._classify_ad_segments(post, job, transcript_segments)
        self._raise_if_cancelled(job, 3, cancel_callback)

        # Fail the job if every LLM classification call failed. Only calls for
        # the configured classifier model count; Whisper, INA, and later
        # chapter/refinement calls are not classification attempts. Without a
        # successful
        # classification call there are no identifications, so the episode would
        # be "completed" with zero ads removed — silently wrong.
        call_counts = self._get_llm_classification_model_call_counts(int(post.id))
        if call_counts is None:
            self.logger.debug(
                "Skipping LLM classification model call status check for post %s "
                "because no processor database session is available.",
                post.id,
            )
        elif call_counts.total == 0:
            self.logger.debug(
                "No LLM classification model calls recorded for post %s — "
                "no segments to classify or classification was skipped.",
                post.id,
            )
        elif call_counts.successful == 0:
            latest_error = self._get_latest_llm_classification_error(int(post.id))
            error_suffix = (
                f" Latest provider error: {latest_error}"
                if latest_error
                else " Check the model-call details for the provider error."
            )
            raise ProcessorException(
                f"LLM classification failed: all {call_counts.total} model call(s) were "
                f"unsuccessful.{error_suffix}"
            )

        # Step 4: Process audio (remove ad segments)
        self.status_manager.update_job_status(
            job, "running", 4, "Processing audio", 90.0
        )
        profanity_bleep_result = self._prepare_profanity_bleeped_audio(
            source_audio_path=unprocessed_audio_path,
            processed_audio_path=processed_audio_path,
            rich_transcript_segments=rich_transcript_segments,
            saved_bleep_windows_ms=(
                saved_bleep_windows_ms if has_saved_bleep_windows else None
            ),
            enable_profanity_bleeping=enable_profanity_bleeping,
        )
        profanity_source_audio_path = profanity_bleep_result.audio_path
        bleep_windows = (
            self._serialize_bleep_windows(profanity_bleep_result.windows_ms)
            if enable_profanity_bleeping
            else None
        )
        try:
            if profanity_source_audio_path == unprocessed_audio_path:
                removed_segments_ms = self.audio_processor.process_audio(
                    post,
                    processed_audio_path,
                )
            else:
                removed_segments_ms = self.audio_processor.process_audio(
                    post,
                    processed_audio_path,
                    input_audio_path=profanity_source_audio_path,
                )
        finally:
            self._cleanup_temp_audio_path(
                profanity_source_audio_path,
                original_audio_path=unprocessed_audio_path,
            )
        removed_segments_sec = [
            (start_ms / 1000.0, end_ms / 1000.0)
            for start_ms, end_ms in removed_segments_ms
        ]

        # Zero-ads guard: the LLM strategy occasionally yields zero ad
        # windows because the model returned malformed JSON for a batch
        # (parse error swallowed in ad_classifier) rather than because the
        # episode is genuinely ad-free. Record the count + parse-error flag
        # so the UI can badge the run, and optionally requeue once.
        ad_classifier = getattr(self, "ad_classifier", None)
        self._evaluate_zero_ads_guard(
            post,
            job,
            ad_windows_count=len(removed_segments_ms),
            had_classification_parse_error=bool(
                getattr(ad_classifier, "had_parse_error", False)
            ),
        )

        chapters_for_output = []
        chapter_source = "none"
        chapter_fallback_enabled = (
            bool(enable_llm_chapter_fallback_tagging)
            if enable_llm_chapter_fallback_tagging is not None
            else bool(
                getattr(self.config, "enable_llm_chapter_fallback_tagging", False)
            )
        )
        if chapter_fallback_enabled:
            # Step 5: chapter generation is its own stage — it runs LLM calls
            # (topic plan, title refinement) and must not masquerade as the
            # audio-processing step it used to be buried in.
            self.status_manager.update_job_status(
                job, "running", 5, "Generating chapters", 95.0
            )
            chapters_for_output, chapter_source = resolve_llm_path_chapters(
                unprocessed_audio_path=unprocessed_audio_path,
                description=post_description,
                transcript_segments=transcript_segments,
                logger_override=self.logger,
            )
            word_refinement_active = self._word_level_boundary_refiner_enabled()
            words_by_sequence = (
                self._load_words_by_sequence(post, rich_transcript_segments)
                if word_refinement_active
                else None
            )
            if chapter_source == "transcript" and chapters_for_output:
                transcript_segments_for_chapters = (
                    self._filter_transcript_segments_for_chapters(
                        transcript_segments, removed_segments_ms
                    )
                )
                if not transcript_segments_for_chapters:
                    self.logger.warning(
                        "All transcript segments overlap removed ad windows for post "
                        "%s; retaining original transcript-derived chapters",
                        post.id,
                    )
                    transcript_segments_for_chapters = transcript_segments

                chapters_for_output = self._refine_transcript_sourced_chapters(
                    chapters_for_output=chapters_for_output,
                    transcript_segments=transcript_segments_for_chapters,
                    post_id=post.id,
                    post_guid=post.guid,
                    # Rust must re-derive the ad-filtered segment set from the
                    # DB, so it needs the removed windows that produced
                    # `transcript_segments_for_chapters`.
                    removed_windows_ms=removed_segments_ms,
                    words_by_sequence=words_by_sequence,
                    full_block_text=self._resolve_chapter_full_block_text(
                        getattr(post, "feed", None)
                    ),
                )
            elif (
                chapter_source == "description"
                and chapters_for_output
                and word_refinement_active
            ):
                chapters_for_output = self._refine_description_sourced_chapters(
                    chapters_for_output=chapters_for_output,
                    transcript_segments=transcript_segments,
                    post_id=post.id,
                    post_guid=post.guid,
                    words_by_sequence=words_by_sequence,
                )
            if chapters_for_output:
                self.logger.info(
                    "LLM path chapter fallback resolved %d chapters via %s",
                    len(chapters_for_output),
                    chapter_source,
                )

        chapter_data_json: str | None = None
        if chapters_for_output:
            write_adjusted_chapters(
                audio_path=processed_audio_path,
                chapters_to_keep=chapters_for_output,
                removed_segments=removed_segments_sec,
            )
            adjusted_chapters = recalculate_chapter_times(
                chapters_for_output, removed_segments_sec
            )
            chapter_data_json = json.dumps(
                {
                    "chapter_source": chapter_source,
                    "chapters_for_output": _serialize_chapters_for_output(
                        adjusted_chapters, processed_audio_path
                    ),
                }
            )

        self._finalize_processing(
            post,
            job,
            processed_audio_path,
            chapter_data=chapter_data_json,
            bleep_windows=bleep_windows,
        )

    def _get_llm_classification_model_call_counts(self, post_id: int) -> Any | None:
        session = getattr(self, "db_session", None)
        if session is None:
            return None
        classifier_model = str(getattr(self.config, "llm_model", "") or "")

        return (
            session.query(
                func.count(ModelCall.id).label("total"),
                func.count(case((ModelCall.status == "success", 1))).label(
                    "successful"
                ),
            )
            .filter(
                ModelCall.post_id == post_id,
                ModelCall.model_name == classifier_model,
            )
            .one()
        )

    def _get_latest_llm_classification_error(self, post_id: int) -> str | None:
        session = getattr(self, "db_session", None)
        if session is None:
            return None
        classifier_model = str(getattr(self.config, "llm_model", "") or "")
        row = (
            session.query(ModelCall.error_message)
            .filter(
                ModelCall.post_id == post_id,
                ModelCall.model_name == classifier_model,
                ModelCall.status != "success",
                ModelCall.error_message.isnot(None),
            )
            .order_by(ModelCall.timestamp.desc(), ModelCall.id.desc())
            .first()
        )
        if row is None or not row[0]:
            return None
        return str(row[0])

    def _perform_chapter_insertion_only_processing(
        self,
        post: Post,
        job: ProcessingJob,
        processed_audio_path: str,
        cancel_callback: Callable[[], bool] | None = None,
        enable_profanity_bleeping: bool = False,
    ) -> None:
        """
        Resolve and write chapters without ad detection or ad removal.
        """
        unprocessed_audio_path = (
            str(post.unprocessed_audio_path) if post.unprocessed_audio_path else None
        )
        if not unprocessed_audio_path:
            raise ProcessorException(
                "No unprocessed audio available for chapter insert"
            )

        post_description = post.description
        transcript_segments: list[Any] = []
        rich_transcript_segments: list[Any] | None = None
        has_saved_bleep_windows, saved_bleep_windows_ms = (
            self._load_saved_bleep_windows(post if enable_profanity_bleeping else None)
        )

        # First attempt chapter resolution without transcription
        self.status_manager.update_job_status(
            job, "running", 2, "Resolving chapters", 50.0
        )
        chapters_for_output, chapter_source = resolve_llm_path_chapters(
            unprocessed_audio_path=unprocessed_audio_path,
            description=post_description,
            transcript_segments=transcript_segments,
            logger_override=self.logger,
        )
        self._raise_if_cancelled(job, 2, cancel_callback)

        # Word-level chapter refinement needs a transcript even when chapters
        # came from the description, so force transcription in that case.
        word_refinement_active = self._word_level_boundary_refiner_enabled()
        needs_transcription_for_description = (
            word_refinement_active and chapter_source == "description"
        )

        # Only transcribe if we still need transcript-based fallback chapters,
        # profanity bleeping, or word-level chapter refinement.
        if (
            chapter_source == "none"
            or enable_profanity_bleeping
            or needs_transcription_for_description
        ):
            if chapter_source == "none":
                fallback_label = "Transcribing audio for chapter generation"
            elif enable_profanity_bleeping:
                fallback_label = "Transcribing audio for profanity bleeping"
            else:
                fallback_label = "Transcribing audio for chapter refinement"
            self.status_manager.update_job_status(
                job,
                "running",
                3,
                fallback_label,
                75.0,
            )
            fallback_progress = self._make_transcribe_progress_callback(
                job, step=3, label=fallback_label, progress_base=75.0
            )
            (
                transcript_segments,
                rich_transcript_segments,
            ) = self._transcribe_for_processing(
                post,
                include_word_timestamps=(
                    (enable_profanity_bleeping and not has_saved_bleep_windows)
                    or word_refinement_active
                ),
                progress_callback=fallback_progress,
            )
            self._raise_if_cancelled(job, 3, cancel_callback)

            if chapter_source == "none":
                chapters_for_output, chapter_source = resolve_llm_path_chapters(
                    unprocessed_audio_path=unprocessed_audio_path,
                    description=post_description,
                    transcript_segments=transcript_segments,
                    logger_override=self.logger,
                )
        else:
            self.status_manager.update_job_status(
                job, "running", 3, "Chapters resolved", 75.0
            )
            self._raise_if_cancelled(job, 3, cancel_callback)

        words_by_sequence = (
            self._load_words_by_sequence(post, rich_transcript_segments)
            if word_refinement_active
            else None
        )
        if (
            chapter_source == "transcript"
            and chapters_for_output
            and transcript_segments
        ):
            chapters_for_output = self._refine_transcript_sourced_chapters(
                chapters_for_output=chapters_for_output,
                transcript_segments=transcript_segments,
                post_id=post.id,
                # Only the unfiltered path is safe to delegate to Rust — the
                # other call site uses ad-window-filtered segments that Rust
                # cannot reconstruct from the DB alone.
                post_guid=post.guid,
                words_by_sequence=words_by_sequence,
                full_block_text=self._resolve_chapter_full_block_text(
                    getattr(post, "feed", None)
                ),
            )
        elif (
            chapter_source == "description"
            and chapters_for_output
            and transcript_segments
            and word_refinement_active
        ):
            chapters_for_output = self._refine_description_sourced_chapters(
                chapters_for_output=chapters_for_output,
                transcript_segments=transcript_segments,
                post_id=post.id,
                post_guid=post.guid,
                words_by_sequence=words_by_sequence,
            )

        self.status_manager.update_job_status(
            job, "running", 4, "Copying audio and writing chapters", 90.0
        )
        profanity_bleep_result = self._prepare_profanity_bleeped_audio(
            source_audio_path=unprocessed_audio_path,
            processed_audio_path=processed_audio_path,
            rich_transcript_segments=rich_transcript_segments,
            saved_bleep_windows_ms=(
                saved_bleep_windows_ms if has_saved_bleep_windows else None
            ),
            enable_profanity_bleeping=enable_profanity_bleeping,
            use_vbr=False,
        )
        profanity_source_audio_path = profanity_bleep_result.audio_path
        bleep_windows = (
            self._serialize_bleep_windows(profanity_bleep_result.windows_ms)
            if enable_profanity_bleeping
            else None
        )
        try:
            assert profanity_source_audio_path is not None
            shutil.copyfile(profanity_source_audio_path, processed_audio_path)
        finally:
            self._cleanup_temp_audio_path(
                profanity_source_audio_path,
                original_audio_path=unprocessed_audio_path,
            )

        chapter_data_json: str | None = None
        if chapters_for_output:
            write_adjusted_chapters(
                audio_path=processed_audio_path,
                chapters_to_keep=chapters_for_output,
                removed_segments=[],
            )
            chapter_data_json = json.dumps(
                {
                    "chapter_source": chapter_source,
                    "chapters_for_output": _serialize_chapters_for_output(
                        chapters_for_output, processed_audio_path
                    ),
                }
            )

        self._finalize_processing(
            post,
            job,
            processed_audio_path,
            chapter_data=chapter_data_json,
            bleep_windows=bleep_windows,
        )

    def _refine_description_sourced_chapters(
        self,
        *,
        chapters_for_output: list[Any],
        transcript_segments: list[Any],
        post_id: int | None,
        post_guid: str | None = None,
        words_by_sequence: Mapping[int, list[Any]] | None = None,
    ) -> list[Any]:
        """Align description-parsed chapter starts to word-level timestamps.

        Uses the title-matching refiner (no extra LLM call). Only adjusts starts
        when the intra-segment boundary toggle is on; otherwise returns the
        chapters untouched.
        """
        if not chapters_for_output or not transcript_segments:
            return chapters_for_output

        refined = refine_description_chapters_with_word_refiner(
            chapters_for_output,
            transcript_segments,
            config=self.config,
            logger_override=self.logger,
            words_by_sequence=words_by_sequence,
        )
        self.logger.info(
            "Using %d description-sourced chapters after word-boundary refinement",
            len(refined),
        )
        return refined

    def _refine_transcript_sourced_chapters(
        self,
        *,
        chapters_for_output: list[Any],
        transcript_segments: list[Any],
        post_id: int | None,
        post_guid: str | None = None,
        removed_windows_ms: list[tuple[int, int]] | None = None,
        words_by_sequence: Mapping[int, list[Any]] | None = None,
        full_block_text: bool = False,
    ) -> list[Any]:
        if not chapters_for_output or not transcript_segments:
            return chapters_for_output

        # When the intra-segment toggle is on, the topic-plan call also returns
        # each chapter's first word and aligns starts to exact word timestamps
        # in the same call (no second LLM round-trip).
        align_word_starts = self._word_level_boundary_refiner_enabled()
        topic_chapters = generate_topic_chapters_from_transcript_with_llm(
            transcript_segments,
            llm_model=getattr(self.config, "llm_model", None),
            llm_api_key=getattr(self.config, "llm_api_key", None),
            openai_base_url=getattr(self.config, "openai_base_url", None),
            openai_timeout_sec=int(getattr(self.config, "openai_timeout", 300)),
            logger_override=self.logger,
            post_id=post_id,
            post_guid=post_guid,
            removed_windows_ms=removed_windows_ms,
            llm_service_tier=getattr(self.config, "llm_service_tier", None),
            align_word_starts=align_word_starts,
            words_by_sequence=words_by_sequence,
            word_align_config=self.config if align_word_starts else None,
            full_block_text=full_block_text,
        )
        if topic_chapters:
            # With the toggle off, fall back to the title-matching heuristic.
            if not align_word_starts:
                topic_chapters = refine_transcript_chapters_with_word_refiner(
                    topic_chapters,
                    transcript_segments,
                    config=self.config,
                    logger_override=self.logger,
                )
            self.logger.info(
                "Using %d topic-based transcript chapters from LLM",
                len(topic_chapters),
            )
            return topic_chapters

        self.logger.warning(
            "Topic-based transcript chapter generation returned no usable plan; "
            "falling back to heuristic transcript chapter boundaries for post %s",
            post_id,
        )
        fallback_chapters = generate_chapters_from_transcript(transcript_segments)
        if fallback_chapters:
            refined_fallback = refine_generated_chapter_titles_with_llm(
                fallback_chapters,
                transcript_segments,
                llm_model=getattr(self.config, "llm_model", None),
                llm_api_key=getattr(self.config, "llm_api_key", None),
                openai_base_url=getattr(self.config, "openai_base_url", None),
                openai_timeout_sec=int(getattr(self.config, "openai_timeout", 300)),
                logger_override=self.logger,
                post_id=post_id,
                llm_service_tier=getattr(self.config, "llm_service_tier", None),
            )
            self.logger.info(
                "Heuristic transcript chapter boundaries retained after LLM "
                "title refinement (count=%d)",
                len(refined_fallback),
            )
            return refined_fallback

        self.logger.warning(
            "No usable transcript segments remained for chapter fallback on post %s; "
            "retaining original transcript-derived chapters",
            post_id,
        )
        return chapters_for_output

    @staticmethod
    def _segment_overlaps_removed_audio(
        segment_start_ms: int,
        segment_end_ms: int,
        removed_segments_ms: list[tuple[int, int]],
    ) -> bool:
        for removed_start_ms, removed_end_ms in removed_segments_ms:
            if removed_end_ms <= segment_start_ms:
                continue
            if removed_start_ms >= segment_end_ms:
                return False
            return True
        return False

    def _filter_transcript_segments_for_chapters(
        self,
        transcript_segments: list[Any],
        removed_segments_ms: list[tuple[int, int]],
    ) -> list[Any]:
        if not transcript_segments or not removed_segments_ms:
            return transcript_segments

        sorted_removed_segments = sorted(
            removed_segments_ms, key=lambda window: window[0]
        )
        kept_segments: list[Any] = []

        for segment in transcript_segments:
            segment_start_ms = int(float(getattr(segment, "start_time", 0.0)) * 1000)
            segment_end_ms = int(float(getattr(segment, "end_time", 0.0)) * 1000)
            segment_end_ms = max(segment_start_ms, segment_end_ms)

            if self._segment_overlaps_removed_audio(
                segment_start_ms,
                segment_end_ms,
                sorted_removed_segments,
            ):
                continue

            kept_segments.append(segment)

        removed_count = len(transcript_segments) - len(kept_segments)
        if removed_count > 0:
            self.logger.info(
                "Excluded %d/%d transcript segments from transcript chapter "
                "generation because they overlap removed ad windows",
                removed_count,
                len(transcript_segments),
            )

        return kept_segments

    def _perform_chapter_based_processing(
        self,
        post: Post,
        job: ProcessingJob,
        processed_audio_path: str,
        cancel_callback: Callable[[], bool] | None = None,
        chapter_filter_strings: str | None = None,
    ) -> None:
        """
        Perform chapter-based ad detection: read chapters, filter by title, remove ads.
        Skips transcription and LLM classification.
        """
        from shared import defaults as DEFAULTS

        # Step 2: Read and filter chapters (skipping transcription)
        self.status_manager.update_job_status(
            job, "running", 2, "Reading chapters", 50.0
        )

        # Get filter strings (per-feed or global default)
        filter_csv = chapter_filter_strings or DEFAULTS.CHAPTER_FILTER_DEFAULT_STRINGS
        filter_strings = parse_filter_strings(filter_csv)

        detector = ChapterAdDetector(filter_strings=filter_strings, logger=self.logger)

        try:
            ad_segments, chapters_to_keep, chapters_to_remove = detector.detect(
                str(post.unprocessed_audio_path)
            )
        except ChapterDetectionError as e:
            raise ProcessorException(str(e)) from e

        self._raise_if_cancelled(job, 2, cancel_callback)

        # Step 3: Skip LLM classification (chapters already filtered)
        self.status_manager.update_job_status(
            job, "running", 3, "Chapters filtered", 75.0
        )
        self._raise_if_cancelled(job, 3, cancel_callback)

        # Step 4: Process audio (remove ad segments)
        self.status_manager.update_job_status(
            job, "running", 4, "Processing audio", 90.0
        )

        # Convert ad segments to milliseconds for audio processing
        ad_segments_ms = [(int(s * 1000), int(e * 1000)) for s, e in ad_segments]

        if ad_segments_ms:
            clip_segments_exact(
                ad_segments_ms=ad_segments_ms,
                in_path=str(post.unprocessed_audio_path),
                out_path=processed_audio_path,
            )
        else:
            # No ads found, copy the original file
            shutil.copyfile(str(post.unprocessed_audio_path), processed_audio_path)

        # Write adjusted chapters to the processed file
        write_adjusted_chapters(
            audio_path=processed_audio_path,
            chapters_to_keep=chapters_to_keep,
            removed_segments=ad_segments,
        )

        # Build chapter data for stats.
        # `chapters_for_output` is what the UI / RSS render, and it must span
        # the full processed audio file (see _serialize_chapters_for_output).
        # `chapters_kept` and `chapters_removed` are diagnostic snapshots of
        # the pre-removal chapter set, so they intentionally stay un-padded.
        adjusted_kept_chapters = recalculate_chapter_times(
            chapters_to_keep, ad_segments
        )
        chapter_data = {
            "filter_strings": filter_strings,
            "chapters_for_output": _serialize_chapters_for_output(
                adjusted_kept_chapters, processed_audio_path
            ),
            "chapters_kept": [
                {
                    "title": ch.title,
                    "start_time": round(ch.start_time_ms / 1000.0, 1),
                    "end_time": round(ch.end_time_ms / 1000.0, 1),
                }
                for ch in chapters_to_keep
            ],
            "chapters_removed": [
                {
                    "title": ch.title,
                    "start_time": round(ch.start_time_ms / 1000.0, 1),
                    "end_time": round(ch.end_time_ms / 1000.0, 1),
                }
                for ch in chapters_to_remove
            ],
        }

        self._finalize_processing(
            post, job, processed_audio_path, chapter_data=json.dumps(chapter_data)
        )

    def _finalize_processing(
        self,
        post: Post,
        job: ProcessingJob,
        processed_audio_path: str,
        chapter_data: str | None = None,
        bleep_windows: list[dict[str, float]] | None = None,
    ) -> None:
        """
        Finalize processing: update database and mark job complete.
        """
        # Capture this BEFORE _remove_unprocessed_audio clears the flag, so
        # we can match the DB update to the on-disk preservation decision.
        suppress_for_retry = (
            self._suppress_unprocessed_cleanup_for_guid is not None
            and post.guid == self._suppress_unprocessed_cleanup_for_guid
        )
        original_unprocessed_path = post.unprocessed_audio_path

        # Update the database with the processed audio path
        self._remove_unprocessed_audio(post)
        update_data: dict[str, Any] = {
            "processed_audio_path": processed_audio_path,
            "bleep_windows": bleep_windows,
        }
        # For the auto-retry handoff we keep the unprocessed path pointed at
        # the original download. Setting it to None here would force the
        # retry to re-download — see _remove_unprocessed_audio for the
        # full DAI rationale.
        if suppress_for_retry and original_unprocessed_path:
            update_data["unprocessed_audio_path"] = original_unprocessed_path
        else:
            update_data["unprocessed_audio_path"] = None
        if chapter_data is not None:
            update_data["chapter_data"] = chapter_data
        # Feeds and stats read Post.duration as the processed runtime, and not
        # every strategy cuts audio through AudioProcessor, which also stores it.
        processed_duration_ms = get_audio_duration_ms(processed_audio_path)
        if processed_duration_ms:
            post.duration = processed_duration_ms / 1000.0
            update_data["duration"] = post.duration
        result = writer_client.update(
            "Post",
            post.id,
            update_data,
            wait=True,
        )
        if not result or not result.success:
            raise RuntimeError(getattr(result, "error", "Failed to update post"))

        # Mark job complete
        self.status_manager.update_job_status(
            job, "completed", job.total_steps or 4, "Processing complete", 100.0
        )

    def _raise_if_cancelled(
        self,
        job: ProcessingJob,
        current_step: int,
        cancel_callback: Callable[[], bool] | None,
    ) -> None:
        """Helper to centralize cancellation checking and update job state."""
        if cancel_callback and cancel_callback():
            self.status_manager.update_job_status(
                job, "cancelled", current_step, "Cancellation requested"
            )
            raise ProcessorException("Cancelled")

    def _classify_ad_segments(
        self,
        post: Post,
        job: ProcessingJob,
        transcript_segments: list[TranscriptSegment],
    ) -> None:
        """
        Classify ad segments in the transcript.

        Args:
            post: The Post object being processed
            job: The ProcessingJob for tracking
            transcript_segments: The transcript segments to classify
        """
        self.status_manager.update_job_status(
            job, "running", 3, "Identifying ads", 75.0
        )
        user_prompt_template = self.get_user_prompt_template(
            DEFAULT_USER_PROMPT_TEMPLATE_PATH
        )
        system_prompt = self.get_system_prompt(DEFAULT_SYSTEM_PROMPT_PATH)
        self.ad_classifier.classify(
            transcript_segments=transcript_segments,
            system_prompt=system_prompt,
            user_prompt_template=user_prompt_template,
            post=post,
        )

    def _simulate_developer_processing(
        self,
        post: Post,
        job: ProcessingJob,
        post_guid: str,
        post_title: str,
        feed_title: str,
        job_id: str,
    ) -> str | None:
        """Short-circuit processing for developer-mode test feeds.

        When developer mode is enabled and a post comes from a synthetic test feed
        (download_url contains "test-feed"), skip the full pipeline and copy a
        tiny bundled MP3 into the expected processed/unprocessed locations. This
        keeps the UI happy without relying on external downloads or LLM calls.
        """

        download_url = (post.download_url or "").lower()
        is_test_feed = "test-feed" in download_url or post_guid.startswith("test-guid")
        if not (self.config.developer_mode or is_test_feed):
            return None

        sample_audio = (
            Path(__file__).resolve().parent.parent / "tests" / "data" / "count_0_99.mp3"
        )
        if not sample_audio.exists():
            self.status_manager.update_job_status(
                job,
                "failed",
                job.current_step or 0,
                "Developer sample audio missing",
            )
            raise ProcessorException("Developer sample audio missing")

        self.status_manager.update_job_status(
            job,
            "running",
            1,
            "Simulating processing (developer mode)",
            25.0,
        )

        unprocessed_path = get_job_unprocessed_path(post_guid, job_id, post_title)
        unprocessed_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sample_audio, unprocessed_path)

        processed_path = (
            get_srv_root()
            / sanitize_title(feed_title)
            / f"{sanitize_title(post_title)}.mp3"
        )
        processed_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sample_audio, processed_path)

        result = writer_client.update(
            "Post",
            post.id,
            {
                "unprocessed_audio_path": str(unprocessed_path),
                "processed_audio_path": str(processed_path),
            },
            wait=True,
        )
        if not result or not result.success:
            raise RuntimeError(getattr(result, "error", "Failed to update post"))

        self.status_manager.update_job_status(
            job,
            "completed",
            job.total_steps or 4,
            "Processing complete (developer mode)",
            100.0,
        )

        return str(processed_path)

    def _handle_download_step(
        self,
        post: Post,
        job: ProcessingJob,
        post_guid: str,
        post_title: str,
        job_id: str,
    ) -> None:
        """
        Handle the download step with progress tracking and robust file checking.
        This method checks for existing files on disk before downloading.

        Args:
            post: The Post object being processed
            job: The ProcessingJob for tracking
            post_guid: Cached post GUID to avoid ORM access
            post_title: Cached post title to avoid ORM access
            job_id: Cached job ID to avoid ORM access
        """
        # If we have a path in the database, check if the file actually exists
        if post.unprocessed_audio_path is not None:
            if (
                os.path.exists(post.unprocessed_audio_path)
                and os.path.getsize(post.unprocessed_audio_path) > 0
            ):
                self.logger.debug(
                    f"Unprocessed audio already available at: {post.unprocessed_audio_path}"
                )
                return
            self.logger.info(
                f"Database path {post.unprocessed_audio_path} doesn't exist or is empty, resetting"
            )
            result = writer_client.update(
                "Post", post.id, {"unprocessed_audio_path": None}, wait=True
            )
            if not result or not result.success:
                raise RuntimeError(getattr(result, "error", "Failed to update post"))

        # Compute a unique per-job expected path
        expected_unprocessed_path = get_job_unprocessed_path(
            post_guid, job_id, post_title
        )

        if (
            expected_unprocessed_path.exists()
            and expected_unprocessed_path.stat().st_size > 0
        ):
            # Found a local unprocessed file
            unprocessed_path_str = str(expected_unprocessed_path.resolve())
            self.logger.info(
                f"Found existing unprocessed audio for post '{post_title}' at '{unprocessed_path_str}'. "
                "Updated the database path."
            )
            result = writer_client.update(
                "Post",
                post.id,
                {"unprocessed_audio_path": unprocessed_path_str},
                wait=True,
            )
            if not result or not result.success:
                raise RuntimeError(getattr(result, "error", "Failed to update post"))
            return

        # Need to download the file
        self.status_manager.update_job_status(
            job, "running", 1, "Downloading episode", 25.0
        )
        self.logger.info(f"Downloading post: {post_title}")
        download_path = self.downloader.download_episode(
            post, dest_path=str(expected_unprocessed_path)
        )
        if download_path is None:
            raise ProcessorException("Download failed")
        result = writer_client.update(
            "Post", post.id, {"unprocessed_audio_path": download_path}, wait=True
        )
        if not result or not result.success:
            raise RuntimeError(getattr(result, "error", "Failed to update post"))

    def make_dirs(self, processing_paths: ProcessingPaths) -> None:
        """Create necessary directories for output files."""
        if processing_paths.post_processed_audio_path:
            processing_paths.post_processed_audio_path.parent.mkdir(
                parents=True, exist_ok=True
            )

    def get_system_prompt(self, system_prompt_path: str) -> str:
        """Load the system prompt from a file."""
        with open(system_prompt_path) as f:
            return f.read()

    def get_user_prompt_template(self, prompt_template_path: str) -> Template:
        """Load the user prompt template from a file."""
        with open(prompt_template_path) as f:
            return Template(f.read())

    def remove_audio_files_and_reset_db(self, post_id: int | None) -> None:
        """
        Removes unprocessed/processed audio for the given post from disk,
        and resets the DB fields so the next run will re-download the files.
        """
        if post_id is None:
            return

        post = self.db_session.get(Post, post_id)
        if not post:
            self.logger.warning(
                f"Could not find Post with ID {post_id} to remove files."
            )
            return

        if post.unprocessed_audio_path and os.path.isfile(post.unprocessed_audio_path):
            try:
                os.remove(post.unprocessed_audio_path)
                self.logger.info(
                    f"Removed unprocessed file: {post.unprocessed_audio_path}"
                )
            except OSError as e:
                self.logger.error(
                    f"Failed to remove unprocessed file '{post.unprocessed_audio_path}': {e}"
                )

        if post.processed_audio_path and os.path.isfile(post.processed_audio_path):
            try:
                os.remove(post.processed_audio_path)
                self.logger.info(f"Removed processed file: {post.processed_audio_path}")
            except OSError as e:
                self.logger.error(
                    f"Failed to remove processed file '{post.processed_audio_path}': {e}"
                )

        result = writer_client.update(
            "Post",
            post.id,
            {"unprocessed_audio_path": None, "processed_audio_path": None},
            wait=True,
        )
        if not result or not result.success:
            raise RuntimeError(getattr(result, "error", "Failed to update post"))

    def _remove_unprocessed_audio(self, post: Post) -> None:
        """
        Delete the downloaded source audio and clear its DB reference.

        Used after we have a finalized processed file so stale downloads do not
        accumulate on disk.
        """
        # DAI-safety: if this run just triggered a zero-ads-guard auto-retry
        # for this post, keep the downloaded bytes so the retry can reuse
        # them. Re-downloading on a dynamic-ad-insertion feed would yield
        # different audio than the transcripts describe, which would silently
        # poison the retry's classifier output. Clear the flag once consumed
        # so subsequent post finalizations (including the retry's own) clean
        # up normally.
        if (
            self._suppress_unprocessed_cleanup_for_guid is not None
            and post.guid == self._suppress_unprocessed_cleanup_for_guid
        ):
            self.logger.info(
                "[ZERO_ADS_GUARD] Preserving unprocessed audio for post %s "
                "so the auto-retry job can reuse the exact bytes the "
                "retained transcripts were made from.",
                post.guid,
            )
            self._suppress_unprocessed_cleanup_for_guid = None
            return

        path = post.unprocessed_audio_path
        if not path:
            return

        if os.path.isfile(path):
            try:
                os.remove(path)
                self.logger.info("Removed unprocessed file after processing: %s", path)
            except OSError as exc:  # best-effort cleanup
                self.logger.warning(
                    "Failed to remove unprocessed file '%s': %s", path, exc
                )
        post.unprocessed_audio_path = None

    def _check_existing_processed_audio(self, post: Post) -> bool:
        """
        Check if processed audio already exists, either in database or on disk.
        Updates the database path if found on disk.

        Returns:
            True if processed audio exists and is valid, False otherwise
        """
        existing_processed_path = find_existing_processed_audio_path(
            processed_audio_path=post.processed_audio_path,
            unprocessed_audio_path=post.unprocessed_audio_path,
            feed_title=getattr(post.feed, "title", None),
            post_title=post.title,
        )
        if existing_processed_path:
            processed_path_str = str(existing_processed_path)
            if post.processed_audio_path != processed_path_str:
                self.logger.info(
                    "Found existing processed audio for post '%s' at '%s'. "
                    "Updated the database path.",
                    post.title,
                    processed_path_str,
                )
                result = writer_client.update(
                    "Post",
                    post.id,
                    {"processed_audio_path": processed_path_str},
                    wait=True,
                )
                if not result or not result.success:
                    raise RuntimeError(
                        getattr(result, "error", "Failed to update post")
                    )
            else:
                self.logger.info(
                    "Processed audio already available at: %s",
                    post.processed_audio_path,
                )
            return True

        if post.processed_audio_path is not None:
            self.logger.info(
                "Database path %s doesn't exist or is empty, resetting",
                post.processed_audio_path,
            )
            result = writer_client.update(
                "Post", post.id, {"processed_audio_path": None}, wait=True
            )
            if not result or not result.success:
                raise RuntimeError(getattr(result, "error", "Failed to update post"))

        return False


class ProcessorException(Exception):
    """Exception raised for podcast processing errors."""
