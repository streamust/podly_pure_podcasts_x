import io
import json
import subprocess
import sys
import types
from typing import Any

import pytest

from app import llm_probe_worker
from app.routes import config_routes


def _client(app: Any, monkeypatch: pytest.MonkeyPatch):
    app.testing = True
    app.register_blueprint(config_routes.config_bp)
    monkeypatch.setattr(config_routes, "require_admin", lambda: (None, None))
    return app.test_client()


def _post(client: Any) -> Any:
    return client.post(
        "/api/config/test-llm",
        json={"llm": {"llm_api_key": "sk-test", "llm_model": "gpt-4o"}},
    )


def test_llm_probe_runs_in_helper_without_key_on_command_line(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, 0, stdout='{"ok": true}')

    monkeypatch.setattr(config_routes.subprocess, "run", run)
    with app.app_context():
        resp = _post(_client(app, monkeypatch))

    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert calls[0]["args"][-2:] == ["-m", "app.llm_probe_worker"]
    assert "sk-test" not in " ".join(calls[0]["args"])
    assert json.loads(calls[0]["input"])["api_key"] == "sk-test"


@pytest.mark.parametrize(
    ("outcome", "expected_error"),
    [
        (
            subprocess.CompletedProcess(
                [], 0, stdout='{"ok": false, "error": "bad key"}'
            ),
            "bad key",
        ),
        (subprocess.TimeoutExpired("probe", 60), "LLM probe helper failed"),
        (
            subprocess.CompletedProcess([], 0, stdout="not json"),
            "LLM probe helper failed",
        ),
    ],
)
def test_llm_probe_failures_return_400(
    app: Any, monkeypatch: pytest.MonkeyPatch, outcome: Any, expected_error: str
) -> None:
    def run(*_args: Any, **_kwargs: Any) -> Any:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(config_routes.subprocess, "run", run)
    with app.app_context():
        resp = _post(_client(app, monkeypatch))

    assert resp.status_code == 400
    assert expected_error in resp.get_json()["error"]


@pytest.mark.parametrize("fails", [False, True])
def test_probe_worker_reports_json_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fails: bool
) -> None:
    requests: list[dict[str, Any]] = []

    def completion(**kwargs: Any) -> None:
        requests.append(kwargs)
        print("sdk noise")
        if fails:
            raise RuntimeError("provider rejected key")

    monkeypatch.setitem(
        sys.modules, "litellm", types.SimpleNamespace(completion=completion)
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {"api_key": "k", "model": "gpt-4o", "base_url": None, "timeout": 5}
            )
        ),
    )

    llm_probe_worker.main()

    result = json.loads(capsys.readouterr().out)
    assert result == (
        {"ok": False, "error": "provider rejected key"} if fails else {"ok": True}
    )
    assert requests[0]["timeout"] == 5
