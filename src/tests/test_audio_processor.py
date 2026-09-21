import logging
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from app.extensions import db
from app.models import (
    AudioSegment,
    Feed,
    Identification,
    ModelCall,
    Post,
    TranscriptSegment,
)
from podcast_processor.audio_processor import (
    AudioProcessor,
    RefinedBoundary,
    TimeWindow,
)
from shared.config import Config
from shared.test_utils import create_standard_test_config


@pytest.fixture
def test_processor(
    test_config: Config,
    test_logger: logging.Logger,
) -> AudioProcessor:
    """Return an AudioProcessor instance with default dependencies for testing."""
    return AudioProcessor(config=test_config, logger=test_logger)


@pytest.fixture
def test_processor_with_mocks(
    test_config: Config,
    test_logger: logging.Logger,
    mock_db_session: MagicMock,
) -> AudioProcessor:
    """Return an AudioProcessor instance with mock dependencies for testing."""
    mock_identification_query = MagicMock()
    mock_transcript_segment_query = MagicMock()
    mock_model_call_query = MagicMock()

    return AudioProcessor(
        config=test_config,
        logger=test_logger,
        identification_query=mock_identification_query,
        transcript_segment_query=mock_transcript_segment_query,
        model_call_query=mock_model_call_query,
        db_session=mock_db_session,
    )


def test_get_ad_segments(app: Flask) -> None:
    """Test retrieving ad segments from the database"""
    # Create test data
    post = Post(id=1, title="Test Post")
    segment = TranscriptSegment(
        id=1,
        post_id=1,
        sequence_num=0,
        start_time=0.0,
        end_time=10.0,
        text="Test segment",
    )
    identification = Identification(
        transcript_segment_id=1, model_call_id=1, label="ad", confidence=0.9
    )

    with app.app_context():
        # Create mocks
        mock_identification_query = MagicMock()
        mock_query_chain = MagicMock()
        mock_identification_query.join.return_value = mock_query_chain
        mock_query_chain.join.return_value = mock_query_chain
        mock_query_chain.filter.return_value = mock_query_chain
        mock_query_chain.all.return_value = [identification]

        # Create processor with mocks
        test_processor = AudioProcessor(
            config=create_standard_test_config(),
            identification_query=mock_identification_query,
        )

        with patch.object(identification, "transcript_segment", segment):
            segments = test_processor.get_ad_segments(post)

            assert len(segments) == 1
            assert segments[0] == (0.0, 10.0)


def test_merge_ad_segments(
    test_processor_with_mocks: AudioProcessor,
) -> None:
    """Test merging of nearby ad segments"""
    duration_ms = 30000  # 30 seconds
    ad_segments = [
        (0.0, 5.0),  # 0-5s
        (6.0, 10.0),  # 6-10s - should merge with first segment
        (20.0, 25.0),  # 20-25s - should stay separate
    ]

    merged = test_processor_with_mocks.merge_ad_segments(
        duration_ms=duration_ms,
        ad_segments=ad_segments,
        min_ad_segment_length_seconds=2.0,
        min_ad_segment_separation_seconds=2.0,
    )

    # Should merge first two segments
    assert len(merged) == 2
    assert merged[0] == (0, 10000)  # 0-10s
    assert merged[1] == (20000, 25000)  # 20-25s


def test_merge_ad_segments_with_short_segments(
    test_processor_with_mocks: AudioProcessor,
) -> None:
    """Test that segments shorter than minimum length are filtered out"""
    duration_ms = 30000
    ad_segments = [
        (0.0, 1.0),  # Too short, should be filtered
        (10.0, 15.0),  # Long enough, should stay
        (20.0, 20.5),  # Too short, should be filtered
    ]

    merged = test_processor_with_mocks.merge_ad_segments(
        duration_ms=duration_ms,
        ad_segments=ad_segments,
        min_ad_segment_length_seconds=2.0,
        min_ad_segment_separation_seconds=2.0,
    )

    assert len(merged) == 1
    assert merged[0] == (10000, 15000)


