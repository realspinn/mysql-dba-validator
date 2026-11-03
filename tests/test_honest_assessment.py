"""Track A: the assessment never claims more than the validator established.

- A1: a statement sqlglot reads only as a keyword plus unparsed text (exp.Command) is not
  analysed, except CALL, whose rule needs only the keyword.
- A2: KILL, SET of anything but user variables, and START statements that are not a
  transaction (START REPLICA) have no rules: not analysed, never LOW / HIGH.
- A3: Execution Risk is not assessed from the SQL alone; with a plan it says what the
  optimizer estimated, never a measured runtime.
- A4: the SQL shown is the statement as submitted; the parser's regeneration is used only
  for the analysis and the EXPLAIN request.

Static checks send no login, so no database connection is made; the evidence checks use
the fake MySQL from test_local_evidence_credentials.
"""

from __future__ import annotations

import logging
import pytest
from fastapi.testclient import TestClient

from backend.analyzer import OFFLINE_EXECUTION_REASON
from backend.db_evidence import _safe_explain_sql
from backend.main import app
from backend.parser import parse_sql
from tests.test_frontend_evidence_rendering import local_fixture
from tests.frontend_page import page_source
from tests.test_local_evidence_credentials import (  # noqa: F401  (mysql is a fixture)
    FakeMySQL,
    assert_read_only_boundary,
    explain_row,
    explains,
    mysql,
    post,
)

client = TestClient(app, base_url="http://127.0.0.1:8420")
NOT_ANALYSED_CHECK = "Do not execute a statement that the validator could not analyze."


def validate(sql: str) -> dict:
    response = client.post("/api/validate", json={"sql": sql})
    assert response.status_code == 200, response.text
    return response.json()


def only(sql: str) -> dict:
    data = validate(sql)
    assert data["statement_count"] == 1, data
    return data["statements"][0]


def dimension(statement: dict, name: str) -> dict:
    return next(d for d in statement["v2_assessment"]["dimensions"] if d["name"] == name)


def assert_not_analysed(statement: dict, status: str, statement_type: str) -> None:
    assert statement["analysis_status"] == status
    assert statement["parse_error"]
    assert (statement["score"], statement["risk_level"], statement["confidence"]) == (100, "CRITICAL", "LOW")
    assert statement["statement_type"] == statement_type and statement["category"] == "OTHER"
    assert NOT_ANALYSED_CHECK in statement["checklist"]
    assert statement["tables"] == [] and statement["analysis_text_differs"] is False


# ---------------------------------------------------------------- A1: unparsed commands

UNPARSED_COMMANDS = [
    ("PREPARE s FROM 'DELETE FROM orders'", "PREPARE"),
    ("EXECUTE s", "EXECUTE"),               # runs whatever was prepared, possibly a DELETE
    ("EXECUTE s USING @a", "EXECUTE"),
    ("LOCK TABLES orders WRITE", "LOCK"),
    ("UNLOCK TABLES", "UNLOCK"),
    ("RENAME TABLE orders TO orders_old", "RENAME"),
    ("OPTIMIZE TABLE orders", "OPTIMIZE"),
    ("CREATE USER app", "CREATE"),
    ("ALTER USER app IDENTIFIED BY 'x'", "ALTER"),
    ("DROP USER app", "DROP"),
    ("CREATE ROLE reporting", "CREATE"),
    ("SET ROLE reporting", "SET"),
    ("GRANT ALL ON *.* TO app WITH GRANT OPTION", "GRANT"),
    ("GRANT SELECT ON shop.orders TO 'app'@'%'", "GRANT"),
    ("REVOKE ALL PRIVILEGES, GRANT OPTION FROM app", "REVOKE"),
    # Its rule assumed a WHERE-less write on an unknown table ("may affect all rows").
    ("REPLACE INTO orders (id) VALUES (1)", "REPLACE"),
]


