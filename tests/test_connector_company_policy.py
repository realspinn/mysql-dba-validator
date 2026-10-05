import asyncio
import hashlib
import hmac
import json
import socket

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import connector.server as connector_server

from connector.policy import (
    CompanyTarget,
    CompanyTargetRegistry,
    TARGET_MODE_COMPANY,
    validate_company_target,
)
from connector.server import ConnectorRuntime, create_app

VALID_ORIGIN = "https://inf-extends-gardening-retrieval.trycloudflare.com"

SAFE_COMPANY_DNS = {
    "db.company.example": [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 3306)),
        (socket.AF_INET6, socket.SOCK_STREAM, 0, "",
         ("2606:2800:220:1:248:1893:25c8:1946", 3306)),
    ],
    "db.dev.company.example": [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.35", 3306)),
    ],
}

def _mock_company_getaddrinfo(host_name, *args, **kwargs):
    normalized = str(host_name).strip().lower()
    if normalized in SAFE_COMPANY_DNS:
        return list(SAFE_COMPANY_DNS[normalized])
    raise socket.gaierror(f"Name or service '{host_name}' not known")


@pytest.fixture
def stage3b_client(monkeypatch):
    monkeypatch.setattr("connector.server.socket.getaddrinfo",
                        _mock_company_getaddrinfo)
    runtime = ConnectorRuntime(
        bind_host="127.0.0.1",
        port=8765,
        allowed_origins=[VALID_ORIGIN],
        pairing_ttl_seconds=30,
        session_ttl_seconds=30,
        company_tls_ca_path="/etc/ssl/certs/company-ca.pem",
    )
    app = create_app(runtime=runtime)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def host_rewriting_client(monkeypatch):
    monkeypatch.setattr("connector.server.socket.getaddrinfo",
                        _mock_company_getaddrinfo)
    runtime = ConnectorRuntime(
        bind_host="127.0.0.1",
        port=8765,
        allowed_origins=[VALID_ORIGIN],
        pairing_ttl_seconds=30,
        session_ttl_seconds=30,
    )
    app = create_app(runtime=runtime)
    with TestClient(app) as client:
        yield client


def _issue_session(client):
    token = client.app.state.connector.issue_pairing_token()
    pair_response = client.post(
        "/pair",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
        json={"pairing_token": token},
    )
    assert pair_response.status_code == 200
    return pair_response.json()["session_token"]


def test_valid_company_target_is_accepted():
    target = CompanyTarget(
        target_id="acme-prod-db",
        display_name="Acme production DB",
        mode=TARGET_MODE_COMPANY,
        host="db.company.example",
        port=3306,
        tls_required=False,
        approval_state="approved",
    )
    validated = validate_company_target(target)
    assert validated.target_id == "acme-prod-db"
    assert validated.host == "db.company.example"
    assert validated.port == 3306
    assert validated.mode == TARGET_MODE_COMPANY


def test_empty_or_malformed_target_fields_are_rejected():
    for bad in [
        CompanyTarget(target_id="", display_name="x",
                      mode=TARGET_MODE_COMPANY, host="db.company.example", port=3306),
        CompanyTarget(target_id="bad id", display_name="x",
                      mode=TARGET_MODE_COMPANY, host="db.company.example", port=3306),
        CompanyTarget(target_id="abc", display_name="",
                      mode=TARGET_MODE_COMPANY, host="db.company.example", port=3306),
        CompanyTarget(target_id="abc", display_name="x",
                      mode=TARGET_MODE_COMPANY, host="", port=3306),
        CompanyTarget(target_id="abc", display_name="x",
                      mode=TARGET_MODE_COMPANY, host="db.company.example", port=0),
        CompanyTarget(target_id="abc", display_name="x",
                      mode="LOCAL", host="db.company.example", port=3306),
    ]:
        with pytest.raises(ValueError):
            validate_company_target(bad)


def test_duplicate_target_id_is_rejected():
    registry = CompanyTargetRegistry()
    registry.register(CompanyTarget(
        target_id="acme-prod-db",
        display_name="Acme production DB",
        mode=TARGET_MODE_COMPANY,
        host="db.company.example",
        port=3306,
        approval_state="approved",
    ))
    with pytest.raises(ValueError, match="duplicate_company_target_id"):
        registry.register(CompanyTarget(
            target_id="acme-prod-db",
            display_name="Acme production DB duplicate",
            mode=TARGET_MODE_COMPANY,
            host="db.company.example",
            port=3306,
            approval_state="approved",
        ))


