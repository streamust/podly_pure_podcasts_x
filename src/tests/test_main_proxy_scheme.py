from typing import Any

from waitress.proxy_headers import proxy_headers_middleware

import main


def test_waitress_trusts_only_forwarded_proto(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(main, "create_web_app", lambda: "app")
    monkeypatch.setattr(main, "serve", lambda app, **kwargs: captured.update(kwargs))

    main.main()

    assert captured["trusted_proxy"] == "*"
    assert captured["trusted_proxy_headers"] == {"x-forwarded-proto"}


def test_forwarded_proto_sets_https_but_forwarded_for_is_ignored() -> None:
    seen: dict[str, Any] = {}

    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        seen.update(environ)
        start_response("200 OK", [])
        return []

    wrapped = proxy_headers_middleware(
        app,
        trusted_proxy="*",
        trusted_proxy_count=1,
        trusted_proxy_headers={"x-forwarded-proto"},
        clear_untrusted=True,
    )
    wrapped(
        {
            "REMOTE_ADDR": "172.20.0.1",
            "wsgi.url_scheme": "http",
            "HTTP_X_FORWARDED_PROTO": "https",
            "HTTP_X_FORWARDED_FOR": "203.0.113.9",
            "SERVER_PORT": "5001",
            "SERVER_NAME": "podly.riste.cloud",
            "HTTP_HOST": "podly.riste.cloud",
        },
        lambda *_args: lambda _data: None,
    )

    assert seen["wsgi.url_scheme"] == "https"
    assert seen["REMOTE_ADDR"] == "172.20.0.1"
    assert "HTTP_X_FORWARDED_FOR" not in seen