@pytest.mark.parametrize("sql, statement_type", UNPARSED_COMMANDS)
def test_a_statement_the_parser_reads_only_by_its_keyword_is_not_analysed(sql, statement_type):
    statement = only(sql)
    assert_not_analysed(statement, "unreadable", statement_type)
    assert "read only the statement's first keyword" in statement["parse_error"]
    assert statement["sql"] == sql


def test_call_keeps_its_own_rule():
    statement = only("CALL archive_orders(2024)")
    assert statement["analysis_status"] == "analysed" and statement["confidence"] == "LIMITED"
    assert "Stored procedure behavior cannot be fully determined from the CALL statement alone." in statement["reasons"]


def test_a_revoke_the_parser_reads_in_full_keeps_its_privilege_rule():
    # Not a blanket rejection: sqlglot reads this REVOKE as a statement, not a Command.
    statement = only("REVOKE SELECT ON shop.orders FROM app")
    assert statement["analysis_status"] == "analysed"
    assert any(f["title"] == "Privilege change" for f in statement["v2_assessment"]["findings"])


def test_a_batch_with_an_unparsed_command_is_never_fully_assessed():
    data = validate("LOCK TABLES orders WRITE; SELECT * FROM orders WHERE id = 1; UNLOCK TABLES")
    lock, select, unlock = data["statements"]
    assert [s["analysis_status"] for s in data["statements"]] == ["unreadable", "analysed", "unreadable"]
    assert (data["overall_risk_level"], data["overall_confidence"]) == ("CRITICAL", "LOW")
    assert (lock["sql"], unlock["sql"]) == ("LOCK TABLES orders WRITE", "UNLOCK TABLES")
    assert select["risk_level"] == "LOW" and select["sql"] == "SELECT * FROM orders WHERE id = 1"


@pytest.mark.parametrize("sql", ["EXECUTE s", "LOCK TABLES users WRITE", "GRANT ALL ON *.* TO app",
                                 "SET GLOBAL read_only = 0", "KILL 42", "START REPLICA"])
def test_a_statement_that_is_not_analysed_sends_nothing_to_the_database(mysql, sql):
    statement = post(sql).json()["statements"][0]
    assert statement["analysis_status"] != "analysed"
    assert statement["evidence"]["plan"]["state"] != "collected"
    assert explains(mysql) == []
    assert_read_only_boundary(mysql)


# ---------------------------------------------------------------- A2: statements without rules

@pytest.mark.parametrize("sql", ["KILL 42", "KILL QUERY 42", "KILL CONNECTION 42"])
def test_kill_is_not_analysed(sql):
    assert_not_analysed(only(sql), "unsupported", "KILL")


@pytest.mark.parametrize("sql", [
    "SET GLOBAL read_only = 0",
    "SET PERSIST max_connections = 10",
    "SET @@GLOBAL.read_only = 0",
    "SET SESSION sql_mode = ''",
    "SET sql_mode = 'ANSI_QUOTES'",
    "SET autocommit = 0",
    "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE",
    "SET NAMES utf8mb4",
    "SET foreign_key_checks = 0",
    "SET @@session.autocommit = 0",
    # A user variable, but from an unreviewed function or a locking read.
    "SET @a = my_func()",
    "SET @a = (SELECT id FROM orders WHERE id = 1 FOR UPDATE)",
    # One system variable among user variables.
    "SET @a = 1, sql_mode = ''",
])
def test_a_set_of_anything_but_a_user_variable_is_not_analysed(sql):
    statement = only(sql)
    assert_not_analysed(statement, "unsupported", "SET")
    assert "GLOBAL or PERSIST settings affect the whole server" in statement["parse_error"]


@pytest.mark.parametrize("sql", ["SET @a = 1", "SET @a := 1", "SET @a = 1, @b = 'x'",
                                 "SET @n = (SELECT COUNT(*) FROM orders)"])