def test_unknown_and_unapproved_targets_are_rejected():
    registry = CompanyTargetRegistry()
    target = CompanyTarget(
        target_id="acme-prod-db",
        display_name="Acme production DB",
        mode=TARGET_MODE_COMPANY,
        host="db.company.example",
        port=3306,
        approval_state="approved",
    )
    registry.register(target)

    stored = registry.get("acme-prod-db")
    assert stored is not None
    assert stored.approval_state == "needs_reapproval"
    assert registry.is_approved("acme-prod-db") is False
    assert registry.is_approved("unknown-target") is False
    assert registry.list_approved() == []

    unapproved = CompanyTarget(
        target_id="acme-dev-db",
        display_name="Acme dev DB",
        mode=TARGET_MODE_COMPANY,
        host="db.dev.company.example",
        port=3306,
        approval_state="pending",
    )
    registry.register(unapproved)
    assert registry.is_approved("acme-dev-db") is False


def test_policy_registry_is_not_a_network_capability():
    registry = CompanyTargetRegistry()
    target = CompanyTarget(
        target_id="approved-target",
        display_name="Approved company target",
        mode=TARGET_MODE_COMPANY,
        host="db.company.example",
        port=3306,
        approval_state="approved",
    )
    registry.register(target)

    stored = registry.get("approved-target")
    assert stored is not None
    assert stored.approval_state == "needs_reapproval"
    assert registry.is_approved("approved-target") is False
    assert registry.list_approved() == []


def test_company_target_policy_rejects_localhost_and_ip_literals():
    for raw_host in [
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "[::1]",
        "10.0.0.5",
        "8.8.8.8",
    ]:
        with pytest.raises(ValueError):
            validate_company_target(CompanyTarget(
                target_id="invalid-target",
                display_name="Invalid target",
                mode=TARGET_MODE_COMPANY,
                host=raw_host,
                port=3306,
                approval_state="approved",
            ))


def test_no_credentials_or_tls_are_required_for_stage3a_policy_only():
    target = CompanyTarget(
        target_id="stage3a-target",
        display_name="Policy-only company target",
        mode=TARGET_MODE_COMPANY,
        host="db.company.example",
        port=3306,
        tls_required=False,
        tls_policy=None,
        allowed_database_policy=None,
        approval_state="approved",
    )
    validated = validate_company_target(target)
    assert validated.tls_policy is None
    assert validated.allowed_database_policy is None
    assert validated.host == "db.company.example"


