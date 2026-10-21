import datetime
import html
import json
import logging
import re
import uuid
from collections.abc import Iterable
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import feedparser
import PyRSS2Gen
import requests
from flask import current_app, g, request
from sqlalchemy.orm import defer, load_only

from app.extensions import db
from app.memory_pressure import request_memory_trim_after_context
from app.models import Feed, Post, ProcessingJob, User, UserFeed
from app.runtime_config import config
from app.writer.client import writer_client
from podcast_processor.podcast_downloader import find_audio_link
from shared.rust_sidecar import (
    try_plan_feed_refresh,
    try_render_aggregate_feed_xml,
    try_render_feed_xml,
)

logger = logging.getLogger("global_logger")


def post_feed_render_defers() -> tuple[Any, ...]:
    """Defer load of large JSON columns that feed rendering never reads.

    transcript_word_timestamps in particular can be megabytes per row; loading
    it for every post on a feed render spikes RSS dramatically.
    """
    return (
        defer(Post.transcript_word_timestamps),
        defer(Post.bleep_windows),
        defer(Post.refined_ad_boundaries),
    )


_FORWARDED_PROTO_RE = re.compile(
    r"(?:^|[;,])\s*proto=(\"?)(https?|[A-Za-z]+)\1", re.IGNORECASE
)
_FEED_TEXT_NORMALIZE_TABLE = str.maketrans(
    {
        "\u00a0": " ",
        "\u200b": "",
        "\u200c": "",
        "\u200d": "",
        "\u2060": "",
        "\ufeff": "",
    }
)


def _format_itunes_duration(duration_seconds: int) -> str:
    total_seconds = max(0, int(duration_seconds))
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _parse_duration_seconds(value: Any) -> int | None:
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value if value >= 0 else None

    if isinstance(value, float):
        return int(value) if value >= 0 else None

    raw_value = str(value).strip()
    if not raw_value:
        return None

    try:
        parsed = int(raw_value)
    except ValueError:
        parsed = None

    if parsed is not None:
        return parsed if parsed >= 0 else None

    parts = [part.strip() for part in raw_value.split(":")]
    if len(parts) not in {2, 3} or any(not part for part in parts):
        return None

    try:
        seconds = float(parts[-1])
        minutes = int(parts[-2])
        hours = int(parts[0]) if len(parts) == 3 else 0
    except ValueError:
        return None

    if hours < 0 or minutes < 0 or seconds < 0:
        return None

    return int((hours * 3600) + (minutes * 60) + seconds)


