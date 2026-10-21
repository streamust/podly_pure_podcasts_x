from __future__ import annotations

import ast

from scripts.check_writer_registry import (
    _action_coverage_failures,
    case_mapping_failure,
    local_literal_action_targets,
)


def test_dynamic_action_resolution_requires_only_literal_local_assignments() -> None:
    module = ast.parse(
        "def run(flag):\n"
        '    action = "finish_transcription_replace"\n'
        "    if flag:\n"
        '        action = "finish_transcription_replace_from_artifact"\n'
        "    writer_client.action(action, {})\n"
    )
    function = module.body[0]
    assert isinstance(function, ast.FunctionDef)
    assert local_literal_action_targets(function, "action", 5) == {
        "finish_transcription_replace",
        "finish_transcription_replace_from_artifact",
    }
    function.body.insert(0, ast.parse("action = choose_runtime_action()").body[0])
    assert local_literal_action_targets(function, "action", 5) is None


def test_case_mapping_requires_a_differential_case() -> None:
    assert (
        case_mapping_failure(
            "create_user", None, {}, set(), "action", action="create_user"
        )
        == "create_user: production-reachable operation has no mapped differential parity case"
    )


def test_case_mapping_requires_a_case_consumed_by_the_differential_runner() -> None:
    cases = {
        "update_post_title": {
            "case_id": "update_post_title",
            "operation": "update",
            "model": "Post",
        }
    }
    assert (
        case_mapping_failure(
            "update:Post", "update_post_title", cases, set(), "update", model="Post"
        )
        == "update:Post: parity case 'update_post_title' is not registered and executed by the differential test"
    )


def test_case_mapping_checks_the_actual_operation_action_and_model() -> None:
    cases = {
        "update_post_title": {
            "case_id": "update_post_title",
            "operation": "update",
            "model": "Post",
        }
    }
    executed = {"update_post_title"}
    assert (
        case_mapping_failure(
            "create_user",
            "update_post_title",
            cases,
            executed,
            "action",
            action="create_user",
        )
        == "create_user: mapped parity case 'update_post_title' covers a different operation"
    )
    assert (
        case_mapping_failure(
            "update:Feed", "update_post_title", cases, executed, "update", model="Feed"
        )
        == "update:Feed: mapped parity case 'update_post_title' covers a different model"
    )


def test_case_mapping_accepts_an_executed_matching_case() -> None:
    cases = {
        "create_user": {
            "case_id": "create_user",
            "operation": "action",
            "action": "create_user",
        }
    }
    assert (
        case_mapping_failure(
            "create_user",
            "create_user",
            cases,
            {"create_user"},
            "action",
            action="create_user",
        )
        is None
    )


def test_unreachable_action_still_validates_every_pinned_parity_case() -> None:
    manifest = {
        "actions": {
            "legacy_action": {
                "parity_cases": ["legacy_success", "legacy_empty"],
                "unreachable_reason": "No current production caller; retained for compatibility.",
            }
        }
    }
    parity_cases = {
        "legacy_success": {
            "case_id": "legacy_success",
            "operation": "action",
            "action": "legacy_action",
        },
        "legacy_empty": {
            "case_id": "legacy_empty",
            "operation": "action",
            "action": "legacy_action",
        },
    }
    kwargs = {
        "manifest": manifest,
        "actions": {"legacy_action"},
        "called_actions": set(),
        "validated": {"legacy_action"},
        "handled": {"legacy_action"},
        "parity_cases": parity_cases,
    }
    assert (
        _action_coverage_failures(
            **kwargs, executed_cases={"legacy_success", "legacy_empty"}
        )
        == []
    )
    failures = _action_coverage_failures(**kwargs, executed_cases={"legacy_success"})
    assert failures == [
        "legacy_action: parity case 'legacy_empty' is not registered and executed by the differential test"
    ]
