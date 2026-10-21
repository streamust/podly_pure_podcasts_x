from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from flask import Flask, Response, g, jsonify
from flask.typing import ResponseReturnValue

from app.auth import AuthSettings
from app.auth.middleware import init_auth_middleware
from app.auth.rate_limiter import FailureRateLimiter, FailureState
from app.auth.state import failure_rate_limiter
from app.extensions import db
from app.models import Feed, Post, User
from app.routes.auth_routes import auth_bp
from app.routes.discord_routes import discord_bp
from app.routes.feed_routes import feed_bp


@pytest.fixture
def auth_app() -> Generator[Flask]:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-secret",
        SESSION_COOKIE_NAME="podly_session",
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )

    settings = AuthSettings(
        require_auth=True,
        admin_username="admin",
        admin_password="password",
    )
    app.config["AUTH_SETTINGS"] = settings
    app.config["REQUIRE_AUTH"] = True
    app.config["DISCORD_SETTINGS"] = SimpleNamespace(enabled=True)

    db.init_app(app)
    with app.app_context():
        db.create_all()
        user = User(username="admin", role="admin")
        user.set_password("password")
        db.session.add(user)
        db.session.commit()

    failure_rate_limiter._storage.clear()

    init_auth_middleware(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(discord_bp)
    app.register_blueprint(feed_bp)

    @app.route("/api/protected", methods=["GET"])
    def protected() -> ResponseReturnValue:
        current = getattr(g, "current_user", None)
        if current is None:
            return jsonify({"error": "missing user"}), 500
        return jsonify({"status": "ok", "user": current.username})

    @app.route("/feed/1", methods=["GET"])
    def feed() -> Response:
        current = getattr(g, "current_user", None)
        if current is None:
            return Response("missing user", status=500)
        return Response("ok", mimetype="text/plain")

    @app.route("/api/posts/<string:guid>/download", methods=["GET"])
    def download(guid: str) -> Response:
        del guid
        current = getattr(g, "current_user", None)
        if current is None:
            return Response("missing user", status=500)
        return Response("download", mimetype="text/plain")

    @app.route("/post/<string:guid>.mp3", methods=["GET"])
    def stream(guid: str) -> Response:
        del guid
        current = getattr(g, "current_user", None)
        if current is None:
            return Response("missing user", status=500)
        return Response("stream", mimetype="audio/mpeg")

    yield app

    with app.app_context():
        db.session.remove()
        db.drop_all()


def test_login_sets_session_cookie_and_allows_authenticated_requests(
    auth_app: Flask,
) -> None:
    client = auth_app.test_client()

    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "password"},
    )
    assert response.status_code == 200
    set_cookie = response.headers.get("Set-Cookie", "")
    assert "podly_session" in set_cookie

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.get_json()["user"]["username"] == "admin"

    protected = client.get("/api/protected")
    assert protected.status_code == 200
    assert protected.get_json()["status"] == "ok"


def test_logout_clears_session(auth_app: Flask) -> None:
    client = auth_app.test_client()
    client.post("/api/auth/login", json={"username": "admin", "password": "password"})

    response = client.post("/api/auth/logout")
    assert response.status_code == 204

    protected = client.get("/api/protected")
    assert protected.status_code == 401
    assert protected.headers.get("WWW-Authenticate") is None