def test_a_set_of_user_variables_keeps_its_assessment(sql):
    statement = only(sql)
    assert statement["analysis_status"] == "analysed" and statement["statement_type"] == "SET"
    assert (statement["risk_level"], statement["confidence"]) == ("LOW", "HIGH")


@pytest.mark.parametrize("sql", ["START REPLICA", "START SLAVE", "START GROUP_REPLICATION",
                                 "START REPLICA IO_THREAD", "START REPLICA UNTIL SQL_AFTER_MTS_GAPS"])
def test_start_replica_is_not_a_transaction(sql):
    statement = only(sql)
    assert_not_analysed(statement, "unreadable", "START")
    assert statement["category"] != "TRANSACTION"
    assert "not a transaction" not in statement["sql"] and statement["sql"] == sql


@pytest.mark.parametrize("sql", ["START TRANSACTION", "START TRANSACTION READ ONLY", "BEGIN", "BEGIN WORK"])
def test_a_real_transaction_start_is_still_a_transaction(sql):
    statement = only(sql)
    assert statement["analysis_status"] == "analysed" and statement["category"] == "TRANSACTION"


@pytest.mark.parametrize("sql", ["STOP REPLICA", "RESET MASTER", "RESET PERSIST"])
def test_other_replication_and_reset_statements_stay_not_analysed(sql):
    assert only(sql)["analysis_status"] == "unreadable"


def test_a_server_setting_in_a_batch_makes_it_incomplete():
    data = validate("SET GLOBAL read_only = 0; UPDATE orders SET status = 'x' WHERE id = 1")
    assert [s["analysis_status"] for s in data["statements"]] == ["unsupported", "analysed"]
    assert (data["overall_risk_level"], data["overall_confidence"]) == ("CRITICAL", "LOW")


# ---------------------------------------------------------------- A3: execution risk

@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders",
    "SELECT * FROM orders WHERE id = 1",
    "SELECT * FROM orders, customers",          # a Cartesian product
    "SELECT * FROM orders CROSS JOIN customers",
    "SELECT my_func(id) FROM orders",           # LIMITED confidence
    "DROP TABLE orders",                        # HIGH confidence, never planned
    "CREATE TABLE t (id INT)",
])
def test_without_a_plan_execution_risk_is_not_assessed(sql):
    statement = only(sql)
    execution = dimension(statement, "Execution Risk")
    assert execution == {"name": "Execution Risk", "level": "UNKNOWN", "reason": OFFLINE_EXECUTION_REASON}
    assert "low execution risk" not in str(statement).lower()


def test_a_locking_read_keeps_its_execution_finding():
    execution = dimension(only("SELECT * FROM orders WHERE id = 1 FOR UPDATE"), "Execution Risk")
    assert execution["level"] == "MEDIUM" and execution["reason"].startswith("Locking read")


def test_a_plan_estimating_a_large_scan_is_reported_as_an_estimate():
    data = local_fixture("SELECT * FROM users ORDER BY name",
                         FakeMySQL(explain=[explain_row("ALL", 1_902_192, extra="Using filesort")]))
    statement = data["statements"][0]
    assert statement["evidence"]["plan"]["state"] == "collected"
    execution = dimension(statement, "Execution Risk")
    assert execution["level"] == "MEDIUM"
    assert "a full table scan and about 1,902,192 rows examined" in execution["reason"]
    assert "estimates, not a measured runtime" in execution["reason"]
    assert "offline" not in execution["reason"].lower()
    # The score is the existing evidence scoring, unchanged.
    assert [f["label"] for f in statement["evidence_factors"]] == [
        "EXPLAIN indicates a full table scan", "EXPLAIN estimated rows are high"]


def test_a_plan_without_a_large_scan_is_not_presented_as_low_execution_risk():
    data = local_fixture("SELECT * FROM users WHERE id = 1",
                         FakeMySQL(explain=[explain_row("const", 1, key="PRIMARY")]))
    statement = data["statements"][0]
    assert statement["evidence"]["plan"]["state"] == "collected"
    execution = dimension(statement, "Execution Risk")
    assert execution["level"] == "UNKNOWN"
    assert execution["reason"] == "Execution plan collected (optimizer estimates, not runtime behaviour)."