def _format_chapter_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, rem = divmod(total_seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def feed_chapters(post: Post) -> list[tuple[float, str]]:
    raw_chapter_data = getattr(post, "chapter_data", None)
    if not raw_chapter_data:
        return []

    try:
        chapter_data = (
            json.loads(raw_chapter_data)
            if isinstance(raw_chapter_data, str)
            else raw_chapter_data
        )
    except Exception:  # noqa: BLE001
        return []

    if not isinstance(chapter_data, dict):
        return []

    chapters = chapter_data.get("chapters_for_output") or chapter_data.get(
        "chapters_kept"
    )
    if not isinstance(chapters, list):
        return []

    out: list[tuple[float, str]] = []
    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        title = str(chapter.get("title") or "").strip()
        if not title:
            continue
        try:
            start_time = float(chapter.get("start_time", 0.0))
        except Exception:  # noqa: BLE001
            continue
        out.append((max(0.0, start_time), title))

    out.sort(key=lambda item: item[0])
    return out


def _render_podly_chapters_html(post: Post) -> str:
    chapters = feed_chapters(post)
    if not chapters:
        return ""

    items = "".join(
        (f"<li>{_format_chapter_timestamp(start_time)} {html.escape(title)}</li>")
        for start_time, title in chapters
    )
    return f"<p><strong>Podly Chapters</strong></p><ul>{items}</ul>"


def _normalize_feed_text(value: str | None) -> str:
    if not value:
        return ""
    return value.translate(_FEED_TEXT_NORMALIZE_TABLE)


def build_post_feed_description_html(post: Post) -> str:
    """Build the description shown in Podly-generated RSS items for a post."""
    description_parts: list[str] = []
    if post.description:
        description_parts.append(_normalize_feed_text(post.description))

    chapters_html = _render_podly_chapters_html(post)
    if chapters_html:
        description_parts.append(chapters_html)

    return "\n".join(description_parts)


def _write_cdata(handler: Any, value: str) -> None:
    if not value:
        return

    escaped_value = value.replace("]]>", "]]]]><![CDATA[>")
    writer = getattr(handler, "_write", None)
    if callable(writer):
        writer(f"<![CDATA[{escaped_value}]]>")
        return

    handler.characters(value)


def _publish_cdata_opt_element(handler: Any, name: str, value: str | None) -> None:
    if value is None:
        return

    handler.startElement(name, {})
    _write_cdata(handler, value)
    handler.endElement(name)


def is_feed_active_for_user(feed_id: int, user: User) -> bool:
    """Check if the feed is within the user's allowance based on subscription date."""
    if user.role == "admin":
        return True

    # Hack: Always treat Feed 1 as active
    if feed_id == 1:
        return True

    # Use manual allowance if set, otherwise fall back to plan allowance
    manual_allowance = user.manual_feed_allowance
    if manual_allowance is not None:
        allowance = int(manual_allowance)
    else:
        allowance = int(getattr(user, "feed_allowance", 0))

    # Sort user's feeds by creation date to determine priority
    user_feeds = sorted(
        cast(Iterable[UserFeed], user.user_feeds),
        key=lambda uf: uf.created_at or datetime.datetime(1970, 1, 1),
    )

    for i, uf in enumerate(user_feeds):
        if uf.feed_id == feed_id:
            return i < allowance

    return False


def _should_auto_whitelist_new_posts(feed: Feed, post: Post | None = None) -> bool:
    """Return True when new posts should default to whitelisted for this feed."""
    override = getattr(feed, "auto_whitelist_new_episodes_override", None)
    if override is not None:
        return bool(override)

    if not getattr(config, "automatically_whitelist_new_episodes", False):
        return False

    from app.auth import is_auth_enabled

    # If auth is disabled, we should auto-whitelist if the global setting is on.
    if not is_auth_enabled():
        return True

    memberships = getattr(feed, "user_feeds", None) or []
    if not memberships:
        # No memberships for this feed. If there are no users in the database at all,
        # still whitelist. This handles fresh installs where no account exists yet.
        if db.session.query(User.id).first() is None:
            return True
        return False

    # Check if at least one member has this feed in their "active" list (within allowance)
    for membership in memberships:
        user = membership.user
        if not user:
            continue

        if is_feed_active_for_user(feed.id, user):
            return True

    return False


def _get_base_url() -> str:
    try:

        def _normalize_proto(value: Any) -> str | None:
            if value is None:
                return None
            first = str(value).split(",")[0].strip().strip('"').lower()
            return first if first in {"http", "https"} else None

        # Check various ways HTTP/2 pseudo-headers might be available
        http2_scheme = (
            request.headers.get(":scheme")
            or request.headers.get("scheme")
            or request.environ.get("HTTP2_SCHEME")
        )
        http2_authority = (
            request.headers.get(":authority")
            or request.headers.get("authority")
            or request.environ.get("HTTP2_AUTHORITY")
        )
        host = request.headers.get("Host")

        if http2_scheme and http2_authority:
            return f"{http2_scheme}://{http2_authority}"

        # Fall back to Host header with scheme detection
        if host:
            forwarded_proto = None
            for header_name in (
                "X-Forwarded-Proto",
                "X-Forwarded-Protocol",
                "X-Forwarded-Scheme",
                "X-Url-Scheme",
            ):
                forwarded_proto = _normalize_proto(request.headers.get(header_name))
                if forwarded_proto:
                    break

            if not forwarded_proto:
                forwarded = request.headers.get("Forwarded")
                if forwarded:
                    match = _FORWARDED_PROTO_RE.search(forwarded)
                    if match:
                        forwarded_proto = _normalize_proto(match.group(2))

            if not forwarded_proto:
                cf_visitor = request.headers.get("CF-Visitor")
                if cf_visitor:
                    try:
                        forwarded_proto = _normalize_proto(
                            json.loads(cf_visitor).get("scheme")
                        )
                    except Exception:  # noqa: BLE001
                        forwarded_proto = None

            # Check multiple indicators for HTTPS
            is_https = forwarded_proto == "https" or (
                request.is_secure
                or request.headers.get("Strict-Transport-Security") is not None
                or request.headers.get("X-Forwarded-Ssl") == "on"
                or request.headers.get("Front-End-Https") == "on"
                or request.headers.get("X-Forwarded-Port") == "443"
                or request.environ.get("HTTPS") == "on"
                or request.scheme == "https"
            )
            scheme = forwarded_proto or ("https" if is_https else "http")
            return f"{scheme}://{host}"
    except RuntimeError:
        # Working outside of request context
        pass

    # Use localhost with main app port
    return "http://localhost:5001"


def _fetch_raw_feed_bytes(url: str) -> tuple[bytes | None, str | None]:
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "podly/1.0"})
        resp.raise_for_status()
        return resp.content, resp.url
    except requests.RequestException as exc:
        logger.warning(f"requests fetch failed: {exc}")
        return None, None


def fetch_feed(url: str) -> feedparser.FeedParserDict:
    logger.info(f"Fetching feed from URL: {url}")
    raw_xml, final_url = _fetch_raw_feed_bytes(url)
    if raw_xml is None:
        # Network failure via requests; fall back to feedparser's own fetch.
        feed_data = feedparser.parse(url)
        for entry in feed_data.entries:
            entry.id = get_guid(entry)
        return feed_data

    feed_data = feedparser.parse(raw_xml)
    feed_data["href"] = final_url
    feed_data["podly_raw_xml"] = raw_xml
    for entry in feed_data.entries:
        entry.id = get_guid(entry)
    return feed_data


