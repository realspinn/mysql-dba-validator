"""Public online demo (MDV_PUBLIC_DEMO=1): static SQL analysis only, enforced by the server.

Contract:
- the setting is read once at startup: "1" turns it on, unset or "0" leaves the local
  application unchanged, anything else (or a public demo without MDV_ALLOWED_HOSTS, or
  with CORS_ALLOW_ORIGINS) stops the server from starting;
- only GET /, GET /demo, GET /api/health, POST /api/validate and GET /static/assets/*
  are served; every other route is 404 before route code runs;
- /api/validate accepts the SQL text alone: any connection field, even empty or null,
  is refused with 403 before the target is classified, so no DNS lookup, socket or
  MySQL connection happens; unknown or nested fields are 422 without echoing values;
- results are the same as static analysis in the local application;
- request bodies are bounded before they are buffered (declared, chunked or
  understated Content-Length);
- only the configured host names are answered; forwarded headers change nothing;
- /api/health never probes MySQL and returns nothing about the deployment;
- the CSP allows only the page's own server (no local connector);
- submitted secrets do not appear in responses or logs.

Verification level: unit / TestClient and direct ASGI calls, with DNS, sockets and
every database entry point replaced by recorders. Nothing is contacted. This does not
exercise a real Uvicorn process or the hosting provider's proxy.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import re
import socket

import pymysql
import pytest
from fastapi.testclient import TestClient

import backend.main as backend_main

HOST = "demo.example.com"
SECRET = "Public-Demo-Secret-4417"
SQL = "DELETE FROM order_audit_log;"


def _reload(monkeypatch, **env):
    for name in ("MDV_PUBLIC_DEMO", "MDV_ALLOWED_HOSTS", "CORS_ALLOW_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    importlib.reload(backend_main)


@pytest.fixture
def restore_local(monkeypatch):
    yield
    for name in ("MDV_PUBLIC_DEMO", "MDV_ALLOWED_HOSTS", "CORS_ALLOW_ORIGINS", "RENDER"):
        monkeypatch.delenv(name, raising=False)
    importlib.reload(backend_main)


@pytest.fixture
def network(monkeypatch):
    """Record every way a request could reach DNS, a socket or MySQL."""
    calls = []

    def recorder(name):
        def fake(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"{name} must not run in the public demo")
        return fake

    monkeypatch.setattr(socket, "getaddrinfo", recorder("getaddrinfo"))
    monkeypatch.setattr(socket, "create_connection", recorder("create_connection"))
    monkeypatch.setattr(pymysql, "connect", recorder("pymysql.connect"))
    for name in ("probe_connection", "discover_databases", "open_evidence_connection", "get_client",
                 "check_for_update", "resolve_approved_company_target", "classify_target"):
        monkeypatch.setattr(backend_main, name, recorder(name))
    return calls


@pytest.fixture
def public(monkeypatch, restore_local):
    _reload(monkeypatch, MDV_PUBLIC_DEMO="1", MDV_ALLOWED_HOSTS=HOST)
    return TestClient(backend_main.app, base_url=f"https://{HOST}")


# ------------------------------------------------------------------ configuration

@pytest.mark.parametrize("value", ["true", "yes", "2", " 1", "on"])
def test_an_unclear_setting_stops_the_server_from_starting(monkeypatch, restore_local, value):
    with pytest.raises(RuntimeError, match="MDV_PUBLIC_DEMO must be 0 or 1"):
        _reload(monkeypatch, MDV_PUBLIC_DEMO=value, MDV_ALLOWED_HOSTS=HOST)


@pytest.mark.parametrize("env", [
    {"MDV_PUBLIC_DEMO": "1"},                                          # no public host name
    {"MDV_PUBLIC_DEMO": "1", "MDV_ALLOWED_HOSTS": "*"},                # only a wildcard
    {"MDV_PUBLIC_DEMO": "1", "MDV_ALLOWED_HOSTS": HOST, "CORS_ALLOW_ORIGINS": "https://other.example"},
])
def test_an_unsafe_public_configuration_stops_the_server_from_starting(monkeypatch, restore_local, env):
    with pytest.raises(RuntimeError):
        _reload(monkeypatch, **env)


@pytest.mark.parametrize("value", ["", "0"])
def test_unset_or_zero_is_the_local_application(monkeypatch, restore_local, value):
    _reload(monkeypatch, MDV_PUBLIC_DEMO=value)
    local = TestClient(backend_main.app, base_url="http://127.0.0.1:8420")
    assert 'id="sqlInput"' in local.get("/").text
    assert "http://127.0.0.1:8765" in local.get("/").headers["content-security-policy"]
    assert local.get("/docs").status_code == 200


# ------------------------------------------------------------------ routes

def test_public_pages_and_assets(public):
    home = public.get("/")
    assert home.status_code == 200 and "Understand SQL risk before execution." in home.text
    demo = public.get("/demo")
    assert demo.status_code == 200 and '<html lang="en" data-public-demo>' in demo.text
    notice = " ".join(demo.text.split())
    assert ("Online demo: SQL is sent to the hosted server for static analysis. Do not paste "
            "confidential company scripts, credentials or production-sensitive information. Real "
            "database connections are available only in the downloaded application."
            ) in notice.replace("<strong>", "").replace("</strong>", "")
    assert public.get("/static/assets/fish-swim.webp").status_code == 200
    # Each page's own stylesheet and script are under the allowed assets folder.
    files = re.findall(r'(?:href|src)="(/static/assets/(?:css|js)/[\w-]+\.(?:css|js))"', home.text + demo.text)
    assert sorted(set(files)) == ["/static/assets/css/home.css", "/static/assets/css/validator.css",
                                  "/static/assets/js/home.js", "/static/assets/js/validator.js"]
    for path in files:
        response = public.get(path)
        kind = "text/css" if path.endswith(".css") else "text/javascript"
        assert response.status_code == 200 and response.headers["content-type"].startswith(kind), path


@pytest.mark.parametrize("method, path", [
    ("POST", "/api/connections/test"),
    ("POST", "/api/connections/discover-databases"),
    ("POST", "/api/evidence"),
    ("POST", "/api/diagnostics/observed-performance"),
    ("POST", "/api/heartbeat"),
    ("POST", "/api/update-status"),
    ("POST", "/api/update-download"),
    ("POST", "/api/update-progress"),
    ("POST", "/api/update-cancel"),
    ("POST", "/api/update-show"),
    ("GET", "/docs"),
    ("GET", "/redoc"),
    ("GET", "/openapi.json"),
    ("GET", "/static/index.html"),     # the validator without the demo marking
    ("GET", "/static/home.html"),
    ("GET", "/api/validate"),
    ("PUT", "/api/validate"),
    ("HEAD", "/"),
    ("OPTIONS", "/api/validate"),
])
def test_everything_else_is_not_served(public, network, method, path):
    login = {"host": "127.0.0.1", "port": 3306, "username": "root", "password": SECRET}
    response = public.request(method, path, json=login if method == "POST" else None)
    assert response.status_code == 404
    assert SECRET not in response.text
    assert network == []


def test_observed_performance_is_refused_even_past_the_route_guard(public, network):
    # The route guard 404s it first; the handler also refuses before any lookup.
    assert backend_main.public_route_allowed("POST", "/api/diagnostics/observed-performance") is False
    request = backend_main.ObservedPerformanceRequest(
        host="127.0.0.1", username="root", password=SECRET, database="app")
    response = backend_main.observed_performance_endpoint(request)
    assert response.status_code == 403 and b"public_demo_static_only" in response.body
    assert SECRET.encode() not in response.body
    assert network == []


@pytest.mark.parametrize("path", ["/static/assets/../index.html", "/static/assets//x", "/static/assets/..\\index.html"])
def test_static_paths_cannot_leave_the_assets_folder(path):
    assert backend_main.public_route_allowed("GET", path) is False


# ------------------------------------------------------------------ /api/validate

def test_static_analysis_matches_the_local_application(monkeypatch, restore_local, network):
    samples = [SQL, "UPDATE orders SET status = 'shipped' WHERE order_id = 1042;",
               "SELECT customer_id, email FROM customers WHERE customer_id = 7;\nALTER TABLE invoices ADD COLUMN paid_at DATETIME NULL;",
               "NOT SQL AT ALL", "UPDATE t SET a = 1 WHERE " + " OR ".join(["b = 1"] * 1500)]
    local = TestClient(backend_main.app, base_url="http://127.0.0.1:8420")
    expected = [local.post("/api/validate", json={"sql": sql}).json() for sql in samples]
    _reload(monkeypatch, MDV_PUBLIC_DEMO="1", MDV_ALLOWED_HOSTS=HOST)
    demo = TestClient(backend_main.app, base_url=f"https://{HOST}")
    for sql, result in zip(samples, expected):
        response = demo.post("/api/validate", json={"sql": sql})
        assert response.status_code == 200 and response.json() == result
    assert expected[0]["overall_risk_level"] == "CRITICAL"
    assert expected[0]["evidence_summary"]["connection"]["state"] == "not_attempted"
    assert expected[0]["statements"][0]["database_evidence"]["available"] is False
    assert expected[-1] == {"error": "sql_too_complex"}   # recursion limit, not a 500
    assert network == []


@pytest.mark.parametrize("extra", [
    {"host": "db.example.com"},
    {"host": "10.0.0.5"},
    {"host": "127.0.0.1"},
    {"host": "localhost"},
    {"host": "::1"},
    {"host": "[fd00::1]"},
    {"host": ""},
    {"host": None},
    {"port": 3306},
    {"database": "app"},
    {"database": ""},
    {"username": "root", "password": SECRET},
    {"username": "", "password": ""},
    {"host": "127.0.0.1", "port": 3306, "username": "root", "password": SECRET, "database": "app"},
    {"mode": "company"},
    {"mode": "local"},
])
def test_any_connection_field_is_refused_before_any_lookup(public, network, extra):
    response = public.post("/api/validate", json={"sql": SQL, **extra})
    assert response.status_code == 403
    assert response.json()["error_code"] == "public_demo_static_only"
    assert SECRET not in response.text
    assert network == []


@pytest.mark.parametrize("body", [
    {"sql": SQL, "connection": {"host": "db.example.com", "password": SECRET}},
    {"sql": SQL, "password": {"value": SECRET}},
    {"sql": {"text": SQL}},
    {"sql": SQL, "port": "x" + SECRET},
])
def test_unknown_nested_or_malformed_fields_are_refused_without_echo(public, network, body):
    response = public.post("/api/validate", json=body)
    assert response.status_code in (403, 422)
    assert SECRET not in response.text
    assert network == []


def test_requests_cannot_switch_the_mode(public, network):
    response = public.post(
        "/api/validate?MDV_PUBLIC_DEMO=0&public_demo=0",
        json={"sql": SQL, "host": "127.0.0.1"},
        headers={"X-MDV-Public-Demo": "0", "Cookie": "MDV_PUBLIC_DEMO=0"},
    )
    assert response.status_code == 403
    assert network == []


def test_a_busy_demo_answers_at_once(public, network):
    slots = backend_main._public_validations
    for _ in range(backend_main.PUBLIC_MAX_CONCURRENT_VALIDATIONS):
        assert slots.acquire(blocking=False)
    try:
        response = public.post("/api/validate", json={"sql": SQL})
        assert response.status_code == 503 and response.json() == {"error": "server_busy"}
    finally:
        for _ in range(backend_main.PUBLIC_MAX_CONCURRENT_VALIDATIONS):
            slots.release()
    assert public.post("/api/validate", json={"sql": SQL}).status_code == 200


# ------------------------------------------------------------------ request size

def _asgi_post(app, chunks, content_length=None):
    """POST /api/validate straight into the ASGI app; returns (status, chunks read)."""
    headers = [(b"host", HOST.encode()), (b"content-type", b"application/json")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "https", "path": "/api/validate", "raw_path": b"/api/validate", "root_path": "",
             "query_string": b"", "headers": headers, "server": (HOST, 443), "client": ("203.0.113.9", 50000)}
    sent, read = [], [0]
    chunks = iter(chunks)

    async def receive():
        try:
            body = next(chunks)
        except StopIteration:
            return {"type": "http.disconnect"}
        read[0] += 1
        return {"type": "http.request", "body": body, "more_body": True}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent[0]["status"], read[0]


def _endless(chunk=64 * 1024):
    while True:
        yield b"x" * chunk


def test_a_declared_oversized_body_is_refused_before_it_is_read(public):
    status, read = _asgi_post(backend_main.app, _endless(), backend_main.PUBLIC_MAX_BODY_BYTES + 1)
    assert (status, read) == (413, 0)


@pytest.mark.parametrize("content_length", [None, 10])  # chunked, or a Content-Length that understates the body
def test_a_streamed_body_stops_being_read_at_the_limit(public, content_length):
    status, read = _asgi_post(backend_main.app, _endless(), content_length)
    assert status == 413
    assert read * 64 * 1024 <= backend_main.PUBLIC_MAX_BODY_BYTES + 64 * 1024


def test_the_largest_legitimate_request_fits_and_bad_bodies_fail_cleanly(public, network):
    # 64 KiB of SQL where every character JSON-escapes to six bytes.
    worst = "\x01" * backend_main.MAX_SQL_BYTES
    body = json.dumps({"sql": worst})
    assert len(body) <= backend_main.PUBLIC_MAX_BODY_BYTES
    assert public.post("/api/validate", content=body, headers={"Content-Type": "application/json"}).status_code == 200
    malformed = public.post("/api/validate", content='{"sql": "' + SECRET, headers={"Content-Type": "application/json"})
    assert malformed.status_code == 422 and SECRET not in malformed.text
    bad_length = public.post("/api/validate", content=b"{}", headers={"Content-Length": "abc"})
    assert bad_length.status_code == 400
    assert network == []


# ------------------------------------------------------------------ host, health, headers, logs

def test_only_the_public_host_is_answered(public):
    assert public.get("/api/health").status_code == 200
    for base_url in ("http://127.0.0.1:8420", "http://localhost", "https://evil.example"):
        assert TestClient(backend_main.app, base_url=base_url).get("/api/health").status_code == 400
    spoofed = TestClient(backend_main.app, base_url="https://evil.example").get(
        "/api/health", headers={"X-Forwarded-Host": HOST, "X-Forwarded-For": "127.0.0.1", "Forwarded": f"host={HOST}"})
    assert spoofed.status_code == 400


def test_health_never_probes_mysql(monkeypatch, public, network):
    for name, value in (("MYSQL_HOST", "db.internal"), ("MYSQL_USER", "root"), ("MYSQL_PASSWORD", SECRET)):
        monkeypatch.setenv(name, value)
    response = public.get("/api/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    assert network == []


def test_the_csp_allows_only_the_pages_own_server(public):
    for path in ("/", "/demo", "/api/health"):
        policy = public.get(path).headers["content-security-policy"]
        assert "connect-src 'self';" in policy and "8765" not in policy
        assert "frame-ancestors 'none'" in policy and "object-src 'none'" in policy


def test_submitted_secrets_stay_out_of_responses_and_logs(public, network, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    deep = "SELECT '" + SECRET + "' FROM t WHERE " + " OR ".join(["a = 1"] * 1500)
    for body in ({"sql": "SELECT '" + SECRET + "';"}, {"sql": deep},
                 {"sql": SQL, "password": SECRET}, {"sql": SQL, "username": SECRET}):
        assert SECRET not in public.post("/api/validate", json=body).text.replace("'" + SECRET + "'", "")
    captured = capsys.readouterr()
    assert SECRET not in caplog.text and SECRET not in captured.out and SECRET not in captured.err
    assert network == []


def test_the_render_blueprint_is_free_manual_and_holds_no_credentials():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "render.yaml").read_text(encoding="utf-8")
    lines = [line.split("#", 1)[0].strip() for line in text.splitlines()]
    for required in ("plan: free", 'autoDeployTrigger: "off"', "healthCheckPath: /api/health",
                     "startCommand: uvicorn backend.main:app --host 0.0.0.0 --port $PORT --workers 1",
                     "- key: MDV_PUBLIC_DEMO", 'value: "1"', "- key: MDV_ALLOWED_HOSTS"):
        assert required in lines, required
    keys = [line.split(":", 1)[1].strip() for line in lines if line.startswith("- key:")]
    assert keys == ["MDV_PUBLIC_DEMO", "MDV_ALLOWED_HOSTS", "PYTHON_VERSION"]


def test_on_render_the_server_starts_only_as_the_public_demo(monkeypatch, restore_local):
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(RuntimeError, match="On Render"):
        _reload(monkeypatch)
    _reload(monkeypatch, MDV_PUBLIC_DEMO="1", MDV_ALLOWED_HOSTS=HOST)
    assert backend_main.PUBLIC_DEMO is True
