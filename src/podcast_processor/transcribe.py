import logging
import shutil
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from groq import Groq

# OpenAI is imported lazily inside OpenAIWhisperTranscriber.__init__ to avoid
# pulling in openai (~566 modules) for runs that use a different transcriber.
from pydantic import BaseModel

from podcast_processor.audio import split_audio
from shared.config import GroqWhisperConfig, RemoteWhisperConfig


class WordTimestamp(BaseModel):
    word: str
    start: float | None = None
    end: float | None = None
    score: float | None = None


class Segment(BaseModel):
    start: float
    end: float
    text: str
    speaker_label: str | None = None
    words: list[WordTimestamp] | None = None


def serialize_segment_word_timestamps(
    segments: Sequence[Segment],
) -> list[dict[str, Any]] | None:
    payload: list[dict[str, Any]] = []
    for sequence_num, segment in enumerate(segments or []):
        words_payload: list[dict[str, Any]] = []
        for word in segment.words or []:
            if word.start is None or word.end is None:
                continue
            words_payload.append(
                {
                    "word": str(word.word),
                    "start": float(word.start),
                    "end": float(word.end),
                    "score": (float(word.score) if word.score is not None else None),
                }
            )
        if words_payload:
            payload.append(
                {
                    "sequence_num": int(sequence_num),
                    "words": words_payload,
                }
            )
    return payload or None


def load_word_timestamps_by_sequence(
    raw_payload: Any,
) -> dict[int, list[WordTimestamp]]:
    words_by_sequence: dict[int, list[WordTimestamp]] = {}
    if not isinstance(raw_payload, list):
        return words_by_sequence

    for segment_payload in raw_payload:
        if not isinstance(segment_payload, dict):
            continue

        sequence_num = segment_payload.get("sequence_num")
        words = segment_payload.get("words")
        if sequence_num is None or not isinstance(words, list):
            continue

        try:
            sequence_num_int = int(sequence_num)
        except Exception:  # noqa: BLE001
            continue

        parsed_words: list[WordTimestamp] = []
        for word_payload in words:
            try:
                parsed = OpenAIWhisperTranscriber._parse_word(word_payload)
            except Exception:  # noqa: BLE001
                parsed = None
            if parsed is not None:
                parsed_words.append(parsed)

        if parsed_words:
            words_by_sequence[sequence_num_int] = parsed_words

    return words_by_sequence


def merge_segments_with_saved_word_timestamps(
    base_segments: Sequence[Any],
    raw_payload: Any,
) -> list[Segment] | None:
    words_by_sequence = load_word_timestamps_by_sequence(raw_payload)
    if not words_by_sequence:
        return None

    merged_segments: list[Segment] = []
    for fallback_sequence_num, segment in enumerate(base_segments or []):
        sequence_num_raw = getattr(segment, "sequence_num", fallback_sequence_num)
        try:
            sequence_num = int(sequence_num_raw)
        except Exception:  # noqa: BLE001
            sequence_num = fallback_sequence_num

        try:
            start_time = float(segment.start_time)
            end_time = float(segment.end_time)
        except Exception:  # noqa: BLE001
            continue

        merged_segments.append(
            Segment(
                start=start_time,
                end=end_time,
                text=str(getattr(segment, "text", "")),
                speaker_label=getattr(segment, "speaker_label", None),
                words=words_by_sequence.get(sequence_num) or None,
            )
        )

    return merged_segments


ChunkProgressCallback = Callable[[int, int], None]
"""Invoked after each chunk transcription with (chunks_completed, total_chunks).
Used to surface sub-stage progress (e.g. "Transcribing audio (chunk 2/3)")
on the job status without requiring a separate progress channel."""


class Transcriber(ABC):
    @property
    @abstractmethod
    def model_name(self) -> str:
        pass

    @abstractmethod
    def transcribe(
        self,
        audio_file_path: str,
        *,
        include_word_timestamps: bool = False,
        progress_callback: ChunkProgressCallback | None = None,
    ) -> list[Segment]:
        pass


class TestWhisperTranscriber(Transcriber):
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    @property
    def model_name(self) -> str:
        return "test_whisper"

    def transcribe(
        self,
        audio_file_path: str,
        *,
        include_word_timestamps: bool = False,
        progress_callback: ChunkProgressCallback | None = None,
    ) -> list[Segment]:
        del audio_file_path
        del include_word_timestamps
        self.logger.info("Using test whisper")
        # Pretend we did a single chunk so test-mode callers still observe
        # the progress contract.
        if progress_callback is not None:
            progress_callback(1, 1)
        return [
            Segment(start=0, end=1, text="This is a test"),
            Segment(start=1, end=2, text="This is another test"),
        ]


