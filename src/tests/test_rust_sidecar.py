from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from shared import rust_sidecar
from shared.rust_sidecar import RustSidecarError


def test_rust_feature_flags_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PODLY_RUST_AUDIO_ENABLED", raising=False)
    monkeypatch.delenv("PODLY_RUST_CHAPTERS_ENABLED", raising=False)
    monkeypatch.delenv("PODLY_RUST_FEED_REFRESH_ENABLED", raising=False)
    monkeypatch.delenv("PODLY_RUST_FEED_XML_ENABLED", raising=False)
    monkeypatch.delenv("PODLY_RUST_JOBS_ENABLED", raising=False)
    monkeypatch.delenv("PODLY_RUST_STATS_ENABLED", raising=False)
    monkeypatch.delenv("PODLY_RUST_TRANSCRIPT_ENABLED", raising=False)
    monkeypatch.delenv("PODLY_RUST_FEED_POSTS_ENABLED", raising=False)

    assert rust_sidecar.rust_audio_enabled() is True
    assert rust_sidecar.rust_chapters_enabled() is True
    assert rust_sidecar.rust_feed_refresh_enabled() is True
    assert rust_sidecar.rust_feed_xml_enabled() is True
    assert rust_sidecar.rust_jobs_enabled() is True
    assert rust_sidecar.rust_stats_enabled() is True
    assert rust_sidecar.rust_transcript_enabled() is True
    assert rust_sidecar.rust_feed_posts_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
def test_rust_feature_flags_opt_out_with_falsy_value(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PODLY_RUST_AUDIO_ENABLED", value)

    assert rust_sidecar.rust_audio_enabled() is False


def test_try_render_feed_posts_returns_none_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PODLY_RUST_FEED_POSTS_ENABLED", "false")
    assert (
        rust_sidecar.try_render_feed_posts(
            db_path=Path("/tmp/x.sqlite"),
            feed_id=1,
            page=1,
            page_size=25,
            whitelisted_only=False,
        )
        is None
    )


def test_try_render_feed_posts_returns_not_found_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PODLY_RUST_FEED_POSTS_ENABLED", "true")
    notify = Mock()
    monkeypatch.setattr(rust_sidecar, "_notify_rust_fallback", notify)

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            command, 0, stdout=b'{"not_found":true}\n', stderr=b""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = rust_sidecar.try_render_feed_posts(
        db_path=Path("/tmp/x.sqlite"),
        feed_id=999,
        page=1,
        page_size=25,
        whitelisted_only=False,
    )
    assert result is rust_sidecar.FEED_POSTS_NOT_FOUND
    notify.assert_not_called()


def test_try_render_feed_posts_rejects_payload_without_items_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PODLY_RUST_FEED_POSTS_ENABLED", "true")

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            command, 0, stdout=b'{"oops":"wrong shape"}', stderr=b""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert (
        rust_sidecar.try_render_feed_posts(
            db_path=Path("/tmp/x.sqlite"),
            feed_id=1,
            page=1,
            page_size=25,
            whitelisted_only=False,
        )
        is None
    )


def test_try_render_feed_posts_returns_raw_bytes_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PODLY_RUST_FEED_POSTS_ENABLED", "true")
    notify = Mock()
    monkeypatch.setattr(rust_sidecar, "_notify_rust_fallback", notify)
    seen: list[list[str]] = []
    raw = (
        b'{"items":[{"id":1,"guid":"g","title":"t"}],'
        b'"page":2,"page_size":25,"total":1,"total_pages":1,"whitelisted_total":0}'
    )

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=raw + b"\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    payload = rust_sidecar.try_render_feed_posts(
        db_path=Path("/srv/podly/sqlite3.db"),
        feed_id=7,
        page=2,
        page_size=25,
        whitelisted_only=True,
    )
    # The wrapper must hand back the raw bytes unchanged (modulo trailing
    # newline) so Flask streams them directly without re-serializing.
    assert payload == raw
    notify.assert_not_called()
    cmd = seen[0]
    assert "posts" in cmd and "feed-list" in cmd
    assert "--feed-id" in cmd and "7" in cmd
    assert "--whitelisted-only" in cmd and "true" in cmd