def test_stage3b_register_and_approve_company_target(stage3b_client):
    session = _issue_session(stage3b_client)
    payload = {
        "target_id": "company-db-01",
        "display_name": "Company DB 01",
        "mode": TARGET_MODE_COMPANY,
        "host": "db.company.example",
        "port": 3306,
    }

    register = stage3b_client.post(
        "/company/targets",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    assert register.status_code == 201
    body = register.json()
    assert body["status"] == "registered"
    assert body["target"]["approval_state"] == "pending"
    assert body["target"]["host"] == "db.company.example"

    approve = stage3b_client.post(
        "/company/targets/company-db-01/approve",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "approved"

    target = stage3b_client.app.state.connector.company_targets.get(
        "company-db-01")
    assert target is not None
    assert target.approval_state == "approved"
    assert target.dns_identity_snapshot is not None
    assert target.dns_identity_snapshot["host"] == "db.company.example"
    assert "93.184.216.34" in target.dns_identity_snapshot["addresses"]
    assert target.tls_hostname == "db.company.example"
    assert target.dns_snapshot_created_at is not None

    listed = stage3b_client.get(
        "/company/targets",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
        },
    )
    assert listed.status_code == 200
    assert listed.json()["targets"][0]["target_id"] == "company-db-01"
    assert listed.json()["targets"][0]["approval_state"] == "approved"


def test_stage3a_approval_fails_when_dns_resolution_fails(stage3b_client, monkeypatch):
    session = _issue_session(stage3b_client)

    def fail_dns(*args, **kwargs):
        raise socket.gaierror("fixture dns failure")

    monkeypatch.setattr("connector.server.socket.getaddrinfo", fail_dns)

    register = stage3b_client.post(
        "/company/targets",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json={
            "target_id": "dns-failure-target",
            "display_name": "DNS failure target",
            "mode": TARGET_MODE_COMPANY,
            "host": "db.company.example",
            "port": 3306,
        },
    )
    assert register.status_code == 201

    approve = stage3b_client.post(
        "/company/targets/dns-failure-target/approve",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
    )
    assert approve.status_code == 400
    target = stage3b_client.app.state.connector.company_targets.get(
        "dns-failure-target")
    assert target is not None
    assert target.approval_state == "needs_reapproval"
    assert target.dns_identity_snapshot is None


def test_stage3a_unsafe_resolved_addresses_fail_closed(stage3b_client, monkeypatch):
    session = _issue_session(stage3b_client)

    for bad_ip in [
        "127.0.0.1",
        "::1",
        "fe80::1",
        "224.0.0.1",
        "::",
        "169.254.10.20",
    ]:
        monkeypatch.setattr("connector.server.socket.getaddrinfo", lambda *args, **kwargs: [
            (socket.AF_INET if ":" not in bad_ip else socket.AF_INET6,
             socket.SOCK_STREAM, 0, "", (bad_ip, 3306)),
        ])
        register = stage3b_client.post(
            "/company/targets",
            headers={
                "Origin": VALID_ORIGIN,
                "Authorization": f"Bearer {session}",
                "Content-Type": "application/json",
            },
            json={
                "target_id": f"unsafe-{bad_ip.replace(':', '-').replace('.', '-')}",
                "display_name": f"Unsafe {bad_ip}",
                "mode": TARGET_MODE_COMPANY,
                "host": "db.company.example",
                "port": 3306,
            },
        )
        assert register.status_code == 201
        approve = stage3b_client.post(
            f"/company/targets/unsafe-{bad_ip.replace(':', '-').replace('.', '-')}/approve",
            headers={
                "Origin": VALID_ORIGIN,
                "Authorization": f"Bearer {session}",
                "Content-Type": "application/json",
            },
        )
        assert approve.status_code == 400
        target = stage3b_client.app.state.connector.company_targets.get(
            f"unsafe-{bad_ip.replace(':', '-').replace('.', '-')}")
        assert target is not None
        assert target.approval_state == "needs_reapproval"
        assert target.dns_identity_snapshot is None


def test_stage3a_multiple_safe_addresses_are_snapshot_normalized(stage3b_client):
    session = _issue_session(stage3b_client)
    register = stage3b_client.post(
        "/company/targets",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json={
            "target_id": "multi-address-target",
            "display_name": "Multi-address target",
            "mode": TARGET_MODE_COMPANY,
            "host": "db.company.example",
            "port": 3306,
        },
    )
    assert register.status_code == 201

    approve = stage3b_client.post(
        "/company/targets/multi-address-target/approve",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
    )
    assert approve.status_code == 200

    target = stage3b_client.app.state.connector.company_targets.get(
        "multi-address-target")
    assert target is not None
    snapshot = target.dns_identity_snapshot
    assert snapshot is not None
    assert snapshot["host"] == "db.company.example"
    assert snapshot["addresses"] == sorted(set(snapshot["addresses"]))
    assert len(snapshot["addresses"]) >= 2


def test_stage3b_rejects_invalid_company_target_registration(stage3b_client):
    session = _issue_session(stage3b_client)
    for payload in [
        {"target_id": "", "display_name": "bad", "mode": TARGET_MODE_COMPANY,
            "host": "db.company.example", "port": 3306},
        {"target_id": "bad", "display_name": "bad",
            "mode": TARGET_MODE_COMPANY, "host": "localhost", "port": 3306},
        {"target_id": "bad", "display_name": "bad",
            "mode": TARGET_MODE_COMPANY, "host": "10.0.0.5", "port": 3306},
        {"target_id": "bad", "display_name": "bad",
            "mode": TARGET_MODE_COMPANY, "host": "db.company.example", "port": 0},
        {"target_id": "dup-target", "display_name": "dup",
            "mode": TARGET_MODE_COMPANY, "host": "db.company.example", "port": 3306},
    ]:
        response = stage3b_client.post(
            "/company/targets",
            headers={
                "Origin": VALID_ORIGIN,
                "Authorization": f"Bearer {session}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if payload.get("target_id") == "dup-target":
            response = stage3b_client.post(
                "/company/targets",
                headers={
                    "Origin": VALID_ORIGIN,
                    "Authorization": f"Bearer {session}",
                    "Content-Type": "application/json",
                },
                json={**payload, "target_id": "dup-target"},
            )
            first = stage3b_client.post(
                "/company/targets",
                headers={
                    "Origin": VALID_ORIGIN,
                    "Authorization": f"Bearer {session}",
                    "Content-Type": "application/json",
                },
                json={**payload, "target_id": "dup-target"},
            )
            assert first.status_code in {400, 409}
            continue
        assert response.status_code in {400, 409}


def test_stage3b_no_dns_or_network_calls_during_registration(stage3b_client, monkeypatch):
    session = _issue_session(stage3b_client)

    def boom(*args, **kwargs):
        raise AssertionError(
            "network resolution must not occur during company-target policy registration"
        )

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(connector_server.pymysql, "connect", boom)

    payload = {
        "target_id": "company-db-02",
        "display_name": "Company DB 02",
        "mode": TARGET_MODE_COMPANY,
        "host": "db.company.example",
        "port": 3306,
    }

    async def invoke_registration():
        scope = {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/company/targets",
            "raw_path": b"/company/targets",
            "query_string": b"",
            "headers": [
                (b"origin", VALID_ORIGIN.encode("utf-8")),
                (b"authorization", f"Bearer {session}".encode("utf-8")),
                (b"content-type", b"application/json"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
            "http_version": "1.1",
            "app": stage3b_client.app,
        }

        async def receive():
            return {
                "type": "http.request",
                "body": json.dumps(payload).encode("utf-8"),
                "more_body": False,
            }

        request = Request(scope, receive)
        endpoint = next(
            route.endpoint
            for route in stage3b_client.app.routes
            if getattr(route, "path", None) == "/company/targets"
            and "POST" in getattr(route, "methods", set())
        )
        response = await endpoint(request)
        if isinstance(response, JSONResponse):
            return response.status_code, json.loads(response.body)
        return response["status_code"], response

    status_code, body = asyncio.run(invoke_registration())
    assert status_code == 201
    assert body["target"]["target_id"] == "company-db-02"


def test_stage3b_public_requests_cannot_create_or_approve_targets(stage3b_client):
    response = stage3b_client.post(
        "/company/targets",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
        json={
            "target_id": "public-target",
            "display_name": "Public target",
            "mode": TARGET_MODE_COMPANY,
            "host": "db.company.example",
            "port": 3306,
        },
    )
    assert response.status_code == 401

    approve = stage3b_client.post(
        "/company/targets/public-target/approve",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
    )
    assert approve.status_code == 401


def test_stage3c_rejects_public_target_injection(host_rewriting_client):
    session = _issue_session(host_rewriting_client)
    response = host_rewriting_client.post(
        "/company/targets/any-target/connect",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json={
            "target_id": "any-target",
            "host": "evil.example",
            "port": 3307,
            "username": "appuser",
            "password": "STAGE3C_SECRET_DO_NOT_LEAK_84721",
        },
    )
    assert response.status_code in {400, 403, 404}


def test_stage3c_b_exact_dns_match_is_accepted(stage3b_client, monkeypatch):
    session = _issue_session(stage3b_client)
    register = stage3b_client.post(
        "/company/targets",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json={
            "target_id": "pin-exact-target",
            "display_name": "Pin exact target",
            "mode": TARGET_MODE_COMPANY,
            "host": "db.company.example",
            "port": 3306,
        },
    )
    assert register.status_code == 201
    approve = stage3b_client.post(
        "/company/targets/pin-exact-target/approve",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
    )
    assert approve.status_code == 200

    monkeypatch.setattr("connector.server.socket.getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 3306)),
        (socket.AF_INET6, socket.SOCK_STREAM, 0, "",
         ("2606:2800:220:1:248:1893:25c8:1946", 3306)),
    ])

    destination = stage3b_client.app.state.connector.resolve_and_validate_approved_company_target(
        "pin-exact-target")
    assert destination.hostname == "db.company.example"
    assert destination.port == 3306
    assert destination.validated_ip in {
        "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"}
    assert destination.address_family in {socket.AF_INET, socket.AF_INET6}


def test_stage3c_b_rejects_address_added_removed_or_replaced(stage3b_client, monkeypatch):
    session = _issue_session(stage3b_client)
    for payload in [
        {"target_id": "pin-added-target", "display_name": "Added",
            "mode": TARGET_MODE_COMPANY, "host": "db.company.example", "port": 3306},
        {"target_id": "pin-removed-target", "display_name": "Removed",
            "mode": TARGET_MODE_COMPANY, "host": "db.company.example", "port": 3306},
        {"target_id": "pin-replaced-target", "display_name": "Replaced",
            "mode": TARGET_MODE_COMPANY, "host": "db.company.example", "port": 3306},
    ]:
        register = stage3b_client.post(
            "/company/targets",
            headers={
                "Origin": VALID_ORIGIN,
                "Authorization": f"Bearer {session}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        assert register.status_code == 201
        approve = stage3b_client.post(
            f"/company/targets/{payload['target_id']}/approve",
            headers={
                "Origin": VALID_ORIGIN,
                "Authorization": f"Bearer {session}",
                "Content-Type": "application/json",
            },
        )
        assert approve.status_code == 200

    snapshot = stage3b_client.app.state.connector.company_targets.get(
        "pin-added-target").dns_identity_snapshot
    assert snapshot is not None

    monkeypatch.setattr("connector.server.socket.getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 3306)),
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 3306)),
    ])
    with pytest.raises(ValueError, match="dns_identity_mismatch"):
        stage3b_client.app.state.connector.resolve_and_validate_approved_company_target(
            "pin-added-target")

    monkeypatch.setattr("connector.server.socket.getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 3306)),
    ])
    with pytest.raises(ValueError, match="dns_identity_mismatch"):
        stage3b_client.app.state.connector.resolve_and_validate_approved_company_target(
            "pin-removed-target")

    monkeypatch.setattr("connector.server.socket.getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.1.1.1", 3306)),
    ])
    with pytest.raises(ValueError, match="dns_identity_mismatch"):
        stage3b_client.app.state.connector.resolve_and_validate_approved_company_target(
            "pin-replaced-target")