def test_auth_status_includes_ui_feature_flags(
    auth_app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PODLY_HIDE_DISCORD_INTEGRATION", "true")
    monkeypatch.setenv("PODLY_HIDE_REPORT_ISSUE_BUTTON", "1")

    client = auth_app.test_client()
    response = client.get("/api/auth/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["show_discord_integration"] is False
    assert payload["show_report_issue_button"] is False


def test_discord_status_is_disabled_when_integration_hidden(
    auth_app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PODLY_HIDE_DISCORD_INTEGRATION", "true")

    client = auth_app.test_client()
    response = client.get("/api/auth/discord/status")

    assert response.status_code == 200
    assert response.get_json()["enabled"] is False


def test_protected_route_without_session_returns_json_401(auth_app: Flask) -> None:
    client = auth_app.test_client()
    response = client.get("/api/protected")
    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required."
    assert response.headers.get("WWW-Authenticate") is None


def test_feed_requires_token_when_no_session(auth_app: Flask) -> None:
    client = auth_app.test_client()

    unauthorized = client.get("/feed/1")
    assert unauthorized.status_code == 401
    assert "Invalid or missing feed token" in unauthorized.get_data(as_text=True)


def test_share_link_generates_token_and_allows_query_access(auth_app: Flask) -> None:
    client = auth_app.test_client()
    with auth_app.app_context():
        feed = Feed(title="Example", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()
        feed_id = feed.id

        post = Post(
            feed_id=feed_id,
            guid="episode-1",
            download_url="https://example.com/audio.mp3",
            title="Episode",
            whitelisted=True,
        )
        db.session.add(post)
        db.session.commit()

    client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    share = client.post(f"/api/feeds/{feed_id}/share-link")
    assert share.status_code == 201
    payload = share.get_json()
    assert payload["feed_id"] == feed_id

    token_id = payload["feed_token"]
    secret = payload["feed_secret"]

    parsed = urlparse(payload["url"])
    params = parse_qs(parsed.query)
    assert params.get("feed_token", [None])[0] == token_id
    assert params.get("feed_secret", [None])[0] == secret

    anon_client = auth_app.test_client()

    feed_response = anon_client.get(
        f"/feed/{feed_id}",
        query_string={"feed_token": token_id, "feed_secret": secret},
    )
    assert feed_response.status_code == 200
    assert feed_response.data == b"ok"

    download_response = anon_client.get(
        "/api/posts/episode-1/download",
        query_string={"feed_token": token_id, "feed_secret": secret},
    )
    assert download_response.status_code == 200

    stream_response = anon_client.get(
        "/post/episode-1.mp3",
        query_string={"feed_token": token_id, "feed_secret": secret},
    )
    assert stream_response.status_code == 200


def test_share_link_returns_same_token_for_user_and_feed(auth_app: Flask) -> None:
    client = auth_app.test_client()
    with auth_app.app_context():
        feed = Feed(title="Stable", rss_url="https://example.com/stable.xml")
        db.session.add(feed)
        db.session.commit()
        feed_id = feed.id

    client.post("/api/auth/login", json={"username": "admin", "password": "password"})

    first = client.post(f"/api/feeds/{feed_id}/share-link").get_json()
    second = client.post(f"/api/feeds/{feed_id}/share-link").get_json()

    assert first["url"] == second["url"]
    assert first["feed_token"] == second["feed_token"]
    assert first["feed_secret"] == second["feed_secret"]


def test_share_link_prefers_https_from_forwarded_header(auth_app: Flask) -> None:
    client = auth_app.test_client()
    with auth_app.app_context():
        feed = Feed(title="Secure", rss_url="https://example.com/secure.xml")
        db.session.add(feed)
        db.session.commit()
        feed_id = feed.id

    client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    share = client.post(
        f"/api/feeds/{feed_id}/share-link",
        headers={"Forwarded": "for=203.0.113.10;proto=https"},
    )

    assert share.status_code == 201
    payload = share.get_json()
    assert payload is not None
    assert payload["url"].startswith("https://")


def test_feeds_endpoint_includes_latest_episode_release_date(auth_app: Flask) -> None:
    client = auth_app.test_client()
    latest_release_date = datetime(2024, 2, 1, 15, 30, tzinfo=UTC)

    with auth_app.app_context():
        dated_feed = Feed(title="Dated Feed", rss_url="https://example.com/dated.xml")
        undated_feed = Feed(
            title="Undated Feed",
            rss_url="https://example.com/undated.xml",
        )
        db.session.add_all([dated_feed, undated_feed])
        db.session.commit()

        db.session.add_all(
            [
                Post(
                    feed_id=dated_feed.id,
                    guid="dated-episode-1",
                    download_url="https://example.com/dated-episode-1.mp3",
                    title="Older Episode",
                    release_date=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
                    whitelisted=True,
                ),
                Post(
                    feed_id=dated_feed.id,
                    guid="dated-episode-2",
                    download_url="https://example.com/dated-episode-2.mp3",
                    title="Newest Episode",
                    release_date=latest_release_date,
                    whitelisted=True,
                ),
                Post(
                    feed_id=undated_feed.id,
                    guid="undated-episode-1",
                    download_url="https://example.com/undated-episode-1.mp3",
                    title="Undated Episode",
                    whitelisted=True,
                ),
            ]
        )
        db.session.commit()

        dated_feed_id = dated_feed.id
        undated_feed_id = undated_feed.id

    client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    response = client.get("/feeds")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None

    feeds_by_id = {feed["id"]: feed for feed in payload}
    assert (
        feeds_by_id[dated_feed_id]["latest_episode_release_date"]
        == latest_release_date.isoformat()
    )
    assert feeds_by_id[undated_feed_id]["latest_episode_release_date"] is None


def _share_token(auth_app: Flask) -> tuple[int, str, str]:
    client = auth_app.test_client()
    with auth_app.app_context():
        feed = Feed(title="Example", rss_url="https://example.com/feed.xml")
        db.session.add(feed)
        db.session.commit()
        db.session.add(
            Post(
                feed_id=feed.id,
                guid="episode-1",
                download_url="https://example.com/audio.mp3",
                title="Episode",
                whitelisted=True,
            )
        )
        db.session.commit()
        feed_id = feed.id
    client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    payload = client.post(f"/api/feeds/{feed_id}/share-link").get_json()
    return feed_id, payload["feed_token"], payload["feed_secret"]


def test_stale_feed_urls_cannot_lock_out_a_valid_token(auth_app: Flask) -> None:
    feed_id, token_id, secret = _share_token(auth_app)
    anon = auth_app.test_client()

    # A podcast app retrying old URLs: no token, and a stale token id.
    for _ in range(8):
        anon.get("/api/posts/deleted-episode/download")
        anon.get(
            "/api/posts/deleted-episode/download",
            query_string={"feed_token": "stale", "feed_secret": "old"},
        )
    assert (
        anon.get(
            "/api/posts/deleted-episode/download",
            query_string={"feed_token": "stale", "feed_secret": "old"},
        ).status_code
        == 429
    )

    response = anon.get(
        f"/feed/{feed_id}",
        query_string={"feed_token": token_id, "feed_secret": secret},
    )
    assert response.status_code == 200


def test_repeated_wrong_secret_for_one_token_is_still_throttled(
    auth_app: Flask,
) -> None:
    feed_id, token_id, _secret = _share_token(auth_app)
    anon = auth_app.test_client()

    statuses = [
        anon.get(
            f"/feed/{feed_id}",
            query_string={"feed_token": token_id, "feed_secret": f"guess-{attempt}"},
        ).status_code
        for attempt in range(6)
    ]
    assert statuses[:3] == [401, 401, 401]
    assert 429 in statuses


def test_blocked_token_id_still_admits_its_owner(auth_app: Flask) -> None:
    feed_id, token_id, secret = _share_token(auth_app)
    attacker = auth_app.test_client()
    for _ in range(6):
        attacker.get(f"/feed/{feed_id}", query_string={"feed_token": token_id})
    assert (
        attacker.get(
            f"/feed/{feed_id}", query_string={"feed_token": token_id}
        ).status_code
        == 429
    )

    owner = auth_app.test_client()
    response = owner.get(
        f"/feed/{feed_id}",
        query_string={"feed_token": token_id, "feed_secret": secret},
    )
    assert response.status_code == 200


def test_failure_limiter_storage_is_bounded() -> None:
    storage: dict[str, FailureState] = {}
    limiter = FailureRateLimiter(storage=storage, max_entries=50)
    for attempt in range(500):
        limiter.register_failure(f"feed_token:fresh-{attempt}")
    assert len(storage) == 50
    assert "feed_token:fresh-499" in storage
    assert "feed_token:fresh-0" not in storage