def test_merge_ad_segments_end_extension(
    test_processor_with_mocks: AudioProcessor,
) -> None:
    """Test that segments near the end are extended to the end"""
    duration_ms = 30000
    ad_segments = [
        (28.0, 29.0),  # Near end, should extend to 30s
    ]

    merged = test_processor_with_mocks.merge_ad_segments(
        duration_ms=duration_ms,
        ad_segments=ad_segments,
        min_ad_segment_length_seconds=2.0,
        min_ad_segment_separation_seconds=2.0,
    )

    assert len(merged) == 1
    assert merged[0] == (28000, 30000)  # Extended to end


def test_merge_ad_segments_preserves_short_preroll_edge(
    test_processor_with_mocks: AudioProcessor,
) -> None:
    merged = test_processor_with_mocks.merge_ad_segments(
        duration_ms=120000,
        ad_segments=[
            (0.3, 2.1),
            (15.059, 65.374),
        ],
        min_ad_segment_length_seconds=14.0,
        min_ad_segment_separation_seconds=5.0,
    )

    assert merged == [(300, 65373)]


def test_merge_ad_segments_preserves_short_postroll_edge(
    test_processor_with_mocks: AudioProcessor,
) -> None:
    merged = test_processor_with_mocks.merge_ad_segments(
        duration_ms=120000,
        ad_segments=[
            (50.0, 90.0),
            (104.0, 106.0),
        ],
        min_ad_segment_length_seconds=14.0,
        min_ad_segment_separation_seconds=5.0,
    )

    assert merged == [(50000, 106000)]


def test_process_audio(
    app: Flask,
    test_config: Config,
    test_logger: logging.Logger,
) -> None:
    """Test the process_audio method"""
    with app.app_context():
        processor = AudioProcessor(
            config=test_config, logger=test_logger, db_session=db.session
        )

        feed = Feed(title="Test Feed", rss_url="http://example.com/rss.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            title="Test Post",
            guid="test-audio-guid",
            download_url="http://example.com/audio.mp3",
            unprocessed_audio_path="path/to/audio.mp3",
        )
        db.session.add(post)
        db.session.commit()

        output_path = "path/to/output.mp3"

        # Set up mocks for get_ad_segments and get_audio_duration_ms
        with (
            patch.object(processor, "get_ad_segments", return_value=[(5.0, 10.0)]),
            patch(
                "podcast_processor.audio_processor.get_audio_duration_ms",
                side_effect=[30000, 24000],
            ),
            patch(
                "podcast_processor.audio_processor.clip_segments_with_fade"
            ) as mock_clip,
        ):
            # Call the method
            removed_segments = processor.process_audio(post, output_path)

            refreshed = db.session.get(Post, post.id)
            assert refreshed is not None
            assert refreshed.duration == 24.0  # processed output duration
            assert refreshed.processed_audio_path == output_path
            # The default test config extends a final ad segment to the end when
            # it is within the minimum separation threshold of the episode end.
            assert removed_segments == [(5000, 30000)]
            mock_clip.assert_called_once()
            assert "use_vbr" not in mock_clip.call_args.kwargs