def test_stage3c_b_requires_approved_target_match_for_private_ip_authorization(stage3b_client, monkeypatch):
    session = _issue_session(stage3b_client)

    def private_dns_lookup(host_name, *args, **kwargs):
        normalized = str(host_name).strip().lower()
        if normalized == "db.private.company.example":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.42.7.18", 3306)),
            ]
        return _mock_company_getaddrinfo(host_name, *args, **kwargs)

    monkeypatch.setattr(
        "connector.server.socket.getaddrinfo", private_dns_lookup)

    register = stage3b_client.post(
        "/company/targets",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json={
            "target_id": "private-approved-target",
            "display_name": "Private approved target",
            "mode": TARGET_MODE_COMPANY,
            "host": "db.private.company.example",
            "port": 3306,
        },
    )
    assert register.status_code == 201

    approve = stage3b_client.post(
        "/company/targets/private-approved-target/approve",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
    )
    assert approve.status_code == 200

    destination = stage3b_client.app.state.connector.resolve_and_validate_approved_company_target(
        "private-approved-target")
    assert destination.hostname == "db.private.company.example"
    assert destination.validated_ip == "10.42.7.18"

    with pytest.raises(ValueError, match="company_target_not_approved"):
        stage3b_client.app.state.connector.resolve_approved_company_target_for_hostname_and_port(
            "db.private.company.example", 3307)

    with pytest.raises(ValueError, match="company_target_not_approved"):
        stage3b_client.app.state.connector.resolve_approved_company_target_for_hostname_and_port(
            "db.internal.company.example", 3306)

    target = stage3b_client.app.state.connector.company_targets.get(
        "private-approved-target")
    assert target is not None
    monkeypatch.setattr("connector.server.socket.getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.42.7.19", 3306)),
    ])
    with pytest.raises(ValueError, match="dns_identity_mismatch"):
        stage3b_client.app.state.connector.resolve_and_validate_approved_company_target(
            "private-approved-target")