def test_try_render_feed_posts_falls_back_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PODLY_RUST_FEED_POSTS_ENABLED", "true")

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            command, 2, stdout=b"", stderr=b"db locked\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert (
        rust_sidecar.try_render_feed_posts(
            db_path=Path("/tmp/x.sqlite"),
            feed_id=1,
            page=1,
            page_size=25,
            whitelisted_only=False,
        )
        is None
    )


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        (OSError("missing binary"), "failed to start podly_tools: missing binary"),
        (
            subprocess.TimeoutExpired("podly_tools", 300),
            "podly_tools timed out after 300s",
        ),
        (
            subprocess.CompletedProcess([], 2, stdout=b"", stderr=b"db locked\n"),
            "podly_tools exited with 2: db locked",
        ),
        (
            subprocess.CompletedProcess([], 1, stdout=b"", stderr=b""),
            "podly_tools exited with 1: <no stderr>",
        ),
        (
            subprocess.CompletedProcess([], 0, stdout=b"not JSON", stderr=b""),
            "podly_tools returned an unexpected payload prefix",
        ),
        (
            subprocess.CompletedProcess([], 0, stdout=b"{}", stderr=b""),
            "podly_tools returned an unexpected payload prefix",
        ),
    ],
)
def test_try_render_feed_posts_notifies_and_logs_fallback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception | subprocess.CompletedProcess[bytes],
    expected_error: str,
) -> None:
    monkeypatch.setenv("PODLY_RUST_FEED_POSTS_ENABLED", "true")
    notify = Mock()
    monkeypatch.setattr(rust_sidecar, "_notify_rust_fallback", notify)

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert (
        rust_sidecar.try_render_feed_posts(
            db_path=Path("/tmp/x.sqlite"),
            feed_id=1,
            page=1,
            page_size=25,
            whitelisted_only=False,
        )
        is None
    )
    notify.assert_called_once_with("posts feed-list", expected_error)
    assert "falling back to Python" in caplog.text


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_env_flag_enabled_accepts_truthy_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PODLY_RUST_AUDIO_ENABLED", value)

    assert rust_sidecar.rust_audio_enabled() is True


def test_run_podly_tools_uses_configured_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setenv("PODLY_RUST_TOOLS_BIN", "/tmp/podly_tools")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert rust_sidecar.run_podly_tools(["audio", "probe", "--input", "x.mp3"]) == {
        "ok": True
    }
    assert calls == [["/tmp/podly_tools", "audio", "probe", "--input", "x.mp3"]]


def test_run_podly_tools_rejects_failed_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RustSidecarError, match="boom"):
        rust_sidecar.run_podly_tools(["audio", "probe", "--input", "x.mp3"])


def test_try_probe_audio_duration_falls_back_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PODLY_RUST_AUDIO_ENABLED", "false")

    assert rust_sidecar.try_probe_audio_duration_ms(Path("x.mp3")) is None


def test_try_probe_audio_duration_returns_valid_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PODLY_RUST_AUDIO_ENABLED", "true")
    monkeypatch.setattr(
        rust_sidecar, "run_podly_tools", lambda args: {"duration_ms": 1234}
    )

    assert rust_sidecar.try_probe_audio_duration_ms(Path("x.mp3")) == 1234


def test_try_probe_audio_duration_falls_back_on_bad_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PODLY_RUST_AUDIO_ENABLED", "true")
    monkeypatch.setattr(
        rust_sidecar, "run_podly_tools", lambda args: {"duration_ms": "1234"}
    )

    assert rust_sidecar.try_probe_audio_duration_ms(Path("x.mp3")) is None


def test_rust_audio_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PODLY_RUST_AUDIO_TIMEOUT_SEC", raising=False)
    assert (
        rust_sidecar.rust_audio_timeout_sec()
        == rust_sidecar.DEFAULT_RUST_AUDIO_TIMEOUT_SEC
    )


