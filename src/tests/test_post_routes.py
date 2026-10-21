import datetime
import json
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

import pytest
from flask import g
from sqlalchemy import event

from app.extensions import db
from app.models import (
    AudioSegment,
    Feed,
    Identification,
    ModelCall,
    Post,
    ProcessingJob,
    TranscriptSegment,
    User,
)
from app.routes.post_routes import post_bp
from app.runtime_config import config as runtime_config
from shared.config import RemoteWhisperConfig


def _encoded_guid(guid: str) -> str:
    return quote(guid, safe="")


def test_processing_estimate_accepts_guid_with_slashes(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Slash GUID Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="tag:audioboom.com,2026-03-26:/posts/8879470",
            download_url="https://example.com/audio.mp3",
            title="Slash GUID Episode",
            duration=1800,
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()
        guid = post.guid

    response = app.test_client().get(
        f"/api/posts/{_encoded_guid(guid)}/processing-estimate"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert payload["post_guid"] == guid
    assert payload["estimated_minutes"] == 30.0


def test_process_post_accepts_guid_with_slashes(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Slash GUID Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="tag:audioboom.com,2026-03-26:/posts/8879470",
            download_url="https://example.com/audio.mp3",
            title="Slash GUID Episode",
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()
        guid = post.guid

    with mock.patch("app.routes.post_routes.get_jobs_manager") as mock_mgr:
        mock_mgr.return_value.start_post_processing.return_value = {
            "status": "started",
            "job_id": "job-123",
            "message": "ok",
        }

        response = app.test_client().post(f"/api/posts/{_encoded_guid(guid)}/process")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert payload["status"] == "started"
    mock_mgr.return_value.start_post_processing.assert_called_once_with(
        guid,
        priority="interactive",
        requested_by_user_id=None,
        billing_user_id=None,
    )


def test_download_endpoints_increment_counter(app, tmp_path):
    """Ensure both processed and original downloads increment the counter."""
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        processed_audio = tmp_path / "processed.mp3"
        processed_audio.write_bytes(b"processed audio")

        original_audio = tmp_path / "original.mp3"
        original_audio.write_bytes(b"original audio")

        post = Post(
            feed_id=feed.id,
            guid="test-guid",
            download_url="https://example.com/audio.mp3",
            title="Test Episode",
            processed_audio_path=str(processed_audio),
            unprocessed_audio_path=str(original_audio),
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()

        client = app.test_client()

        # Mock writer_client to simulate DB update
        with mock.patch("app.routes.post_utils.writer_client") as mock_writer:

            def side_effect(action, params, wait=False):
                if action == "increment_download_count":
                    post_id = params["post_id"]
                    Post.query.filter_by(id=post_id).update(
                        {Post.download_count: (Post.download_count or 0) + 1}
                    )
                    db.session.commit()

            mock_writer.action.side_effect = side_effect

            response = client.get(f"/api/posts/{post.guid}/download")
            assert response.status_code == 200
            db.session.refresh(post)
            assert post.download_count == 1

            response = client.get(f"/api/posts/{post.guid}/download/original")
            assert response.status_code == 200
            db.session.refresh(post)
            assert post.download_count == 2


def test_audio_endpoint_supports_range_requests(app, tmp_path):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        processed_audio = tmp_path / "processed.mp3"
        processed_audio.write_bytes(b"processed audio")

        post = Post(
            feed_id=feed.id,
            guid="stream-guid",
            download_url="https://example.com/audio.mp3",
            title="Stream Episode",
            processed_audio_path=str(processed_audio),
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()
        post_guid = post.guid

    client = app.test_client()
    response = client.get(
        f"/api/posts/{post_guid}/audio",
        headers={"Range": "bytes=0-8"},
    )

    assert response.status_code == 206
    assert response.data == b"processed"
    assert response.headers["Accept-Ranges"] == "bytes"
    assert "attachment" not in response.headers.get("Content-Disposition", "").lower()


def test_audio_endpoint_increments_counter_for_initial_requests(app, tmp_path):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        processed_audio = tmp_path / "processed.mp3"
        processed_audio.write_bytes(b"processed audio")

        post = Post(
            feed_id=feed.id,
            guid="count-audio-guid",
            download_url="https://example.com/audio.mp3",
            title="Count Audio Episode",
            processed_audio_path=str(processed_audio),
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()

        client = app.test_client()

        with mock.patch("app.routes.post_utils.writer_client") as mock_writer:

            def side_effect(action, params, wait=False):
                if action == "increment_download_count":
                    post_id = params["post_id"]
                    Post.query.filter_by(id=post_id).update(
                        {Post.download_count: (Post.download_count or 0) + 1}
                    )
                    db.session.commit()

            mock_writer.action.side_effect = side_effect

            response = client.get(f"/api/posts/{post.guid}/audio")
            assert response.status_code == 200
            db.session.refresh(post)
            assert post.download_count == 1

            response = client.get(
                f"/api/posts/{post.guid}/audio",
                headers={"Range": "bytes=0-8"},
            )
            assert response.status_code == 206
            db.session.refresh(post)
            assert post.download_count == 2


def test_audio_endpoint_skips_counter_for_non_initial_range_requests(app, tmp_path):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        processed_audio = tmp_path / "processed.mp3"
        processed_audio.write_bytes(b"processed audio")

        post = Post(
            feed_id=feed.id,
            guid="skip-range-guid",
            download_url="https://example.com/audio.mp3",
            title="Skip Range Episode",
            processed_audio_path=str(processed_audio),
            whitelisted=True,
            download_count=7,
        )
        db.session.add(post)
        db.session.commit()

        client = app.test_client()

        with mock.patch("app.routes.post_utils.writer_client") as mock_writer:
            response = client.get(
                f"/api/posts/{post.guid}/audio",
                headers={"Range": "bytes=9-15"},
            )
            assert response.status_code == 206
            db.session.refresh(post)
            assert post.download_count == 7
            mock_writer.action.assert_not_called()


def test_audio_triggers_processing_when_enabled(app):
    """Start processing when streamed audio is missing and toggle is enabled."""
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="missing-stream-guid",
            download_url="https://example.com/audio.mp3",
            title="Missing Stream Audio",
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()
        post_guid = post.guid

    client = app.test_client()
    original_flag = runtime_config.autoprocess_on_download
    runtime_config.autoprocess_on_download = True
    try:
        with mock.patch("app.routes.post_utils.get_jobs_manager") as mock_mgr:
            mock_mgr.return_value.start_post_processing.return_value = {
                "status": "started",
                "job_id": "job-stream-123",
            }
            response = client.get(f"/post/{post_guid}.mp3")
            assert response.status_code == 202
            payload = response.get_json()
            assert payload["status"] == "started"
            mock_mgr.return_value.start_post_processing.assert_called_once_with(
                post_guid,
                priority="download",
                requested_by_user_id=None,
                billing_user_id=None,
            )
    finally:
        runtime_config.autoprocess_on_download = original_flag


def test_audio_auto_whitelists_post(app, tmp_path):
    """Inline audio request should whitelist the post automatically."""
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        processed_audio = tmp_path / "processed.mp3"
        processed_audio.write_bytes(b"processed audio")

        post = Post(
            feed_id=feed.id,
            guid="stream-auto-whitelist-guid",
            download_url="https://example.com/audio.mp3",
            title="Auto Whitelist Stream Episode",
            processed_audio_path=str(processed_audio),
            whitelisted=False,
        )
        db.session.add(post)
        db.session.commit()
        post_guid = post.guid
        post_id = post.id

    client = app.test_client()

    original_flag = runtime_config.autoprocess_on_download
    runtime_config.autoprocess_on_download = True
    try:
        with mock.patch("app.routes.post_utils.writer_client") as mock_writer:
            mock_writer.action.return_value = SimpleNamespace(success=True, data=None)
            response = client.get(f"/post/{post_guid}.mp3")
            assert response.status_code == 200
            mock_writer.action.assert_has_calls(
                [
                    mock.call("whitelist_post", {"post_id": post_id}, wait=True),
                    mock.call(
                        "increment_download_count",
                        {"post_id": post_id},
                        wait=False,
                    ),
                ]
            )
    finally:
        runtime_config.autoprocess_on_download = original_flag


def test_download_triggers_processing_when_enabled(app):
    """Start processing when processed audio is missing and toggle is enabled."""
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="missing-audio-guid",
            download_url="https://example.com/audio.mp3",
            title="Missing Audio",
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()
        post_guid = post.guid

    client = app.test_client()
    original_flag = runtime_config.autoprocess_on_download
    runtime_config.autoprocess_on_download = True
    try:
        with mock.patch("app.routes.post_utils.get_jobs_manager") as mock_mgr:
            mock_mgr.return_value.start_post_processing.return_value = {
                "status": "started",
                "job_id": "job-123",
            }
            response = client.get(f"/api/posts/{post_guid}/download")
            assert response.status_code == 202
            payload = response.get_json()
            assert payload["status"] == "started"
            mock_mgr.return_value.start_post_processing.assert_called_once_with(
                post_guid,
                priority="download",
                requested_by_user_id=None,
                billing_user_id=None,
            )
    finally:
        runtime_config.autoprocess_on_download = original_flag


def test_download_missing_audio_returns_404_when_disabled(app):
    """Keep existing 404 behavior when toggle is off."""
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="missing-audio-404",
            download_url="https://example.com/audio.mp3",
            title="Missing Audio",
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()
        post_guid = post.guid

    client = app.test_client()
    original_flag = runtime_config.autoprocess_on_download
    runtime_config.autoprocess_on_download = False
    try:
        with mock.patch("app.routes.post_utils.get_jobs_manager") as mock_mgr:
            response = client.get(f"/api/posts/{post_guid}/download")
            assert response.status_code == 404
            mock_mgr.return_value.start_post_processing.assert_not_called()
    finally:
        runtime_config.autoprocess_on_download = original_flag


def test_download_auto_whitelists_post(app, tmp_path):
    """Download request should whitelist the post automatically."""
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        processed_audio = tmp_path / "processed.mp3"
        processed_audio.write_bytes(b"processed audio")

        post = Post(
            feed_id=feed.id,
            guid="auto-whitelist-guid",
            download_url="https://example.com/audio.mp3",
            title="Auto Whitelist Episode",
            processed_audio_path=str(processed_audio),
            whitelisted=False,
        )
        db.session.add(post)
        db.session.commit()
        post_guid = post.guid
        post_id = post.id

    client = app.test_client()

    original_flag = runtime_config.autoprocess_on_download
    runtime_config.autoprocess_on_download = True

    with mock.patch("app.routes.post_utils.writer_client") as mock_writer:
        mock_writer.action.return_value = SimpleNamespace(success=True, data=None)
        response = client.get(f"/api/posts/{post_guid}/download")
        assert response.status_code == 200
        mock_writer.action.assert_has_calls(
            [
                mock.call("whitelist_post", {"post_id": post_id}, wait=True),
                mock.call("increment_download_count", {"post_id": post_id}, wait=False),
            ]
        )
    runtime_config.autoprocess_on_download = original_flag


def test_download_rejects_when_not_whitelisted_and_toggle_off(app):
    """Ensure download is forbidden when not whitelisted and auto-process toggle is off."""
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Test Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="no-autoprocess-whitelist",
            download_url="https://example.com/audio.mp3",
            title="No Auto",
            whitelisted=False,
        )
        db.session.add(post)
        db.session.commit()
        post_guid = post.guid

    client = app.test_client()
    original_flag = runtime_config.autoprocess_on_download
    runtime_config.autoprocess_on_download = False
    try:
        response = client.get(f"/api/posts/{post_guid}/download")
        assert response.status_code == 403
    finally:
        runtime_config.autoprocess_on_download = original_flag


def test_toggle_whitelist_all_requires_admin(app):
    """Ensure bulk whitelist actions are limited to admins."""
    app.testing = True
    app.register_blueprint(post_bp)
    app.config["AUTH_SETTINGS"] = SimpleNamespace(require_auth=True)

    with app.app_context():
        admin_user = User(username="admin", password_hash="hash", role="admin")
        regular_user = User(username="user", password_hash="hash", role="user")
        feed = Feed(title="Admin Feed", rss_url="https://example.com/feed.xml")
        db.session.add_all([admin_user, regular_user, feed])
        db.session.commit()

        posts = [
            Post(
                feed_id=feed.id,
                guid=f"guid-{idx}",
                download_url=f"https://example.com/{idx}.mp3",
                title=f"Episode {idx}",
                whitelisted=False,
            )
            for idx in range(2)
        ]
        db.session.add_all(posts)
        db.session.commit()

        admin_id = admin_user.id
        regular_id = regular_user.id
        feed_id = feed.id

    current_user = {"id": admin_id}

    @app.before_request
    def _mock_auth() -> None:
        g.current_user = SimpleNamespace(id=current_user["id"])

    client = app.test_client()
    current_user["id"] = regular_id
    response = client.post(f"/api/feeds/{feed_id}/toggle-whitelist-all")
    assert response.status_code == 403
    assert response.get_json()["error"].startswith("Only admins")

    current_user["id"] = admin_id
    response = client.post(f"/api/feeds/{feed_id}/toggle-whitelist-all")
    assert response.status_code == 200
    with app.app_context():
        whitelisted = Post.query.filter_by(feed_id=feed_id, whitelisted=True).count()
        assert whitelisted == 2


@pytest.mark.parametrize("initial_statuses", [[], [True, False], [True, True]])
def test_toggle_whitelist_all_does_not_load_post_entities(app, initial_statuses):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Bulk Feed", rss_url="https://example.com/bulk.xml")
        db.session.add(feed)
        db.session.flush()
        feed_id = feed.id
        db.session.add_all(
            [
                Post(
                    feed_id=feed_id,
                    guid=f"bulk-memory-{index}",
                    title=f"Episode {index}",
                    download_url=f"https://example.com/{index}.mp3",
                    whitelisted=status,
                    transcript_word_timestamps=[{"word": "unused", "start": 0}],
                )
                for index, status in enumerate(initial_statuses)
            ]
        )
        db.session.commit()

    loaded_posts = []

    def record_post_load(post, context):
        loaded_posts.append(post.id)

    event.listen(Post, "load", record_post_load)
    try:
        response = app.test_client().post(f"/api/feeds/{feed_id}/toggle-whitelist-all")
    finally:
        event.remove(Post, "load", record_post_load)

    assert response.status_code == 200
    assert loaded_posts == []
    payload = response.get_json()
    assert payload["total_count"] == len(initial_statuses)
    if not initial_statuses:
        assert payload["message"] == "No posts found in this feed"
        assert payload["whitelisted_count"] == 0
    else:
        expected_status = not all(initial_statuses)
        assert payload["all_whitelisted"] is expected_status
        assert payload["updated_count"] == len(initial_statuses)
        assert payload["whitelisted_count"] == (
            len(initial_statuses) if expected_status else 0
        )
        with app.app_context():
            assert Post.query.filter_by(
                feed_id=feed_id, whitelisted=expected_status
            ).count() == len(initial_statuses)


def test_feed_posts_pagination_and_filtering(app):
    """Feed posts endpoint should paginate and support whitelisted filter."""

    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Paged Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        base_date = datetime.date(2024, 1, 1)
        posts = []
        # Create 30 posts with descending dates; even ones whitelisted.
        for idx in range(30):
            post = Post(
                feed_id=feed.id,
                guid=f"guid-{idx}",
                download_url=f"https://example.com/{idx}.mp3",
                title=f"Episode {idx}",
                release_date=base_date + datetime.timedelta(days=idx),
                whitelisted=(idx % 2 == 0),
            )
            posts.append(post)

        db.session.add_all(posts)
        db.session.commit()

        client = app.test_client()

        # Default page (1) should return 25 items ordered newest-first
        response = client.get(f"/api/feeds/{feed.id}/posts")
        assert response.status_code == 200
        data = response.get_json()
        assert data["page"] == 1
        assert data["page_size"] == 25
        assert data["total"] == 30
        assert data["total_pages"] == 2
        assert len(data["items"]) == 25
        # First item should be newest (idx 29)
        assert data["items"][0]["guid"] == "guid-29"
        # Last item on page 1 should be idx 5 (25 items: 29..5)
        assert data["items"][-1]["guid"] == "guid-5"

        # Page 2 should return remaining 5
        response = client.get(f"/api/feeds/{feed.id}/posts", query_string={"page": 2})
        assert response.status_code == 200
        data_page_2 = response.get_json()
        assert data_page_2["page"] == 2
        assert len(data_page_2["items"]) == 5
        # Items should be 4..0
        assert {item["guid"] for item in data_page_2["items"]} == {
            "guid-4",
            "guid-3",
            "guid-2",
            "guid-1",
            "guid-0",
        }

        # Whitelisted filter should only return whitelisted posts (15 total)
        response = client.get(
            f"/api/feeds/{feed.id}/posts",
            query_string={"whitelisted_only": "true"},
        )
        assert response.status_code == 200
        filtered = response.get_json()
        assert filtered["total"] == 15
        assert filtered["whitelisted_total"] == 15
        assert all(item["whitelisted"] for item in filtered["items"])


def test_feed_posts_include_podly_description_html(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Chapter Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="chapter-guid",
            download_url="https://example.com/chapter.mp3",
            title="Episode With Chapters",
            description="<p>Original episode description</p>",
            chapter_data=json.dumps(
                {
                    "chapters_for_output": [
                        {"start_time": 0.0, "title": "Intro"},
                        {"start_time": 485.0, "title": "Gold mission"},
                    ]
                }
            ),
        )
        db.session.add(post)
        db.session.commit()

        client = app.test_client()
        response = client.get(f"/api/feeds/{feed.id}/posts")

    assert response.status_code == 200
    data = response.get_json()
    assert data is not None
    item = data["items"][0]
    assert item["description"] == "<p>Original episode description</p>"
    assert "Original episode description" in item["podly_description_html"]
    assert "Podly Chapters" in item["podly_description_html"]
    assert "<li>00:00 Intro</li>" in item["podly_description_html"]
    assert "<li>08:05 Gold mission</li>" in item["podly_description_html"]
    assert "Podly Post JSON" not in item["podly_description_html"]


def test_feed_posts_defers_heavyweight_json_columns(app):
    """api_feed_posts must not load transcript_word_timestamps / bleep_windows /
    refined_ad_boundaries — those JSON blobs can be megabytes per row and the
    endpoint never serializes them, so loading them turns a paginated GET into
    a ~50MB allocation per call.
    """
    from sqlalchemy import inspect as sa_inspect

    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Heavy JSON Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        large_word_timestamps = [
            {"word": f"word-{i}", "start": float(i), "end": float(i) + 0.5}
            for i in range(200)
        ]
        post = Post(
            feed_id=feed.id,
            guid="defer-guid",
            download_url="https://example.com/defer.mp3",
            title="Heavy episode",
            transcript_word_timestamps=large_word_timestamps,
            bleep_windows=[[1000, 2000]],
            refined_ad_boundaries=[[10.0, 20.0]],
        )
        db.session.add(post)
        db.session.commit()
        db.session.expire_all()

        client = app.test_client()
        response = client.get(f"/api/feeds/{feed.id}/posts")
        assert response.status_code == 200

        # After the endpoint runs, re-fetching via the same defer options must
        # leave the heavy columns unloaded. Use the same helper the endpoint
        # uses so we test the exact configuration the endpoint applies.
        from app.feeds import post_feed_render_defers

        db.session.expire_all()
        fetched = (
            Post.query.filter_by(feed_id=feed.id)
            .options(*post_feed_render_defers())
            .first()
        )
        unloaded = sa_inspect(fetched).unloaded
        assert "transcript_word_timestamps" in unloaded
        assert "bleep_windows" in unloaded
        assert "refined_ad_boundaries" in unloaded
        # Spot-check we didn't accidentally defer something the endpoint needs.
        assert "title" not in unloaded
        assert "description" not in unloaded


def test_reprocess_keep_transcript_accepts_active_whisper_model_call(app):
    app.testing = True
    app.register_blueprint(post_bp)
    original_whisper = runtime_config.whisper
    runtime_config.whisper = RemoteWhisperConfig(api_key="test-key", model="whisper-1")

    try:
        with app.app_context():
            feed = Feed(
                title="Remote Whisper Feed", rss_url="https://example.com/feed.xml"
            )
            db.session.add(feed)
            db.session.commit()

            post = Post(
                feed_id=feed.id,
                guid="remote-whisper-guid",
                download_url="https://example.com/audio.mp3",
                title="Remote Whisper Episode",
                whitelisted=True,
            )
            db.session.add(post)
            db.session.commit()

            db.session.add(
                TranscriptSegment(
                    post_id=post.id,
                    sequence_num=0,
                    start_time=0.0,
                    end_time=5.0,
                    text="hello",
                )
            )
            db.session.add(
                ModelCall(
                    post_id=post.id,
                    first_segment_sequence_num=0,
                    last_segment_sequence_num=0,
                    model_name="whisper-1",
                    prompt="Whisper transcription job",
                    status="success",
                )
            )
            db.session.commit()
            guid = post.guid

        client = app.test_client()

        with (
            mock.patch("app.routes.post_routes.get_jobs_manager") as mock_mgr,
            mock.patch(
                "app.routes.post_routes.clear_post_processing_data_keep_transcript"
            ) as clear_mock,
        ):
            mock_mgr.return_value.start_post_processing.return_value = {
                "status": "started",
                "job_id": "job-123",
                "message": "ok",
            }

            response = client.post(f"/api/posts/{guid}/reprocess/keep-transcript")

        assert response.status_code == 200
        payload = response.get_json()
        assert payload is not None
        assert payload["status"] == "started"
        clear_mock.assert_called_once()
    finally:
        runtime_config.whisper = original_whisper


def test_reprocess_keep_transcript_rejects_transcript_for_old_whisper_model(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Remote Whisper Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="mismatched-whisper-guid",
            download_url="https://example.com/audio.mp3",
            title="Mismatched Whisper Episode",
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()

        db.session.add(
            TranscriptSegment(
                post_id=post.id,
                sequence_num=0,
                start_time=0.0,
                end_time=5.0,
                text="hello",
            )
        )
        db.session.add(
            ModelCall(
                post_id=post.id,
                first_segment_sequence_num=0,
                last_segment_sequence_num=0,
                model_name="whisper-1",
                prompt="Whisper transcription job",
                status="success",
            )
        )
        db.session.commit()
        guid = post.guid

    client = app.test_client()
    original_whisper = runtime_config.whisper
    runtime_config.whisper = RemoteWhisperConfig(
        api_key="test-key", model="whisper-large-v3"
    )

    try:
        with mock.patch(
            "app.routes.post_routes.clear_post_processing_data_keep_transcript"
        ) as clear_mock:
            response = client.post(f"/api/posts/{guid}/reprocess/keep-transcript")
    finally:
        runtime_config.whisper = original_whisper

    assert response.status_code == 400
    payload = response.get_json()
    assert payload is not None
    assert payload["error_code"] == "NO_REUSABLE_TRANSCRIPT"
    clear_mock.assert_not_called()


@pytest.mark.parametrize("use_rust", [False, True])
def test_post_stats_does_not_select_word_timestamps(app, use_rust):
    app.testing = True
    app.register_blueprint(post_bp)
    guid = "stats-deferred-words"

    with app.app_context():
        feed = Feed(title="Stats Feed", rss_url="https://example.com/stats.xml")
        db.session.add(feed)
        db.session.flush()
        db.session.add(
            Post(
                feed_id=feed.id,
                guid=guid,
                title="Stats Episode",
                download_url="https://example.com/audio.mp3",
                transcript_word_timestamps=[{"word": "unused", "start": 0}],
            )
        )
        db.session.commit()
        engine = db.engine

    statements = []

    def capture_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    rust_payload = {"post": {"guid": guid}, "processing_stats": {}}
    event.listen(engine, "before_cursor_execute", capture_sql)
    try:
        with mock.patch(
            "app.routes.post_routes.try_render_post_stats",
            return_value=rust_payload if use_rust else None,
        ) as render_mock:
            response = app.test_client().get(f"/api/posts/{guid}/stats")
    finally:
        event.remove(engine, "before_cursor_execute", capture_sql)

    assert response.status_code == 200
    assert response.get_json()["post"]["guid"] == guid
    render_mock.assert_called_once()
    assert any("select" in statement for statement in statements)
    assert all(
        "transcript_word_timestamps" not in statement for statement in statements
    )
    if use_rust:
        assert response.get_json() == rust_payload


def test_post_stats_omits_debug_info_when_disabled(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Stats Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="stats-no-debug-guid",
            download_url="https://example.com/audio.mp3",
            title="Stats Episode",
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()
        guid = post.guid

    client = app.test_client()

    with mock.patch.dict("os.environ", {"PODLY_STATS_DEBUG": "false"}, clear=False):
        response = client.get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert "debug_info" not in payload


def test_post_stats_returns_rust_payload_when_available(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Rust Stats Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="stats-rust-guid",
            download_url="https://example.com/audio.mp3",
            title="Stats Rust Episode",
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()
        guid = post.guid

    rust_payload = {
        "post": {"guid": guid, "title": "from rust"},
        "processing_stats": {"total_segments": 0},
    }
    with mock.patch(
        "app.routes.post_routes.try_render_post_stats", return_value=rust_payload
    ) as render_mock:
        response = app.test_client().get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    assert response.get_json() == rust_payload
    render_mock.assert_called_once()


def test_post_stats_falls_back_when_rust_payload_unavailable(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(
            title="Rust Stats Fallback Feed", rss_url="https://example.com/feed.xml"
        )
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="stats-rust-fallback-guid",
            download_url="https://example.com/audio.mp3",
            title="Stats Rust Fallback Episode",
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()
        guid = post.guid

    with mock.patch(
        "app.routes.post_routes.try_render_post_stats", return_value=None
    ) as render_mock:
        response = app.test_client().get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert payload["post"]["guid"] == guid
    render_mock.assert_called_once()


def test_post_stats_includes_estimated_cost_for_admin(app):
    app.testing = True
    app.register_blueprint(post_bp)

    @app.before_request
    def _set_admin_user() -> None:
        g.current_user = SimpleNamespace(id=1, role="admin")

    with app.app_context():
        user = User(id=1, username="admin", password_hash="hash", role="admin")
        feed = Feed(title="Stats Cost Feed", rss_url="https://example.com/feed.xml")
        db.session.add_all([user, feed])
        db.session.flush()
        post = Post(
            feed_id=feed.id,
            guid="stats-cost-admin-guid",
            download_url="https://example.com/audio.mp3",
            title="Stats Cost Episode",
            whitelisted=True,
        )
        db.session.add(post)
        db.session.flush()
        db.session.add(
            ModelCall(
                post_id=post.id,
                first_segment_sequence_num=0,
                last_segment_sequence_num=1,
                model_name="gpt-4o-mini",
                prompt="classify",
                status="success",
                prompt_tokens=1_000_000,
                cached_prompt_tokens=500_000,
                completion_tokens=250_000,
                # The writer is responsible for populating this column at
                # finalize time. Set it directly here so we test the stats
                # endpoint's read-path (which must NOT import litellm)
                # rather than the writer's compute-path.
                estimated_cost_usd=0.3375,
            )
        )
        db.session.commit()
        guid = post.guid

    with (
        mock.patch("app.routes.post_routes.try_render_post_stats", return_value=None),
        mock.patch(
            "app.config_store.read_combined",
            return_value={
                "app": {
                    "whisper_cost_rate_per_hour": 0.04,
                    "ina_cost_rate_per_hour": 0.02,
                }
            },
        ),
    ):
        response = app.test_client().get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    # LLM-only post (no whisper/ina rows) → total equals the persisted
    # ModelCall.estimated_cost_usd.
    assert payload["processing_stats"]["estimated_cost"] == 0.3375


def test_post_stats_omits_estimated_cost_for_non_admin(app):
    app.testing = True
    app.register_blueprint(post_bp)

    @app.before_request
    def _set_regular_user() -> None:
        g.current_user = SimpleNamespace(id=1, role="user")

    with app.app_context():
        user = User(id=1, username="user", password_hash="hash", role="user")
        feed = Feed(title="Stats Cost Feed", rss_url="https://example.com/feed.xml")
        db.session.add_all([user, feed])
        db.session.flush()
        post = Post(
            feed_id=feed.id,
            guid="stats-cost-user-guid",
            download_url="https://example.com/audio.mp3",
            title="Stats Cost Episode",
            whitelisted=True,
        )
        db.session.add(post)
        db.session.flush()
        db.session.add(
            ModelCall(
                post_id=post.id,
                first_segment_sequence_num=0,
                last_segment_sequence_num=1,
                model_name="gpt-4o-mini",
                prompt="classify",
                status="success",
                prompt_tokens=1_000_000,
                cached_prompt_tokens=500_000,
                completion_tokens=250_000,
            )
        )
        db.session.commit()
        guid = post.guid

    with mock.patch("app.routes.post_routes.try_render_post_stats", return_value=None):
        response = app.test_client().get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert "estimated_cost" not in payload["processing_stats"]


def test_post_stats_estimated_cost_includes_whisper_and_ina_when_present(app):
    """Admin per-episode total = LLM + Whisper(rate * hours) + INA(rate * hours).

    Each model_call also carries an `estimated_cost_usd` so the modal can
    render a per-call cost column. Whisper / INA per-call costs are the
    duration-based fee split across the success calls of that type.
    """
    app.testing = True
    app.register_blueprint(post_bp)

    @app.before_request
    def _set_admin_user() -> None:
        g.current_user = SimpleNamespace(id=1, role="admin")

    with app.app_context():
        user = User(id=1, username="admin2", password_hash="hash", role="admin")
        feed = Feed(title="Stats Cost Feed", rss_url="https://example.com/feed.xml")
        db.session.add_all([user, feed])
        db.session.flush()
        post = Post(
            feed_id=feed.id,
            guid="stats-cost-whisper-ina-guid",
            download_url="https://example.com/audio.mp3",
            title="Stats Cost Episode",
            whitelisted=True,
            duration=3600.0,  # 1 hour
        )
        db.session.add(post)
        db.session.flush()
        db.session.add_all(
            [
                ModelCall(
                    post_id=post.id,
                    first_segment_sequence_num=0,
                    last_segment_sequence_num=1,
                    model_name="gpt-4o-mini",
                    prompt="classify",
                    status="success",
                    estimated_cost_usd=0.3375,
                ),
                ModelCall(
                    post_id=post.id,
                    first_segment_sequence_num=0,
                    last_segment_sequence_num=-1,
                    model_name="whisper-large-v3-turbo",
                    prompt="Whisper transcription job",
                    status="success",
                ),
                ModelCall(
                    post_id=post.id,
                    first_segment_sequence_num=0,
                    last_segment_sequence_num=-1,
                    model_name="ina:speech_music_noise",
                    prompt="INA",
                    status="success",
                ),
            ]
        )
        db.session.commit()
        guid = post.guid

    with (
        mock.patch("app.routes.post_routes.try_render_post_stats", return_value=None),
        mock.patch(
            "app.config_store.read_combined",
            return_value={
                "app": {
                    "whisper_cost_rate_per_hour": 0.04,
                    "ina_cost_rate_per_hour": 0.02,
                }
            },
        ),
    ):
        response = app.test_client().get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    # 0.3375 (LLM) + 0.04 (Whisper * 1h) + 0.02 (INA * 1h) = 0.3975
    assert payload["processing_stats"]["estimated_cost"] == 0.3975

    calls_by_model = {c["model_name"]: c for c in payload["model_calls"]}
    assert calls_by_model["gpt-4o-mini"]["estimated_cost_usd"] == 0.3375
    assert calls_by_model["whisper-large-v3-turbo"]["estimated_cost_usd"] == 0.04
    assert calls_by_model["ina:speech_music_noise"]["estimated_cost_usd"] == 0.02


def test_post_stats_rust_path_accepts_guid_with_slashes(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(
            title="Rust Slash Stats Feed", rss_url="https://example.com/feed.xml"
        )
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="tag:example.com,2026:/posts/123",
            download_url="https://example.com/audio.mp3",
            title="Stats Slash Episode",
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()
        guid = post.guid

    rust_payload = {"post": {"guid": guid}, "processing_stats": {}}
    with mock.patch(
        "app.routes.post_routes.try_render_post_stats", return_value=rust_payload
    ) as render_mock:
        response = app.test_client().get(f"/api/posts/{_encoded_guid(guid)}/stats")

    assert response.status_code == 200
    assert response.get_json() == rust_payload
    assert render_mock.call_args.kwargs["post_guid"] == guid


def test_post_stats_include_chapters_for_chapter_insert_strategy(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(
            title="Chapter Insert Feed",
            rss_url="https://example.com/feed.xml",
            ad_detection_strategy="chapter_insert",
        )
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="chapter-insert-stats-guid",
            download_url="https://example.com/audio.mp3",
            title="Chapter Insert Episode",
            processed_audio_path="/tmp/chapter-insert-output.mp3",
            chapter_data=json.dumps(
                {
                    "chapter_source": "description",
                    "chapters_for_output": [
                        {
                            "title": "Intro",
                            "start_time": 0.0,
                            "end_time": 12.5,
                        },
                        {
                            "title": "Main Topic",
                            "start_time": 12.5,
                            "end_time": 30.0,
                        },
                    ],
                }
            ),
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()

        db.session.add_all(
            [
                TranscriptSegment(
                    post_id=post.id,
                    sequence_num=0,
                    start_time=0.0,
                    end_time=12.5,
                    text="Chapter intro",
                    speaker_label="SPEAKER_00",
                ),
                TranscriptSegment(
                    post_id=post.id,
                    sequence_num=1,
                    start_time=12.5,
                    end_time=30.0,
                    text="Chapter main topic",
                    speaker_label="SPEAKER_01",
                ),
            ]
        )
        db.session.commit()
        guid = post.guid

    client = app.test_client()
    response = client.get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert payload["ad_detection_strategy"] == "chapter_insert"
    assert payload["chapters"]["total_chapters"] == 2
    assert payload["chapters"]["chapters_kept"] == 2
    assert payload["chapters"]["chapters_removed"] == 0
    assert payload["chapters"]["chapters"] == [
        {
            "title": "Intro",
            "start_time": 0.0,
            "end_time": 12.5,
            "label": "content",
        },
        {
            "title": "Main Topic",
            "start_time": 12.5,
            "end_time": 30.0,
            "label": "content",
        },
    ]
    assert payload["processing_stats"]["speaker_breakdown"] == [
        {
            "speaker_label": "SPEAKER_01",
            "speaking_time_seconds": 17.5,
            "speaking_percentage": 58.3,
            "segment_count": 1,
        },
        {
            "speaker_label": "SPEAKER_00",
            "speaking_time_seconds": 12.5,
            "speaking_percentage": 41.7,
            "segment_count": 1,
        },
    ]


def test_post_stats_include_bleep_windows(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Stats Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="stats-bleeps-guid",
            download_url="https://example.com/audio.mp3",
            title="Stats Bleeps Episode",
            duration=100,
            bleep_windows=[
                {"start_time": 4.125, "end_time": 4.625},
                {"start_time": 44.0, "end_time": 44.5},
            ],
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()
        guid = post.guid

    client = app.test_client()
    response = client.get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert payload["processing_stats"]["has_bleep_windows"] is True
    assert payload["processing_stats"]["bleep_windows"] == [
        {"start_time": 4.125, "end_time": 4.625},
        {"start_time": 44.0, "end_time": 44.5},
    ]
    assert payload["processing_stats"]["bleeped_time_seconds"] == 1.0
    assert payload["processing_stats"]["bleeped_percentage"] == 1.0


def test_post_chapters_reads_processed_mp3_chapters(app, tmp_path, monkeypatch):
    app.testing = True
    app.register_blueprint(post_bp)

    processed_path = tmp_path / "processed.mp3"
    processed_path.write_bytes(b"fake mp3")

    monkeypatch.setattr(
        "app.routes.post_routes.read_chapters",
        lambda _path: [
            SimpleNamespace(
                title="Intro",
                start_time_ms=0,
                end_time_ms=120_000,
            ),
            SimpleNamespace(
                title="Main Topic",
                start_time_ms=120_000,
                end_time_ms=360_500,
            ),
        ],
    )

    with app.app_context():
        feed = Feed(title="Chapter Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="chapter-player-guid",
            download_url="https://example.com/audio.mp3",
            title="Chapter Player Episode",
            processed_audio_path=str(processed_path),
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()

    response = app.test_client().get("/api/posts/chapter-player-guid/chapters")

    assert response.status_code == 200
    assert response.get_json() == {
        "chapters": [
            {"title": "Intro", "start_time": 0.0, "end_time": 120.0},
            {"title": "Main Topic", "start_time": 120.0, "end_time": 360.5},
        ]
    }


def test_post_stats_report_no_bleep_windows_when_absent(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Stats Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="stats-no-bleeps-guid",
            download_url="https://example.com/audio.mp3",
            title="Stats No Bleeps Episode",
            duration=100,
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()
        guid = post.guid

    client = app.test_client()
    response = client.get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert payload["processing_stats"]["has_bleep_windows"] is False
    assert payload["processing_stats"]["bleep_windows"] == []
    assert payload["processing_stats"]["bleeped_time_seconds"] == 0.0
    assert payload["processing_stats"]["bleeped_percentage"] == 0.0


def test_post_stats_use_original_duration_for_ad_and_bleep_percentages(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Stats Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="stats-original-duration-guid",
            download_url="https://example.com/audio.mp3",
            title="Stats Original Duration Episode",
            duration=100,
            bleep_windows=[
                {"start_time": 4.0, "end_time": 5.0},
                {"start_time": 44.0, "end_time": 45.0},
            ],
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()

        ad_segment = TranscriptSegment(
            post_id=post.id,
            sequence_num=0,
            start_time=10.0,
            end_time=20.0,
            text="Sponsored message",
        )
        model_call = ModelCall(
            post_id=post.id,
            first_segment_sequence_num=0,
            last_segment_sequence_num=0,
            model_name="gpt-4.1-mini",
            prompt="Classify transcript segment",
            status="success",
        )
        db.session.add_all([ad_segment, model_call])
        db.session.commit()

        db.session.add(
            Identification(
                transcript_segment_id=ad_segment.id,
                model_call_id=model_call.id,
                label="ad",
                confidence=0.99,
            )
        )
        db.session.commit()
        guid = post.guid

    client = app.test_client()
    response = client.get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert payload["processing_stats"]["original_duration_seconds"] == 110.0
    assert payload["processing_stats"]["edited_duration_seconds"] == 100.0
    assert payload["processing_stats"]["estimated_ad_time_seconds"] == 10.0
    assert payload["processing_stats"]["ad_percentage"] == 9.1
    assert payload["processing_stats"]["edited_ad_markers"] == [
        {
            "edited_start_time": 10.0,
            "edited_end_time": 10.0,
            "original_start_time": 10.0,
            "original_end_time": 20.0,
            "removed_duration_seconds": 10.0,
        }
    ]
    assert payload["processing_stats"]["bleeped_time_seconds"] == 2.0
    assert payload["processing_stats"]["bleeped_percentage"] == 1.8
    assert payload["processing_stats"]["edited_bleep_windows"] == [
        {
            "edited_start_time": 4.0,
            "edited_end_time": 5.0,
            "original_start_time": 4.0,
            "original_end_time": 5.0,
        },
        {
            "edited_start_time": 34.0,
            "edited_end_time": 35.0,
            "original_start_time": 44.0,
            "original_end_time": 45.0,
        },
    ]


def test_post_stats_include_speaker_labels_and_related_logs(app, tmp_path):
    app.testing = True
    app.register_blueprint(post_bp)

    log_file = tmp_path / "app.log"

    with app.app_context():
        feed = Feed(title="Stats Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="stats-speaker-guid",
            download_url="https://example.com/audio.mp3",
            title="Stats Episode",
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()

        db.session.add(
            TranscriptSegment(
                post_id=post.id,
                sequence_num=0,
                start_time=0.0,
                end_time=4.0,
                text="Host intro",
                speaker_label="SPEAKER_00",
            )
        )
        db.session.add(
            TranscriptSegment(
                post_id=post.id,
                sequence_num=1,
                start_time=4.0,
                end_time=8.0,
                text="Guest answer",
                speaker_label="SPEAKER_01",
            )
        )
        db.session.add(
            TranscriptSegment(
                post_id=post.id,
                sequence_num=2,
                start_time=8.0,
                end_time=10.0,
                text="Cross-talk",
                speaker_label=None,
            )
        )
        db.session.add(
            TranscriptSegment(
                post_id=post.id,
                sequence_num=3,
                start_time=10.0,
                end_time=12.0,
                text="Host follow-up",
                speaker_label="SPEAKER_00",
            )
        )
        db.session.add(
            ProcessingJob(
                id="job-123",
                post_guid=post.guid,
                status="completed",
                current_step=4,
                step_name="Processing complete",
                total_steps=4,
            )
        )
        db.session.commit()

        log_file.write_text(
            "\n".join(
                [
                    "2026-04-05 03:15:38,000 INFO [JOB_STATUS_UPDATE] job_id=job-123 status=running step=2 step_name=Transcribing audio bound=True",
                    f"2026-04-05 03:15:38,050 INFO [TRANSCRIBE_START] Calling transcriber whisper-1 for post {post.id}, audio: /tmp/audio.mp3",
                    f"2026-04-05 03:15:39,000 INFO Starting ad classification for post {post.id} with 1 segments.",
                    "2026-04-05 03:15:40,000 INFO [JOB_STATUS_UPDATE] job_id=job-123 status=running step=4 step_name=Processing audio bound=True",
                    "2026-04-05 03:15:41,000 INFO unrelated line for another post",
                ]
            ),
            encoding="utf-8",
        )
        guid = post.guid

    client = app.test_client()

    with mock.patch("app.routes.post_routes.get_app_log_path", return_value=log_file):
        response = client.get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None

    assert payload["transcript_segments"][0]["speaker_label"] == "SPEAKER_00"
    assert payload["processing_stats"]["speaker_breakdown"] == [
        {
            "speaker_label": "SPEAKER_00",
            "speaking_time_seconds": 6.0,
            "speaking_percentage": 50.0,
            "segment_count": 2,
        },
        {
            "speaker_label": "SPEAKER_01",
            "speaking_time_seconds": 4.0,
            "speaking_percentage": 33.3,
            "segment_count": 1,
        },
        {
            "speaker_label": None,
            "speaking_time_seconds": 2.0,
            "speaking_percentage": 16.7,
            "segment_count": 1,
        },
    ]
    assert payload["related_logs"]["latest_job_id"] == "job-123"
    assert any(
        entry["stage"] == "transcription" and entry["step_name"] == "Transcribing audio"
        for entry in payload["related_logs"]["entries"]
    )
    assert any(
        entry["stage"] == "classification"
        for entry in payload["related_logs"]["entries"]
    )
    assert any(
        entry["stage"] == "audio" and entry["step_name"] == "Processing audio"
        for entry in payload["related_logs"]["entries"]
    )


def test_post_stats_include_audio_segments(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Stats Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="stats-audio-segments-guid",
            download_url="https://example.com/audio.mp3",
            title="Stats Audio Segments Episode",
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()

        model_call = ModelCall(
            post_id=post.id,
            first_segment_sequence_num=0,
            last_segment_sequence_num=2,
            model_name="ina:speech_music_noise",
            prompt="INA speech segmenter analysis",
            status="success",
        )
        db.session.add(model_call)
        db.session.commit()
        model_call_id = model_call.id

        db.session.add_all(
            [
                AudioSegment(
                    post_id=post.id,
                    model_call_id=model_call.id,
                    start_time=0.0,
                    end_time=1.5,
                    label="music",
                ),
                AudioSegment(
                    post_id=post.id,
                    model_call_id=model_call.id,
                    start_time=1.5,
                    end_time=3.0,
                    label="speech",
                ),
                AudioSegment(
                    post_id=post.id,
                    model_call_id=model_call.id,
                    start_time=3.0,
                    end_time=4.0,
                    label="noise",
                ),
            ]
        )
        db.session.commit()
        guid = post.guid

    client = app.test_client()
    response = client.get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert payload["processing_stats"]["audio_segments_count"] == 3
    assert payload["audio_segments"] == [
        {
            "id": payload["audio_segments"][0]["id"],
            "start_time": 0.0,
            "end_time": 1.5,
            "label": "music",
            "model_call_id": model_call_id,
        },
        {
            "id": payload["audio_segments"][1]["id"],
            "start_time": 1.5,
            "end_time": 3.0,
            "label": "speech",
            "model_call_id": model_call_id,
        },
        {
            "id": payload["audio_segments"][2]["id"],
            "start_time": 3.0,
            "end_time": 4.0,
            "label": "noise",
            "model_call_id": model_call_id,
        },
    ]


def test_post_stats_bridge_music_only_gap_into_ad_blocks(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Stats Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="stats-audio-bridge-guid",
            download_url="https://example.com/audio.mp3",
            title="Stats Audio Bridge Episode",
            whitelisted=True,
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

        transcript_segments = [
            TranscriptSegment(
                post_id=post.id,
                sequence_num=8,
                start_time=27.3,
                end_time=29.0,
                text="No such thing.",
                speaker_label="SPEAKER_01",
            ),
            TranscriptSegment(
                post_id=post.id,
                sequence_num=9,
                start_time=40.3,
                end_time=41.6,
                text="This is an iHeart Podcast.",
                speaker_label="SPEAKER_12",
            ),
            TranscriptSegment(
                post_id=post.id,
                sequence_num=10,
                start_time=42.8,
                end_time=43.7,
                text="Guaranteed human.",
                speaker_label="SPEAKER_12",
            ),
        ]
        db.session.add_all(transcript_segments)
        db.session.commit()

        db.session.add_all(
            [
                Identification(
                    transcript_segment_id=transcript_segments[0].id,
                    model_call_id=llm_call.id,
                    label="ad",
                    confidence=0.98,
                ),
                Identification(
                    transcript_segment_id=transcript_segments[1].id,
                    model_call_id=llm_call.id,
                    label="ad",
                    confidence=0.97,
                ),
                Identification(
                    transcript_segment_id=transcript_segments[2].id,
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
        guid = post.guid

    client = app.test_client()
    response = client.get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert payload["processing_stats"]["ad_blocks"] == [
        {
            "start_time": 27.3,
            "end_time": 43.7,
        }
    ]


def test_post_stats_expand_preroll_ad_block_with_edge_audio(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Stats Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="stats-preroll-edge-guid",
            download_url="https://example.com/audio.mp3",
            title="Stats Preroll Edge Episode",
            refined_ad_boundaries=[
                {
                    "orig_start": 15.1,
                    "orig_end": 65.4,
                    "refined_start": 15.059,
                    "refined_end": 65.374,
                }
            ],
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()

        llm_call = ModelCall(
            post_id=post.id,
            first_segment_sequence_num=0,
            last_segment_sequence_num=1,
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

        transcript_segments = [
            TranscriptSegment(
                post_id=post.id,
                sequence_num=0,
                start_time=15.1,
                end_time=21.1,
                text="Ad segment",
            ),
            TranscriptSegment(
                post_id=post.id,
                sequence_num=1,
                start_time=57.0,
                end_time=65.4,
                text="Ad segment",
            ),
        ]
        db.session.add_all(transcript_segments)
        db.session.commit()

        db.session.add_all(
            [
                Identification(
                    transcript_segment_id=transcript_segments[0].id,
                    model_call_id=llm_call.id,
                    label="ad",
                    confidence=0.98,
                ),
                Identification(
                    transcript_segment_id=transcript_segments[1].id,
                    model_call_id=llm_call.id,
                    label="ad",
                    confidence=0.97,
                ),
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
        guid = post.guid

    client = app.test_client()
    response = client.get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert payload["processing_stats"]["ad_blocks"] == [
        {
            "start_time": 0.0,
            "end_time": 65.4,
        }
    ]
    assert payload["processing_stats"]["edited_ad_markers"] == [
        {
            "edited_start_time": 0.0,
            "edited_end_time": 0.0,
            "original_start_time": 0.0,
            "original_end_time": 65.374,
            "removed_duration_seconds": 65.374,
        }
    ]


def test_post_stats_use_cut_ready_windows_with_unrefined_trailing_fragment(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Stats Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="stats-refined-plus-trailing-guid",
            download_url="https://example.com/audio.mp3",
            title="Stats Refined Plus Trailing Episode",
            refined_ad_boundaries=[
                {
                    "orig_start": 15.1,
                    "orig_end": 60.9,
                    "refined_start": 15.059,
                    "refined_end": 60.85,
                }
            ],
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()

        llm_call = ModelCall(
            post_id=post.id,
            first_segment_sequence_num=0,
            last_segment_sequence_num=2,
            model_name="gemini/gemini-3-flash-preview",
            prompt="Classify ads",
            status="success",
        )
        db.session.add(llm_call)
        db.session.commit()

        transcript_segments = [
            TranscriptSegment(
                post_id=post.id,
                sequence_num=0,
                start_time=15.1,
                end_time=21.1,
                text="Ad segment",
            ),
            TranscriptSegment(
                post_id=post.id,
                sequence_num=1,
                start_time=57.0,
                end_time=60.9,
                text="Ad segment",
            ),
            TranscriptSegment(
                post_id=post.id,
                sequence_num=2,
                start_time=72.0,
                end_time=75.0,
                text="Ad segment",
            ),
        ]
        db.session.add_all(transcript_segments)
        db.session.commit()

        db.session.add_all(
            [
                Identification(
                    transcript_segment_id=segment.id,
                    model_call_id=llm_call.id,
                    label="ad",
                    confidence=0.98,
                )
                for segment in transcript_segments
            ]
        )
        db.session.commit()
        guid = post.guid

    client = app.test_client()
    response = client.get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert payload["processing_stats"]["ad_blocks"] == [
        {
            "start_time": 15.1,
            "end_time": 75.0,
        }
    ]
    assert payload["processing_stats"]["edited_ad_markers"] == [
        {
            "edited_start_time": 15.059,
            "edited_end_time": 15.059,
            "original_start_time": 15.059,
            "original_end_time": 75.0,
            "removed_duration_seconds": 59.941,
        }
    ]


def test_post_stats_exposes_retry_count_separately_from_attempt_count(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(title="Stats Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="stats-retry-guid",
            download_url="https://example.com/audio.mp3",
            title="Stats Retry Episode",
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()

        db.session.add_all(
            [
                ModelCall(
                    post_id=post.id,
                    first_segment_sequence_num=0,
                    last_segment_sequence_num=0,
                    model_name="gemini/gemini-3.1-flash-lite-preview",
                    prompt="Prompt",
                    status="success",
                    retry_attempts=1,
                ),
                ModelCall(
                    post_id=post.id,
                    first_segment_sequence_num=1,
                    last_segment_sequence_num=1,
                    model_name="gemini/gemini-3.1-flash-lite-preview",
                    prompt="Prompt retry",
                    status="success",
                    retry_attempts=3,
                ),
                ModelCall(
                    post_id=post.id,
                    first_segment_sequence_num=0,
                    last_segment_sequence_num=0,
                    model_name="whisper-1",
                    prompt="Whisper transcription job",
                    status="success",
                    retry_attempts=0,
                ),
            ]
        )
        db.session.commit()
        guid = post.guid

    client = app.test_client()
    response = client.get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None

    retry_counts = {
        (call["model_name"], call["segment_range"]): (
            call["retry_attempts"],
            call["retry_count"],
        )
        for call in payload["model_calls"]
    }

    assert retry_counts[("gemini/gemini-3.1-flash-lite-preview", "0-0")] == (1, 0)
    assert retry_counts[("gemini/gemini-3.1-flash-lite-preview", "1-1")] == (3, 2)
    assert retry_counts[("whisper-1", "0-0")] == (0, 0)


def test_post_stats_includes_chapter_llm_model_calls(app):
    app.testing = True
    app.register_blueprint(post_bp)

    with app.app_context():
        feed = Feed(
            title="Chapter Stats Feed",
            rss_url="https://example.com/feed.xml",
            ad_detection_strategy="chapter_insert",
        )
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="chapter-llm-model-call-stats-guid",
            download_url="https://example.com/audio.mp3",
            title="Chapter LLM Model Call Stats",
            processed_audio_path="/tmp/chapter-output.mp3",
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()

        db.session.add_all(
            [
                ModelCall(
                    post_id=post.id,
                    first_segment_sequence_num=-100,
                    last_segment_sequence_num=-100,
                    model_name="gemini/gemini-3-flash-preview",
                    prompt="chapter title prompt",
                    response="chapter title response",
                    status="success",
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                ),
                ModelCall(
                    post_id=post.id,
                    first_segment_sequence_num=-200,
                    last_segment_sequence_num=-200,
                    model_name="gemini/gemini-3-flash-preview",
                    prompt="chapter topic prompt",
                    response="chapter topic response",
                    status="success",
                    prompt_tokens=20,
                    completion_tokens=8,
                    total_tokens=28,
                ),
            ]
        )
        db.session.commit()
        guid = post.guid

    response = app.test_client().get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    assert payload["processing_stats"]["total_model_calls"] == 2
    assert payload["processing_stats"]["model_types"] == {
        "gemini/gemini-3-flash-preview": 2
    }
    ranges = {call["segment_range"] for call in payload["model_calls"]}
    assert "chapter titles (LLM)" in ranges
    assert "chapter topic plan (LLM)" in ranges
    assert "-100--100" not in ranges


def test_segment_range_label_maps_chapter_word_boundary_sentinel() -> None:
    from app.routes.post_routes import _format_segment_range_label

    assert _format_segment_range_label(-100, -100) == "chapter titles (LLM)"
    assert _format_segment_range_label(-200, -200) == "chapter topic plan (LLM)"
    # Real transcript ranges fall through to the raw "first-last" form.
    assert _format_segment_range_label(5, 9) == "5-9"


def test_post_stats_includes_debug_info_when_enabled(app, tmp_path):
    app.testing = True
    app.register_blueprint(post_bp)

    processed_audio = tmp_path / "processed.mp3"
    processed_audio_bytes = b"processed-audio-bytes"
    processed_audio.write_bytes(processed_audio_bytes)

    unprocessed_audio = tmp_path / "unprocessed.mp3"
    unprocessed_audio_bytes = b"unprocessed-audio-bytes"
    unprocessed_audio.write_bytes(unprocessed_audio_bytes)

    with app.app_context():
        feed = Feed(title="Stats Feed", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()

        post = Post(
            feed_id=feed.id,
            guid="stats-debug-guid",
            download_url="https://example.com/audio.mp3",
            title="Stats Episode",
            processed_audio_path=str(processed_audio),
            unprocessed_audio_path=str(unprocessed_audio),
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()
        guid = post.guid

    client = app.test_client()

    with mock.patch.dict("os.environ", {"PODLY_STATS_DEBUG": "true"}, clear=False):
        response = client.get(f"/api/posts/{guid}/stats")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None

    debug_info = payload["debug_info"]
    assert debug_info["guid"] == "stats-debug-guid"
    assert debug_info["download_url"] == "https://example.com/audio.mp3"

    processed_info = debug_info["processed_audio"]
    assert processed_info["path"] == str(processed_audio)
    assert processed_info["exists"] is True
    assert processed_info["is_file"] is True
    assert processed_info["size_bytes"] == len(processed_audio_bytes)

    unprocessed_info = debug_info["unprocessed_audio"]
    assert unprocessed_info["path"] == str(unprocessed_audio)
    assert unprocessed_info["exists"] is True
    assert unprocessed_info["is_file"] is True
    assert unprocessed_info["size_bytes"] == len(unprocessed_audio_bytes)

    candidates = debug_info["processed_audio_path_candidates"]
    assert any(
        c["path"] == str(processed_audio.resolve()) and c["exists"] is True
        for c in candidates
    )


def _stats_post_with_transcript(duration, segments, ad_sequence_nums=()):
    feed = Feed(title="Stats Feed", rss_url="https://example.com/stats.xml")
    db.session.add(feed)
    db.session.flush()
    post = Post(
        feed_id=feed.id,
        guid="stats-transcript-guid",
        download_url="https://example.com/audio.mp3",
        title="Stats Transcript Episode",
        duration=duration,
        whitelisted=True,
    )
    db.session.add(post)
    db.session.flush()
    rows = [
        TranscriptSegment(
            post_id=post.id, sequence_num=i, start_time=start, end_time=end, text="t"
        )
        for i, (start, end) in enumerate(segments)
    ]
    whisper = ModelCall(
        post_id=post.id,
        first_segment_sequence_num=0,
        last_segment_sequence_num=-1,
        model_name="whisper-large-v3-turbo",
        prompt="Whisper transcription job",
        status="success",
    )
    classifier = ModelCall(
        post_id=post.id,
        first_segment_sequence_num=0,
        last_segment_sequence_num=len(segments) - 1,
        model_name="gpt-4.1-mini",
        prompt="Classify transcript segment",
        status="success",
    )
    db.session.add_all([*rows, whisper, classifier])
    db.session.flush()
    db.session.add_all(
        Identification(
            transcript_segment_id=rows[i].id,
            model_call_id=classifier.id,
            label="ad",
            confidence=0.99,
        )
        for i in ad_sequence_nums
    )
    db.session.commit()
    return post.guid


def _admin_stats(app, guid):
    @app.before_request
    def _set_admin_user() -> None:
        g.current_user = SimpleNamespace(id=1, role="admin")

    db.session.add(User(id=1, username="admin", password_hash="hash", role="admin"))
    db.session.commit()
    with (
        mock.patch("app.routes.post_routes.try_render_post_stats", return_value=None),
        mock.patch(
            "app.config_store.read_combined",
            return_value={"app": {"whisper_cost_rate_per_hour": 0.04}},
        ),
    ):
        response = app.test_client().get(f"/api/posts/{guid}/stats")
    assert response.status_code == 200
    return response.get_json()


def test_post_stats_bill_whisper_on_original_length(app):
    app.testing = True
    app.register_blueprint(post_bp)
    guid = _stats_post_with_transcript(
        3000, [(0.0, 600.0), (600.0, 3600.0)], ad_sequence_nums=[0]
    )

    payload = _admin_stats(app, guid)

    assert payload["processing_stats"]["original_duration_seconds"] == 3600.0
    calls = {c["model_name"]: c for c in payload["model_calls"]}
    assert calls["whisper-large-v3-turbo"]["estimated_cost_usd"] == 0.04


def test_post_stats_original_length_is_at_least_transcript_end(app):
    app.testing = True
    app.register_blueprint(post_bp)
    # Current settings find no ads, but the processed file is shorter than
    # the transcribed original.
    guid = _stats_post_with_transcript(3000, [(0.0, 600.0), (600.0, 3600.0)])

    payload = _admin_stats(app, guid)

    assert payload["processing_stats"]["original_duration_seconds"] == 3600.0
