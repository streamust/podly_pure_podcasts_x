"""Disposable LiteLLM connection probe; stdin settings, stdout {"ok", "error"}."""

import contextlib
import json
import sys
from typing import Any


def main() -> None:
    settings = json.load(sys.stdin)
    # SDK diagnostics must not corrupt the JSON IPC response.
    with contextlib.redirect_stdout(sys.stderr):
        try:
            import litellm

            from shared.llm_utils import model_uses_max_completion_tokens

            litellm.api_key = settings["api_key"]
            if settings.get("base_url"):
                litellm.api_base = settings["base_url"]
            model = settings["model"]
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are a healthcheck probe."},
                    {"role": "user", "content": "ping"},
                ],
                "timeout": settings["timeout"],
            }
            token_key = (
                "max_completion_tokens"
                if model_uses_max_completion_tokens(model)
                else "max_tokens"
            )
            kwargs[token_key] = 1
            litellm.completion(**kwargs)
            result: dict[str, Any] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