def test_stage3c_b_rejects_unsafe_and_empty_dns_results(stage3b_client, monkeypatch):
    session = _issue_session(stage3b_client)
    register = stage3b_client.post(
        "/company/targets",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json={
            "target_id": "pin-unsafe-target",
            "display_name": "Pin unsafe target",
            "mode": TARGET_MODE_COMPANY,
            "host": "db.company.example",
            "port": 3306,
        },
    )
    assert register.status_code == 201
    approve = stage3b_client.post(
        "/company/targets/pin-unsafe-target/approve",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
    )
    assert approve.status_code == 200

    monkeypatch.setattr("connector.server.socket.getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 3306)),
    ])
    with pytest.raises(ValueError, match="unsafe_destination"):
        stage3b_client.app.state.connector.resolve_and_validate_approved_company_target(
            "pin-unsafe-target")

    monkeypatch.setattr("connector.server.socket.getaddrinfo",
                        lambda *args, **kwargs: [])
    with pytest.raises(ValueError, match="dns_result_invalid"):
        stage3b_client.app.state.connector.resolve_and_validate_approved_company_target(
            "pin-unsafe-target")

    monkeypatch.setattr("connector.server.socket.getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("not-an-ip", 3306)),
    ])
    with pytest.raises(ValueError, match="dns_result_invalid"):
        stage3b_client.app.state.connector.resolve_and_validate_approved_company_target(
            "pin-unsafe-target")


def test_stage3c_b_requires_approved_snapshot_and_rejects_unapproved_or_missing_target(stage3b_client):
    runtime = stage3b_client.app.state.connector
    target = CompanyTarget(
        target_id="legacy-no-snapshot",
        display_name="Legacy target",
        mode=TARGET_MODE_COMPANY,
        host="db.company.example",
        port=3306,
        approval_state="approved",
        dns_identity_snapshot=None,
    )
    runtime.company_targets.targets[target.target_id] = target
    with pytest.raises(ValueError, match="target_needs_reapproval"):
        runtime.resolve_and_validate_approved_company_target(
            "legacy-no-snapshot")

    with pytest.raises(ValueError, match="target_not_found"):
        runtime.resolve_and_validate_approved_company_target("missing-target")


def test_stage3c_b_refuses_arbitrary_network_host_inputs(stage3b_client, monkeypatch):
    session = _issue_session(stage3b_client)
    register = stage3b_client.post(
        "/company/targets",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json={
            "target_id": "pin-generic-target",
            "display_name": "Generic target",
            "mode": TARGET_MODE_COMPANY,
            "host": "db.company.example",
            "port": 3306,
        },
    )
    assert register.status_code == 201
    approve = stage3b_client.post(
        "/company/targets/pin-generic-target/approve",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
    )
    assert approve.status_code == 200

    monkeypatch.setattr("connector.server.socket.getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 53)),
    ])

    with pytest.raises(ValueError, match="dns_identity_mismatch"):
        stage3b_client.app.state.connector.resolve_and_validate_approved_company_target(
            "pin-generic-target")