def test_get_ad_segments_bridges_music_only_gap_with_ina_markers(app: Flask) -> None:
    with app.app_context():
        test_config = create_standard_test_config()
        test_config.output.min_ad_segment_separation_seconds = 5
        processor = AudioProcessor(
            config=test_config,
            logger=logging.getLogger("test_audio_processor"),
            db_session=db.session,
        )

        feed = Feed(title="Test Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="bridge-audio-guid",
            title="Bridge Audio Episode",
            download_url="https://example.com/audio.mp3",
        )
        db.session.add(post)
        db.session.commit()

        llm_call = ModelCall(
            post_id=post.id,
            first_segment_sequence_num=6,
            last_segment_sequence_num=10,
            model_name="groq/openai/gpt-oss-120b",
            prompt="Classify ads",
            status="success",
        )
        ina_call = ModelCall(
            post_id=post.id,
            first_segment_sequence_num=0,
            last_segment_sequence_num=0,
            model_name="ina:speech_music_noise",
            prompt="INA speech segmenter analysis",
            status="success",
        )
        db.session.add_all([llm_call, ina_call])
        db.session.commit()

        segments = [
            TranscriptSegment(
                post_id=post.id,
                sequence_num=6,
                start_time=26.0,
                end_time=26.5,
                text="There's no such thing.",
            ),
            TranscriptSegment(
                post_id=post.id,
                sequence_num=8,
                start_time=27.3,
                end_time=29.0,
                text="No such thing.",
            ),
            TranscriptSegment(
                post_id=post.id,
                sequence_num=9,
                start_time=40.3,
                end_time=41.6,
                text="This is an iHeart Podcast.",
            ),
            TranscriptSegment(
                post_id=post.id,
                sequence_num=10,
                start_time=42.8,
                end_time=43.7,
                text="Guaranteed human.",
            ),
        ]
        db.session.add_all(segments)
        db.session.commit()

        db.session.add_all(
            [
                Identification(
                    transcript_segment_id=segments[0].id,
                    model_call_id=llm_call.id,
                    label="ad",
                    confidence=0.98,
                ),
                Identification(
                    transcript_segment_id=segments[1].id,
                    model_call_id=llm_call.id,
                    label="ad",
                    confidence=0.98,
                ),
                Identification(
                    transcript_segment_id=segments[2].id,
                    model_call_id=llm_call.id,
                    label="ad",
                    confidence=0.97,
                ),
                Identification(
                    transcript_segment_id=segments[3].id,
                    model_call_id=llm_call.id,
                    label="ad",
                    confidence=0.97,
                ),
                AudioSegment(
                    post_id=post.id,
                    model_call_id=ina_call.id,
                    start_time=27.4,
                    end_time=39.0,
                    label="music",
                ),
                AudioSegment(
                    post_id=post.id,
                    model_call_id=ina_call.id,
                    start_time=39.0,
                    end_time=40.2,
                    label="noEnergy",
                ),
                AudioSegment(
                    post_id=post.id,
                    model_call_id=ina_call.id,
                    start_time=41.1,
                    end_time=42.6,
                    label="music",
                ),
            ]
        )
        db.session.commit()

        segments = processor.get_ad_segments(post)

        assert segments == [(26.0, 43.7)]


def _processor_with_refinement() -> AudioProcessor:
    test_config = create_standard_test_config()
    test_config.enable_boundary_refinement = True
    test_config.output.min_ad_segment_separation_seconds = 60
    return AudioProcessor(
        config=test_config,
        logger=logging.getLogger("test_audio_processor"),
        db_session=db.session,
    )


def _create_post_with_ad_segments(
    *,
    guid: str,
    segment_windows: list[tuple[float, float]],
    refined_ad_boundaries: list[dict[str, object]] | None,
) -> Post:
    feed = Feed(title=f"Test Feed {guid}", rss_url=f"https://example.com/{guid}.xml")
    db.session.add(feed)
    db.session.commit()

    post = Post(
        feed_id=feed.id,
        guid=guid,
        title=f"Test Post {guid}",
        download_url=f"https://example.com/{guid}.mp3",
        refined_ad_boundaries=refined_ad_boundaries,
    )
    db.session.add(post)
    db.session.commit()

    llm_call = ModelCall(
        post_id=post.id,
        first_segment_sequence_num=0,
        last_segment_sequence_num=len(segment_windows) - 1,
        model_name="gemini/gemini-3-flash-preview",
        prompt="Classify ads",
        status="success",
    )
    db.session.add(llm_call)
    db.session.commit()

    segments = [
        TranscriptSegment(
            post_id=post.id,
            sequence_num=index,
            start_time=start,
            end_time=end,
            text=f"Ad segment {index}",
        )
        for index, (start, end) in enumerate(segment_windows)
    ]
    db.session.add_all(segments)
    db.session.commit()

    db.session.add_all(
        [
            Identification(
                transcript_segment_id=segment.id,
                model_call_id=llm_call.id,
                label="ad",
                confidence=0.94,
            )
            for segment in segments
        ]
    )
    db.session.commit()
    return post


