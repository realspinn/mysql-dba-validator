"""The database evidence contract, end to end through /api/validate and the connector.

Each statement result carries an `evidence` object (backend.evidence_state) that says
what database evidence the result actually has. Everything else that talks about
evidence (analysis_mode, evidence_summary, reasons, v2_assessment gaps, the
compatibility fields database_evidence/metadata_status) must agree with it, and
confidence stays HIGH/LIMITED/LOW whatever evidence exists.

Fake MySQL only (tests/test_local_evidence_credentials.py and
tests/test_connector_remote_evidence.py); no real server is involved.
"""

from __future__ import annotations

import pytest

from backend.analyzer import OFFLINE_EXECUTION_REASON
from backend.evidence import NO_CONNECTION_REASON
from backend.evidence_state import analysis_mode as derive_mode
from backend.risk_engine import OFFLINE_EVIDENCE_REASON, OFFLINE_ROW_IMPACT_REASON
from tests import test_connector_remote_evidence as remote
from tests.test_local_evidence_credentials import (  # noqa: F401  (mysql is a fixture)
    DB,
    PASSWORD,
    USER,
    assert_no_secrets,
    assert_read_only_boundary,
    explains,
    mysql,
    mysql_error,
    post,
)

CONFIDENCE_VALUES = {"HIGH", "LIMITED", "LOW"}
EVIDENCE_KEYS = {"connection", "plan", "metadata", "overall"}
PLAN_STATES = {"collected", "not_collected_read_only", "not_eligible", "not_supported", "failed", "not_attempted"}
METADATA_STATES = {"collected", "failed", "not_applicable", "not_attempted"}
OVERALL_STATES = {"static_only", "collected", "partial", "unavailable", "not_applicable"}


def assert_coherent(data: dict) -> None:
    """No result may tell two different stories about its evidence."""
    states = [statement["evidence"] for statement in data["statements"]]
    assert data["analysis_mode"] == derive_mode(states)
    summary = data["evidence_summary"]
    assert sum(summary["plan"].values()) == sum(summary["metadata"].values()) == len(states)
    any_collected = any("collected" in (s["plan"]["state"], s["metadata"]["state"]) for s in states)
    any_failed = any("failed" in (s["plan"]["state"], s["metadata"]["state"]) for s in states)
    assert summary["partial"] is (any_collected and any_failed)
    if data["analysis_mode"] == "STATIC_DATABASE_UNAVAILABLE":
        assert not any_collected, "mode says unavailable but evidence was collected"
    for statement in data["statements"]:
        evidence = statement["evidence"]
        assert set(evidence) == EVIDENCE_KEYS
        assert evidence["connection"] == summary["connection"]
        assert evidence["plan"]["state"] in PLAN_STATES
        assert evidence["metadata"]["state"] in METADATA_STATES
        assert evidence["overall"] in OVERALL_STATES
        assert statement["confidence"] in CONFIDENCE_VALUES
        # Compatibility fields agree with the evidence state.
        assert statement["database_evidence"]["available"] is (evidence["plan"]["state"] == "collected")
        assert (statement["metadata_status"] == "available") is (evidence["metadata"]["state"] == "collected")
        if evidence["plan"]["state"] != "collected":
            assert statement["evidence_factors"] == []
            assert statement["database_evidence"]["access_type"] is None
        connected = evidence["connection"]["state"] == "connected"
        reasons = " ".join(statement["reasons"]).lower()
        gaps = (statement.get("v2_assessment") or {}).get("evidence_missing", [])
        dimensions = (statement.get("v2_assessment") or {}).get("dimensions", [])
        if evidence["connection"]["state"] != "not_attempted":
            # The static-only sentences never survive once evidence was attempted.
            assert OFFLINE_EVIDENCE_REASON not in statement["reasons"]
            assert OFFLINE_ROW_IMPACT_REASON not in statement["reasons"]
            assert "offline" not in reasons
            assert all(gap["reason"] != NO_CONNECTION_REASON for gap in gaps)
            assert all(d["reason"] != OFFLINE_EXECUTION_REASON for d in dimensions)
        if evidence["metadata"]["state"] == "collected":
            assert "metadata" not in reasons or "metadata was collected" in reasons
            assert "table_metadata" in statement["v2_assessment"]["evidence_available"]
        else:
            assert "metadata was collected" not in reasons
            assert evidence["metadata"]["tables"] == []
        if evidence["plan"]["state"] == "collected":
            assert all(gap["name"] != "execution_plan" for gap in gaps)
        if not connected:
            assert evidence["plan"]["state"] == "not_attempted"
            assert evidence["metadata"]["state"] == "not_attempted"


