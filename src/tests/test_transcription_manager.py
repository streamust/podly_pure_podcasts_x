import logging
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock

import pytest
from flask import Flask

from app.extensions import db
from app.models import Feed, ModelCall, Post, TranscriptSegment
from podcast_processor import transcription_manager as transcription_manager_module
from podcast_processor.transcribe import Segment, Transcriber, WordTimestamp
from podcast_processor.transcription_manager import TranscriptionManager
from shared.config import Config, TestWhisperConfig
from shared.test_utils import create_standard_test_config


class MockTranscriber(Transcriber):
    """Mock transcriber for testing TranscriptionManager."""

    def __init__(self, mock_response: list[Segment] | Exception | None = None):
        self.mock_response: list[Segment] | Exception = mock_response or []
        self._model_name = "mock_transcriber"

    @property
    def model_name(self) -> str:
        """Implementation of the abstract property"""
        return self._model_name

    def transcribe(
        self,
        audio_file_path: str,
        *,
        include_word_timestamps: bool = False,
        progress_callback: Any = None,
    ) -> list[Segment]:
        """Return mock segments or raise exception based on configuration."""
        del audio_file_path
        del include_word_timestamps
        del progress_callback
        if isinstance(self.mock_response, Exception):
            raise self.mock_response
        return self.mock_response


@pytest.fixture
def app() -> Generator[Flask]:
    """Create and configure a Flask app for testing."""
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    with app.app_context():
        db.init_app(app)
        db.create_all()
        yield app


@pytest.fixture
def test_config() -> Config:
    config = create_standard_test_config()
    # Override whisper config to use test mode
    config.whisper = TestWhisperConfig()
    return config


@pytest.fixture
def test_logger() -> logging.Logger:
    return logging.getLogger("test_logger")


@pytest.fixture
def mock_db_session() -> MagicMock:
    """Create a mock database session"""
    mock_session = MagicMock()
    mock_session.add = MagicMock()
    mock_session.add_all = MagicMock()
    mock_session.commit = MagicMock()
    mock_session.rollback = MagicMock()
    return mock_session


@pytest.fixture
def mock_transcriber() -> MockTranscriber:
    """Return a mock transcriber for testing."""
    return MockTranscriber(
        [
            Segment(start=0.0, end=5.0, text="Test segment 1"),
            Segment(start=5.0, end=10.0, text="Test segment 2"),
        ]
    )


@pytest.fixture
def test_manager(
    test_config: Config,
    test_logger: logging.Logger,
    mock_db_session: MagicMock,
    mock_transcriber: MockTranscriber,
    app: Flask,
) -> TranscriptionManager:
    """Return a TranscriptionManager instance for testing."""
    with app.app_context():
        # We need to create mock query objects with proper structure
        mock_model_call_query = MagicMock()
        mock_segment_query = MagicMock()

        # Create a manager with our mocks
        return TranscriptionManager(
            test_logger,
            test_config,
            model_call_query=mock_model_call_query,
            segment_query=mock_segment_query,
            db_session=mock_db_session,
            transcriber=mock_transcriber,
        )


def test_check_existing_transcription_success(
    test_manager: TranscriptionManager,
    app: Flask,
) -> None:
    """Test finding existing successful transcription"""
    post = Post(id=1, title="Test Post")

    # Create test data
    model_call = ModelCall(
        post_id=1,
        model_name=test_manager.transcriber.model_name,
        status="success",
        first_segment_sequence_num=0,
        last_segment_sequence_num=1,
    )
    segments = [
        TranscriptSegment(
            post_id=1, sequence_num=0, start_time=0.0, end_time=5.0, text="Segment 1"
        ),
        TranscriptSegment(
            post_id=1, sequence_num=1, start_time=5.0, end_time=10.0, text="Segment 2"
        ),
    ]

    with app.app_context():
        # Configure the existing mocks in the manager
        test_manager.model_call_query.filter_by().order_by().first.return_value = (
            model_call
        )
        test_manager.segment_query.filter_by().order_by().all.return_value = segments

        result = test_manager._check_existing_transcription(post)

        assert result is not None
        assert len(result) == 2
        assert result[0].text == "Segment 1"
        assert result[1].text == "Segment 2"