def test_stage3c_c1_uses_exact_validated_ip_for_socket_and_mysql_handoff(stage3b_client, monkeypatch):
    session = _issue_session(stage3b_client)
    register = stage3b_client.post(
        "/company/targets",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json={
            "target_id": "pin-socket-target",
            "display_name": "Pin socket target",
            "mode": TARGET_MODE_COMPANY,
            "host": "db.company.example",
            "port": 3306,
        },
    )
    assert register.status_code == 201

    approve = stage3b_client.post(
        "/company/targets/pin-socket-target/approve",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
    )
    assert approve.status_code == 200

    calls = {"count": 0}

    def fake_dns(*args, **kwargs):
        calls["count"] += 1
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 3306)),
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "",
             ("2606:2800:220:1:248:1893:25c8:1946", 3306)),
        ]

    seen = {}

    class FakeSocket:
        def __init__(self, family, type_):
            seen["family"] = family
            seen["type"] = type_
            self.closed = False

        def settimeout(self, timeout):
            seen["timeout"] = timeout

        def connect(self, destination):
            seen["destination"] = destination

        def close(self):
            self.closed = True

    class FakePyMySQLConnection:
        def __init__(self):
            self.sock = None
            self.closed = False

        def connect(self, *, sock=None, **kwargs):
            seen["pymysql_sock"] = sock
            self.sock = sock

        def close(self):
            self.closed = True

    fake_conn = FakePyMySQLConnection()

    monkeypatch.setattr("connector.server.socket.getaddrinfo", fake_dns)
    monkeypatch.setattr("connector.server.socket.socket", FakeSocket)
    monkeypatch.setattr("connector.server.pymysql.connect",
                        lambda *args, **kwargs: fake_conn)

    runtime = stage3b_client.app.state.connector
    runtime.company_tls_ca_path = "/etc/ssl/certs/company-ca.pem"
    destination = runtime.resolve_and_validate_approved_company_target(
        "pin-socket-target")
    calls["count"] = 0
    connected = runtime.connect_company_target_mysql(
        destination,
        username="appuser",
        password="secret",
        database="appdb",
        connect_timeout=2,
        tls_hostname="db.company.example",
        tls_ca_path="/etc/ssl/certs/company-ca.pem",
    )

    assert connected is fake_conn
    assert seen["family"] == socket.AF_INET
    assert seen["type"] == socket.SOCK_STREAM
    assert seen["destination"] == ("93.184.216.34", 3306)
    assert seen["timeout"] == 2
    assert seen["pymysql_sock"] is not None
    assert calls["count"] == 0


def test_stage3c_c1_closes_socket_when_connect_fails(stage3b_client, monkeypatch):
    session = _issue_session(stage3b_client)
    register = stage3b_client.post(
        "/company/targets",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json={
            "target_id": "pin-failure-target",
            "display_name": "Pin failure target",
            "mode": TARGET_MODE_COMPANY,
            "host": "db.company.example",
            "port": 3306,
        },
    )
    assert register.status_code == 201

    approve = stage3b_client.post(
        "/company/targets/pin-failure-target/approve",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
    )
    assert approve.status_code == 200

    seen = {}

    class FakeSocket:
        def __init__(self, family, type_):
            seen["family"] = family
            seen["type"] = type_
            self.closed = False

        def settimeout(self, timeout):
            seen["timeout"] = timeout

        def connect(self, destination):
            seen["destination"] = destination
            raise TimeoutError("socket connect timeout")

        def close(self):
            self.closed = True

    fake_conn_called = {"value": False}

    def fail_pymysql_connect(*args, **kwargs):
        fake_conn_called["value"] = True
        raise AssertionError(
            "PyMySQL should not be asked to resolve a hostname after socket failure")

    monkeypatch.setattr("connector.server.socket.socket", FakeSocket)
    monkeypatch.setattr("connector.server.pymysql.connect",
                        fail_pymysql_connect)

    runtime = stage3b_client.app.state.connector
    runtime.company_tls_ca_path = "/etc/ssl/certs/company-ca.pem"
    destination = runtime.resolve_and_validate_approved_company_target(
        "pin-failure-target")
    with pytest.raises(ValueError, match="database_connection_failed"):
        runtime.connect_company_target_mysql(
            destination,
            username="appuser",
            password="secret",
            database="appdb",
            connect_timeout=2,
            tls_hostname="db.company.example",
            tls_ca_path="/etc/ssl/certs/company-ca.pem",
        )

    assert seen["destination"] == ("93.184.216.34", 3306)
    assert fake_conn_called["value"] is False


