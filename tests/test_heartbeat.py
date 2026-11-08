"""POST /api/heartbeat: keeps the windowed desktop app from idling out while its page is open.

Contract:
- accepted (204) only from this server's own loopback page: the browser's Origin must
  be exactly http:// + Host, and Host must be 127.0.0.1 or localhost;
- refused (403) for other websites and requests without Origin; a request whose Host
  is not 127.0.0.1 or localhost (a DNS-rebinding page) is refused (400) by the
  server-wide Host check before it reaches the endpoint;
- carries no data, returns no body, creates no session and sets no CORS header;
- only an accepted heartbeat reaches the idle monitor; without one (console version,
  development server) it does nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from tests.frontend_page import source

client = TestClient(app, base_url="http://127.0.0.1:8420")


@pytest.fixture
def beats():
    seen = []
    app.state.on_heartbeat = lambda: seen.append(1)
    yield seen
    del app.state.on_heartbeat


def beat(origin=None, host="127.0.0.1:8420"):
    headers = {"Host": host}
    if origin is not None:
        headers["Origin"] = origin
    return client.post("/api/heartbeat", headers=headers)


@pytest.mark.parametrize("origin, host", [
    ("http://127.0.0.1:8420", "127.0.0.1:8420"),
    ("http://localhost:8420", "localhost:8420"),
])
def test_own_page_heartbeat_is_accepted_and_reaches_the_monitor(beats, origin, host):
    response = beat(origin, host)
    assert response.status_code == 204
    assert response.content == b""
    assert beats == [1]


@pytest.mark.parametrize("origin, host, status", [
    (None, "127.0.0.1:8420", 403),                             # not from a page
    ("null", "127.0.0.1:8420", 403),                           # sandboxed/opaque origin
    ("http://evil.example", "127.0.0.1:8420", 403),            # another website
    ("https://127.0.0.1:8420", "127.0.0.1:8420", 403),         # different scheme
    ("http://127.0.0.1:9999", "127.0.0.1:8420", 403),          # different port
    ("http://localhost:8420", "127.0.0.1:8420", 403),          # origin and host disagree
    ("http://evil.example:8420", "evil.example:8420", 400),    # DNS rebinding to 127.0.0.1
    ("http://127.0.0.1.evil.example:8420", "127.0.0.1.evil.example:8420", 400),
    ("http://10.0.0.5:8420", "10.0.0.5:8420", 400),            # not loopback
])
def test_heartbeat_from_anything_but_the_own_page_is_refused(beats, origin, host, status):
    response = beat(origin, host)
    assert response.status_code == status
    assert beats == []
    assert "access-control-allow-origin" not in response.headers


def test_heartbeat_without_idle_monitor_does_nothing():
    assert getattr(app.state, "on_heartbeat", None) is None
    assert beat("http://127.0.0.1:8420").status_code == 204


def test_heartbeat_accepts_no_other_method_and_ignores_any_body(beats):
    assert client.get("/api/heartbeat", headers={"Origin": "http://127.0.0.1:8420",
                                                 "Host": "127.0.0.1:8420"}).status_code == 405
    response = client.post("/api/heartbeat", headers={"Origin": "http://127.0.0.1:8420", "Host": "127.0.0.1:8420"},
                           json={"password": "Never-Read-5521"})
    assert response.status_code == 204 and response.content == b""


def test_page_sends_a_bodyless_heartbeat_every_minute_and_when_shown_again():
    local, hook = source("api/local.ts"), source("app/useHeartbeat.ts")
    assert "fetch('/api/heartbeat', { method: 'POST', cache: 'no-store' })" in local
    assert "const HEARTBEAT_MS = 60000;" in hook
    assert "setInterval(sendHeartbeat, HEARTBEAT_MS)" in hook
    assert "document.visibilityState === 'visible'" in hook
    # Not in the online demo.
    assert "useHeartbeat(!PUBLIC_DEMO)" in source("app/App.tsx")
