from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = REPO_ROOT / "docs" / "contracts"
FIXTURE_ROOT = CONTRACT_ROOT / "writer-rpc" / "v1"


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    decoded: dict[str, object] = {}
    for key, value in pairs:
        if key in decoded:
            raise ValueError(f"duplicate object key: {key}")
        decoded[key] = value
    return decoded


@pytest.mark.parametrize(
    "fixture_name",
    ["action-request.json", "update-request.json", "transaction-request.json"],
)
def test_writer_v1_request_fixtures_match_schema(fixture_name: str) -> None:
    schema = json.loads(
        (CONTRACT_ROOT / "writer-rpc-v1-request.schema.json").read_text()
    )
    fixture = json.loads((FIXTURE_ROOT / fixture_name).read_text())

    jsonschema.Draft202012Validator(schema).validate(fixture)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "accepted-response.json",
        "success-response.json",
        "failure-response.json",
        "rejected-response.json",
        "rejected-no-command-id-response.json",
        "unknown-response.json",
    ],
)
def test_writer_v1_response_fixtures_match_schema(fixture_name: str) -> None:
    schema = json.loads(
        (CONTRACT_ROOT / "writer-rpc-v1-response.schema.json").read_text()
    )
    fixture = json.loads((FIXTURE_ROOT / fixture_name).read_text())

    jsonschema.Draft202012Validator(schema).validate(fixture)


def test_writer_v1_ready_fixture_matches_schema() -> None:
    schema = json.loads((CONTRACT_ROOT / "writer-rpc-v1-ready.schema.json").read_text())
    fixture = json.loads((FIXTURE_ROOT / "ready-response.json").read_text())

    jsonschema.Draft202012Validator(schema).validate(fixture)


def test_writer_v1_duplicate_keys_are_rejected_before_validation() -> None:
    duplicate = '{"version":1,"version":1,"command_id":"duplicate"}'

    with pytest.raises(ValueError, match="duplicate object key: version"):
        json.loads(duplicate, object_pairs_hook=_reject_duplicate_keys)


def test_writer_v1_request_schema_rejects_extra_envelope_fields() -> None:
    schema = json.loads(
        (CONTRACT_ROOT / "writer-rpc-v1-request.schema.json").read_text()
    )
    fixture = json.loads((FIXTURE_ROOT / "action-request.json").read_text())
    fixture["secret"] = "must-not-be-accepted"

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(fixture)


def test_writer_v1_request_schema_rejects_nested_transaction() -> None:
    schema = json.loads(
        (CONTRACT_ROOT / "writer-rpc-v1-request.schema.json").read_text()
    )
    fixture = json.loads((FIXTURE_ROOT / "transaction-request.json").read_text())
    fixture["commands"][0] = {
        "command_id": "nested",
        "operation": "transaction",
        "commands": [],
    }

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(fixture)