def test_rust_audio_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PODLY_RUST_AUDIO_TIMEOUT_SEC", "120")
    assert rust_sidecar.rust_audio_timeout_sec() == 120


def test_rust_audio_timeout_invalid_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PODLY_RUST_AUDIO_TIMEOUT_SEC", "not-a-number")
    assert (
        rust_sidecar.rust_audio_timeout_sec()
        == rust_sidecar.DEFAULT_RUST_AUDIO_TIMEOUT_SEC
    )


def test_bleep_audio_uses_long_audio_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Regression: the bleep sidecar command must run with the generous audio
    # timeout, not the 300s default -- long bleep jobs were timing out and
    # falling back to Python.
    monkeypatch.setenv("PODLY_RUST_AUDIO_ENABLED", "true")
    monkeypatch.setenv("PODLY_RUST_AUDIO_TIMEOUT_SEC", "1800")
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], timeout_sec: int = 300) -> dict[str, Any]:
        captured["timeout_sec"] = timeout_sec
        return {"ok": True}

    monkeypatch.setattr(rust_sidecar, "run_podly_tools", fake_run)

    ok = rust_sidecar.try_bleep_audio(
        windows_ms=[(1000, 2000)],
        input_path=tmp_path / "in.mp3",
        output_path=tmp_path / "out.mp3",
        beep_frequency_hz=1000,
        beep_volume=0.5,
        duck_volume=0.0,
        encoding="cbr",
        cbr_bitrate_bps=128000,
    )

    assert ok is True
    assert captured["timeout_sec"] == 1800


def test_try_render_feed_xml_uses_rust_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[bytes]:
        calls.append(command[1:])
        assert "text" not in kwargs
        return subprocess.CompletedProcess(
            command, 0, stdout=b'<?xml version="1.0"?><rss></rss>\n', stderr=b""
        )

    monkeypatch.setenv("PODLY_RUST_FEED_XML_ENABLED", "true")
    monkeypatch.setattr(rust_sidecar.subprocess, "run", fake_run)

    result = rust_sidecar.try_render_feed_xml(
        db_path=Path("/tmp/db.sqlite"),
        feed_id=12,
        base_url="https://podly.example",
        include_unprocessed=False,
        feed_token="token",
        feed_secret="secret",
    )

    assert result == b'<?xml version="1.0"?><rss></rss>\n'
    assert calls == [
        [
            "feed",
            "render",
            "--db",
            "/tmp/db.sqlite",
            "--feed-id",
            "12",
            "--base-url",
            "https://podly.example",
            "--feed-token",
            "token",
            "--feed-secret",
            "secret",
            "--raw-xml",
        ]
    ]


def test_try_render_feed_xml_passes_include_unprocessed_as_bare_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[bytes]:
        calls.append(command[1:])
        assert "text" not in kwargs
        return subprocess.CompletedProcess(
            command, 0, stdout=b'<?xml version="1.0"?><rss></rss>\n', stderr=b""
        )

    monkeypatch.setenv("PODLY_RUST_FEED_XML_ENABLED", "true")
    monkeypatch.setattr(rust_sidecar.subprocess, "run", fake_run)

    rust_sidecar.try_render_feed_xml(
        db_path=Path("/tmp/db.sqlite"),
        feed_id=12,
        base_url="https://podly.example",
        include_unprocessed=True,
        feed_token=None,
        feed_secret=None,
    )

    assert "--include-unprocessed" in calls[0]
    idx = calls[0].index("--include-unprocessed")
    assert calls[0][idx + 1 : idx + 2] != ["true"]
    assert calls[0][idx + 1 : idx + 2] != ["false"]