def local(sql, **overrides):
    response = post(sql, **overrides)
    assert response.status_code == 200, response.text
    data = response.json()
    assert_coherent(data)
    assert_no_secrets(response.text)
    return data


def only(data):
    assert data["statement_count"] == 1
    return data["statements"][0]


# ---------------------------------------------------------------- local matrix


def test_static_only_select_without_a_login(mysql):
    data = local("SELECT * FROM users WHERE id = 1", username=None, password=None)
    statement = only(data)
    assert statement["evidence"] == {
        "connection": {"state": "not_attempted", "reason": "not_configured"},
        "plan": {"state": "not_attempted", "reason": None},
        "metadata": {"state": "not_attempted", "reason": None, "tables": []},
        "overall": "static_only",
    }
    assert data["analysis_mode"] == "STATIC"
    assert data["evidence_summary"]["connection"] == {"state": "not_attempted", "reason": "not_configured"}
    assert statement["confidence"] == "HIGH"
    assert mysql.connect_calls == []


def test_static_only_update_keeps_the_static_reasons_and_gaps(mysql):
    statement = only(local("UPDATE users SET active = 0 WHERE status = 'old'", username=None, password=None))
    assert OFFLINE_EVIDENCE_REASON in statement["reasons"]
    assert OFFLINE_ROW_IMPACT_REASON in statement["reasons"]
    assert {gap["reason"] for gap in statement["v2_assessment"]["evidence_missing"]} == {NO_CONNECTION_REASON}
    assert statement["confidence"] == "LIMITED"


def test_local_select_with_plan_and_metadata(mysql):
    data = local("SELECT * FROM users WHERE status = 'old'")
    statement = only(data)
    assert statement["evidence"] == {
        "connection": {"state": "connected", "reason": None},
        "plan": {"state": "collected", "reason": None},
        "metadata": {"state": "collected", "reason": None, "tables": [{"name": "users", "found": True}]},
        "overall": "collected",
    }
    assert data["analysis_mode"] == "DATABASE_EVIDENCE"
    # Evidence never turns into a confidence value.
    assert statement["confidence"] == "HIGH"
    assert data["evidence_summary"] == {
        "connection": {"state": "connected", "reason": None},
        "plan": {"collected": 1}, "metadata": {"collected": 1}, "overall": {"collected": 1},
        "partial": False,
    }


@pytest.mark.parametrize("sql", [
    "UPDATE users SET active = 0 WHERE status = 'old'",
    "DELETE FROM users WHERE status = 'old'",
], ids=["update", "delete"])
def test_local_dml_has_metadata_and_an_intentionally_uncollected_plan(mysql, sql):
    data = local(sql)
    statement = only(data)
    evidence = statement["evidence"]
    assert evidence["connection"]["state"] == "connected"
    assert evidence["plan"] == {"state": "not_collected_read_only", "reason": "plan_not_collected_read_only"}
    assert evidence["metadata"]["state"] == "collected"
    assert evidence["overall"] == "collected"
    assert data["analysis_mode"] == "STATIC_DATABASE_METADATA"
    assert data["evidence_summary"]["partial"] is False
    assert statement["confidence"] == "LIMITED"
    assert statement["score"] == statement["static_score"]
    # The "Why" tells the evidence story: metadata yes, plan deliberately not collected.
    why = " ".join(statement["reasons"])
    assert "table metadata was collected" in why
    assert "not collected because the local evidence connection is read-only" in why
    assert "Actual affected-row counts are not known." in why
    gaps = {gap["name"]: gap["reason"] for gap in statement["v2_assessment"]["evidence_missing"]}
    assert "read-only" in gaps["execution_plan"]
    assert "estimates, not affected-row counts" in gaps["affected_row_count"]
    # Nothing was planned or executed on the local connection.
    assert explains(mysql) == []
    assert_read_only_boundary(mysql)