def _build_refresh_feed_payload(
    feed: Feed,
    feed_data: feedparser.FeedParserDict,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    updates = {}
    image_info = feed_data.feed.get("image")
    if image_info and "href" in image_info:
        new_image_url = image_info["href"]
        if feed.image_url != new_image_url:
            updates["image_url"] = new_image_url

    # Refresh matching only needs source metadata. Loading feed.posts also
    # materializes every transcript/processing JSON document in the archive.
    posts = (
        db.session.query(Post)
        .filter(Post.feed_id == feed.id)
        .options(
            load_only(
                Post.id,
                Post.guid,
                Post.download_url,
                Post.release_date,
                Post.title,
                Post.description,
                Post.image_url,
                Post.processed_audio_path,
                Post.duration,
            )
        )
        .all()
    )
    existing_posts = {post.guid: post for post in posts}
    existing_posts_by_url = {
        post.download_url: post for post in posts if post.download_url
    }
    oldest_post = min(
        (post for post in posts if post.release_date),
        key=lambda p: p.release_date,
        default=None,
    )

    new_posts = []
    existing_post_updates = []
    for entry in feed_data.entries:
        existing_post = existing_posts.get(entry.id)
        repaired_guid: str | None = None
        if existing_post is None:
            # GUID didn't match; try recovering by audio URL. This catches
            # posts whose stored guid was synthesized from the enclosure URL
            # by the legacy `get_guid` fallback. Without this lookup, the
            # corrected upstream guid would orphan the existing row and the
            # episode would re-appear to subscribers as new.
            audio_url = find_audio_link(entry)
            if audio_url:
                existing_post = existing_posts_by_url.get(audio_url)
                if existing_post is not None and existing_post.guid != entry.id:
                    repaired_guid = entry.id
        if existing_post is None:
            new_posts.append(_new_post_refresh_payload(feed, entry, oldest_post))
        else:
            post_update = _existing_post_refresh_payload(
                existing_post, entry, feed, repaired_guid=repaired_guid
            )
            if post_update:
                existing_post_updates.append(post_update)

    del existing_posts
    return updates, new_posts, existing_post_updates


def _new_post_refresh_payload(
    feed: Feed,
    entry: feedparser.FeedParserDict,
    oldest_post: Post | None,
) -> dict[str, Any]:
    logger.debug("found new podcast: %s", entry.title)
    post = make_post(feed, entry)
    # do not allow automatic download of any backcatalog added to the feed
    if (
        oldest_post is not None
        and post.release_date
        and oldest_post.release_date
        and post.release_date.date() < oldest_post.release_date.date()
    ):
        post.whitelisted = False
        logger.debug(
            f"skipping post from archive due to \
number_of_episodes_to_whitelist_from_archive_of_new_feed setting: {entry.title}"
        )
    else:
        post.whitelisted = _should_auto_whitelist_new_posts(feed, post)

    return {
        "guid": post.guid,
        "title": post.title,
        "description": post.description,
        "download_url": post.download_url,
        "release_date": post.release_date.isoformat() if post.release_date else None,
        "duration": post.duration,
        "image_url": post.image_url,
        "whitelisted": post.whitelisted,
        "feed_id": feed.id,
    }


def _existing_post_refresh_payload(
    existing_post: Post,
    entry: feedparser.FeedParserDict,
    feed: Feed,
    *,
    repaired_guid: str | None = None,
) -> dict[str, Any]:
    post_update: dict[str, Any] = {"post_id": existing_post.id}

    if repaired_guid is not None:
        post_update["guid"] = repaired_guid

    updated_title = str(getattr(entry, "title", "") or "").strip()
    if updated_title and existing_post.title != updated_title:
        post_update["title"] = updated_title

    updated_description = _extract_post_description(entry)
    if existing_post.description != updated_description:
        post_update["description"] = updated_description

    updated_image_url = _extract_episode_image_url(entry, feed)
    if existing_post.image_url != updated_image_url:
        post_update["image_url"] = updated_image_url

    parsed_duration = get_duration(entry)
    if (
        existing_post.processed_audio_path is None
        and parsed_duration is not None
        and existing_post.duration != parsed_duration
    ):
        post_update["duration"] = parsed_duration

    return post_update if len(post_update) > 1 else {}


def refresh_feed(feed: Feed) -> list[str]:
    """Refresh a feed and return guids of newly discovered whitelisted posts.

    The returned guids are for genuinely new posts whose refresh payload has
    ``whitelisted=True`` (back-catalog/archive posts are never whitelisted).
    Callers use them to create processing jobs at discovery time instead of
    relying on a later unbounded sweep. Returns an empty list when nothing
    new was whitelisted or when the writer action did not confirm
    persistence (fail closed: no job without a stored post).
    """
    from shared.rust_sidecar import rust_feed_refresh_enabled

    logger.info(f"Refreshing feed with ID: {feed.id}")
    feed_id = feed.id
    new_posts: list[dict[str, Any]] = []
    existing_post_updates: list[dict[str, Any]] = []
    updates: dict[str, Any] = {}
    feed_data: feedparser.FeedParserDict | None = None
    raw_xml: bytes | None = None

    # When Rust planning is enabled, try the Rust path with just raw XML and
    # skip the expensive feedparser.parse on the happy path. feedparser parsing
    # of a 3MB+ feed can dominate refresh latency (1+ second).
    if rust_feed_refresh_enabled():
        logger.info(f"Fetching feed from URL: {feed.rss_url}")
        raw_xml, _ = _fetch_raw_feed_bytes(feed.rss_url)

    try:
        rust_plan: dict[str, Any] | None = None
        if raw_xml is not None:
            rust_plan = _try_build_rust_refresh_feed_payload_from_xml(feed, raw_xml)

        if rust_plan is not None:
            updates = cast(dict[str, Any], rust_plan["updates"])
            new_posts = cast(list[dict[str, Any]], rust_plan["new_posts"])
            existing_post_updates = cast(
                list[dict[str, Any]], rust_plan["existing_post_updates"]
            )
            # Positive confirmation log so operators can verify the Rust
            # path is actually hot for this feed (the wrapper only logs
            # the negative "falling back" case). Grep for "rust_path=feed_refresh".
            logger.info(
                "rust_path=feed_refresh feed_id=%s new=%d updated=%d",
                feed_id,
                len(new_posts),
                len(existing_post_updates),
            )
        else:
            # Either Rust disabled, raw fetch failed, or Rust planner returned None.
            # Run the standard feedparser-based path (parses raw_xml if we have it,
            # otherwise fetches the URL again through feedparser).
            if feed_data is None:
                if raw_xml is not None:
                    feed_data = feedparser.parse(raw_xml)
                    for entry in feed_data.entries:
                        entry.id = get_guid(entry)
                else:
                    feed_data = fetch_feed(feed.rss_url)
            updates, new_posts, existing_post_updates = _build_refresh_feed_payload(
                feed, feed_data
            )
        if updates or new_posts or existing_post_updates:
            write_result = writer_client.action(
                "refresh_feed",
                {
                    "feed_id": feed_id,
                    "updates": updates,
                    "new_posts": new_posts,
                    "existing_post_updates": existing_post_updates,
                },
                wait=True,
            )
            write_succeeded = bool(write_result and write_result.success)
            if not write_succeeded:
                logger.error(
                    "refresh_feed writer action failed for feed_id=%s; "
                    "no discovery guids will be returned",
                    feed_id,
                )
            # Refreshes are written through the separate writer service, so expire the
            # current request session before serializing the feed response.
            db.session.expire_all()
        else:
            write_succeeded = False

        # Guids of genuinely new posts whitelisted by this refresh. Callers
        # create processing jobs from these at discovery time. Empty when the
        # writer did not confirm persistence (fail closed).
        new_whitelisted_guids: list[str] = (
            [
                str(payload["guid"])
                for payload in new_posts
                if payload.get("whitelisted") and payload.get("guid")
            ]
            if write_succeeded
            else []
        )
    finally:
        # The plan owns the same lists as the aliases above. Drop it too before
        # the single teardown trim, after the DB session releases its objects.
        del rust_plan, feed_data, raw_xml, updates, new_posts, existing_post_updates
        request_memory_trim_after_context(f"feed refresh feed_id={feed_id}")

    logger.info(f"Feed with ID: {feed_id} refreshed")
    return new_whitelisted_guids


def _try_build_rust_refresh_feed_payload(
    feed: Feed,
    feed_data: feedparser.FeedParserDict,
) -> dict[str, Any] | None:
    raw_xml = _raw_feed_xml_from_feedparser_result(feed_data)
    if raw_xml is None:
        logger.info(
            "Rust feed refresh planning skipped because feedparser result has no raw XML"
        )
        return None
    return _try_build_rust_refresh_feed_payload_from_xml(feed, raw_xml)


def _try_build_rust_refresh_feed_payload_from_xml(
    feed: Feed,
    raw_xml: str | bytes,
) -> dict[str, Any] | None:
    from shared.processing_paths import get_instance_dir

    db_path = get_instance_dir() / "sqlite3.db"

    return try_plan_feed_refresh(
        db_path=db_path,
        feed_id=int(feed.id),
        feed_xml=raw_xml,
        auto_whitelist_new_posts=_should_auto_whitelist_new_posts(feed),
    )


def _raw_feed_xml_from_feedparser_result(
    feed_data: feedparser.FeedParserDict,
) -> str | bytes | None:
    for field in ("content", "rawdata", "data", "podly_raw_xml", "_podly_raw_xml"):
        value = getattr(feed_data, field, None)
        if isinstance(value, str | bytes) and value:
            return value
        if isinstance(feed_data, dict):
            value = feed_data.get(field)
            if isinstance(value, str | bytes) and value:
                return value
    return None


def add_or_refresh_feed(url: str) -> Feed:
    feed_data = fetch_feed(url)
    if "title" not in feed_data.feed:
        logger.error("Invalid feed URL")
        raise ValueError(f"Invalid feed URL: {url}")

    feed = Feed.query.filter_by(rss_url=url).first()
    if feed:
        # Discovery-time job creation intentionally ignored here: adding a
        # feed can whitelist several recent archive episodes at once and those
        # are picked up gradually by the bounded repair instead.
        refresh_feed(feed)
    else:
        feed = add_feed(feed_data)
    return feed


def add_feed(feed_data: feedparser.FeedParserDict) -> Feed:
    logger.info(f"Storing feed: {feed_data.feed.title}")
    try:
        feed_dict = {
            "title": feed_data.feed.title,
            "description": feed_data.feed.get("description", ""),
            "author": feed_data.feed.get("author", ""),
            "rss_url": feed_data.href,
            "image_url": feed_data.feed.image.href,
        }

        # Create a temporary feed object to use make_post helper
        temp_feed = Feed(**feed_dict)
        temp_feed.id = 0  # Dummy ID

        posts_data = []
        num_posts_added = 0
        for entry in feed_data.entries:
            p = make_post(temp_feed, entry)
            if (
                config.number_of_episodes_to_whitelist_from_archive_of_new_feed
                is not None
                and num_posts_added
                >= config.number_of_episodes_to_whitelist_from_archive_of_new_feed
            ):
                p.whitelisted = False
            else:
                num_posts_added += 1
                p.whitelisted = config.automatically_whitelist_new_episodes

            post_data = {
                "guid": p.guid,
                "title": p.title,
                "description": p.description,
                "download_url": p.download_url,
                "release_date": p.release_date.isoformat() if p.release_date else None,
                "duration": p.duration,
                "image_url": p.image_url,
                "whitelisted": p.whitelisted,
            }
            posts_data.append(post_data)

        result = writer_client.action(
            "add_feed", {"feed": feed_dict, "posts": posts_data}, wait=True
        )

        if result is None or result.data is None:
            raise RuntimeError("Failed to get result from writer action")

        feed_id = result.data["feed_id"]
        logger.info(f"Feed stored with ID: {feed_id}")

        # Return the feed object
        feed = db.session.get(Feed, feed_id)
        if feed is None:
            raise RuntimeError(f"Feed {feed_id} not found after creation")
        return feed

    except Exception as e:
        logger.error(f"Failed to store feed: {e}")
        raise e


class ItunesRSSItem(PyRSS2Gen.RSSItem):
    def __init__(
        self,
        *,
        title: str,
        enclosure: PyRSS2Gen.Enclosure,
        description: str,
        guid: str,
        pubDate: str | None,
        image_url: str | None = None,
        duration_seconds: int | None = None,
        chapters_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.image_url = image_url
        self.duration_seconds = duration_seconds
        self.chapters_url = chapters_url
        super().__init__(
            title=title,
            enclosure=enclosure,
            description=description,
            guid=guid,
            pubDate=pubDate,
            **kwargs,
        )

    def publish_extensions(self, handler: Any) -> None:
        if self.image_url:
            handler.startElement("itunes:image", {"href": self.image_url})
            handler.endElement("itunes:image")
        if self.duration_seconds:
            handler.startElement("itunes:duration", {})
            handler.characters(_format_itunes_duration(self.duration_seconds))
            handler.endElement("itunes:duration")
        _publish_cdata_opt_element(handler, "content:encoded", self.description)
        super().publish_extensions(handler)

    def publish(self, handler: Any) -> None:
        # PyRSS2Gen escapes item descriptions with handler.characters(), which
        # flattens rich HTML from source feeds and the appended Podly chapters.
        handler.startElement("item", self.element_attrs)
        PyRSS2Gen._opt_element(handler, "title", self.title)
        PyRSS2Gen._opt_element(handler, "link", self.link)
        self.publish_extensions(handler)
        _publish_cdata_opt_element(handler, "description", self.description)
        PyRSS2Gen._opt_element(handler, "author", self.author)

        for item_category in self.categories:
            category = (
                PyRSS2Gen.Category(item_category)
                if isinstance(item_category, str)
                else item_category
            )
            category.publish(handler)

        PyRSS2Gen._opt_element(handler, "comments", self.comments)
        if self.enclosure is not None:
            self.enclosure.publish(handler)
        if self.chapters_url:
            # Podcasting 2.0 chapters let clients that stream before parsing the
            # MP3's embedded ID3 chapters still show them.
            handler.startElement(
                "podcast:chapters",
                {"url": self.chapters_url, "type": "application/json+chapters"},
            )
            handler.endElement("podcast:chapters")
        PyRSS2Gen._opt_element(handler, "guid", self.guid)

        pub_date = self.pubDate
        if isinstance(pub_date, datetime.datetime):
            pub_date = PyRSS2Gen.DateElement("pubDate", pub_date)
        PyRSS2Gen._opt_element(handler, "pubDate", pub_date)

        if self.source is not None:
            self.source.publish(handler)

        handler.endElement("item")


def _feed_item_duration_seconds(post: Post) -> int | None:
    parsed_duration = _parse_duration_seconds(getattr(post, "duration", None))
    if parsed_duration is not None:
        return parsed_duration

    return None


def feed_item(post: Post, prepend_feed_title: bool = False) -> PyRSS2Gen.RSSItem:
    """
    Given a post, return the corresponding RSS item. Reference:
    https://github.com/Podcast-Standards-Project/PSP-1-Podcast-RSS-Specification?tab=readme-ov-file#required-item-elements
    """

    base_url = _get_base_url()

    # Podcast clients stream enclosure URLs directly, so use the inline MP3 route
    # rather than the attachment-style download endpoint. Encode the guid so that
    # guids containing slashes / question marks (e.g. supercast URL-as-guid posts)
    # don't break URL parsing on the client or Flask routing on the server.
    audio_url = _append_feed_token_params(
        f"{base_url}/post/{quote(post.guid, safe='')}.mp3"
    )
    description = build_post_feed_description_html(post)

    title = post.title
    if prepend_feed_title and post.feed:
        title = f"[{post.feed.title}] {title}"

    duration_seconds = _feed_item_duration_seconds(post)

    item = ItunesRSSItem(
        title=title,
        enclosure=PyRSS2Gen.Enclosure(
            url=audio_url,
            type="audio/mpeg",
            length=post.audio_len_bytes(),
        ),
        description=description,
        guid=post.guid,
        pubDate=_format_pub_date(post.release_date),
        image_url=post.image_url,
        duration_seconds=duration_seconds,
        chapters_url=(
            _append_feed_token_params(
                f"{base_url}/post/{quote(post.guid, safe='')}/chapters.json"
            )
            if feed_chapters(post)
            else None
        ),
    )

    return item


def _feed_last_changed_at_aware(feed: Feed) -> datetime.datetime:
    """Return `feed.last_changed_at` as an aware UTC datetime.

    `last_changed_at` is stored naive (UTC) to match other timestamps in
    the schema; HTTP date / RFC 2822 formatters require an aware value.
    Falls back to `now()` for legacy rows that may pre-date the column.
    """
    raw = getattr(feed, "last_changed_at", None)
    if raw is None:
        return datetime.datetime.now(datetime.UTC)
    if raw.tzinfo is None:
        return raw.replace(tzinfo=datetime.UTC)
    return raw.astimezone(datetime.UTC)


def get_aggregate_feed_last_changed_at(user: User | None) -> datetime.datetime:
    """Return the max `last_changed_at` across feeds in this user's aggregate.

    Used as the aggregate feed's `lastBuildDate` and for ETag freshness.
    Falls back to `now()` when there are no feeds (or the configured user
    has no subscriptions yet).
    """
    if not current_app.config.get("REQUIRE_AUTH") or user is None:
        latest = db.session.query(db.func.max(Feed.last_changed_at)).scalar()
    else:
        latest = (
            db.session.query(db.func.max(Feed.last_changed_at))
            .join(UserFeed, UserFeed.feed_id == Feed.id)
            .filter(UserFeed.user_id == user.id)
            .scalar()
        )
    if latest is None:
        return datetime.datetime.now(datetime.UTC)
    if latest.tzinfo is None:
        return latest.replace(tzinfo=datetime.UTC)
    return latest.astimezone(datetime.UTC)


def generate_feed_xml(feed: Feed) -> Any:
    logger.info(f"Generating XML for feed with ID: {feed.id}")

    include_unprocessed = getattr(config, "autoprocess_on_download", True)
    rust_xml = _try_generate_feed_xml_with_rust(feed, include_unprocessed)
    if rust_xml is not None:
        return rust_xml

    if include_unprocessed:
        # Hit Post directly with defers rather than walking feed.posts: the
        # relationship loads every column including the megabyte-class
        # transcript_word_timestamps JSON that feed_item never reads.
        #
        # Hide posts that are queued or running their *first* processing pass:
        # podcast clients (notably Pocket Casts) cache ID3 metadata on first
        # parse and won't re-read after we add chapter tags, so publishing the
        # un-chaptered MP3 means a permanently chapter-less episode in the
        # client until the user restarts the app. Once a post has a
        # processed_audio_path it stays visible — re-processing leaves the
        # last good audio in place and the client already cached it anyway.
        active_job_subq = (
            db.session.query(ProcessingJob.id)
            .filter(
                ProcessingJob.post_guid == Post.guid,
                ProcessingJob.status.in_(("pending", "running")),
            )
            .exists()
        )
        posts = (
            Post.query.filter(
                Post.feed_id == feed.id,
                db.or_(
                    Post.processed_audio_path.isnot(None),
                    ~active_job_subq,
                ),
            )
            .options(*post_feed_render_defers())
            .order_by(Post.release_date.desc().nullslast(), Post.id.desc())
            .all()
        )
    else:
        posts = (
            Post.query.filter(
                Post.feed_id == feed.id,
                Post.whitelisted.is_(True),
                Post.processed_audio_path.isnot(None),
            )
            .options(*post_feed_render_defers())
            .order_by(Post.release_date.desc().nullslast(), Post.id.desc())
            .all()
        )

    items = [feed_item(post) for post in posts]

    base_url = _get_base_url()
    link = _append_feed_token_params(f"{base_url}/feed/{feed.id}")

    last_build_date = format_datetime(_feed_last_changed_at_aware(feed))

    rss_feed = PyRSS2Gen.RSS2(
        title="[podly] " + feed.title,
        link=link,
        description=_normalize_feed_text(feed.description),
        lastBuildDate=last_build_date,
        image=PyRSS2Gen.Image(url=feed.image_url, title=feed.title, link=link),
        items=items,
    )

    rss_feed.rss_attrs["xmlns:itunes"] = "http://www.itunes.com/dtds/podcast-1.0.dtd"
    rss_feed.rss_attrs["xmlns:content"] = "http://purl.org/rss/1.0/modules/content/"
    rss_feed.rss_attrs["xmlns:podcast"] = "https://podcastindex.org/namespace/1.0"

    xml_content = rss_feed.to_xml("utf-8")

    logger.info(f"XML generated for feed with ID: {feed.id}")
    return xml_content


def generate_aggregate_feed_xml(user: User | None) -> Any:
    """Generate RSS XML for a user's aggregate feed (last 3 processed posts per feed)."""
    username = user.username if user else "Public"
    user_id = user.id if user else 0
    logger.info(f"Generating aggregate feed XML for: {username}")
    rust_xml = _try_generate_aggregate_feed_xml_with_rust(user)
    if rust_xml is not None:
        return rust_xml

    posts = get_user_aggregate_posts(user_id)
    items = [feed_item(post, prepend_feed_title=True) for post in posts]

    base_url = _get_base_url()
    link = _append_feed_token_params(f"{base_url}/feed/user/{user_id}")

    last_build_date = format_datetime(get_aggregate_feed_last_changed_at(user))

    if current_app.config.get("REQUIRE_AUTH") and user:
        feed_title = f"Podly Podcasts - {user.username}"
        feed_description = f"Aggregate feed for {user.username} - Last 3 processed episodes from each subscribed feed."
    else:
        feed_title = "Podly Podcasts"
        feed_description = (
            "Aggregate feed - Last 3 processed episodes from each subscribed feed."
        )

    rss_feed = PyRSS2Gen.RSS2(
        title=feed_title,
        link=link,
        description=feed_description,
        lastBuildDate=last_build_date,
        items=items,
        image=PyRSS2Gen.Image(
            url=f"{base_url}/static/images/logos/manifest-icon-512.maskable.png",
            title=feed_title,
            link=link,
        ),
    )

    rss_feed.rss_attrs["xmlns:itunes"] = "http://www.itunes.com/dtds/podcast-1.0.dtd"
    rss_feed.rss_attrs["xmlns:content"] = "http://purl.org/rss/1.0/modules/content/"
    rss_feed.rss_attrs["xmlns:podcast"] = "https://podcastindex.org/namespace/1.0"

    xml_content = rss_feed.to_xml("utf-8")

    logger.info(f"Aggregate XML generated for: {username}")
    return xml_content


def _sqlite_db_path_from_app_config() -> Path | None:
    uri = str(current_app.config.get("SQLALCHEMY_DATABASE_URI") or "")
    prefix = "sqlite:///"
    if not uri.startswith(prefix) or uri == "sqlite:///:memory:":
        return None
    # SQLAlchemy URIs may append a query string (e.g. "?timeout=60"); strip it
    # so we hand sqlite a real filesystem path, not a URI fragment.
    path = uri.removeprefix(prefix).split("?", 1)[0]
    if not path or path == ":memory:":
        return None
    db_path = Path(path)
    if db_path.is_absolute():
        return db_path
    return (Path(current_app.instance_path) / db_path).resolve()


def _feed_token_request_values() -> tuple[str | None, str | None]:
    try:
        token_result = getattr(g, "feed_token", None)
        token_id = request.args.get("feed_token")
        secret = request.args.get("feed_secret")
    except RuntimeError:
        return None, None

    if token_result is not None:
        token_id = token_id or token_result.token.token_id
        secret = secret or token_result.token.token_secret
    return token_id, secret


def _try_generate_feed_xml_with_rust(
    feed: Feed,
    include_unprocessed: bool,
) -> bytes | None:
    db_path = _sqlite_db_path_from_app_config()
    if db_path is None:
        return None
    token_id, secret = _feed_token_request_values()
    return try_render_feed_xml(
        db_path=db_path,
        feed_id=int(feed.id),
        base_url=_get_base_url(),
        include_unprocessed=bool(include_unprocessed),
        feed_token=token_id,
        feed_secret=secret,
    )


def _try_generate_aggregate_feed_xml_with_rust(user: User | None) -> bytes | None:
    db_path = _sqlite_db_path_from_app_config()
    if db_path is None:
        return None
    user_id = int(user.id) if user else 0
    token_id, secret = _feed_token_request_values()
    return try_render_aggregate_feed_xml(
        db_path=db_path,
        user_id=user_id,
        base_url=_get_base_url(),
        require_auth=bool(current_app.config.get("REQUIRE_AUTH")),
        limit_per_feed=3,
        feed_token=token_id,
        feed_secret=secret,
    )


def get_user_aggregate_posts(user_id: int, limit_per_feed: int = 3) -> list[Post]:
    """Fetch last N processed posts from each of the user's subscribed feeds."""
    if not current_app.config.get("REQUIRE_AUTH") or user_id == 0:
        feed_ids = [r[0] for r in Feed.query.with_entities(Feed.id).all()]
    else:
        user_feeds = UserFeed.query.filter_by(user_id=user_id).all()
        feed_ids = [uf.feed_id for uf in user_feeds]

    all_posts = []
    for feed_id in feed_ids:
        # Fetch last N processed posts for this feed
        posts = (
            Post.query.filter(
                Post.feed_id == feed_id,
                Post.whitelisted.is_(True),
                Post.processed_audio_path.isnot(None),
            )
            .options(*post_feed_render_defers())
            .order_by(Post.release_date.desc().nullslast(), Post.id.desc())
            .limit(limit_per_feed)
            .all()
        )
        all_posts.extend(posts)

    # Sort all posts by release date descending
    all_posts.sort(key=lambda p: p.release_date or datetime.datetime.min, reverse=True)

    return all_posts


def _append_feed_token_params(url: str) -> str:
    if not current_app.config.get("REQUIRE_AUTH"):
        return url

    try:
        token_result = getattr(g, "feed_token", None)
        token_id = request.args.get("feed_token")
        secret = request.args.get("feed_secret")
    except RuntimeError:
        return url

    if token_result is not None:
        token_id = token_id or token_result.token.token_id
        secret = secret or token_result.token.token_secret

    if not token_id or not secret:
        return url

    parsed = urlparse(url)
    query_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query_params["feed_token"] = token_id
    query_params["feed_secret"] = secret
    new_query = urlencode(query_params)
    return urlunparse(parsed._replace(query=new_query))


def _extract_episode_image_url(
    entry: feedparser.FeedParserDict,
    feed: Feed,
) -> str | None:
    """Prefer episode-level artwork when the source feed exposes it."""
    episode_image_url = None

    # Try to get episode-specific image from various RSS fields
    if hasattr(entry, "image") and entry.image:
        if isinstance(entry.image, dict) and "href" in entry.image:
            episode_image_url = entry.image["href"]
        elif isinstance(entry.image, str):
            episode_image_url = entry.image

    # Try iTunes image tag
    if not episode_image_url and hasattr(entry, "itunes_image"):
        if isinstance(entry.itunes_image, dict) and "href" in entry.itunes_image:
            episode_image_url = entry.itunes_image["href"]
        elif isinstance(entry.itunes_image, str):
            episode_image_url = entry.itunes_image

    # Try media:thumbnail or media:content
    if not episode_image_url and hasattr(entry, "media_thumbnail"):
        if entry.media_thumbnail and len(entry.media_thumbnail) > 0:
            episode_image_url = entry.media_thumbnail[0].get("url")

    # Fallback to feed image if no episode-specific image found
    if not episode_image_url:
        episode_image_url = feed.image_url

    return episode_image_url


def _extract_post_description(entry: feedparser.FeedParserDict) -> str:
    """Prefer rich HTML payloads when a source feed exposes them."""
    content_items = getattr(entry, "content", None) or []
    for content in content_items:
        value = str(content.get("value", "") or "").strip()
        content_type = str(content.get("type", "") or "").strip().lower()
        if value and content_type in {"text/html", "application/xhtml+xml"}:
            return value

    for field in ("description", "summary"):
        value = str(entry.get(field, "") or "").strip()
        if value:
            return value

    for content in content_items:
        value = str(content.get("value", "") or "").strip()
        if value:
            return value

    return str(entry.get("subtitle", "") or "").strip()


def make_post(feed: Feed, entry: feedparser.FeedParserDict) -> Post:
    return Post(
        feed_id=feed.id,
        guid=get_guid(entry),
        download_url=find_audio_link(entry),
        title=entry.title,
        description=_extract_post_description(entry),
        release_date=_parse_release_date(entry),
        duration=get_duration(entry),
        image_url=_extract_episode_image_url(entry, feed),
    )


def _get_entry_field(
    entry: feedparser.FeedParserDict | dict[str, Any], field: str
) -> Any | None:
    value = getattr(entry, field, None)
    return value if value is not None else entry.get(field)


def _parse_datetime_string(value: str | None, field: str) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except TypeError, ValueError:
        logger.debug("Failed to parse %s string for release date", field)
        return None


def _parse_struct_time(value: Any | None, field: str) -> datetime.datetime | None:
    if not value:
        return None
    try:
        dt = datetime.datetime(*value[:6])
    except TypeError, ValueError:
        logger.debug("Failed to parse %s for release date", field)
        return None
    gmtoff = getattr(value, "tm_gmtoff", None)
    if gmtoff is not None:
        dt = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(seconds=gmtoff)))
    return dt