def test_get_ad_segments_applies_refinement_to_single_block(app: Flask) -> None:
    with app.app_context():
        processor = _processor_with_refinement()
        post = _create_post_with_ad_segments(
            guid="single-refined-block",
            segment_windows=[(15.1, 21.1), (57.0, 65.4)],
            refined_ad_boundaries=[
                {
                    "orig_start": 15.1,
                    "orig_end": 65.4,
                    "refined_start": 15.059,
                    "refined_end": 60.85,
                }
            ],
        )

        assert processor.get_ad_segments(post) == [(15.059, 60.85)]


def test_get_ad_segments_preserves_unrefined_preroll_edge(app: Flask) -> None:
    with app.app_context():
        processor = _processor_with_refinement()
        post = _create_post_with_ad_segments(
            guid="preserve-preroll-edge",
            segment_windows=[(0.3, 1.7), (1.7, 2.1), (15.1, 21.1), (57.0, 60.9)],
            refined_ad_boundaries=[
                {
                    "orig_start": 15.1,
                    "orig_end": 60.9,
                    "refined_start": 15.059,
                    "refined_end": 60.85,
                }
            ],
        )

        assert processor.get_ad_segments(post) == [(0.3, 60.85)]


def test_get_ad_segments_expands_preroll_to_audio_start_when_transcript_misses_edge(
    app: Flask,
) -> None:
    with app.app_context():
        processor = _processor_with_refinement()
        post = _create_post_with_ad_segments(
            guid="expand-preroll-audio-edge",
            segment_windows=[(15.1, 21.1), (57.0, 65.4)],
            refined_ad_boundaries=[
                {
                    "orig_start": 15.1,
                    "orig_end": 65.4,
                    "refined_start": 15.059,
                    "refined_end": 65.374,
                }
            ],
        )
        ina_call = ModelCall(
            post_id=post.id,
            first_segment_sequence_num=0,
            last_segment_sequence_num=2,
            model_name="ina:speech_music_noise",
            prompt="INA speech segmenter analysis",
            status="success",
        )
        db.session.add(ina_call)
        db.session.commit()
        db.session.add_all(
            [
                AudioSegment(
                    post_id=post.id,
                    model_call_id=ina_call.id,
                    start_time=0.0,
                    end_time=6.96,
                    label="speech",
                ),
                AudioSegment(
                    post_id=post.id,
                    model_call_id=ina_call.id,
                    start_time=6.96,
                    end_time=12.38,
                    label="music",
                ),
                AudioSegment(
                    post_id=post.id,
                    model_call_id=ina_call.id,
                    start_time=12.38,
                    end_time=30.06,
                    label="speech",
                ),
            ]
        )
        db.session.commit()

        assert processor.get_ad_segments(post) == [(0.0, 65.374)]