def test_invalid_local_login_reports_unavailable_without_a_metadata_claim(mysql):
    mysql.connect_error = mysql_error(1045, f"Access denied for user '{USER}'")
    data = local("UPDATE users SET active = 0 WHERE status = 'old'")
    statement = only(data)
    assert statement["evidence"] == {
        "connection": {"state": "failed", "reason": "auth_failed"},
        "plan": {"state": "not_attempted", "reason": None},
        "metadata": {"state": "not_attempted", "reason": None, "tables": []},
        "overall": "unavailable",
    }
    assert data["analysis_mode"] == "STATIC_DATABASE_UNAVAILABLE"
    assert statement["metadata_status"] == "unavailable"
    why = " ".join(statement["reasons"])
    assert "Database evidence could not be collected (the database rejected the login)" in why
    assert statement["score"] == statement["static_score"]


def test_metadata_failure_with_a_plan_is_partial_evidence(mysql):
    mysql.metadata_error = mysql_error(1142, "SELECT command denied")
    data = local("SELECT * FROM users WHERE status = 'old'")
    statement = only(data)
    assert statement["evidence"]["plan"]["state"] == "collected"
    assert statement["evidence"]["metadata"] == {"state": "failed", "reason": "permission_denied", "tables": []}
    assert statement["evidence"]["overall"] == "partial"
    assert data["evidence_summary"]["partial"] is True
    # DATABASE_EVIDENCE only says a plan exists; the summary keeps the failure visible.
    assert data["analysis_mode"] == "DATABASE_EVIDENCE"
    assert data["evidence_summary"]["metadata"] == {"failed": 1}


def test_dml_with_failed_metadata_is_unavailable_not_metadata(mysql):
    mysql.metadata_error = mysql_error(1142, "SELECT command denied")
    data = local("UPDATE users SET active = 0 WHERE id = 1")
    assert only(data)["evidence"]["overall"] == "unavailable"
    assert data["analysis_mode"] == "STATIC_DATABASE_UNAVAILABLE"


def test_select_into_is_not_eligible_for_a_plan(mysql):
    data = local("SELECT * INTO @a FROM users")
    statement = only(data)
    assert statement["evidence"]["plan"] == {"state": "not_eligible", "reason": "plan_not_eligible"}
    assert statement["database_evidence"]["status"] == "plan_not_eligible"
    assert explains(mysql) == []
    assert_read_only_boundary(mysql)


SELECT_INTO_FORMS = [
    "SELECT * INTO @a FROM users",
    "SELECT id FROM users WHERE id = 1 INTO @a",
    "SELECT * INTO @a, @b FROM users",
    "SELECT * FROM users INTO OUTFILE '/tmp/x'",
    "SELECT * FROM users INTO DUMPFILE '/tmp/x'",
]


def assert_not_eligible_without_a_plan(statement):
    assert statement["statement_type"] == "SELECT"
    assert statement["evidence"]["plan"] == {"state": "not_eligible", "reason": "plan_not_eligible"}
    assert statement["database_evidence"]["status"] == "plan_not_eligible"
    assert statement["database_evidence"]["available"] is False
    assert statement["database_evidence"]["explain_available"] is False
    assert statement["database_evidence"]["access_type"] is None
    assert statement["evidence_factors"] == []
    assert statement["confidence"] == "HIGH"