def _normalize_to_utc(dt: datetime.datetime | None) -> datetime.datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)


def _parse_release_date(
    entry: feedparser.FeedParserDict,
) -> datetime.datetime | None:
    """Parse a release datetime from a feed entry and normalize to UTC."""
    for field in ("published", "updated"):
        dt = _parse_datetime_string(_get_entry_field(entry, field), field)
        normalized = _normalize_to_utc(dt)
        if normalized:
            return normalized

    for field in ("published_parsed", "updated_parsed"):
        dt = _parse_struct_time(_get_entry_field(entry, field), field)
        normalized = _normalize_to_utc(dt)
        if normalized:
            return normalized

    return None


def _format_pub_date(release_date: datetime.datetime | None) -> str | None:
    if not release_date:
        return None

    normalized = release_date
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=datetime.UTC)

    return format_datetime(normalized.astimezone(datetime.UTC))


def get_guid(entry: feedparser.FeedParserDict) -> str:
    """Return the post GUID, preferring the upstream `<guid>` verbatim.

    Real feeds rarely use UUIDs as their `<guid>`; URLs and `tag:` URIs are
    common. Hashing the enclosure URL as a substitute breaks subscriber
    libraries whenever the CDN path or tracking params change, so we only
    fall back to URL hashing when the upstream provides no usable id.
    """
    raw = getattr(entry, "id", None) or getattr(entry, "guid", None)
    if isinstance(raw, str):
        candidate = raw.strip()
        if candidate:
            return candidate
    dlurl = find_audio_link(entry)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, dlurl))


def get_duration(entry: feedparser.FeedParserDict | dict[str, Any]) -> int | None:
    for field in ("itunes_duration", "duration"):
        parsed_duration = _parse_duration_seconds(_get_entry_field(entry, field))
        if parsed_duration is not None:
            return parsed_duration

    return None