def test_get_ad_segments_does_not_expand_over_transcribed_intro_content(
    app: Flask,
) -> None:
    with app.app_context():
        processor = _processor_with_refinement()
        feed = Feed(
            title="Test Feed transcribed-intro",
            rss_url="https://example.com/transcribed-intro.xml",
        )
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="transcribed-intro-before-ad",
            title="Transcribed Intro Before Ad",
            download_url="https://example.com/transcribed-intro.mp3",
            refined_ad_boundaries=[
                {
                    "orig_start": 15.1,
                    "orig_end": 65.4,
                    "refined_start": 15.059,
                    "refined_end": 65.374,
                }
            ],
        )
        db.session.add(post)
        db.session.commit()

        llm_call = ModelCall(
            post_id=post.id,
            first_segment_sequence_num=1,
            last_segment_sequence_num=2,
            model_name="gemini/gemini-3-flash-preview",
            prompt="Classify ads",
            status="success",
        )
        ina_call = ModelCall(
            post_id=post.id,
            first_segment_sequence_num=0,
            last_segment_sequence_num=2,
            model_name="ina:speech_music_noise",
            prompt="INA speech segmenter analysis",
            status="success",
        )
        db.session.add_all([llm_call, ina_call])
        db.session.commit()

        intro_segment = TranscriptSegment(
            post_id=post.id,
            sequence_num=0,
            start_time=0.0,
            end_time=10.0,
            text="Welcome back. Before the ad, here is today's setup.",
        )
        ad_segments = [
            TranscriptSegment(
                post_id=post.id,
                sequence_num=1,
                start_time=15.1,
                end_time=21.1,
                text="Ad segment 1",
            ),
            TranscriptSegment(
                post_id=post.id,
                sequence_num=2,
                start_time=57.0,
                end_time=65.4,
                text="Ad segment 2",
            ),
        ]
        db.session.add_all([intro_segment, *ad_segments])
        db.session.commit()

        db.session.add_all(
            [
                Identification(
                    transcript_segment_id=segment.id,
                    model_call_id=llm_call.id,
                    label="ad",
                    confidence=0.94,
                )
                for segment in ad_segments
            ]
        )
        db.session.add_all(
            [
                AudioSegment(
                    post_id=post.id,
                    model_call_id=ina_call.id,
                    start_time=0.0,
                    end_time=10.0,
                    label="speech",
                ),
                AudioSegment(
                    post_id=post.id,
                    model_call_id=ina_call.id,
                    start_time=10.0,
                    end_time=15.1,
                    label="music",
                ),
                AudioSegment(
                    post_id=post.id,
                    model_call_id=ina_call.id,
                    start_time=15.1,
                    end_time=30.06,
                    label="speech",
                ),
            ]
        )
        db.session.commit()

        assert processor.get_ad_segments(post) == [(15.059, 65.374)]


def test_get_ad_segments_preserves_unrefined_trailing_edge(app: Flask) -> None:
    with app.app_context():
        processor = _processor_with_refinement()
        post = _create_post_with_ad_segments(
            guid="preserve-trailing-edge",
            segment_windows=[(15.1, 21.1), (57.0, 60.9), (72.0, 75.0)],
            refined_ad_boundaries=[
                {
                    "orig_start": 15.1,
                    "orig_end": 60.9,
                    "refined_start": 15.059,
                    "refined_end": 60.85,
                }
            ],
        )

        assert processor.get_ad_segments(post) == [(15.059, 75.0)]


def test_get_ad_segments_projects_multiple_refined_sub_blocks(app: Flask) -> None:
    with app.app_context():
        processor = _processor_with_refinement()
        post = _create_post_with_ad_segments(
            guid="multiple-refined-sub-blocks",
            segment_windows=[(10.0, 20.0), (32.0, 42.0), (55.0, 65.0)],
            refined_ad_boundaries=[
                {
                    "orig_start": 10.0,
                    "orig_end": 20.0,
                    "refined_start": 11.0,
                    "refined_end": 19.0,
                },
                {
                    "orig_start": 55.0,
                    "orig_end": 65.0,
                    "refined_start": 56.0,
                    "refined_end": 63.0,
                },
            ],
        )

        assert processor.get_ad_segments(post) == [(11.0, 63.0)]