@pytest.mark.parametrize("sql", SELECT_INTO_FORMS)
def test_every_select_into_form_is_not_eligible_locally_and_never_explained(mysql, sql):
    data = local(sql)
    assert_not_eligible_without_a_plan(only(data))
    assert data["analysis_mode"] != "DATABASE_EVIDENCE"
    assert explains(mysql) == []
    assert_read_only_boundary(mysql)


@pytest.mark.parametrize("sql", SELECT_INTO_FORMS)
def test_every_select_into_form_is_not_eligible_remotely_and_never_explained(monkeypatch, sql):
    server = remote.FakeServer(explain=[remote.explain_row("ALL", 500)])
    data = remote_data(monkeypatch, server, sql)
    assert_not_eligible_without_a_plan(only(data))
    assert data["analysis_mode"] != "DATABASE_EVIDENCE"
    assert not any(executed.upper().startswith("EXPLAIN") for executed, _ in server.executed)


def test_a_lossy_parse_marks_every_statement_of_the_script():
    from backend.parser import parse_sql

    assert [f.lossy_parse for f in parse_sql("SELECT * FROM users WHERE id = 1; UPDATE users SET a = 1")] == [
        False, False]
    assert [f.lossy_parse for f in parse_sql("SELECT * INTO @a FROM users")] == [False]
    assert [f.lossy_parse for f in parse_sql("SELECT id FROM users WHERE id = 1 INTO @a")] == [True]
    assert [f.lossy_parse for f in parse_sql("SELECT 1; SELECT * FROM users INTO OUTFILE '/x'")] == [True, True]


def test_a_select_in_a_lossy_script_is_not_planned(mysql):
    """The fallback can't vouch for any statement of the script, so none is planned."""
    data = local("SELECT * FROM users WHERE id = 1; SELECT * FROM users INTO OUTFILE '/tmp/x';")
    assert [s["evidence"]["plan"]["state"] for s in data["statements"]] == ["not_eligible", "not_eligible"]
    assert explains(mysql) == []


def test_local_plan_failure_with_metadata_is_partial_evidence(mysql):
    mysql.explain_error = mysql_error(1142, "SELECT command denied to user")
    data = local("SELECT * FROM users WHERE status = 'old'")
    statement = only(data)
    assert statement["evidence"]["plan"] == {"state": "failed", "reason": "insufficient_privileges"}
    assert statement["evidence"]["metadata"]["state"] == "collected"
    assert statement["evidence"]["overall"] == "partial"
    assert data["evidence_summary"]["partial"] is True
    assert data["analysis_mode"] == "STATIC_DATABASE_METADATA"
    assert statement["score"] == statement["static_score"]
    assert len(explains(mysql)) == 1


@pytest.mark.parametrize("sql,plan_state", [
    ("(SELECT id FROM users) UNION (SELECT id FROM orders)", "not_supported"),
    ("WITH a AS (SELECT 1 AS id) UPDATE users SET active = 0 WHERE id IN (SELECT id FROM a)",
     "not_collected_read_only"),
    ("UPDATE /*+ SET_VAR(transaction_read_only = OFF) */ users SET active = 0 WHERE id = 1",
     "not_collected_read_only"),
    ("UPDATE users SET active = 0 WHERE id = 1 /*! , name = 'x' */", "not_collected_read_only"),
    ("INSERT INTO users SELECT * FROM users", "not_supported"),
])
def test_unsafe_or_ambiguous_statements_are_never_planned(mysql, sql, plan_state):
    data = local(sql)
    assert explains(mysql) == []
    assert_read_only_boundary(mysql)
    for statement in data["statements"]:
        assert statement["evidence"]["plan"]["state"] == plan_state