class OpenAIWhisperTranscriber(Transcriber):
    def __init__(self, logger: logging.Logger, config: RemoteWhisperConfig):
        from openai import OpenAI

        self.logger = logger
        self.config = config

        self.openai_client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=config.timeout_sec,
        )

    @property
    def model_name(self) -> str:
        return self.config.model  # e.g. "whisper-1"

    def transcribe(
        self,
        audio_file_path: str,
        *,
        include_word_timestamps: bool = False,
        progress_callback: ChunkProgressCallback | None = None,
    ) -> list[Segment]:
        self.logger.info(
            "[WHISPER_REMOTE] Starting remote whisper transcription for: %s",
            audio_file_path,
        )
        audio_chunk_path = audio_file_path + "_parts"

        chunks = split_audio(
            Path(audio_file_path),
            Path(audio_chunk_path),
            self.config.chunksize_mb * 1024 * 1024,
        )

        total_chunks = len(chunks)
        self.logger.info("[WHISPER_REMOTE] Processing %d chunks", total_chunks)
        all_segments: list[Segment] = []

        for idx, chunk in enumerate(chunks):
            chunk_path, offset = chunk
            self.logger.info(
                "[WHISPER_REMOTE] Processing chunk %d/%d: %s",
                idx + 1,
                total_chunks,
                chunk_path,
            )
            segments = self.get_segments_for_chunk(
                str(chunk_path),
                include_word_timestamps=include_word_timestamps,
            )
            self.logger.info(
                "[WHISPER_REMOTE] Chunk %d/%d complete: %d segments",
                idx + 1,
                total_chunks,
                len(segments),
            )
            all_segments.extend(self.add_offset_to_segments(segments, offset))
            if progress_callback is not None:
                # Best-effort: never let a UI-only progress hook fail the
                # actual transcription.
                try:
                    progress_callback(idx + 1, total_chunks)
                except Exception:
                    self.logger.exception(
                        "[WHISPER_REMOTE] progress_callback raised; ignoring"
                    )

        shutil.rmtree(audio_chunk_path)
        self.logger.info(
            "[WHISPER_REMOTE] Transcription complete: %d total segments",
            len(all_segments),
        )
        return all_segments

    @staticmethod
    def add_offset_to_segments(
        segments: list[Segment], offset_ms: int
    ) -> list[Segment]:
        offset_sec = float(offset_ms) / 1000.0
        for segment in segments:
            segment.start += offset_sec
            segment.end += offset_sec
            if segment.words:
                for word in segment.words:
                    if word.start is not None:
                        word.start += offset_sec
                    if word.end is not None:
                        word.end += offset_sec

        return segments

    def build_transcription_request_kwargs(
        self,
        audio_file: Any,
        *,
        include_word_timestamps: bool = False,
    ) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {
            "model": self.config.model,
            "file": audio_file,
            "timestamp_granularities": (
                ["segment", "word"] if include_word_timestamps else ["segment"]
            ),
            "language": self.config.language,
            "response_format": "verbose_json",
        }

        extra_body: dict[str, Any] = {}
        if include_word_timestamps:
            extra_body["align"] = True

        if self.config.diarize:
            extra_body["align"] = True
            extra_body["diarize"] = True
            if self.config.speaker_embeddings:
                extra_body["speaker_embeddings"] = True

        if extra_body:
            request_kwargs["extra_body"] = extra_body

        return request_kwargs

    @staticmethod
    def _get_segment_field(segment: Any, field_name: str) -> Any | None:
        if isinstance(segment, dict):
            return segment.get(field_name)
        return getattr(segment, field_name, None)

    @classmethod
    def _get_word_entries(cls, segment: Any) -> list[Any]:
        words = cls._get_segment_field(segment, "words")
        if isinstance(words, list):
            return words

        word_segments = cls._get_segment_field(segment, "word_segments")
        if isinstance(word_segments, list):
            return word_segments

        return []

    @classmethod
    def _parse_word(cls, word: Any) -> WordTimestamp | None:
        text = cls._get_segment_field(word, "word")
        if text is None:
            return None

        start = cls._get_segment_field(word, "start")
        end = cls._get_segment_field(word, "end")
        score = cls._get_segment_field(word, "score")

        return WordTimestamp(
            word=str(text),
            start=float(start) if start is not None else None,
            end=float(end) if end is not None else None,
            score=float(score) if score is not None else None,
        )

    @classmethod
    def _get_speaker_label(cls, segment: Any) -> str | None:
        for field_name in ("speaker_label", "speaker", "speaker_id"):
            value = cls._get_segment_field(segment, field_name)
            if value is None:
                continue

            speaker_label = str(value).strip()
            if speaker_label:
                return speaker_label

        return None

    @classmethod
    def _parse_segment(cls, segment: Any) -> Segment:
        start = cls._get_segment_field(segment, "start")
        end = cls._get_segment_field(segment, "end")
        text = cls._get_segment_field(segment, "text")

        if start is None or end is None or text is None:
            raise ValueError(
                "Remote transcription segment is missing one of required fields: "
                "start, end, text"
            )

        parsed_words = [
            parsed_word
            for parsed_word in (
                cls._parse_word(word) for word in cls._get_word_entries(segment)
            )
            if parsed_word is not None
        ]

        return Segment(
            start=float(start),
            end=float(end),
            text=str(text),
            speaker_label=cls._get_speaker_label(segment),
            words=parsed_words or None,
        )

    @classmethod
    def extract_segments_from_transcription(cls, transcription: Any) -> list[Segment]:
        segment_payloads: Any | None = None
        if isinstance(transcription, dict):
            segment_payloads = transcription.get("segments")
        else:
            segment_payloads = getattr(transcription, "segments", None)
            if segment_payloads is None and hasattr(transcription, "to_dict"):
                serialized = transcription.to_dict()
                if isinstance(serialized, dict):
                    segment_payloads = serialized.get("segments")

        if segment_payloads is None:
            return []

        if isinstance(segment_payloads, dict):
            nested_segments = segment_payloads.get("segments")
            if nested_segments is None:
                raise ValueError(
                    "Remote transcription segments dict is missing nested 'segments' list"
                )
            segment_payloads = nested_segments

        if not isinstance(segment_payloads, list):
            raise ValueError(
                f"Remote transcription segments must be a list, got {type(segment_payloads).__name__}"
            )

        return [cls._parse_segment(segment) for segment in segment_payloads]

    def get_segments_for_chunk(
        self,
        chunk_path: str,
        *,
        include_word_timestamps: bool = False,
    ) -> list[Segment]:
        with open(chunk_path, "rb") as f:
            self.logger.info(
                "[WHISPER_API_CALL] Sending chunk to API: %s (timeout=%ds diarize=%s speaker_embeddings=%s include_word_timestamps=%s)",
                chunk_path,
                self.config.timeout_sec,
                self.config.diarize,
                self.config.speaker_embeddings,
                include_word_timestamps,
            )

            transcription = self.openai_client.audio.transcriptions.create(
                **self.build_transcription_request_kwargs(
                    f,
                    include_word_timestamps=include_word_timestamps,
                ),
            )

            self.logger.debug(
                "Got transcription response type: %s",
                type(transcription).__name__,
            )

            segments = self.extract_segments_from_transcription(transcription)

            self.logger.debug("Got %d segments", len(segments))

            return segments