def test_the_page_makes_no_execution_claim_of_its_own():
    # The page shows findings, not the dimensions; it has no execution-risk wording.
    page = page_source("index.html").lower()
    assert "low execution risk" not in page and "execution risk" not in page


# ---------------------------------------------------------------- A4: SQL shown as submitted

SUBMITTED_AS_IS = [
    "SELECT * FROM t WHERE name REGEXP '^a'",               # REGEXP_LIKE(name, '^a')
    "SELECT * FROM orders WHERE id = 1 LOCK IN SHARE MODE",  # FOR SHARE
    "SELECT * FROM orders ORDER BY id LIMIT 20, 10",         # LIMIT 10 OFFSET 20
    "SELECT DISTINCTROW status FROM orders",                 # DISTINCT
    "EXPLAIN SELECT * FROM orders WHERE id = 1",             # DESCRIBE
    "SELECT 'a' 'b'",                                        # CONCAT('a', 'b')
    "SELECT * FROM users WHERE id NOT IN (SELECT user_id FROM orders)",
    "select  ID\n  from   orders o /* note */ where o.id = 1",
    "UPDATE accounts SET balance = balance--1 WHERE id = 1",  # read as balance - -1
]


@pytest.mark.parametrize("sql", SUBMITTED_AS_IS)
def test_the_sql_is_shown_exactly_as_submitted(sql):
    statement = only(sql)
    assert statement["analysis_status"] == "analysed"
    assert statement["sql"] == sql and statement["analysis_text_differs"] is False


def test_each_statement_of_a_batch_is_shown_as_submitted():
    sql = "select 1;\n  SELECT * FROM t WHERE a REGEXP 'x' ;SELECT DISTINCTROW b FROM t"
    shown = [s["sql"] for s in validate(sql)["statements"]]
    assert shown == ["select 1", "SELECT * FROM t WHERE a REGEXP 'x'", "SELECT DISTINCTROW b FROM t"]


def test_the_analysis_still_reads_the_statement():
    # LOCK IN SHARE MODE is shown as written and still assessed as a locking read.
    statement = only("SELECT * FROM orders WHERE id = 1 LOCK IN SHARE MODE")
    assert statement["confidence"] == "LIMITED" and statement["score"] >= 20


def test_a_statement_that_is_not_analysed_is_also_shown_exactly_as_submitted():
    sql = "SELECT balance--1 FROM accounts INTO @x"
    statement = only(sql)
    assert statement["analysis_status"] == "unreadable" and statement["sql"] == sql


@pytest.mark.parametrize("sql", ["SELECT * FROM users WHERE name REGEXP '^a'",
                                 "SELECT * FROM users ORDER BY id LIMIT 20, 10"])
def test_the_explain_request_is_unchanged_by_the_display(mysql, sql):
    statement = post(sql).json()["statements"][0]
    assert statement["sql"] == sql
    # The EXPLAIN sent is built from the parser's regeneration, as before.
    assert explains(mysql) == [_safe_explain_sql(parse_sql(sql)[0].raw_sql)]
    assert_read_only_boundary(mysql)


# ---------------------------------------------------------------- logging

def test_validation_logs_no_sql_and_no_login(mysql, caplog):
    secret_sql = "SELECT secret_salary FROM payroll WHERE ssn = '123-45-6789'"
    with caplog.at_level(logging.DEBUG):
        post(secret_sql)
        post("EXECUTE hidden_stmt")
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for value in ("secret_salary", "123-45-6789", "hidden_stmt", "payroll"):
        assert value not in logged
    from tests.test_local_evidence_credentials import PASSWORD
    assert PASSWORD not in logged