def test_check_existing_transcription_no_model_call(
    test_manager: TranscriptionManager,
    app: Flask,
) -> None:
    """Test when no existing ModelCall exists"""
    post = Post(id=1, title="Test Post")

    with app.app_context():
        # Set return value for the existing mock in the manager
        test_manager.model_call_query.filter_by().order_by().first.return_value = None

        result = test_manager._check_existing_transcription(post)
        assert result is None


def test_transcribe_new(
    test_config: Config,
    test_logger: logging.Logger,
    app: Flask,
) -> None:
    """Test transcribing a new audio file"""
    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="http://example.com/rss.xml")
        post = Post(
            feed=feed,
            guid="guid-1",
            download_url="http://example.com/audio-1.mp3",
            title="Test Post",
            unprocessed_audio_path="/path/to/audio.mp3",
        )
        db.session.add_all([feed, post])
        db.session.commit()

        transcriber = MockTranscriber(
            [
                Segment(start=0.0, end=5.0, text="Test segment 1"),
                Segment(start=5.0, end=10.0, text="Test segment 2"),
            ]
        )
        manager = TranscriptionManager(
            test_logger,
            test_config,
            db_session=db.session,
            transcriber=transcriber,
        )

        segments = manager.transcribe(post)

        assert len(segments) == 2
        assert segments[0].text == "Test segment 1"
        assert segments[1].text == "Test segment 2"
        assert TranscriptSegment.query.filter_by(post_id=post.id).count() == 2
        assert ModelCall.query.filter_by(post_id=post.id).count() == 1
        assert ModelCall.query.filter_by(post_id=post.id).first().status == "success"


def test_transcribe_new_writes_transcript_in_chunks(
    test_config: Config,
    test_logger: logging.Logger,
    app: Flask,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PODLY_WRITER_BATCH_SIZE", "1")
    action_names: list[str] = []
    original_action = transcription_manager_module.writer_client.action

    # Filter out `dequeue_job` because the background job-scheduler thread
    # in the writer service polls via that action regardless of what the
    # test is doing. If a prior test in the same pytest invocation left
    # the scheduler running, those polls land in `action_names` and break
    # the equality assertion below. We only care about the writes the
    # transcription manager itself emits.
    def spy_action(action_name: str, params: dict, wait: bool = True):
        if action_name != "dequeue_job":
            action_names.append(action_name)
        return original_action(action_name, params, wait=wait)

    monkeypatch.setattr(
        transcription_manager_module.writer_client, "action", spy_action
    )

    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="http://example.com/rss-chunked.xml")
        post = Post(
            feed=feed,
            guid="guid-chunked",
            download_url="http://example.com/audio-chunked.mp3",
            title="Test Post",
            unprocessed_audio_path="/path/to/audio.mp3",
        )
        db.session.add_all([feed, post])
        db.session.commit()

        manager = TranscriptionManager(
            test_logger,
            test_config,
            db_session=db.session,
            transcriber=MockTranscriber(
                [
                    Segment(start=0.0, end=1.0, text="one"),
                    Segment(start=1.0, end=2.0, text="two"),
                    Segment(start=2.0, end=3.0, text="three"),
                ]
            ),
        )

        db_segments, _ = manager.transcribe_for_processing(post)

        assert [segment.text for segment in db_segments] == ["one", "two", "three"]
        assert action_names == [
            "upsert_whisper_model_call",
            "start_transcription_replace",
            "insert_transcript_segments",
            "insert_transcript_segments",
            "insert_transcript_segments",
            "finish_transcription_replace",
        ]


def test_transcribe_handles_error(
    test_config: Config,
    test_logger: logging.Logger,
    app: Flask,
) -> None:
    """Test error handling during transcription"""
    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="http://example.com/rss.xml")
        post = Post(
            feed=feed,
            guid="guid-err",
            download_url="http://example.com/audio-err.mp3",
            title="Test Post",
            unprocessed_audio_path="/path/to/audio.mp3",
        )
        db.session.add_all([feed, post])
        db.session.commit()

        # Create a mock transcriber that raises an exception
        error_transcriber = MockTranscriber(Exception("Transcription failed"))

        manager = TranscriptionManager(
            test_logger,
            test_config,
            db_session=db.session,
            transcriber=error_transcriber,
        )

        # Test the exception
        with pytest.raises(Exception) as exc_info:
            manager.transcribe(post)

        assert str(exc_info.value) == "Transcription failed"
        call = (
            ModelCall.query.filter_by(post_id=post.id)
            .order_by(ModelCall.timestamp.desc())
            .first()
        )
        assert call is not None
        assert call.status == "failed_permanent"
        assert call.error_message == "Transcription failed"