def test_an_unknown_gap_is_never_closed_by_unrelated_evidence():
    from backend.evidence_state import UNKNOWN_GAP_REASON, restate_assessment
    from backend.models import EvidenceGap, minimal_assessment

    collected = {"connection": {"state": "connected", "reason": None},
                 "plan": {"state": "collected", "reason": None},
                 "metadata": {"state": "collected", "reason": None, "tables": []},
                 "overall": "collected"}
    assessment = minimal_assessment(0, confidence="HIGH")
    assessment.evidence_missing = [EvidenceGap(name="lock_wait_profile", reason="x")]
    restated = restate_assessment(assessment, collected)
    assert [(gap.name, gap.reason) for gap in restated.evidence_missing] == [
        ("lock_wait_profile", UNKNOWN_GAP_REASON)]
    assert "lock_wait_profile" not in restated.evidence_available


def test_error_1792_defensively_maps_to_the_read_only_plan_state(mysql):
    mysql.explain_error = mysql_error(1792, "Cannot execute statement in a READ ONLY transaction.")
    data = local("SELECT * FROM users WHERE id = 1")
    statement = only(data)
    assert statement["evidence"]["plan"] == {"state": "not_collected_read_only",
                                             "reason": "plan_not_collected_read_only"}
    assert statement["evidence"]["metadata"]["state"] == "collected"
    assert data["analysis_mode"] == "STATIC_DATABASE_METADATA"
    assert len(explains(mysql)) == 1 and len(mysql.connect_calls) == 1
    assert mysql.connections[0].close_calls == 1


def test_mixed_batch_keeps_per_statement_evidence_and_a_batch_summary(mysql):
    data = local("SELECT * FROM users WHERE id = 1; UPDATE users SET active = 0 WHERE id = 2; SELECT 1;")
    plans = [statement["evidence"]["plan"]["state"] for statement in data["statements"]]
    assert plans == ["collected", "not_collected_read_only", "not_supported"]
    assert data["analysis_mode"] == "DATABASE_EVIDENCE"
    assert data["evidence_summary"]["plan"] == {"collected": 1, "not_collected_read_only": 1, "not_supported": 1}
    assert data["evidence_summary"]["metadata"] == {"collected": 2, "not_applicable": 1}
    assert len(mysql.connect_calls) == 1 and mysql.connections[0].close_calls == 1


def test_connected_but_no_evidence_applies_is_not_called_unavailable(mysql):
    data = local("SELECT 1")
    statement = only(data)
    assert statement["evidence"]["plan"]["state"] == "not_supported"
    assert statement["evidence"]["metadata"]["state"] == "not_applicable"
    assert statement["evidence"]["overall"] == "not_applicable"
    assert data["analysis_mode"] == "STATIC"


def test_insert_with_metadata_is_not_called_unavailable(mysql):
    data = local("INSERT INTO users (id) VALUES (1)")
    statement = only(data)
    assert statement["evidence"]["plan"]["state"] == "not_supported"
    assert statement["evidence"]["metadata"]["state"] == "collected"
    assert data["analysis_mode"] == "STATIC_DATABASE_METADATA"


def test_a_missing_table_is_not_reported_as_found(mysql):
    real = mysql.rows_for

    def no_orders_table(sql, args, connection):
        if "information_schema.TABLES" in sql and args and args[1] == "orders":
            mysql.executed.append(sql.strip())
            return []
        return real(sql, args, connection)

    mysql.rows_for = no_orders_table
    statement = only(local("UPDATE orders SET active = 0 WHERE id = 1"))
    assert statement["evidence"]["metadata"]["tables"] == [{"name": "orders", "found": False}]
    assert "not found in the selected database: orders" in " ".join(statement["reasons"])


def test_missing_evidence_never_reduces_risk_or_changes_confidence(mysql):
    sqls = ["UPDATE users SET active = 0 WHERE status = 'pending'", "DELETE FROM users",
            "SELECT * FROM users WHERE id = 1", "UPDATE users SET active = 0 WHERE id = 1",
            "INSERT INTO users (id) VALUES (1)", "DROP TABLE users"]
    failures = [None, mysql_error(1045, "denied"), mysql_error(2003, "unreachable")]
    for sql in sqls:
        static = only(local(sql, username=None, password=None))
        for failure in failures:
            mysql.connect_error = failure
            statement = only(local(sql))
            assert statement["score"] >= static["score"], (sql, failure)
            assert statement["risk_level"] == static["risk_level"], (sql, failure)
            assert statement["confidence"] == static["confidence"], (sql, failure)
        mysql.connect_error = None