def test_try_render_aggregate_feed_xml_passes_require_auth_as_bare_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[bytes]:
        calls.append(command[1:])
        assert "text" not in kwargs
        return subprocess.CompletedProcess(
            command, 0, stdout=b'<?xml version="1.0"?><rss></rss>\n', stderr=b""
        )

    monkeypatch.setenv("PODLY_RUST_FEED_XML_ENABLED", "true")
    monkeypatch.setattr(rust_sidecar.subprocess, "run", fake_run)

    result = rust_sidecar.try_render_aggregate_feed_xml(
        db_path=Path("/tmp/db.sqlite"),
        user_id=7,
        base_url="https://podly.example",
        require_auth=True,
        limit_per_feed=25,
        feed_token=None,
        feed_secret=None,
    )

    assert result == b'<?xml version="1.0"?><rss></rss>\n'
    assert calls == [
        [
            "feed",
            "render-aggregate",
            "--db",
            "/tmp/db.sqlite",
            "--user-id",
            "7",
            "--base-url",
            "https://podly.example",
            "--limit-per-feed",
            "25",
            "--require-auth",
            "--raw-xml",
        ]
    ]


def test_try_render_aggregate_feed_xml_omits_require_auth_when_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[bytes]:
        calls.append(command[1:])
        assert "text" not in kwargs
        return subprocess.CompletedProcess(
            command, 0, stdout=b'<?xml version="1.0"?><rss></rss>\n', stderr=b""
        )

    monkeypatch.setenv("PODLY_RUST_FEED_XML_ENABLED", "true")
    monkeypatch.setattr(rust_sidecar.subprocess, "run", fake_run)

    rust_sidecar.try_render_aggregate_feed_xml(
        db_path=Path("/tmp/db.sqlite"),
        user_id=7,
        base_url="https://podly.example",
        require_auth=False,
        limit_per_feed=25,
        feed_token=None,
        feed_secret=None,
    )

    assert "--require-auth" not in calls[0]


@pytest.mark.parametrize(
    "failure",
    [
        OSError("missing binary"),
        subprocess.TimeoutExpired("podly_tools", 300),
        subprocess.CompletedProcess([], 2, stdout=b"", stderr=b"old binary"),
        subprocess.CompletedProcess(
            [], 0, stdout=b'{"xml":"old protocol"}', stderr=b""
        ),
        subprocess.CompletedProcess([], 0, stdout=b"<?xml truncated", stderr=b""),
    ],
)
def test_raw_rss_failures_notify_and_fall_back(monkeypatch, failure, caplog):
    notices = []

    def run(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(rust_sidecar.subprocess, "run", run)
    monkeypatch.setattr(
        rust_sidecar, "_notify_rust_fallback", lambda *args: notices.append(args)
    )
    assert rust_sidecar._try_feed_xml_command(["feed", "render"], "feed render") is None
    assert len(notices) == 1
    assert notices[0][0] == "feed render"
    assert "falling back to Python" in caplog.text


def test_raw_rss_returns_original_bytes_without_json_roundtrip(monkeypatch):
    xml = '<?xml version="1.0"?><rss><title>Café &amp; tea</title></rss>\n'.encode()
    monkeypatch.setattr(
        rust_sidecar.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 0, stdout=xml, stderr=b""
        ),
    )
    assert rust_sidecar._try_feed_xml_command(["feed", "render"], "feed render") is xml


def test_try_render_post_stats_returns_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PODLY_RUST_STATS_ENABLED", "false")

    assert (
        rust_sidecar.try_render_post_stats(
            db_path=Path("/tmp/db.sqlite"),
            post_guid="post-guid",
            min_confidence=0.8,
            min_ad_segment_separation_seconds=30.0,
            enable_boundary_refinement=True,
            stats_debug=False,
            log_path=Path("/tmp/app.log"),
            in_root=Path("/tmp/in"),
            srv_root=Path("/tmp/srv"),
        )
        is None
    )