def test_transcribe_reuses_placeholder_model_call(
    test_config: Config,
    test_logger: logging.Logger,
    app: Flask,
) -> None:
    """Ensure we reuse existing placeholder ModelCall rows instead of crashing on uniqueness."""
    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="http://example.com/rss.xml")
        post = Post(
            feed=feed,
            guid="guid-123",
            download_url="http://example.com/audio.mp3",
            title="Test Post",
            unprocessed_audio_path="/tmp/audio.mp3",
        )
        db.session.add_all([feed, post])
        db.session.commit()

        existing_call = ModelCall(
            post_id=post.id,
            model_name="mock_transcriber",
            first_segment_sequence_num=0,
            last_segment_sequence_num=-1,
            prompt="Whisper transcription job",
            status="failed_permanent",
        )
        db.session.add(existing_call)
        db.session.commit()

        manager = TranscriptionManager(
            test_logger,
            test_config,
            db_session=db.session,
            transcriber=MockTranscriber(
                [
                    Segment(start=0.0, end=5.0, text="Segment 1"),
                    Segment(start=5.0, end=10.0, text="Segment 2"),
                ]
            ),
        )

        segments = manager.transcribe(post)

        assert len(segments) == 2
        assert ModelCall.query.count() == 1
        refreshed_call = ModelCall.query.first()
        assert refreshed_call.id == existing_call.id
        assert refreshed_call.status == "success"
        assert refreshed_call.last_segment_sequence_num == 1


def test_transcribe_persists_optional_speaker_labels(
    test_config: Config,
    test_logger: logging.Logger,
    app: Flask,
) -> None:
    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="http://example.com/rss-speaker.xml")
        post = Post(
            feed=feed,
            guid="guid-speaker",
            download_url="http://example.com/audio-speaker.mp3",
            title="Test Post",
            unprocessed_audio_path="/path/to/audio.mp3",
        )
        db.session.add_all([feed, post])
        db.session.commit()

        transcriber = MockTranscriber(
            [
                Segment(
                    start=0.0,
                    end=5.0,
                    text="Host intro",
                    speaker_label="SPEAKER_00",
                ),
                Segment(
                    start=5.0,
                    end=10.0,
                    text="Promo read",
                    speaker_label="SPEAKER_01",
                ),
            ]
        )
        manager = TranscriptionManager(
            test_logger,
            test_config,
            db_session=db.session,
            transcriber=transcriber,
        )

        segments = manager.transcribe(post)

        assert len(segments) == 2
        assert segments[0].speaker_label == "SPEAKER_00"
        assert segments[1].speaker_label == "SPEAKER_01"


def test_transcribe_for_processing_returns_rich_segments_when_requested(
    test_config: Config,
    test_logger: logging.Logger,
    app: Flask,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PODLY_INSTANCE_DIR", str(tmp_path / "instance"))
    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="http://example.com/rss-rich.xml")
        post = Post(
            feed=feed,
            guid="guid-rich",
            download_url="http://example.com/audio-rich.mp3",
            title="Test Post",
            unprocessed_audio_path="/path/to/audio.mp3",
        )
        db.session.add_all([feed, post])
        db.session.commit()

        rich_segments = [
            Segment(
                start=0.0,
                end=1.0,
                text="Hello",
                words=[WordTimestamp(word="Hello", start=0.0, end=1.0, score=0.98)],
            ),
            Segment(
                start=1.0,
                end=2.0,
                text="world",
                words=[WordTimestamp(word="world", start=1.0, end=2.0, score=0.97)],
            ),
        ]
        manager = TranscriptionManager(
            test_logger,
            test_config,
            db_session=db.session,
            transcriber=MockTranscriber(rich_segments),
        )

        db_segments, returned_rich_segments = manager.transcribe_for_processing(
            post,
            include_word_timestamps=True,
        )

        assert len(db_segments) == 2
        assert returned_rich_segments == rich_segments
        db.session.refresh(post)
        assert post.transcript_word_timestamps == [
            {
                "sequence_num": 0,
                "words": [{"word": "Hello", "start": 0.0, "end": 1.0, "score": 0.98}],
            },
            {
                "sequence_num": 1,
                "words": [{"word": "world", "start": 1.0, "end": 2.0, "score": 0.97}],
            },
        ]


