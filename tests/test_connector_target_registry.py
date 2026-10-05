"""Persistent approved-target registry (partial B2 work retained after the
per-request credential correction). The registry holds destination policy only."""

import json
import os
import socket

import pytest
from fastapi.testclient import TestClient

from connector.policy import CompanyTarget
from connector.registry_store import (
    REGISTRY_KIND,
    REGISTRY_SCHEMA_VERSION,
    RegistryCorruptError,
    RegistryPersistenceError,
    TargetRegistryStore,
)
from connector.server import CompanyTargetOperationError, ConnectorRuntime, create_app

ORIGIN = "http://127.0.0.1:8420"
HOST = "db.company.example"


@pytest.fixture
def dns(monkeypatch):
    answers = {HOST: ["203.0.113.10"]}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host not in answers:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0)) for ip in answers[host]]

    monkeypatch.setattr("connector.server.socket.getaddrinfo", fake_getaddrinfo)
    return answers


def new_runtime(path):
    runtime = ConnectorRuntime(allowed_origins=[ORIGIN], registry_store=TargetRegistryStore(path))
    runtime.load_registry()
    return runtime


def register(runtime, target_id="t1", host=HOST):
    return runtime.register_company_target(CompanyTarget(
        target_id=target_id, display_name=target_id, host=host, port=3306, approval_state="pending"))


def test_missing_registry_file_means_no_targets(tmp_path):
    runtime = new_runtime(tmp_path / "targets.json")
    assert runtime.company_targets.targets == {}
    assert not (tmp_path / "targets.json").exists()


def test_operator_workflow_survives_restart(tmp_path, dns):
    path = tmp_path / "targets.json"
    runtime = new_runtime(path)
    register(runtime)
    runtime.approve_company_target("t1")

    restarted = new_runtime(path)
    target = restarted.company_targets.get("t1")
    assert target.approval_state == "approved"
    assert target.dns_identity_snapshot["addresses"] == ["203.0.113.10"]
    assert target.created_at is not None and target.updated_at is not None
    assert restarted.is_company_target_approved("t1")

    restarted.revoke_company_target("t1")
    again = new_runtime(path)
    assert again.company_targets.get("t1").approval_state == "revoked"
    assert again.company_targets.get("t1").dns_identity_snapshot is None
    assert not again.is_company_target_approved("t1")

    again.delete_company_target("t1")
    final = new_runtime(path)
    assert final.company_targets.targets == {}
    with pytest.raises(ValueError, match="deleted_company_target_id_reserved"):
        register(final)


def test_registry_file_contains_only_target_policy(tmp_path, dns):
    path = tmp_path / "targets.json"
    runtime = new_runtime(path)
    register(runtime)
    runtime.approve_company_target("t1")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["kind"] == REGISTRY_KIND
    assert document["schema_version"] == REGISTRY_SCHEMA_VERSION
    keys = set(document["targets"][0])
    assert not {"username", "password", "credential", "secret"} & keys
    assert "password" not in path.read_text(encoding="utf-8").lower()


def test_writes_are_atomic_and_leave_no_temp_files(tmp_path, dns):
    path = tmp_path / "targets.json"
    runtime = new_runtime(path)
    register(runtime)
    runtime.approve_company_target("t1")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["targets.json"]