def test_try_render_post_stats_returns_valid_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fake_run(args: list[str]) -> dict[str, object]:
        calls.append(args)
        return {"stats": {"post": {"guid": "post-guid"}}}

    monkeypatch.setenv("PODLY_RUST_STATS_ENABLED", "true")
    monkeypatch.setattr(rust_sidecar, "run_podly_tools", fake_run)

    result = rust_sidecar.try_render_post_stats(
        db_path=Path("/tmp/db.sqlite"),
        post_guid="post-guid/with/slash",
        min_confidence=0.8,
        min_ad_segment_separation_seconds=30.0,
        enable_boundary_refinement=True,
        stats_debug=False,
        log_path=Path("/tmp/app.log"),
        in_root=Path("/tmp/in"),
        srv_root=Path("/tmp/srv"),
    )

    assert result == {"post": {"guid": "post-guid"}}
    assert calls == [
        [
            "stats",
            "render",
            "--db",
            "/tmp/db.sqlite",
            "--post-guid",
            "post-guid/with/slash",
            "--min-confidence",
            "0.8",
            "--min-ad-segment-separation-seconds",
            "30.0",
            "--enable-boundary-refinement",
            "true",
            "--stats-debug",
            "false",
            "--log-path",
            "/tmp/app.log",
            "--in-root",
            "/tmp/in",
            "--srv-root",
            "/tmp/srv",
        ]
    ]


def test_try_render_post_stats_falls_back_on_bad_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PODLY_RUST_STATS_ENABLED", "true")
    monkeypatch.setattr(rust_sidecar, "run_podly_tools", lambda args: {"stats": []})

    assert (
        rust_sidecar.try_render_post_stats(
            db_path=Path("/tmp/db.sqlite"),
            post_guid="post-guid",
            min_confidence=0.8,
            min_ad_segment_separation_seconds=30.0,
            enable_boundary_refinement=True,
            stats_debug=False,
            log_path=Path("/tmp/app.log"),
            in_root=Path("/tmp/in"),
            srv_root=Path("/tmp/srv"),
        )
        is None
    )


def test_try_render_post_stats_falls_back_on_process_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(args: list[str]) -> dict[str, object]:
        raise RustSidecarError("boom")

    monkeypatch.setenv("PODLY_RUST_STATS_ENABLED", "true")
    monkeypatch.setattr(rust_sidecar, "run_podly_tools", fake_run)

    assert (
        rust_sidecar.try_render_post_stats(
            db_path=Path("/tmp/db.sqlite"),
            post_guid="post-guid",
            min_confidence=0.8,
            min_ad_segment_separation_seconds=30.0,
            enable_boundary_refinement=True,
            stats_debug=False,
            log_path=Path("/tmp/app.log"),
            in_root=Path("/tmp/in"),
            srv_root=Path("/tmp/srv"),
        )
        is None
    )


def test_try_list_active_jobs_returns_valid_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fake_run(args: list[str]) -> dict[str, object]:
        calls.append(args)
        return {"jobs": [{"job_id": "job-1"}]}

    monkeypatch.setenv("PODLY_RUST_JOBS_ENABLED", "true")
    monkeypatch.setattr(rust_sidecar, "run_podly_tools", fake_run)

    assert rust_sidecar.try_list_active_jobs(
        db_path=Path("/tmp/db.sqlite"),
        limit=12,
    ) == [{"job_id": "job-1"}]
    assert calls == [
        [
            "jobs",
            "active",
            "--db",
            "/tmp/db.sqlite",
            "--limit",
            "12",
        ]
    ]


def test_try_list_all_jobs_falls_back_on_bad_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PODLY_RUST_JOBS_ENABLED", "true")
    monkeypatch.setattr(rust_sidecar, "run_podly_tools", lambda args: {"jobs": {}})

    assert (
        rust_sidecar.try_list_all_jobs(
            db_path=Path("/tmp/db.sqlite"),
            limit=12,
        )
        is None
    )