def test_stage3c_c2_tls_uses_original_hostname_not_ip_and_requires_verified_tls(stage3b_client, monkeypatch):
    session = _issue_session(stage3b_client)
    register = stage3b_client.post(
        "/company/targets",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json={
            "target_id": "pin-tls-target",
            "display_name": "Pin TLS target",
            "mode": TARGET_MODE_COMPANY,
            "host": "db.company.example",
            "port": 3306,
        },
    )
    assert register.status_code == 201

    approve = stage3b_client.post(
        "/company/targets/pin-tls-target/approve",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
    )
    assert approve.status_code == 200

    target = stage3b_client.app.state.connector.company_targets.get(
        "pin-tls-target")
    assert target is not None
    target.tls_hostname = "db.company.example"

    seen = {}

    class FakeSocket:
        def __init__(self, family, type_):
            self.family = family
            self.type = type_
            self.closed = False

        def settimeout(self, timeout):
            seen["timeout"] = timeout

        def connect(self, destination):
            seen["destination"] = destination

        def close(self):
            self.closed = True

    class FakeConnection:
        def __init__(self):
            self.closed = False
            self.kwargs = None

        def connect(self, *, sock=None, **kwargs):
            seen["pymysql_sock"] = sock

        def close(self):
            self.closed = True

    fake_conn = FakeConnection()

    def fake_connect(*args, **kwargs):
        seen["ssl"] = kwargs.get("ssl")
        seen["cursorclass"] = kwargs.get("cursorclass")
        return fake_conn

    monkeypatch.setattr("connector.server.socket.socket", FakeSocket)
    monkeypatch.setattr("connector.server.pymysql.connect", fake_connect)

    destination = stage3b_client.app.state.connector.resolve_and_validate_approved_company_target(
        "pin-tls-target")
    stage3b_client.app.state.connector.connect_company_target_mysql(
        destination,
        username="appuser",
        password="SUPER_SECRET_TEST_PASSWORD",
        database="appdb",
        connect_timeout=2,
        tls_hostname="db.company.example",
        tls_ca_path="/etc/ssl/certs/company-ca.pem",
    )

    assert seen["destination"] == ("93.184.216.34", 3306)
    assert seen["ssl"]["ca"] == "/etc/ssl/certs/company-ca.pem"
    assert seen["ssl"]["check_hostname"] is True
    assert seen["ssl"]["verify_mode"] == 2
    assert seen["pymysql_sock"] is not None
    # Unbuffered and dict rows: the shared evidence collectors' row contract.
    assert seen["cursorclass"] is connector_server.pymysql.cursors.SSDictCursor


def test_stage3c_c2_rejects_tls_mismatch_and_missing_ca(stage3b_client, monkeypatch):
    session = _issue_session(stage3b_client)
    register = stage3b_client.post(
        "/company/targets",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json={
            "target_id": "pin-bad-tls-target",
            "display_name": "Bad TLS target",
            "mode": TARGET_MODE_COMPANY,
            "host": "db.company.example",
            "port": 3306,
        },
    )
    assert register.status_code == 201
    approve = stage3b_client.post(
        "/company/targets/pin-bad-tls-target/approve",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
    )
    assert approve.status_code == 200

    runtime = stage3b_client.app.state.connector
    target = runtime.company_targets.get("pin-bad-tls-target")
    assert target is not None
    target.tls_hostname = "db.company.example"

    class FakeSocket:
        def __init__(self, family, type_):
            self.closed = False

        def settimeout(self, timeout):
            pass

        def connect(self, destination):
            pass

        def close(self):
            self.closed = True

    monkeypatch.setattr("connector.server.socket.socket", FakeSocket)

    with pytest.raises(ValueError, match="database_connection_failed"):
        runtime.connect_company_target_mysql(
            runtime.resolve_and_validate_approved_company_target(
                "pin-bad-tls-target"),
            username="appuser",
            password="SUPER_SECRET_TEST_PASSWORD",
            database="appdb",
            connect_timeout=2,
            tls_hostname="evil.example",
            tls_ca_path="/etc/ssl/certs/company-ca.pem",
        )

    runtime.company_tls_ca_path = None
    with pytest.raises(ValueError, match="database_connection_failed"):
        runtime.connect_company_target_mysql(
            runtime.resolve_and_validate_approved_company_target(
                "pin-bad-tls-target"),
            username="appuser",
            password="SUPER_SECRET_TEST_PASSWORD",
            database="appdb",
            connect_timeout=2,
            tls_hostname="db.company.example",
            tls_ca_path=None,
        )


