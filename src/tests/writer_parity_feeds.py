"""Differential parity cases for all currently registered feed actions.

Every case uses synthetic fixture data and independent Python/Rust database
clones. Only generated IDs, secrets, hashes derived from generated secrets, and
clock fields are normalized, with each nondeterministic value checked against
its semantic contract.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from flask import Flask

from app.extensions import db
from app.writer.protocol import WriteCommand, WriteCommandType
from tests.writer_parity_fixtures import WriterParityPair

WRITER_DIFFERENTIAL_CASES = (
    {
        "case_id": "feed_refresh_existing_post_and_metadata",
        "operation": "action",
        "action": "refresh_feed",
        "owner": "refresh_feed",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "feed_refresh",
        "expect_success": True,
    },
    {
        "case_id": "feed_refresh_empty_payload_noop",
        "operation": "action",
        "action": "refresh_feed",
        "owner": "refresh_feed",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "refresh_noop",
        "expect_success": True,
    },
    {
        "case_id": "feed_add_with_post_defaults",
        "operation": "action",
        "action": "add_feed",
        "owner": "add_feed",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "add_feed",
        "expect_success": True,
    },
    {
        "case_id": "feed_update_settings",
        "operation": "action",
        "action": "update_feed_settings",
        "owner": "update_feed_settings",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "exact",
        "expect_success": True,
    },
    {
        "case_id": "feed_update_settings_empty_payload_noop",
        "operation": "action",
        "action": "update_feed_settings",
        "owner": "update_feed_settings",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "feed_settings_noop",
        "expect_success": True,
    },
    {
        "case_id": "feed_increment_download_count",
        "operation": "action",
        "action": "increment_download_count",
        "owner": "increment_download_count",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "exact",
        "expect_success": True,
    },
    {
        "case_id": "feed_whitelist_post",
        "operation": "action",
        "action": "whitelist_post",
        "owner": "whitelist_post",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "exact",
        "expect_success": True,
    },
    {
        "case_id": "feed_whitelist_missing_post_noop",
        "operation": "action",
        "action": "whitelist_post",
        "owner": "whitelist_post",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "whitelist_missing_post",
        "expect_success": True,
    },
    {
        "case_id": "feed_ensure_membership",
        "operation": "action",
        "action": "ensure_user_feed_membership",
        "owner": "ensure_user_feed_membership",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "membership",
        "expect_success": True,
    },
    {
        "case_id": "feed_remove_membership",
        "operation": "action",
        "action": "remove_user_feed_membership",
        "owner": "remove_user_feed_membership",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "exact",
        "expect_success": True,
    },
    {
        "case_id": "feed_whitelist_latest_post",
        "operation": "action",
        "action": "whitelist_latest_post_for_feed",
        "owner": "whitelist_latest_post_for_feed",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "exact",
        "expect_success": True,
    },
    {
        "case_id": "feed_toggle_whitelist_all",
        "operation": "action",
        "action": "toggle_whitelist_all_for_feed",
        "owner": "toggle_whitelist_all_for_feed",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "exact",
        "expect_success": True,
    },
    {
        "case_id": "feed_create_dev_test_feed",
        "operation": "action",
        "action": "create_dev_test_feed",
        "owner": "create_dev_test_feed",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "dev_feed",
        "expect_success": True,
    },
    {
        "case_id": "feed_delete_cascade",
        "operation": "action",
        "action": "delete_feed_cascade",
        "owner": "delete_feed_cascade",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "exact",
        "expect_success": True,
    },
    {
        "case_id": "feed_create_access_token",
        "operation": "action",
        "action": "create_feed_access_token",
        "owner": "create_feed_access_token",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "token_create",
        "expect_success": True,
    },
    {
        "case_id": "feed_touch_access_token",
        "operation": "action",
        "action": "touch_feed_access_token",
        "owner": "touch_feed_access_token",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "token_touch",
        "expect_success": True,
    },
    {
        "case_id": "feed_refresh_merge_new_posts_and_foreign_owner",
        "operation": "action",
        "action": "refresh_feed",
        "owner": "refresh_feed",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "feed_refresh",
        "expect_success": True,
    },
    {
        "case_id": "feed_refresh_partial_failure_rolls_back",
        "operation": "action",
        "action": "refresh_feed",
        "owner": "refresh_feed",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "rollback",
        "expect_success": False,
    },
    {
        "case_id": "feed_add_partial_failure_rolls_back",
        "operation": "action",
        "action": "add_feed",
        "owner": "add_feed",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "rollback",
        "expect_success": False,
    },
    {
        "case_id": "feed_delete_cascade_partial_failure_rolls_back",
        "operation": "action",
        "action": "delete_feed_cascade",
        "owner": "delete_feed_cascade",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "rollback",
        "expect_success": False,
    },
    {
        "case_id": "feed_delete_missing_noop",
        "operation": "action",
        "action": "delete_feed_cascade",
        "owner": "delete_feed_cascade",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "exact",
        "expect_success": True,
    },
    {
        "case_id": "feed_ensure_membership_repeat_existing",
        "operation": "action",
        "action": "ensure_user_feed_membership",
        "owner": "ensure_user_feed_membership",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "membership",
        "expect_success": True,
        "repeat_count": 2,
    },
    {
        "case_id": "feed_remove_membership_missing_noop",
        "operation": "action",
        "action": "remove_user_feed_membership",
        "owner": "remove_user_feed_membership",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "exact",
        "expect_success": True,
        "repeat_count": 2,
    },
    {
        "case_id": "feed_whitelist_latest_already_whitelisted_noop",
        "operation": "action",
        "action": "whitelist_latest_post_for_feed",
        "owner": "whitelist_latest_post_for_feed",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "exact",
        "expect_success": True,
        "repeat_count": 2,
    },
    {
        "case_id": "feed_whitelist_latest_empty_feed_noop",
        "operation": "action",
        "action": "whitelist_latest_post_for_feed",
        "owner": "whitelist_latest_post_for_feed",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "exact",
        "expect_success": True,
    },
    {
        "case_id": "feed_increment_missing_post_noop",
        "operation": "action",
        "action": "increment_download_count",
        "owner": "increment_download_count",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "exact",
        "expect_success": True,
    },
    {
        "case_id": "feed_create_access_token_reuse_active",
        "operation": "action",
        "action": "create_feed_access_token",
        "owner": "create_feed_access_token",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "token_reuse",
        "expect_success": True,
        "repeat_count": 2,
    },
    {
        "case_id": "feed_create_dev_test_feed_repeat_existing",
        "operation": "action",
        "action": "create_dev_test_feed",
        "owner": "create_dev_test_feed",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "dev_feed",
        "expect_success": True,
        "repeat_count": 2,
    },
    {
        "case_id": "feed_create_access_token_rotates_legacy_secret",
        "operation": "action",
        "action": "create_feed_access_token",
        "owner": "create_feed_access_token",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "token_rotation",
        "expect_success": True,
    },
    {
        "case_id": "feed_create_access_token_ignores_revoked",
        "operation": "action",
        "action": "create_feed_access_token",
        "owner": "create_feed_access_token",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "token_rotation",
        "expect_success": True,
    },
    {
        "case_id": "feed_touch_legacy_token_records_secret",
        "operation": "action",
        "action": "touch_feed_access_token",
        "owner": "touch_feed_access_token",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "token_touch_legacy",
        "expect_success": True,
    },
    {
        "case_id": "feed_touch_revoked_token_noop",
        "operation": "action",
        "action": "touch_feed_access_token",
        "owner": "touch_feed_access_token",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "exact",
        "expect_success": True,
    },
    {
        "case_id": "feed_touch_missing_token_noop",
        "operation": "action",
        "action": "touch_feed_access_token",
        "owner": "touch_feed_access_token",
        "owner_group": "feed",
        "source": "src/app/writer/actions/feeds.py",
        "semantics": "exact",
        "expect_success": True,
    },
)


def _params(case_id: str, pair: WriterParityPair) -> dict[str, Any]:
    user_id = pair.manifest.user_id
    feed_id, second_feed_id = pair.manifest.feed_ids
    post_id, second_post_id = pair.manifest.post_ids
    token_id = pair.manifest.token_ids[0]
    token_ids = pair.manifest.token_ids
    factories: dict[str, Callable[[], dict[str, Any]]] = {
        "feed_refresh_existing_post_and_metadata": lambda: {
            "feed_id": feed_id,
            "updates": {"description": "Updated by differential parity"},
            "new_posts": [],
            "existing_post_updates": [
                {
                    "post_id": post_id,
                    "title": "Refreshed parity episode",
                    "duration": 3600.25,
                }
            ],
        },
        "feed_refresh_empty_payload_noop": lambda: {
            "feed_id": feed_id,
            "updates": {},
            "new_posts": [],
            "existing_post_updates": [],
        },
        "feed_add_with_post_defaults": lambda: {
            "feed": {
                "title": "Differentially added feed",
                "rss_url": "https://added.example.invalid/parity.xml",
                "description": "Synthetic add-feed parity fixture",
                "author": "Parity Test",
            },
            "posts": [
                {
                    "guid": "parity-added-feed-post",
                    "title": "Added synthetic episode",
                    "download_url": "https://added.example.invalid/episode.mp3",
                    "release_date": "2026-01-03T04:05:06",
                    "duration": 123.5,
                    "whitelisted": False,
                }
            ],
        },
        "feed_update_settings": lambda: {
            "feed_id": second_feed_id,
            "auto_whitelist_new_episodes_override": True,
        },
        "feed_update_settings_empty_payload_noop": lambda: {
            "feed_id": feed_id,
        },
        "feed_increment_download_count": lambda: {"post_id": post_id},
        "feed_whitelist_post": lambda: {"post_id": second_post_id},
        "feed_whitelist_missing_post_noop": lambda: {
            "post_id": pair.manifest.missing_post_id,
        },
        "feed_ensure_membership": lambda: {
            "feed_id": second_feed_id,
            "user_id": user_id,
        },
        "feed_remove_membership": lambda: {"feed_id": feed_id, "user_id": user_id},
        "feed_whitelist_latest_post": lambda: {"feed_id": second_feed_id},
        "feed_toggle_whitelist_all": lambda: {
            "feed_id": feed_id,
            "new_status": False,
        },
        "feed_create_dev_test_feed": lambda: {
            "rss_url": "https://dev-parity.example.invalid/feed.xml",
            "title": "Synthetic developer feed",
            "description": "Small differential fixture",
            "post_count": 2,
            "guid_prefix": "parity-dev",
            "download_url_prefix": "https://dev-parity.example.invalid/audio",
        },
        "feed_create_dev_test_feed_repeat_existing": lambda: {
            "rss_url": "https://dev-repeat-parity.example.invalid/feed.xml",
            "title": "Synthetic developer feed repeated",
            "post_count": 2,
            "guid_prefix": "parity-dev-repeat",
            "download_url_prefix": "https://dev-repeat-parity.example.invalid/audio",
        },
        "feed_delete_cascade": lambda: {"feed_id": feed_id},
        "feed_create_access_token": lambda: {
            "user_id": user_id,
            "feed_id": second_feed_id,
        },
        "feed_touch_access_token": lambda: {
            "token_id": token_id,
            "secret": "synthetic-parity-touch-secret",
        },
        "feed_refresh_merge_new_posts_and_foreign_owner": lambda: {
            "feed_id": feed_id,
            "updates": {"description": "Merged feed refresh"},
            "new_posts": [
                {
                    "feed_id": feed_id,
                    "guid": "parity-refresh-whitelisted",
                    "title": "Whitelisted new episode",
                    "download_url": "https://one.example.invalid/new-whitelisted.mp3",
                    "release_date": "2026-01-03T04:05:06",
                    "whitelisted": True,
                },
                {
                    "feed_id": feed_id,
                    "guid": "parity-refresh-unlisted",
                    "title": "Unlisted new episode",
                    "download_url": "https://one.example.invalid/new-unlisted.mp3",
                    "release_date": "2026-01-03T04:05:07",
                    "whitelisted": False,
                },
            ],
            "existing_post_updates": [
                {"post_id": post_id, "title": "Merged existing episode"},
                {"post_id": second_post_id, "title": "Must not cross feed ownership"},
            ],
        },
        "feed_refresh_partial_failure_rolls_back": lambda: {
            "feed_id": feed_id,
            "updates": {"description": "Must roll back"},
            "new_posts": [
                {
                    "feed_id": feed_id,
                    "guid": "parity-refresh-before-failure",
                    "title": "Would be inserted before failure",
                    "download_url": "https://one.example.invalid/before-failure.mp3",
                    "release_date": "2026-01-03T04:05:06",
                    "whitelisted": True,
                },
                {
                    "feed_id": feed_id,
                    "guid": pair.manifest.post_guids[0],
                    "title": "Duplicate GUID must abort the command",
                    "download_url": "https://one.example.invalid/duplicate.mp3",
                },
            ],
        },
        "feed_add_partial_failure_rolls_back": lambda: {
            "feed": {
                "title": "Feed insert must roll back",
                "rss_url": "https://rollback.example.invalid/feed.xml",
            },
            "posts": [
                {
                    "guid": "parity-add-before-failure",
                    "title": "Would be inserted before failure",
                    "download_url": "https://rollback.example.invalid/first.mp3",
                    "whitelisted": True,
                },
                {
                    "guid": pair.manifest.post_guids[0],
                    "title": "Duplicate GUID must abort the command",
                    "download_url": "https://rollback.example.invalid/duplicate.mp3",
                },
            ],
        },
        "feed_delete_cascade_partial_failure_rolls_back": lambda: {"feed_id": feed_id},
        "feed_delete_missing_noop": lambda: {"feed_id": pair.manifest.missing_post_id},
        "feed_ensure_membership_repeat_existing": lambda: {
            "feed_id": second_feed_id,
            "user_id": user_id,
        },
        "feed_remove_membership_missing_noop": lambda: {
            "feed_id": second_feed_id,
            "user_id": 999_101,
        },
        "feed_whitelist_latest_already_whitelisted_noop": lambda: {
            "feed_id": feed_id,
        },
        "feed_whitelist_latest_empty_feed_noop": lambda: {
            "feed_id": pair.manifest.missing_post_id,
        },
        "feed_increment_missing_post_noop": lambda: {
            "post_id": pair.manifest.missing_post_id,
        },
        "feed_create_access_token_reuse_active": lambda: {
            "user_id": user_id,
            "feed_id": feed_id,
        },
        "feed_create_access_token_rotates_legacy_secret": lambda: {
            "user_id": user_id,
            "feed_id": feed_id,
        },
        "feed_create_access_token_ignores_revoked": lambda: {
            "user_id": user_id,
            "feed_id": second_feed_id,
        },
        "feed_touch_legacy_token_records_secret": lambda: {
            "token_id": token_ids[1],
            "secret": "synthetic-legacy-token-secret",
        },
        "feed_touch_revoked_token_noop": lambda: {
            "token_id": token_ids[2],
            "secret": "synthetic-revoked-token-secret",
        },
        "feed_touch_missing_token_noop": lambda: {
            "token_id": pair.manifest.missing_token_id,
            "secret": "synthetic-missing-token-secret",
        },
    }
    try:
        return factories[case_id]()
    except KeyError:
        raise AssertionError(
            f"No feed action parameters defined for {case_id}"
        ) from None


def prepare_writer_feed_case(case_id: str, pair: WriterParityPair) -> None:
    """Set up synthetic preconditions identically in the two isolated clones."""
    if case_id not in {
        "feed_delete_cascade_partial_failure_rolls_back",
        "feed_add_partial_failure_rolls_back",
        "feed_refresh_partial_failure_rolls_back",
        "feed_ensure_membership_repeat_existing",
        "feed_create_access_token_rotates_legacy_secret",
        "feed_touch_legacy_token_records_secret",
    }:
        return

    for backend in (pair.python, pair.rust):
        with sqlite3.connect(backend.db_path) as connection:
            if case_id == "feed_delete_cascade_partial_failure_rolls_back":
                connection.execute(
                    "CREATE TRIGGER parity_abort_token_delete "
                    "BEFORE DELETE ON feed_access_token "
                    "WHEN OLD.feed_id=201 "
                    "BEGIN SELECT RAISE(ABORT, 'synthetic cascade failure'); END"
                )
            elif case_id in {
                "feed_add_partial_failure_rolls_back",
                "feed_refresh_partial_failure_rolls_back",
            }:
                connection.execute(
                    "CREATE TRIGGER parity_abort_second_post "
                    "BEFORE INSERT ON post "
                    "WHEN NEW.guid IN (SELECT guid FROM post WHERE id=301) "
                    "BEGIN SELECT RAISE(ABORT, 'synthetic post insert failure'); END"
                )
            elif case_id == "feed_ensure_membership_repeat_existing":
                connection.execute(
                    "INSERT INTO feed_supporter(feed_id,user_id,created_at) "
                    "VALUES (202,101,'2026-01-02 03:04:05')"
                )
            elif case_id == "feed_create_access_token_rotates_legacy_secret":
                connection.execute(
                    "UPDATE feed_access_token SET token_secret=NULL WHERE token_id=?",
                    (pair.manifest.token_ids[0],),
                )
            elif case_id == "feed_touch_legacy_token_records_secret":
                connection.execute(
                    "UPDATE feed_access_token SET token_secret=NULL WHERE token_id=?",
                    (pair.manifest.token_ids[1],),
                )


def build_writer_feed_case(
    case_id: str, pair: WriterParityPair
) -> tuple[dict[str, Any], WriteCommand]:
    case = next(
        record for record in WRITER_DIFFERENTIAL_CASES if record["case_id"] == case_id
    )
    params = _params(case_id, pair)
    rust_operation = {"operation": "action", "action": case["action"], "params": params}
    python_command = WriteCommand(
        id=f"parity-{case_id}",
        type=WriteCommandType.ACTION,
        model=None,
        data={"action": case["action"], "params": params},
    )
    return rust_operation, python_command


def _rows_by_table(backend: Any) -> dict[str, list[dict[str, Any]]]:
    with sqlite3.connect(backend.db_path) as connection:
        connection.row_factory = sqlite3.Row
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables_as_dicts = {
            table: [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
            for table in tables
        }
    for rows in tables_as_dicts.values():
        for row in rows:
            for field in ("unprocessed_audio_path", "processed_audio_path"):
                value = row.get(field)
                if isinstance(value, str):
                    try:
                        row[field] = str(Path(value).relative_to(backend.instance_dir))
                    except ValueError:
                        pass
    return tables_as_dicts


def _replace_stage_history_timestamps(value: Any) -> Any:
    if isinstance(value, list):
        return [_replace_stage_history_timestamps(item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                "<timestamp>"
                if isinstance(item, str)
                and key in {"started_at", "completed_at", "timestamp"}
                else _replace_stage_history_timestamps(item)
            )
            for key, item in value.items()
        }
    return value


def _assert_db_equal(  # noqa: PLR0912 - normalization is parameterized by table/row semantics
    observation: dict[str, Any],
    *,
    ignored_fields: dict[str, set[str]] | None = None,
    ignored_row_predicates: dict[str, Callable[[dict[str, Any]], bool]] | None = None,
    generated_job_prefix: str | None = None,
    generated_token_ids: set[str] | None = None,
) -> None:
    pair: WriterParityPair = observation["pair"]
    left = _rows_by_table(pair.python)
    right = _rows_by_table(pair.rust)
    ignored_fields = ignored_fields or {}
    ignored_row_predicates = ignored_row_predicates or {}
    for rows in (left, right):
        for table, fields in ignored_fields.items():
            for row in rows.get(table, []):
                predicate = ignored_row_predicates.get(table)
                if predicate is not None and not predicate(row):
                    continue
                if table == "feed_access_token" and generated_token_ids is not None:
                    if row.get("token_id") not in generated_token_ids:
                        continue
                for field in fields:
                    if field in row:
                        row[field] = f"<nondeterministic:{table}.{field}>"
        if generated_job_prefix:
            for row in rows.get("processing_job", []):
                if not str(row.get("post_guid", "")).startswith(generated_job_prefix):
                    continue
                uuid.UUID(str(row["id"]))
                for field in ("id", "created_at", "started_at", "completed_at"):
                    if field in row:
                        row[field] = f"<nondeterministic:processing_job.{field}>"
                history = row.get("stage_history")
                if isinstance(history, str):
                    try:
                        row["stage_history"] = json.dumps(
                            _replace_stage_history_timestamps(json.loads(history)),
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    except json.JSONDecodeError as error:
                        raise AssertionError(
                            "processing-job stage_history is invalid JSON"
                        ) from error
    if left != right:
        differences = []
        differing_table_count = 0
        for table in sorted(left.keys() | right.keys()):
            python_rows = left.get(table, [])
            rust_rows = right.get(table, [])
            if python_rows == rust_rows:
                continue
            differing_table_count += 1

            row_differences = []
            for index, (python_row, rust_row) in enumerate(
                zip(python_rows, rust_rows, strict=False)
            ):
                if python_row == rust_row:
                    continue
                if isinstance(python_row, dict) and isinstance(rust_row, dict):
                    fields = sorted(
                        field
                        for field in python_row.keys() | rust_row.keys()
                        if python_row.get(field) != rust_row.get(field)
                    )
                    row_differences.append(f"row[{index}] fields={fields[:10]}")
                else:
                    row_differences.append(f"row[{index}] differs")
                if len(row_differences) == 3:
                    break

            if len(python_rows) != len(rust_rows):
                row_differences.append(
                    f"remaining rows: Python={len(python_rows)}, Rust={len(rust_rows)}"
                )
            details = ", ".join(row_differences) or "row ordering differs"
            differences.append(f"{table}: {details}")
            if len(differences) == 8:
                break

        suffix = "; ".join(differences)
        if differing_table_count > len(differences):
            suffix += "; additional differing tables omitted"
        raise AssertionError(f"feed writer persisted table rows differ: {suffix}")


def _assert_timestamp(value: Any) -> None:
    assert isinstance(value, str) and value
    datetime.fromisoformat(value.replace(" ", "T", 1))


def _assert_python_auth_accepts_writer_token(
    backend: Any, *, token_id: str, secret: str, user_id: int, feed_id: int
) -> None:
    """Exercise the real Python token verifier against each writer's DB clone."""
    with sqlite3.connect(backend.db_path) as connection:
        connection.execute("UPDATE users SET role='user' WHERE id=?", (user_id,))
        connection.execute(
            "DELETE FROM feed_supporter WHERE feed_id=? AND user_id=?",
            (feed_id, user_id),
        )

    app = Flask(f"feed-token-auth-parity-{backend.name}")
    app.config.update(
        SQLALCHEMY_DATABASE_URI=backend.database_uri,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    with (
        app.app_context(),
        patch("app.auth.feed_tokens.writer_client.action") as action,
    ):
        from app.auth.feed_tokens import authenticate_feed_token

        assert (
            authenticate_feed_token(
                token_id, "wrong-synthetic-secret", f"/feed/{feed_id}"
            )
            is None
        )
        assert authenticate_feed_token(token_id, secret, f"/feed/{feed_id - 1}") is None
        assert authenticate_feed_token(token_id, secret, f"/feed/{feed_id}") is None
        action.assert_not_called()

        with sqlite3.connect(backend.db_path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO feed_supporter(feed_id,user_id,created_at) "
                "VALUES (?,?,'2026-01-02 03:04:05')",
                (feed_id, user_id),
            )
        # End the first SQLAlchemy read transaction so it observes the fixture
        # membership written through the separate SQLite connection.
        db.session.rollback()
        accepted = authenticate_feed_token(token_id, secret, f"/feed/{feed_id}")
        assert accepted is not None
        assert accepted.user.id == user_id
        assert accepted.user.role == "user"
        assert accepted.feed_id == feed_id
        action.assert_called_once_with(
            "touch_feed_access_token",
            {"token_id": token_id, "secret": secret},
            wait=False,
        )

        action.reset_mock()
        aggregate_secret = "parity-token-secret-not-a-secret"
        aggregate = authenticate_feed_token(
            "aggregate-token-active", aggregate_secret, f"/feed/user/{user_id}"
        )
        assert aggregate is not None
        assert aggregate.user.id == user_id
        assert aggregate.feed_id is None
        action.assert_called_once_with(
            "touch_feed_access_token",
            {"token_id": "aggregate-token-active", "secret": aggregate_secret},
            wait=False,
        )
        action.reset_mock()
        assert (
            authenticate_feed_token(
                "aggregate-token-active",
                aggregate_secret,
                f"/feed/user/{user_id + 1}",
            )
            is None
        )
        action.assert_not_called()

        assert (
            authenticate_feed_token(
                "feed-token-revoked",
                aggregate_secret,
                f"/feed/{feed_id}",
            )
            is None
        )
        action.assert_not_called()
        db.session.remove()


def assert_writer_feed_parity(  # noqa: PLR0912 - action-specific semantic assertions
    observation: dict[str, Any],
) -> None:
    case = observation["case"]
    case_id = case["case_id"]
    if not case.get("expect_success", True):
        assert observation["python_success"] is False, observation["python_data"]
        assert observation["rust_success"] is False, observation["rust_result"]
        assert observation["python_error"]
        assert observation["rust_error"]
        assert observation["python_rows"] == observation["python_before"], (
            f"Python failure mutated state: {case_id}"
        )
        assert observation["rust_rows"] == observation["rust_before"], (
            f"Rust failure mutated state: {case_id}"
        )
        _assert_db_equal(observation)
        return

    assert observation["python_success"] is True, observation["python_error"]
    assert observation["rust_success"] is True, observation["rust_error"]

    python_results = observation.get("python_results", [observation["python_data"]])
    rust_results = observation.get("rust_results", [observation["rust_result"]])
    assert len(python_results) == len(rust_results)
    if case_id not in {
        "feed_create_access_token",
        "feed_create_access_token_rotates_legacy_secret",
        "feed_create_access_token_ignores_revoked",
    }:
        assert python_results == rust_results, {
            "case_id": case_id,
            "python_results": python_results,
            "rust_results": rust_results,
        }
    if (
        case.get("repeat_count", 1) > 1
        and case_id != "feed_create_dev_test_feed_repeat_existing"
    ):
        assert all(result == python_results[0] for result in python_results)
        assert all(result == rust_results[0] for result in rust_results)

    if case_id == "feed_refresh_existing_post_and_metadata":
        assert observation["python_data"] == observation["rust_result"]
        assert observation["python_data"] == {
            "feed_id": 201,
            "new_posts_count": 0,
            "updated_posts_count": 1,
        }
        _assert_db_equal(
            observation,
            ignored_fields={"feed": {"last_changed_at"}},
            ignored_row_predicates={"feed": lambda row: row["id"] == 201},
        )
        for backend in (observation["pair"].python, observation["pair"].rust):
            tables = _rows_by_table(backend)
            feed = next(row for row in tables["feed"] if row["id"] == 201)
            post = next(row for row in tables["post"] if row["id"] == 301)
            assert feed["description"] == "Updated by differential parity"
            assert post["title"] == "Refreshed parity episode"
            assert post["duration"] == 3600.25
            _assert_timestamp(feed["last_changed_at"])
        return

    if case_id == "feed_refresh_empty_payload_noop":
        assert (
            observation["python_data"]
            == observation["rust_result"]
            == {
                "feed_id": 201,
                "new_posts_count": 0,
                "updated_posts_count": 0,
            }
        )
        _assert_db_equal(observation)
        return

    if case_id == "feed_update_settings_empty_payload_noop":
        assert (
            observation["python_data"]
            == observation["rust_result"]
            == {
                "feed_id": 201,
            }
        )
        _assert_db_equal(observation)
        return

    if case_id == "feed_whitelist_missing_post_noop":
        assert (
            observation["python_data"]
            == observation["rust_result"]
            == {
                "post_id": observation["pair"].manifest.missing_post_id,
                "updated": 0,
            }
        )
        _assert_db_equal(observation)
        return

    if case_id == "feed_refresh_merge_new_posts_and_foreign_owner":
        assert observation["python_data"] == {
            "feed_id": 201,
            "new_posts_count": 2,
            "updated_posts_count": 1,
        }
        _assert_db_equal(
            observation,
            ignored_fields={"feed": {"last_changed_at"}},
            ignored_row_predicates={"feed": lambda row: row["id"] == 201},
            generated_job_prefix="parity-refresh-",
        )
        for backend in (observation["pair"].python, observation["pair"].rust):
            tables = _rows_by_table(backend)
            feed = next(row for row in tables["feed"] if row["id"] == 201)
            owned = next(row for row in tables["post"] if row["id"] == 301)
            foreign = next(row for row in tables["post"] if row["id"] == 302)
            new_whitelisted = next(
                row
                for row in tables["post"]
                if row["guid"] == "parity-refresh-whitelisted"
            )
            new_unlisted = next(
                row
                for row in tables["post"]
                if row["guid"] == "parity-refresh-unlisted"
            )
            assert feed["description"] == "Merged feed refresh"
            assert owned["title"] == "Merged existing episode"
            assert foreign["title"] == "Fractional duration episode"
            assert new_whitelisted["feed_id"] == 201
            assert new_unlisted["feed_id"] == 201
            assert bool(new_whitelisted["whitelisted"])
            assert not bool(new_unlisted["whitelisted"])
            jobs = [
                row
                for row in tables["processing_job"]
                if row["post_guid"] == "parity-refresh-whitelisted"
            ]
            assert len(jobs) == 1 and jobs[0]["status"] == "pending"
            assert not any(
                row["post_guid"] == "parity-refresh-unlisted"
                for row in tables["processing_job"]
            )
            _assert_timestamp(feed["last_changed_at"])
        return

    if case_id == "feed_add_with_post_defaults":
        assert observation["python_data"] == observation["rust_result"]
        _assert_db_equal(
            observation,
            ignored_fields={"feed": {"last_changed_at"}},
            ignored_row_predicates={
                "feed": lambda row: (
                    row["rss_url"] == "https://added.example.invalid/parity.xml"
                )
            },
        )
        for backend in (observation["pair"].python, observation["pair"].rust):
            tables = _rows_by_table(backend)
            created_feed = next(
                row
                for row in tables["feed"]
                if row["rss_url"] == "https://added.example.invalid/parity.xml"
            )
            added_post = next(
                row for row in tables["post"] if row["guid"] == "parity-added-feed-post"
            )
            assert created_feed["ad_detection_strategy"] == "llm"
            assert created_feed["enable_profanity_bleeping"] is False or (
                created_feed["enable_profanity_bleeping"] == 0
            )
            assert added_post["duration"] == 123.5
            assert added_post["whitelisted"] is False or added_post["whitelisted"] == 0
            _assert_timestamp(created_feed["last_changed_at"])
        return

    if case_id == "feed_create_access_token_reuse_active":
        expected = {
            "token_id": observation["pair"].manifest.token_ids[0],
            "secret": "parity-token-secret-not-a-secret",
        }
        assert all(result == expected for result in python_results)
        _assert_db_equal(observation)
        return

    if case_id in {
        "feed_create_access_token_rotates_legacy_secret",
        "feed_create_access_token_ignores_revoked",
    }:
        assert len(python_results) == 1
        for backend, result in (
            (observation["pair"].python, python_results[0]),
            (observation["pair"].rust, rust_results[0]),
        ):
            assert set(result) == {"token_id", "secret"}
            assert re.fullmatch(r"[A-Za-z0-9_-]{24}", result["secret"])
            tables = _rows_by_table(backend)
            token = next(
                row
                for row in tables["feed_access_token"]
                if row["token_id"] == result["token_id"]
            )
            assert token["token_secret"] == result["secret"]
            assert (
                token["token_hash"]
                == hashlib.sha256(result["secret"].encode()).hexdigest()
            )
            assert token["revoked"] in (False, 0)
            if case_id == "feed_create_access_token_rotates_legacy_secret":
                assert result["token_id"] == observation["pair"].manifest.token_ids[0]
            else:
                assert result["token_id"] != observation["pair"].manifest.token_ids[2]
                assert any(
                    row["token_id"] == observation["pair"].manifest.token_ids[2]
                    and row["revoked"] in (True, 1)
                    for row in tables["feed_access_token"]
                )
        _assert_db_equal(
            observation,
            ignored_fields={
                "feed_access_token": {
                    "token_id",
                    "token_hash",
                    "token_secret",
                    "created_at",
                }
            },
            generated_token_ids={
                result["token_id"] for result in (python_results[0], rust_results[0])
            },
        )
        return

    if case_id == "feed_touch_legacy_token_records_secret":
        assert python_results == rust_results == [{"updated": True}]
        _assert_db_equal(
            observation,
            ignored_fields={"feed_access_token": {"last_used_at"}},
            ignored_row_predicates={
                "feed_access_token": lambda row: (
                    row["token_id"] == observation["pair"].manifest.token_ids[1]
                )
            },
        )
        for backend in (observation["pair"].python, observation["pair"].rust):
            token = next(
                row
                for row in _rows_by_table(backend)["feed_access_token"]
                if row["token_id"] == observation["pair"].manifest.token_ids[1]
            )
            assert token["token_secret"] == "synthetic-legacy-token-secret"
            assert (
                token["token_hash"]
                == hashlib.sha256(b"parity-token-secret-not-a-secret").hexdigest()
            )
            _assert_timestamp(token["last_used_at"])
        return

    if case_id == "feed_ensure_membership":
        assert (
            observation["python_data"]
            == observation["rust_result"]
            == {
                "created": True,
                "previous_count": 0,
            }
        )
        _assert_db_equal(
            observation,
            ignored_fields={"feed_supporter": {"created_at"}},
            ignored_row_predicates={
                "feed_supporter": lambda row: (
                    row["feed_id"] == 202
                    and row["user_id"] == observation["pair"].manifest.user_id
                )
            },
        )
        for backend in (observation["pair"].python, observation["pair"].rust):
            membership = next(
                row
                for row in _rows_by_table(backend)["feed_supporter"]
                if row["feed_id"] == 202 and row["user_id"] == 101
            )
            _assert_timestamp(membership["created_at"])
        return

    if case_id in {
        "feed_create_dev_test_feed",
        "feed_create_dev_test_feed_repeat_existing",
    }:
        if case_id == "feed_create_dev_test_feed_repeat_existing":
            assert python_results == rust_results
            assert python_results[0]["created"] is True
            assert python_results[1] == {
                "feed_id": python_results[0]["feed_id"],
                "created": False,
            }
            assert rust_results[0]["created"] is True
            assert rust_results[1] == {
                "feed_id": rust_results[0]["feed_id"],
                "created": False,
            }
        else:
            assert observation["python_data"] == observation["rust_result"]
        _assert_db_equal(
            observation,
            ignored_fields={"feed": {"last_changed_at"}, "post": {"release_date"}},
            ignored_row_predicates={
                "feed": lambda row: (
                    row["rss_url"]
                    in {
                        "https://dev-parity.example.invalid/feed.xml",
                        "https://dev-repeat-parity.example.invalid/feed.xml",
                    }
                ),
                "post": lambda row: row["guid"].startswith("parity-dev"),
            },
            generated_job_prefix="parity-dev",
        )
        feed_id = python_results[0]["feed_id"]
        feeds = _rows_by_table(observation["pair"].python)
        jobs = [
            row
            for row in feeds["processing_job"]
            if row["post_guid"].startswith("parity-dev")
            and f"-{feed_id}-" in row["post_guid"]
        ]
        assert len(jobs) == 2
        for job in jobs:
            assert job["status"] == "completed"
            assert job["current_step"] == 4
            _assert_timestamp(job["created_at"])
            _assert_timestamp(job["started_at"])
            _assert_timestamp(job["completed_at"])
        for backend in (observation["pair"].python, observation["pair"].rust):
            tables = _rows_by_table(backend)
            feed = next(row for row in tables["feed"] if row["id"] == feed_id)
            _assert_timestamp(feed["last_changed_at"])
            dev_posts = [
                row
                for row in tables["post"]
                if row["guid"].startswith("parity-dev")
                and f"-{feed_id}-" in row["guid"]
            ]
            assert len(dev_posts) == 2
            assert all(row["whitelisted"] for row in dev_posts)
            assert all(row["duration"] == 3600 for row in dev_posts)
            dev_jobs = [
                row
                for row in tables["processing_job"]
                if row["post_guid"].startswith("parity-dev")
                and f"-{feed_id}-" in row["post_guid"]
            ]
            assert len(dev_jobs) == 2
            assert all(row["status"] == "completed" for row in dev_jobs)
        return

    if case_id == "feed_create_access_token":
        left_result = observation["python_data"]
        right_result = observation["rust_result"]
        assert set(left_result) == set(right_result) == {"token_id", "secret"}
        for backend, result in (
            (observation["pair"].python, left_result),
            (observation["pair"].rust, right_result),
        ):
            uuid.UUID(hex=result["token_id"])
            assert re.fullmatch(r"[A-Za-z0-9_-]{24}", result["secret"])
            token_row = next(
                row
                for row in _rows_by_table(backend)["feed_access_token"]
                if row["token_id"] == result["token_id"]
            )
            assert token_row["token_secret"] == result["secret"]
            assert (
                token_row["token_hash"]
                == hashlib.sha256(result["secret"].encode()).hexdigest()
            )
            assert token_row["user_id"] == observation["pair"].manifest.user_id
            assert token_row["feed_id"] == observation["pair"].manifest.feed_ids[1]
            assert token_row["revoked"] is False or token_row["revoked"] == 0
            assert token_row["last_used_at"] is None
            _assert_timestamp(token_row["created_at"])
        _assert_db_equal(
            observation,
            ignored_fields={
                "feed_access_token": {
                    "token_id",
                    "token_hash",
                    "token_secret",
                    "created_at",
                }
            },
            generated_token_ids={
                left_result["token_id"],
                right_result["token_id"],
            },
        )
        for backend, result in (
            (observation["pair"].python, left_result),
            (observation["pair"].rust, right_result),
        ):
            _assert_python_auth_accepts_writer_token(
                backend,
                token_id=result["token_id"],
                secret=result["secret"],
                user_id=observation["pair"].manifest.user_id,
                feed_id=observation["pair"].manifest.feed_ids[1],
            )
        return

    if case_id == "feed_touch_access_token":
        assert (
            observation["python_data"]
            == observation["rust_result"]
            == {"updated": True}
        )
        _assert_db_equal(
            observation,
            ignored_fields={"feed_access_token": {"last_used_at"}},
            ignored_row_predicates={
                "feed_access_token": lambda row: (
                    row["token_id"] == observation["pair"].manifest.token_ids[0]
                )
            },
        )
        for backend in (observation["pair"].python, observation["pair"].rust):
            token_row = next(
                row
                for row in _rows_by_table(backend)["feed_access_token"]
                if row["token_id"] == observation["pair"].manifest.token_ids[0]
            )
            _assert_timestamp(token_row["last_used_at"])
        return

    if case_id in {"feed_touch_revoked_token_noop", "feed_touch_missing_token_noop"}:
        assert python_results == rust_results == [{"updated": False}]
        _assert_db_equal(observation)
        return

    assert observation["python_data"] == observation["rust_result"], {
        "case_id": case_id,
        "python": observation["python_data"],
        "rust": observation["rust_result"],
    }
    assert observation["python_rows"] == observation["rust_rows"], (
        f"feed writer persisted table rows differ for {case_id}"
    )
    tables = _rows_by_table(observation["pair"].python)
    if case_id == "feed_update_settings":
        assert next(row for row in tables["feed"] if row["id"] == 202)[
            "auto_whitelist_new_episodes_override"
        ] in (True, 1)
    elif case_id == "feed_increment_download_count":
        assert (
            next(row for row in tables["post"] if row["id"] == 301)["download_count"]
            == 3
        )
    elif case_id == "feed_whitelist_post":
        assert next(row for row in tables["post"] if row["id"] == 302)[
            "whitelisted"
        ] in (True, 1)
    elif case_id == "feed_remove_membership":
        assert not any(
            row["feed_id"] == 201 and row["user_id"] == 101
            for row in tables["feed_supporter"]
        )
    elif case_id == "feed_ensure_membership_repeat_existing":
        assert (
            python_results
            == rust_results
            == [
                {"created": False, "previous_count": 1},
                {"created": False, "previous_count": 1},
            ]
        )
        assert sum(row["feed_id"] == 202 for row in tables["feed_supporter"]) == 1
    elif case_id == "feed_remove_membership_missing_noop":
        assert python_results == rust_results == [{"removed": 0}, {"removed": 0}]
    elif case_id == "feed_whitelist_latest_already_whitelisted_noop":
        assert (
            python_results
            == rust_results
            == [
                {
                    "updated": False,
                    "post_guid": observation["pair"].manifest.post_guids[0],
                },
                {
                    "updated": False,
                    "post_guid": observation["pair"].manifest.post_guids[0],
                },
            ]
        )
    elif case_id == "feed_whitelist_latest_empty_feed_noop":
        assert observation["python_data"] == {"updated": False}
    elif case_id == "feed_increment_missing_post_noop":
        assert observation["python_data"] == {
            "post_id": observation["pair"].manifest.missing_post_id,
            "updated": 0,
        }
    elif case_id == "feed_delete_missing_noop":
        assert observation["python_data"] == {"deleted": False}
    elif case_id == "feed_whitelist_latest_post":
        assert next(row for row in tables["post"] if row["id"] == 302)[
            "whitelisted"
        ] in (True, 1)
    elif case_id == "feed_toggle_whitelist_all":
        assert all(
            not row["whitelisted"] for row in tables["post"] if row["feed_id"] == 201
        )
    elif case_id == "feed_delete_cascade":
        for backend in (observation["pair"].python, observation["pair"].rust):
            tables = _rows_by_table(backend)
            assert not any(row["id"] == 201 for row in tables["feed"])
            assert not any(row["feed_id"] == 201 for row in tables["post"])
            assert not any(row["feed_id"] == 201 for row in tables["feed_access_token"])
            assert not any(row["feed_id"] == 201 for row in tables["feed_supporter"])
            assert not any(
                row["id"] == 601 and row["post_id"] == 301
                for row in tables["model_call"]
            )
            assert not any(
                row["id"] == 701 and row["post_id"] == 301
                for row in tables["transcript_segment"]
            )
            assert not any(row["id"] == 801 for row in tables["identification"])
            # The existing Python action does not delete audio_segment rows;
            # parity compares the retained orphan in the full projection.
            assert not any(
                row["post_guid"] == observation["pair"].manifest.post_guids[0]
                for row in tables["processing_job"]
            )