def test_get_ad_segments_ignores_malformed_refinement_data(app: Flask) -> None:
    with app.app_context():
        processor = _processor_with_refinement()
        post = _create_post_with_ad_segments(
            guid="malformed-refinement-data",
            segment_windows=[(15.1, 21.1), (57.0, 65.4)],
            refined_ad_boundaries=[
                {
                    "orig_start": "not-a-number",
                    "orig_end": 65.4,
                    "refined_start": 15.059,
                    "refined_end": 60.85,
                },
                {
                    "orig_start": 15.1,
                    "orig_end": 65.4,
                    "refined_start": 60.85,
                    "refined_end": 15.059,
                },
                {
                    "orig_start": 65.4,
                    "orig_end": 15.1,
                    "refined_start": 15.059,
                    "refined_end": 60.85,
                },
            ],
        )

        assert processor.get_ad_segments(post) == [(15.1, 65.4)]


def test_get_ad_segments_unions_multiple_refined_windows_in_one_block(
    app: Flask,
) -> None:
    """Regression test: several refined ad windows inside a single atomic
    transcript block must all be honored (union), not just the best-overlap one.

    Reproduces the Odd Lots episode "How LA Is Quietly Becoming America's New
    Industrial Tech Hub": the atomic block 1620.2-1781.2 contained three refined
    windows, but only 1694.0-1775.0 was cut, leaving ~74s of ad audio behind.
    """
    with app.app_context():
        processor = _processor_with_refinement()
        post = _create_post_with_ad_segments(
            guid="union-refined-block",
            segment_windows=[(1620.2, 1650.0), (1652.0, 1709.0), (1710.0, 1781.2)],
            refined_ad_boundaries=[
                {
                    "orig_start": 1620.2,
                    "orig_end": 1650.0,
                    "refined_start": 1620.2,
                    "refined_end": 1650.2,
                },
                {
                    "orig_start": 1652.0,
                    "orig_end": 1709.0,
                    "refined_start": 1654.6,
                    "refined_end": 1709.6,
                },
                {
                    "orig_start": 1710.0,
                    "orig_end": 1781.2,
                    "refined_start": 1694.0,
                    "refined_end": 1775.0,
                },
            ],
        )

        assert processor.get_ad_segments(post) == [(1620.2, 1775.0)]


def test_project_atomic_block_unions_overlapping_refined_windows() -> None:
    block = TimeWindow(start=1620.2, end=1781.2)
    refined = [
        RefinedBoundary(
            orig_start=1620.2,
            orig_end=1650.0,
            refined_start=1620.2,
            refined_end=1650.2,
        ),
        RefinedBoundary(
            orig_start=1652.0,
            orig_end=1709.0,
            refined_start=1654.6,
            refined_end=1709.6,
        ),
        RefinedBoundary(
            orig_start=1710.0,
            orig_end=1781.2,
            refined_start=1694.0,
            refined_end=1775.0,
        ),
    ]

    projected = AudioProcessor._project_atomic_block(block, refined)

    assert (projected.start, projected.end) == (1620.2, 1775.0)


def test_project_atomic_block_ignores_non_overlapping_refined_windows() -> None:
    block = TimeWindow(start=100.0, end=200.0)
    refined = [
        RefinedBoundary(
            orig_start=0.0, orig_end=10.0, refined_start=0.0, refined_end=10.0
        ),
        RefinedBoundary(
            orig_start=120.0,
            orig_end=180.0,
            refined_start=121.5,
            refined_end=179.0,
        ),
        RefinedBoundary(
            orig_start=500.0,
            orig_end=600.0,
            refined_start=500.0,
            refined_end=600.0,
        ),
    ]

    projected = AudioProcessor._project_atomic_block(block, refined)

    assert (projected.start, projected.end) == (121.5, 179.0)


def test_project_atomic_block_without_match_returns_block() -> None:
    block = TimeWindow(start=100.0, end=200.0)
    refined = [
        RefinedBoundary(
            orig_start=500.0,
            orig_end=600.0,
            refined_start=500.0,
            refined_end=600.0,
        ),
    ]

    assert AudioProcessor._project_atomic_block(block, refined) == block