class GroqTranscriptionSegment(BaseModel):
    start: float
    end: float
    text: str
    words: list[WordTimestamp] | None = None


class GroqWhisperTranscriber(Transcriber):
    def __init__(self, logger: logging.Logger, config: GroqWhisperConfig):
        self.logger = logger
        self.config = config
        self.client = Groq(
            api_key=config.api_key,
            max_retries=config.max_retries,
        )

    @property
    def model_name(self) -> str:
        return f"groq_{self.config.model}"

    def transcribe(
        self,
        audio_file_path: str,
        *,
        include_word_timestamps: bool = False,
        progress_callback: ChunkProgressCallback | None = None,
    ) -> list[Segment]:
        self.logger.info(
            "[WHISPER_GROQ] Starting Groq whisper transcription for: %s",
            audio_file_path,
        )
        audio_chunk_path = audio_file_path + "_parts"

        # 12MB seems to cause instability in Groq
        chunks = split_audio(
            Path(audio_file_path), Path(audio_chunk_path), 6 * 1024 * 1024
        )

        total_chunks = len(chunks)
        self.logger.info("[WHISPER_GROQ] Processing %d chunks", total_chunks)
        all_segments: list[GroqTranscriptionSegment] = []

        for idx, chunk in enumerate(chunks):
            chunk_path, offset = chunk
            self.logger.info(
                "[WHISPER_GROQ] Processing chunk %d/%d: %s",
                idx + 1,
                total_chunks,
                chunk_path,
            )
            segments = self.get_segments_for_chunk(
                str(chunk_path), include_word_timestamps=include_word_timestamps
            )
            self.logger.info(
                "[WHISPER_GROQ] Chunk %d/%d complete: %d segments",
                idx + 1,
                total_chunks,
                len(segments),
            )
            all_segments.extend(self.add_offset_to_segments(segments, offset))
            if progress_callback is not None:
                try:
                    progress_callback(idx + 1, total_chunks)
                except Exception:
                    self.logger.exception(
                        "[WHISPER_GROQ] progress_callback raised; ignoring"
                    )

        shutil.rmtree(audio_chunk_path)
        self.logger.info(
            "[WHISPER_GROQ] Transcription complete: %d total segments",
            len(all_segments),
        )
        return self.convert_segments(all_segments)

    @staticmethod
    def convert_segments(segments: list[GroqTranscriptionSegment]) -> list[Segment]:
        return [
            Segment(
                start=seg.start,
                end=seg.end,
                text=seg.text,
                words=seg.words,
            )
            for seg in segments
        ]

    @staticmethod
    def add_offset_to_segments(
        segments: list[GroqTranscriptionSegment], offset_ms: int
    ) -> list[GroqTranscriptionSegment]:
        offset_sec = float(offset_ms) / 1000.0
        for segment in segments:
            segment.start += offset_sec
            segment.end += offset_sec
            # Word timestamps are chunk-relative in the API response, just
            # like segment times — shift them by the same offset.
            for word in segment.words or []:
                if word.start is not None:
                    word.start += offset_sec
                if word.end is not None:
                    word.end += offset_sec

        return segments

    def get_segments_for_chunk(
        self, chunk_path: str, *, include_word_timestamps: bool = False
    ) -> list[GroqTranscriptionSegment]:
        retries = self.config.max_retries if self.config.max_retries is not None else 0
        max_attempts = retries + 1
        for attempt in range(1, max_attempts + 1):
            self.logger.info(
                "[GROQ_API_CALL] Sending chunk to Groq API: %s (attempt %d/%d)",
                chunk_path,
                attempt,
                max_attempts,
            )
            try:
                create_kwargs: dict[str, Any] = {
                    "file": Path(chunk_path),
                    "model": self.config.model,
                    "response_format": "verbose_json",  # Ensure segments are included
                    "language": self.config.language,
                }
                if include_word_timestamps:
                    create_kwargs["timestamp_granularities"] = ["segment", "word"]
                transcription = self.client.audio.transcriptions.create(**create_kwargs)
            except Exception as exc:
                self.logger.warning(
                    "[GROQ_API_CALL] Attempt %d/%d failed for %s: %s",
                    attempt,
                    max_attempts,
                    chunk_path,
                    exc,
                )
                if attempt == max_attempts:
                    raise
                time.sleep(1.5**attempt)
                continue

            self.logger.info(
                "[GROQ_API_CALL] Received response from Groq API for: %s (attempt %d/%d)",
                chunk_path,
                attempt,
                max_attempts,
            )

            # The verbose_json response exposes `segments`, but it isn't on the
            # Groq Transcription type stub, so read it defensively.
            transcription_segments = getattr(transcription, "segments", None)
            if transcription_segments is None:
                self.logger.warning(
                    "[GROQ_API_CALL] No segments found in transcription for %s",
                    chunk_path,
                )
                return []

            groq_segments = []
            for seg in transcription_segments:
                # Reuse the OpenAI transcriber's defensive word parser: the
                # verbose_json word entries have the same shape.
                parsed_words = [
                    parsed_word
                    for parsed_word in (
                        OpenAIWhisperTranscriber._parse_word(word)
                        for word in seg.get("words", []) or []
                    )
                    if parsed_word is not None
                ]
                groq_segments.append(
                    GroqTranscriptionSegment(
                        start=seg["start"],
                        end=seg["end"],
                        text=seg["text"],
                        words=parsed_words or None,
                    )
                )

            self.logger.info(
                "[GROQ_API_CALL] Got %d segments from chunk (attempt %d/%d)",
                len(groq_segments),
                attempt,
                max_attempts,
            )
            return groq_segments

        # unreachable, but satisfies type checker
        return []