def test_write_failure_rolls_back_and_fails(tmp_path, dns, monkeypatch):
    path = tmp_path / "targets.json"
    runtime = new_runtime(path)
    register(runtime)
    before = path.read_bytes()

    def failing_replace(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("connector.registry_store.os.replace", failing_replace)
    with pytest.raises(RegistryPersistenceError):
        runtime.approve_company_target("t1")
    assert runtime.company_targets.get("t1").approval_state == "pending"
    with pytest.raises(RegistryPersistenceError):
        register(runtime, "t2")
    assert runtime.company_targets.get("t2") is None
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["targets.json"]


def _write(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")


def _valid_document():
    return {
        "kind": REGISTRY_KIND, "schema_version": REGISTRY_SCHEMA_VERSION, "updated_at": 1.0,
        "retired_target_ids": [],
        "targets": [{
            "target_id": "t1", "display_name": "t1", "mode": "COMPANY", "host": HOST, "port": 3306,
            "tls_required": True, "approval_state": "approved",
            "dns_identity_snapshot": {"host": HOST, "port": 3306, "addresses": ["203.0.113.10"]},
            "tls_hostname": HOST,
        }],
    }


def _mutations():
    def target(doc):
        return doc["targets"][0]
    return [
        ("bad json", None),
        ("wrong kind", lambda d: d.update(kind="something-else")),
        ("future version", lambda d: d.update(schema_version=2)),
        ("unknown top-level key", lambda d: d.update(extra=True)),
        ("password field smuggled in", lambda d: target(d).update(password="hunter2")),
        ("username field smuggled in", lambda d: target(d).update(username="dba")),
        ("approved without snapshot", lambda d: target(d).update(dns_identity_snapshot=None)),
        ("snapshot with bad address", lambda d: target(d)["dns_identity_snapshot"].update(addresses=["not-an-ip"])),
        ("snapshot for another host", lambda d: target(d)["dns_identity_snapshot"].update(host="evil.example")),
        ("tls hostname mismatch", lambda d: target(d).update(tls_hostname="evil.example")),
        ("invalid host", lambda d: target(d).update(host="127.0.0.1")),
        ("bad port", lambda d: target(d).update(port="3306")),
        ("bad approval state", lambda d: target(d).update(approval_state="trusted")),
        ("duplicate id", lambda d: d["targets"].append(dict(target(d)))),
        ("retired id in use", lambda d: d.update(retired_target_ids=["t1"])),
        ("targets not a list", lambda d: d.update(targets={})),
    ]


@pytest.mark.parametrize("label, mutate", _mutations(), ids=[m[0] for m in _mutations()])
def test_corrupt_registry_fails_closed_and_is_not_replaced(tmp_path, label, mutate):
    path = tmp_path / "targets.json"
    if mutate is None:
        path.write_text("{not json", encoding="utf-8")
    else:
        document = _valid_document()
        mutate(document)
        _write(path, document)
    original = path.read_bytes()
    runtime = ConnectorRuntime(allowed_origins=[ORIGIN], registry_store=TargetRegistryStore(path))
    with pytest.raises(RegistryCorruptError):
        runtime.load_registry()
    assert runtime.company_targets.targets == {}
    assert path.read_bytes() == original


def test_valid_document_loads(tmp_path):
    path = tmp_path / "targets.json"
    _write(path, _valid_document())
    runtime = new_runtime(path)
    assert runtime.is_company_target_approved("t1")


def test_oversized_registry_is_rejected(tmp_path):
    path = tmp_path / "targets.json"
    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(RegistryCorruptError, match="registry_too_large"):
        TargetRegistryStore(path).load()


def test_needs_reapproval_requires_explicit_reapproval(tmp_path, dns):
    path = tmp_path / "targets.json"
    runtime = new_runtime(path)
    register(runtime)
    dns.pop(HOST)
    with pytest.raises(CompanyTargetOperationError) as failed:
        runtime.approve_company_target("t1")
    assert failed.value.status_code == 400
    assert new_runtime(path).company_targets.get("t1").approval_state == "needs_reapproval"

    dns[HOST] = ["203.0.113.10"]
    with pytest.raises(CompanyTargetOperationError) as blocked:
        runtime.approve_company_target("t1")
    assert blocked.value.code == "company_target_needs_reapproval"
    runtime.approve_company_target("t1", allow_reapproval=True)
    assert new_runtime(path).is_company_target_approved("t1")


def test_destination_downgrade_is_persisted(tmp_path, dns):
    path = tmp_path / "targets.json"
    runtime = new_runtime(path)
    register(runtime)
    runtime.approve_company_target("t1")
    runtime.company_targets.get("t1").dns_identity_snapshot = None
    with pytest.raises(ValueError, match="target_needs_reapproval"):
        runtime.resolve_and_validate_approved_company_target("t1")
    # The fail-closed downgrade is persisted, so a restart cannot revert it to "approved".
    assert new_runtime(path).company_targets.get("t1").approval_state == "needs_reapproval"


def _session(client, runtime, scope):
    code = runtime.issue_pairing_token(scope=scope)
    response = client.post("/pair", headers={"Origin": ORIGIN, "Content-Type": "application/json"},
                           json={"pairing_token": code})
    return {"Origin": ORIGIN, "Authorization": f"Bearer {response.json()['session_token']}",
            "Content-Type": "application/json"}


def test_http_listing_and_revoke_scopes(tmp_path, dns):
    runtime = new_runtime(tmp_path / "targets.json")
    register(runtime, "approved-one")
    runtime.approve_company_target("approved-one")
    register(runtime, "pending-one")
    client = TestClient(create_app(runtime=runtime))
    browser = _session(client, runtime, "browser")
    operator = _session(client, runtime, "operator")

    browser_list = client.get("/company/targets", headers=browser).json()["targets"]
    assert [t["target_id"] for t in browser_list] == ["approved-one"]
    operator_list = client.get("/company/targets", headers=operator).json()["targets"]
    assert {t["target_id"] for t in operator_list} == {"approved-one", "pending-one"}

    denied = client.post("/company/targets/approved-one/revoke", headers=browser)
    assert denied.status_code == 403
    revoked = client.post("/company/targets/approved-one/revoke", headers=operator)
    assert revoked.status_code == 200 and revoked.json()["target"]["approval_state"] == "revoked"
    assert client.get("/company/targets", headers=browser).json()["targets"] == []
    assert client.post("/company/targets/missing/revoke", headers=operator).status_code == 404


def test_http_registry_write_failure_is_reported(tmp_path, dns, monkeypatch):
    runtime = new_runtime(tmp_path / "targets.json")
    client = TestClient(create_app(runtime=runtime))
    operator = _session(client, runtime, "operator")
    monkeypatch.setattr("connector.registry_store.os.replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    response = client.post("/company/targets", headers=operator,
                           json={"target_id": "t1", "display_name": "t1", "host": HOST, "port": 3306})
    assert response.status_code == 500
    assert response.json()["error_code"] == "target_registry_unavailable"
    assert runtime.company_targets.get("t1") is None