# ---------------------------------------------------------------- remote (connector) semantics


def remote_data(monkeypatch, server, sql, **kwargs):
    response, _ = remote.remote_validate(monkeypatch, server, sql, **kwargs)
    assert response.status_code == 200, response.text
    data = response.json()
    assert_coherent(data)
    assert remote.SECRET not in response.text and remote.USER not in response.text
    return data


def test_remote_select_has_the_same_evidence_meaning(monkeypatch):
    server = remote.FakeServer(explain=[remote.explain_row("const", 1, key="PRIMARY")])
    data = remote_data(monkeypatch, server, "SELECT * FROM users WHERE id = 1")
    statement = only(data)
    assert statement["evidence"]["plan"]["state"] == "collected"
    assert statement["evidence"]["metadata"]["tables"] == [{"name": "users", "found": True}]
    assert statement["confidence"] == "HIGH"
    assert data["analysis_mode"] == "DATABASE_EVIDENCE"


def test_remote_dml_keeps_its_plan_collection_and_reports_it(monkeypatch):
    """Remote DML plans are collected under the connector's own rules (unchanged)."""
    server = remote.FakeServer(explain=[remote.explain_row("ALL", 500, extra="Using where")], table_rows=250_000)
    data = remote_data(monkeypatch, server, "UPDATE users SET active = 0 WHERE status = 'old'")
    statement = only(data)
    assert any(sql.startswith("EXPLAIN UPDATE") for sql, _ in server.executed)
    assert statement["evidence"]["plan"]["state"] == "collected"
    assert statement["confidence"] == "LIMITED"
    assert statement["score"] > statement["static_score"]


def test_remote_without_a_database_reports_metadata_as_not_requested(monkeypatch):
    server = remote.FakeServer(explain=[remote.explain_row("const", 1, key="PRIMARY")])
    data = remote_data(monkeypatch, server, "UPDATE users SET active = 0 WHERE id = 5", database=None)
    statement = only(data)
    assert statement["evidence"]["metadata"] == {"state": "not_attempted", "reason": "no_database_selected",
                                                 "tables": []}
    assert statement["metadata_status"] == "unavailable"
    assert "no database was selected" in " ".join(statement["reasons"])


@pytest.mark.parametrize("sql,explain", [
    ("SELECT * FROM users WHERE id = 1", [remote.explain_row("const", 1, key="PRIMARY")]),
    ("SELECT * FROM users WHERE status = 'old'", [remote.explain_row("ALL", 500)]),
    ("INSERT INTO users (id) VALUES (1)", []),
])
def test_remote_and_local_evidence_representation_match(monkeypatch, sql, explain):
    """Where collection is the same on both paths, the evidence objects are identical."""
    server = remote.FakeServer(explain=explain)
    remote_json = remote_data(monkeypatch, server, sql)
    local_json = remote.local_validate_with(monkeypatch, remote.FakeServer(explain=explain), sql)
    assert_coherent(local_json)
    assert [s["evidence"] for s in remote_json["statements"]] == [s["evidence"] for s in local_json["statements"]]
    assert remote_json["evidence_summary"] == local_json["evidence_summary"]
    assert remote_json["analysis_mode"] == local_json["analysis_mode"]


def test_credentials_never_appear_in_the_evidence_fields(mysql):
    mysql.connect_error = mysql_error(1045, f"Access denied for user '{USER}' using password {PASSWORD}")
    response = post("SELECT * FROM users")
    assert_no_secrets(response.text)
    mysql.connect_error = None
    response = post("SELECT * FROM users")
    assert_no_secrets(response.text)
    assert DB in str(response.json()["statements"][0]["database_evidence"]["database"])
