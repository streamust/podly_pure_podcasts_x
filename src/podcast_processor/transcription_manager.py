import json
import logging
from pathlib import Path
from typing import Any

from app.extensions import db
from app.memory_pressure import collect_incremental
from app.models import ModelCall, Post, TranscriptSegment
from app.writer.batching import (
    batch_count_for,
    get_writer_batch_size,
    iter_writer_batches,
)
from app.writer.client import writer_client
from shared.config import (
    Config,
    GroqWhisperConfig,
    RemoteWhisperConfig,
    TestWhisperConfig,
)
from shared.processing_paths import get_base_podcast_data_dir

from .transcribe import (
    ChunkProgressCallback,
    GroqWhisperTranscriber,
    OpenAIWhisperTranscriber,
    Segment,
    TestWhisperTranscriber,
    Transcriber,
    merge_segments_with_saved_word_timestamps,
    serialize_segment_word_timestamps,
)


class TranscriptionManager:
    """Handles the transcription of podcast audio files."""

    def __init__(
        self,
        logger: logging.Logger,
        config: Config,
        model_call_query: Any | None = None,
        segment_query: Any | None = None,
        db_session: Any | None = None,
        transcriber: Transcriber | None = None,
    ):
        self.logger = logger
        self.config = config
        self.transcriber = transcriber or self._create_transcriber()
        self._model_call_query_provided = model_call_query is not None
        self.model_call_query = model_call_query or ModelCall.query
        self._segment_query_provided = segment_query is not None
        self.segment_query = segment_query or TranscriptSegment.query
        self.db_session = db_session or db.session

    def _create_transcriber(self) -> Transcriber:
        """Create the appropriate transcriber based on configuration."""
        assert self.config.whisper is not None, (
            "validate_whisper_config ensures that even if old style whisper "
            "config is given, it will be translated and config.whisper set."
        )

        if isinstance(self.config.whisper, TestWhisperConfig):
            return TestWhisperTranscriber(self.logger)
        if isinstance(self.config.whisper, RemoteWhisperConfig):
            return OpenAIWhisperTranscriber(self.logger, self.config.whisper)
        if isinstance(self.config.whisper, GroqWhisperConfig):
            return GroqWhisperTranscriber(self.logger, self.config.whisper)
        raise ValueError(f"unhandled whisper config {self.config.whisper}")

    def _check_existing_transcription(
        self, post: Post
    ) -> list[TranscriptSegment] | None:
        """Checks for existing successful transcription and returns segments if valid.

        NOTE: Defaults to using self.db_session for queries to keep a single session,
        but will honor injected model_call_query/segment_query when provided (e.g. tests).
        """
        model_call_query = (
            self.model_call_query
            if self._model_call_query_provided
            else self.db_session.query(ModelCall)
        )
        segment_query = (
            self.segment_query
            if self._segment_query_provided
            else self.db_session.query(TranscriptSegment)
        )

        existing_whisper_call = (
            model_call_query.filter_by(
                post_id=post.id,
                model_name=self.transcriber.model_name,
                status="success",
            )
            .order_by(ModelCall.timestamp.desc())
            .first()
        )

        if existing_whisper_call:
            self.logger.info(
                f"Found existing successful Whisper ModelCall {existing_whisper_call.id} for post {post.id}."
            )
            db_segments: list[TranscriptSegment] = (
                segment_query.filter_by(post_id=post.id)
                .order_by(TranscriptSegment.sequence_num)
                .all()
            )
            if db_segments:
                if (
                    existing_whisper_call.last_segment_sequence_num
                    == len(db_segments) - 1
                ):
                    self.logger.info(
                        f"Returning {len(db_segments)} existing transcript segments from database for post {post.id}."
                    )
                    return db_segments
                self.logger.warning(
                    f"ModelCall {existing_whisper_call.id} for post {post.id} indicates {existing_whisper_call.last_segment_sequence_num + 1} segments, but found {len(db_segments)} in DB. Re-transcribing."
                )
            else:
                self.logger.warning(
                    f"Successful ModelCall {existing_whisper_call.id} found for post {post.id}, but no transcript segments in DB. Re-transcribing."
                )
        else:
            self.logger.info(
                f"No existing successful Whisper ModelCall found for post {post.id} with model {self.transcriber.model_name}. Proceeding to transcribe."
            )
        return None

    def get_reusable_transcription(self, post: Post) -> list[TranscriptSegment] | None:
        """Return existing transcript segments only when they are reusable as-is.

        Reuse requires a successful Whisper model call for the active transcriber
        model and a matching set of persisted transcript segments.
        """
        return self._check_existing_transcription(post)

    def _get_or_create_whisper_model_call(self, post: Post) -> ModelCall:
        """Create or reuse the placeholder ModelCall row for a Whisper run via writer."""
        result = writer_client.action(
            "upsert_whisper_model_call",
            {
                "post_id": post.id,
                "model_name": self.transcriber.model_name,
                "first_segment_sequence_num": 0,
                "last_segment_sequence_num": -1,
                "prompt": "Whisper transcription job",
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

    def _persist_transcript_word_timestamps(
        self,
        post_id: int,
        transcript_word_timestamps: list[dict[str, Any]] | None,
    ) -> None:
        result = writer_client.update(
            "Post",
            post_id,
            {"transcript_word_timestamps": transcript_word_timestamps},
            wait=True,
        )
        if not result or not result.success:
            raise RuntimeError(
                getattr(result, "error", "Failed to persist transcript word timestamps")
            )

    def _transcriber_supports_word_timestamps(self) -> bool:
        return isinstance(
            self.transcriber, (OpenAIWhisperTranscriber, GroqWhisperTranscriber)
        )

    def _persist_transcription_chunks(
        self,
        *,
        post: Post,
        model_call_id: int,
        segments: list[Segment],
        transcript_word_timestamps: list[dict[str, Any]] | None,
    ) -> None:
        start_res = writer_client.action(
            "start_transcription_replace",
            {"post_id": post.id, "model_call_id": model_call_id},
            wait=True,
        )
        if not start_res or not start_res.success:
            raise RuntimeError(
                getattr(start_res, "error", "Failed to start transcription replace")
            )

        batch_size = get_writer_batch_size()
        total_batches = batch_count_for(len(segments), batch_size=batch_size)
        inserted_count = 0
        self.logger.info(
            "[TRANSCRIBE_WRITE] post_id=%s model_call_id=%s segments=%s batches=%s batch_size=%s",
            post.id,
            model_call_id,
            len(segments),
            total_batches,
            batch_size,
        )

        for batch_index, batch in enumerate(
            iter_writer_batches(enumerate(segments), batch_size=batch_size), start=1
        ):
            segment_batch = [
                {
                    "sequence_num": i,
                    "start_time": round(seg.start, 1),
                    "end_time": round(seg.end, 1),
                    "text": seg.text,
                    "speaker_label": seg.speaker_label,
                }
                for i, seg in batch
            ]
            write_res = writer_client.action(
                "insert_transcript_segments",
                {"post_id": post.id, "segments": segment_batch},
                wait=True,
            )
            if not write_res or not write_res.success:
                raise RuntimeError(
                    getattr(write_res, "error", "Failed to insert transcript segments")
                )
            inserted_count += int((write_res.data or {}).get("inserted") or 0)
            collect_incremental(
                f"transcription manager batch {batch_index}/{total_batches}",
                self.logger,
            )

        finish_action = "finish_transcription_replace"
        finish_payload: dict[str, Any] = {
            "post_id": post.id,
            "model_call_id": model_call_id,
            "segment_count": inserted_count,
            "transcript_word_timestamps": transcript_word_timestamps,
        }
        if transcript_word_timestamps is not None:
            artifact_path = self._write_transcript_word_timestamps_artifact(
                post_id=post.id,
                model_call_id=model_call_id,
                transcript_word_timestamps=transcript_word_timestamps,
            )
            finish_action = "finish_transcription_replace_from_artifact"
            finish_payload = {
                "post_id": post.id,
                "model_call_id": model_call_id,
                "segment_count": inserted_count,
                "artifact_path": str(artifact_path),
            }

        finish_res = writer_client.action(
            finish_action,
            finish_payload,
            wait=True,
        )
        if not finish_res or not finish_res.success:
            raise RuntimeError(
                getattr(finish_res, "error", "Failed to finish transcription replace")
            )

    def _write_transcript_word_timestamps_artifact(
        self,
        *,
        post_id: int,
        model_call_id: int,
        transcript_word_timestamps: list[dict[str, Any]],
    ) -> Path:
        artifact_dir = (
            get_base_podcast_data_dir()
            / "transcript_artifacts"
            / f"post_{post_id}"
            / f"model_call_{model_call_id}"
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / "word_timestamps.json"
        temp_path = artifact_path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(transcript_word_timestamps, separators=(",", ":")),
            encoding="utf-8",
        )
        temp_path.replace(artifact_path)
        return artifact_path

    def transcribe(
        self,
        post: Post,
        *,
        progress_callback: ChunkProgressCallback | None = None,
    ) -> list[TranscriptSegment]:
        db_segments, _ = self.transcribe_for_processing(
            post,
            include_word_timestamps=True,
            progress_callback=progress_callback,
        )
        return db_segments

    def transcribe_for_processing(
        self,
        post: Post,
        *,
        include_word_timestamps: bool = False,
        progress_callback: ChunkProgressCallback | None = None,
    ) -> tuple[list[TranscriptSegment], list[Segment] | None]:
        """
        Transcribes a podcast audio file, or retrieves existing transcription.

        Args:
            post: The Post object containing the podcast audio to transcribe

        Returns:
            The persisted transcript segments and, when requested, the richer
            in-memory transcription payload that may include word timestamps.
        """
        self.logger.info(
            f"Starting transcription process for post {post.id} using {self.transcriber.model_name}"
        )

        existing_segments = self.get_reusable_transcription(post)
        if existing_segments is not None:
            rich_segments = None
            if include_word_timestamps:
                rich_segments = merge_segments_with_saved_word_timestamps(
                    existing_segments,
                    getattr(post, "transcript_word_timestamps", None),
                )
                if rich_segments is None:
                    if self._transcriber_supports_word_timestamps():
                        self.logger.info(
                            "Existing transcript found for post %s but no saved word timestamps were available; requesting them from %s.",
                            post.id,
                            self.transcriber.model_name,
                        )
                        rich_segments = self.transcriber.transcribe(
                            post.unprocessed_audio_path,
                            include_word_timestamps=True,
                            progress_callback=progress_callback,
                        )
                        self._persist_transcript_word_timestamps(
                            post.id,
                            serialize_segment_word_timestamps(rich_segments),
                        )
                    else:
                        self.logger.info(
                            "Existing transcript found for post %s, but transcriber %s does not provide reusable word timestamps. Falling back to segment-level refinement.",
                            post.id,
                            self.transcriber.model_name,
                        )
                else:
                    self.logger.info(
                        "Reused saved word timestamps for existing transcript on post %s.",
                        post.id,
                    )
            return existing_segments, rich_segments

        # Create or reuse the ModelCall record for this transcription attempt
        current_whisper_call = self._get_or_create_whisper_model_call(post)
        self.logger.info(
            f"Prepared Whisper ModelCall {current_whisper_call.id} for post {post.id}."
        )

        try:
            self.logger.info(
                f"[TRANSCRIBE_START] Calling transcriber {self.transcriber.model_name} for post {post.id}, audio: {post.unprocessed_audio_path}"
            )
            # Expire session state before long-running transcription to avoid stale locks
            self.db_session.expire_all()

            pydantic_segments = self.transcriber.transcribe(
                post.unprocessed_audio_path,
                include_word_timestamps=include_word_timestamps,
                progress_callback=progress_callback,
            )
            self.logger.info(
                f"[TRANSCRIBE_COMPLETE] Transcription by {self.transcriber.model_name} for post {post.id} resulted in {len(pydantic_segments)} segments."
            )

            self._persist_transcription_chunks(
                post=post,
                model_call_id=current_whisper_call.id,
                segments=pydantic_segments or [],
                transcript_word_timestamps=serialize_segment_word_timestamps(
                    pydantic_segments
                ),
            )

            segment_query = (
                self.segment_query
                if self._segment_query_provided
                else self.db_session.query(TranscriptSegment)
            )
            db_segments: list[TranscriptSegment] = (
                segment_query.filter_by(post_id=post.id)
                .order_by(TranscriptSegment.sequence_num)
                .all()
            )
            self.logger.info(
                f"Successfully stored {len(db_segments)} transcript segments and updated ModelCall {current_whisper_call.id} for post {post.id}."
            )
            return db_segments, (pydantic_segments if include_word_timestamps else None)

        except Exception as e:
            self.logger.error(
                f"Transcription failed for post {post.id} using {self.transcriber.model_name}. Error: {e}",
                exc_info=True,
            )

            fail_res = writer_client.action(
                "mark_model_call_failed",
                {
                    "model_call_id": current_whisper_call.id,
                    "error_message": str(e),
                    "status": "failed_permanent",
                },
                wait=True,
            )
            if not fail_res or not fail_res.success:
                self.logger.error(
                    "Failed to mark ModelCall %s as failed via writer: %s",
                    current_whisper_call.id,
                    getattr(fail_res, "error", None),
                )

            raise
