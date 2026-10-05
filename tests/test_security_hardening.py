import asyncio
import importlib

from starlette.requests import Request
from starlette.responses import Response

import backend.main as backend_main

app = backend_main.app
configured_cors_origins = backend_main.configured_cors_origins


def test_cors_origins_are_configurable_without_enabling_credentials(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS",
                       "https://review.example, http://localhost:3000")

    assert configured_cors_origins() == [
        "https://review.example",
        "http://localhost:3000",
    ]


def test_cors_defaults_to_fail_closed(monkeypatch):
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)

    assert configured_cors_origins() == []


def test_api_responses_include_security_and_no_store_headers():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/health",
        "raw_path": b"/api/health",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "http_version": "1.1",
    }

    async def call_next(request):
        return Response("ok")

    from backend.main import add_security_headers

    response = asyncio.run(add_security_headers(Request(scope), call_next))

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


def test_cors_configuration_does_not_enable_credentials(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS",
                       "https://review.example, http://localhost:3000")
    importlib.reload(backend_main)
    try:
        cors_middleware = next(
            middleware for middleware in backend_main.app.user_middleware
            if middleware.cls.__name__ == "CORSMiddleware"
        )

        assert cors_middleware.kwargs["allow_credentials"] is False
        assert cors_middleware.kwargs["allow_methods"] == [
            "GET", "POST", "OPTIONS"]
        assert cors_middleware.kwargs["allow_headers"] == ["Content-Type"]
        assert cors_middleware.kwargs["allow_origins"] == [
            "https://review.example",
            "http://localhost:3000",
        ]
    finally:
        monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
        importlib.reload(backend_main)