def test_stage3c_c2_credential_is_not_exposed_in_response_or_logs(stage3b_client, monkeypatch, caplog):
    # Per-request credential model: the login travels in the connect request body
    # and is used for that connection only (no credential provider/vault).
    session = _issue_session(stage3b_client)
    register = stage3b_client.post(
        "/company/targets",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json={
            "target_id": "pin-cred-target",
            "display_name": "Cred target",
            "mode": TARGET_MODE_COMPANY,
            "host": "db.company.example",
            "port": 3306,
        },
    )
    assert register.status_code == 201
    approve = stage3b_client.post(
        "/company/targets/pin-cred-target/approve",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
    )
    assert approve.status_code == 200
    connector_runtime = stage3b_client.app.state.connector
    connector_runtime.company_database_scopes[
        ("pin-cred-target", session)] = {
            "databases": ["appdb"], "complete": True,
            "session_token": session,
            "login_binding": connector_runtime.company_login_binding("appuser")}

    class FakeSocket:
        def __init__(self, family, type_):
            self.closed = False

        def settimeout(self, timeout):
            pass

        def connect(self, destination):
            pass

        def close(self):
            self.closed = True

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def connect(self, *, sock=None, **kwargs):
            pass

        def close(self):
            self.closed = True

    monkeypatch.setattr("connector.server.socket.socket", FakeSocket)
    monkeypatch.setattr("connector.server.pymysql.connect",
                        lambda *args, **kwargs: FakeConnection())

    response = stage3b_client.post(
        "/company/targets/pin-cred-target/connect",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json={
            "target_id": "pin-cred-target",
            "database": "appdb",
            "username": "appuser",
            "password": "SUPER_SECRET_TEST_PASSWORD",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "connected"}
    assert "SUPER_SECRET_TEST_PASSWORD" not in response.text
    assert "SUPER_SECRET_TEST_PASSWORD" not in caplog.text


def test_d2_bounded_explain_operations_are_read_only(monkeypatch):
    runtime = ConnectorRuntime()
    target_id = "approved-target"
    runtime.company_targets.targets[target_id] = CompanyTarget(
        target_id=target_id,
        display_name="Approved target",
        mode=TARGET_MODE_COMPANY,
        host="db.company.example",
        port=3306,
        approval_state="approved",
        dns_identity_snapshot={"addresses": ["93.184.216.34"]},
        tls_hostname="db.company.example",
    )

    executed = []

    class FakeCursor:
        def execute(self, statement):
            executed.append(statement)

        def fetchall(self):
            return []

        def fetchmany(self, size):
            return self.fetchall()[:size]

        def close(self):
            pass

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(runtime, "resolve_and_validate_approved_company_target",
                        lambda _: connector_server.CompanyTargetDestination(
                            hostname="db.company.example", validated_ip="93.184.216.34",
                            address_family=socket.AF_INET, port=3306))
    monkeypatch.setattr(runtime, "connect_company_target_mysql",
                        lambda *args, **kwargs: FakeConnection())

    for operation in ("EXPLAIN_SELECT", "EXPLAIN_UPDATE", "EXPLAIN_DELETE"):
        scope = {"database": "appdb", "table": "users", "columns": ["status"]}
        sql = runtime.build_bounded_explain_sql(
            target_id=target_id, operation=operation, scope=scope)
        result = runtime.execute_bounded_explain_statement(
            target_id=target_id,
            operation=operation,
            scope=scope,
            sql_digest=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            username="appuser",
            password="secret",
        )
        assert result["status"] == "authorized"

    assert len(executed) == 3
    assert all(statement.startswith("EXPLAIN ") for statement in executed)
    assert all(not statement.startswith(("UPDATE ", "DELETE "))
               for statement in executed)


def test_d2_sql_digest_mismatch_rejects_before_database_connection(monkeypatch):
    runtime = ConnectorRuntime()
    target_id = "digest-target"
    runtime.company_targets.targets[target_id] = CompanyTarget(
        target_id=target_id,
        display_name="Digest target",
        mode=TARGET_MODE_COMPANY,
        host="db.company.example",
        port=3306,
        approval_state="approved",
        dns_identity_snapshot={"addresses": ["93.184.216.34"]},
        tls_hostname="db.company.example",
    )
    connection_calls = []
    monkeypatch.setattr(runtime, "connect_company_target_mysql",
                        lambda *args, **kwargs: connection_calls.append(True))
    scope = {"database": "appdb", "table": "users", "columns": ["id"]}
    sql = runtime.build_bounded_explain_sql(
        target_id=target_id, operation="EXPLAIN_SELECT", scope=scope)

    with pytest.raises(ValueError, match="invalid_evidence_authorization"):
        runtime.execute_bounded_explain_statement(
            target_id=target_id,
            operation="EXPLAIN_SELECT",
            scope=scope,
            sql_digest=hashlib.sha256(b"different SQL;").hexdigest(),
            username="appuser",
            password="secret",
        )

    assert hashlib.sha256(sql.encode(
        "utf-8")).hexdigest() != hashlib.sha256(b"different SQL;").hexdigest()
    assert connection_calls == []