def test_transcribe_for_processing_reuses_saved_word_timestamps(
    test_config: Config,
    test_logger: logging.Logger,
    app: Flask,
) -> None:
    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="http://example.com/rss-reuse.xml")
        post = Post(
            feed=feed,
            guid="guid-reuse",
            download_url="http://example.com/audio-reuse.mp3",
            title="Test Post",
            unprocessed_audio_path="/path/to/audio.mp3",
            transcript_word_timestamps=[
                {
                    "sequence_num": 0,
                    "words": [
                        {
                            "word": "Hello",
                            "start": 0.0,
                            "end": 0.4,
                            "score": 0.98,
                        },
                        {
                            "word": "world",
                            "start": 0.41,
                            "end": 1.0,
                            "score": 0.97,
                        },
                    ],
                }
            ],
        )
        db.session.add_all([feed, post])
        db.session.commit()

        db.session.add(
            TranscriptSegment(
                post_id=post.id,
                sequence_num=0,
                start_time=0.0,
                end_time=1.0,
                text="Hello world",
            )
        )
        db.session.add(
            ModelCall(
                post_id=post.id,
                model_name="mock_transcriber",
                first_segment_sequence_num=0,
                last_segment_sequence_num=0,
                prompt="Whisper transcription job",
                status="success",
            )
        )
        db.session.commit()

        manager = TranscriptionManager(
            test_logger,
            test_config,
            db_session=db.session,
            transcriber=MockTranscriber(Exception("should not re-transcribe")),
        )

        db_segments, rich_segments = manager.transcribe_for_processing(
            post,
            include_word_timestamps=True,
        )

        assert len(db_segments) == 1
        assert rich_segments is not None
        assert [word.word for word in (rich_segments[0].words or [])] == [
            "Hello",
            "world",
        ]


def test_transcribe_for_processing_falls_back_when_existing_transcript_has_no_saved_words_and_transcriber_cannot_supply_them(
    test_config: Config,
    test_logger: logging.Logger,
    app: Flask,
) -> None:
    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="http://example.com/rss-fallback.xml")
        post = Post(
            feed=feed,
            guid="guid-fallback",
            download_url="http://example.com/audio-fallback.mp3",
            title="Test Post",
            unprocessed_audio_path="/path/to/audio.mp3",
        )
        db.session.add_all([feed, post])
        db.session.commit()

        db.session.add(
            TranscriptSegment(
                post_id=post.id,
                sequence_num=0,
                start_time=0.0,
                end_time=1.0,
                text="Hello world",
            )
        )
        db.session.add(
            ModelCall(
                post_id=post.id,
                model_name="mock_transcriber",
                first_segment_sequence_num=0,
                last_segment_sequence_num=0,
                prompt="Whisper transcription job",
                status="success",
            )
        )
        db.session.commit()

        manager = TranscriptionManager(
            test_logger,
            test_config,
            db_session=db.session,
            transcriber=MockTranscriber(Exception("should not re-transcribe")),
        )

        db_segments, rich_segments = manager.transcribe_for_processing(
            post,
            include_word_timestamps=True,
        )

        assert len(db_segments) == 1
        assert rich_segments is None


def test_supports_word_timestamps_for_groq_transcriber(
    test_config: Config,
    test_logger: logging.Logger,
    mock_db_session: MagicMock,
    app: Flask,
    mocker: Any,
) -> None:
    """GroqWhisperTranscriber must be recognized as word-timestamp capable."""
    from podcast_processor.transcribe import GroqWhisperTranscriber
    from shared.config import GroqWhisperConfig

    mocker.patch("podcast_processor.transcribe.Groq", return_value=MagicMock())
    with app.app_context():
        groq_transcriber = GroqWhisperTranscriber(
            test_logger,
            GroqWhisperConfig(api_key="test", model="whisper-large-v3-turbo"),
        )
        manager = TranscriptionManager(
            test_logger,
            test_config,
            model_call_query=MagicMock(),
            segment_query=MagicMock(),
            db_session=mock_db_session,
            transcriber=groq_transcriber,
        )
        assert manager._transcriber_supports_word_timestamps() is True