def test_try_plan_feed_refresh_returns_valid_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str]) -> dict[str, object]:
        calls.append(args)
        feed_xml_path = Path(args[args.index("--feed-xml") + 1])
        assert feed_xml_path.read_text() == "<rss />"
        return {
            "updates": {"image_url": "https://example.com/feed.png"},
            "new_posts": [{"guid": "new-guid"}],
            "existing_post_updates": [{"post_id": 1, "title": "Updated"}],
        }

    monkeypatch.setenv("PODLY_RUST_FEED_REFRESH_ENABLED", "true")
    monkeypatch.setattr(rust_sidecar, "run_podly_tools", fake_run)

    assert rust_sidecar.try_plan_feed_refresh(
        db_path=Path("/tmp/db.sqlite"),
        feed_id=12,
        feed_xml="<rss />",
        auto_whitelist_new_posts=True,
    ) == {
        "updates": {"image_url": "https://example.com/feed.png"},
        "new_posts": [{"guid": "new-guid"}],
        "existing_post_updates": [{"post_id": 1, "title": "Updated"}],
    }
    assert calls
    assert calls[0][:6] == [
        "feed",
        "refresh-plan",
        "--db",
        "/tmp/db.sqlite",
        "--feed-id",
        "12",
    ]
    assert calls[0][-2:] == ["--auto-whitelist-new-posts", "true"]


def test_try_plan_feed_refresh_falls_back_on_bad_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PODLY_RUST_FEED_REFRESH_ENABLED", "true")
    monkeypatch.setattr(rust_sidecar, "run_podly_tools", lambda args: {"updates": []})

    assert (
        rust_sidecar.try_plan_feed_refresh(
            db_path=Path("/tmp/db.sqlite"),
            feed_id=12,
            feed_xml=b"<rss />",
            auto_whitelist_new_posts=False,
        )
        is None
    )


def test_try_read_chapters_returns_valid_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PODLY_RUST_CHAPTERS_ENABLED", "true")
    monkeypatch.setattr(
        rust_sidecar,
        "run_podly_tools",
        lambda args: {
            "chapters": [
                {
                    "element_id": "chp0",
                    "title": "Intro",
                    "start_time_ms": 0,
                    "end_time_ms": 1000,
                }
            ]
        },
    )

    assert rust_sidecar.try_read_chapters(Path("/tmp/audio.mp3")) == [
        {
            "element_id": "chp0",
            "title": "Intro",
            "start_time_ms": 0,
            "end_time_ms": 1000,
        }
    ]


def test_try_read_chapters_falls_back_on_bad_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PODLY_RUST_CHAPTERS_ENABLED", "true")
    monkeypatch.setattr(
        rust_sidecar,
        "run_podly_tools",
        lambda args: {"chapters": [{"element_id": "chp0"}]},
    )

    assert rust_sidecar.try_read_chapters(Path("/tmp/audio.mp3")) is None


def test_try_detect_chapter_ads_returns_valid_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "ad_segments": [[1.0, 2.0]],
        "chapters_to_keep": [
            {
                "element_id": "chp0",
                "title": "Intro",
                "start_time_ms": 0,
                "end_time_ms": 1000,
            }
        ],
        "chapters_to_remove": [
            {
                "element_id": "chp1",
                "title": "Sponsor",
                "start_time_ms": 1000,
                "end_time_ms": 2000,
            }
        ],
    }
    monkeypatch.setenv("PODLY_RUST_CHAPTERS_ENABLED", "true")
    monkeypatch.setattr(rust_sidecar, "run_podly_tools", lambda args: payload)

    assert (
        rust_sidecar.try_detect_chapter_ads(Path("/tmp/audio.mp3"), "sponsor")
        == payload
    )


def test_try_detect_chapter_ads_falls_back_on_bad_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PODLY_RUST_CHAPTERS_ENABLED", "true")
    monkeypatch.setattr(
        rust_sidecar,
        "run_podly_tools",
        lambda args: {"ad_segments": ["not-a-window"]},
    )

    assert (
        rust_sidecar.try_detect_chapter_ads(Path("/tmp/audio.mp3"), "sponsor") is None
    )


def test_admin_costs_payload_without_audio_hours_falls_back() -> None:
    payload: dict[str, Any] = {
        "year": 2026,
        "month": 5,
        "total_cost": 0.0,
        "total_llm_cost": 0.0,
        "total_whisper_cost": 0.0,
        "total_ina_cost": 0.0,
        "total_audio_hours": 0.0,
        "users": [],
        "feeds": [],
    }
    assert rust_sidecar._is_valid_admin_costs_payload(payload)
    del payload["total_audio_hours"]
    assert not rust_sidecar._is_valid_admin_costs_payload(payload)
