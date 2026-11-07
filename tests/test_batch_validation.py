"""Batch validation: 1 to 16 statements through /api/validate, offline.

No login is sent, so no database connection is made (static analysis only). Checks the
statement limit, statement integrity (count, order, identifiers, the SQL each result
belongs to), the batch aggregate (highest score, weakest confidence) and how comments,
quoted semicolons, duplicates and malformed SQL are handled.
"""

from __future__ import annotations

import pytest
import sqlglot
from fastapi.testclient import TestClient

from backend.main import app
from backend.validation_results import MAX_STATEMENTS

client = TestClient(app, base_url="http://127.0.0.1:8420")


def validate(sql: str) -> dict:
    response = client.post("/api/validate", json={"sql": sql})
    assert response.status_code == 200, response.text
    return response.json()


INSERTS = [f"INSERT INTO orders (id, status) VALUES ({i}, 'new')" for i in range(1, 7)]
SELECTS = [f"SELECT * FROM orders WHERE id = {i}" for i in range(1, 7)]
UPDATES = [f"UPDATE orders SET status = 'x' WHERE id = {i}" for i in range(1, 7)]
DELETES = [f"DELETE FROM orders WHERE id = {i}" for i in range(1, 7)]

MIXED_16 = [
    INSERTS[0], SELECTS[0], UPDATES[0], DELETES[0], INSERTS[1], "SELECT COUNT(*) FROM orders",
    UPDATES[1], DELETES[1], "UPDATE orders SET status = 'x' WHERE created_at < '2024-01-01'",
    "SELECT * FROM orders", "DELETE FROM orders WHERE status = 'old'", INSERTS[2], SELECTS[2],
    "UPDATE orders SET status = 'y' WHERE id > 0", DELETES[2],
    "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.id = 5",
]


def test_the_limit_is_sixteen_statements():
    assert MAX_STATEMENTS == 16 and len(MIXED_16) == 16


@pytest.mark.parametrize("name, statements, statement_type", [
    ("inserts", INSERTS, "INSERT"), ("selects", SELECTS, "SELECT"),
    ("updates", UPDATES, "UPDATE"), ("deletes", DELETES, "DELETE"),
])
def test_six_statements_of_one_type_each_get_their_own_result(name, statements, statement_type):
    data = validate("; ".join(statements))
    assert data["statement_count"] == 6
    assert [s["index"] for s in data["statements"]] == list(range(6))
    assert [s["statement_type"] for s in data["statements"]] == [statement_type] * 6
    # Each result is the statement submitted at that position (they differ only by id).
    for i, statement in enumerate(data["statements"], 1):
        assert f"{i}" in statement["sql"], (name, statement["sql"])


def test_exactly_sixteen_mixed_statements_are_all_returned_in_order():
    data = validate("; ".join(MIXED_16))
    statements = data["statements"]
    assert data["statement_count"] == len(statements) == 16
    assert [s["index"] for s in statements] == list(range(16))
    assert [s["statement_type"] for s in statements] == [sql.split()[0].upper() for sql in MIXED_16]
    assert {"INSERT", "SELECT", "UPDATE", "DELETE"} <= {s["statement_type"] for s in statements}
    assert len({s["sql"] for s in statements}) == 16  # none merged or counted twice
    # The batch is as risky as its riskiest statement, and as confident as its least confident.
    assert data["overall_score"] == max(s["score"] for s in statements)
    worst = max(statements, key=lambda s: s["score"])
    assert worst["sql"].startswith("UPDATE orders SET status = 'y' WHERE id > 0")
    assert data["overall_risk_level"] == worst["risk_level"]
    order = {"LOW": 0, "LIMITED": 1, "HIGH": 2}
    assert data["overall_confidence"] == min((s["confidence"] for s in statements), key=order.get)


def test_seventeen_statements_are_rejected_as_a_whole():
    assert validate("; ".join(MIXED_16 + [SELECTS[1]])) == {"error": "statement_limit_exceeded"}


def test_comments_and_empty_fragments_are_not_statements():
    # Regression: a comment before a semicolon was returned as a "SEMICOLON" statement
    # (scored MEDIUM) and counted toward the 16-statement limit.
    data = validate("-- note\n;; SELECT 1; /* block */ ;\n# hash\nSELECT * FROM orders WHERE id = 1;;")
    assert [s["statement_type"] for s in data["statements"]] == ["SELECT", "SELECT"]
    assert data["overall_risk_level"] == "LOW"
    padded = ";\n-- spacer\n;".join(MIXED_16)
    assert validate(padded)["statement_count"] == 16


@pytest.mark.parametrize("position", [0, 7, 15])
def test_the_riskiest_statement_sets_the_batch_risk_wherever_it_is(position):
    statements = [SELECTS[i % 6] for i in range(16)]
    statements[position] = "DELETE FROM orders"
    data = validate("; ".join(statements))
    worst = max(data["statements"], key=lambda s: s["score"])
    assert worst["index"] == position and worst["statement_type"] == "DELETE"
    assert (data["overall_score"], data["overall_risk_level"]) == (worst["score"], "CRITICAL")
    # The unrestricted delete's finding stays on that statement only.
    for statement in data["statements"]:
        has_all_rows = statement["scope"] is not None and statement["scope"].get("basis") == "all_rows"
        assert has_all_rows is (statement["index"] == position)


def test_semicolons_inside_string_literals_do_not_split_statements():
    data = validate("SELECT * FROM orders WHERE status = 'a;b'; UPDATE orders SET note = 'x;y' WHERE id = 1")
    assert [s["statement_type"] for s in data["statements"]] == ["SELECT", "UPDATE"]
    assert "'a;b'" in data["statements"][0]["sql"] and "'x;y'" in data["statements"][1]["sql"]


def test_duplicate_statements_are_each_reported():
    data = validate("; ".join([DELETES[0]] * 3))
    assert data["statement_count"] == 3 and [s["index"] for s in data["statements"]] == [0, 1, 2]


def test_malformed_sql_is_not_classified_as_a_statement():
    # Regression: "SELEC * FRM orders" was read as an expression and scored LOW with HIGH
    # confidence as statement type "ALIAS".
    statement = validate("SELEC * FRM orders")["statements"][0]
    assert statement["statement_type"] == "UNKNOWN"
    # MySQL rejects a statement that does not begin with a statement keyword.
    assert statement["analysis_status"] == "invalid"
    assert statement["parse_error"] == 'a MySQL statement cannot begin with "SELEC"'
    assert (statement["risk_level"], statement["confidence"]) == ("CRITICAL", "LOW")
    assert "Do not execute a statement that the validator could not analyze." in statement["checklist"]


def test_one_malformed_statement_keeps_the_others_and_lowers_batch_confidence():
    data = validate("SELECT * FROM orders WHERE id = 1; UPDAT orders SET x = 1; DELETE FROM orders WHERE id = 2")
    types = [s["statement_type"] for s in data["statements"]]
    assert types == ["SELECT", "UNKNOWN", "DELETE"]
    assert data["overall_risk_level"] == "CRITICAL" and data["overall_confidence"] == "LOW"
    # The other statements were each read in full on their own: their own confidence.
    assert [s["confidence"] for s in data["statements"]] == ["HIGH", "LOW", "LIMITED"]
    assert not any(s["analysis_text_differs"] for s in data["statements"])


def test_an_empty_parenthesised_where_does_not_crash():
    # Regression: an AttributeError in the predicate classifier returned HTTP 500.
    statement = validate("SELECT 1 FROM t WHERE (((")["statements"][0]
    assert statement["confidence"] == "LOW"


# Completeness: a statement is analysed only when the strict parser read all of its text.
# Text the parser cannot read is never dropped while the rest is scored as the statement.

def strictly_readable(sql: str) -> bool:
    try:
        sqlglot.parse(sql, read="mysql", error_level=sqlglot.ErrorLevel.RAISE)
    except Exception:
        return False
    return True


def assert_not_analysed(statement: dict, submitted: str):
    assert statement["analysis_status"] in ("invalid", "unreadable")
    assert statement["parse_error"]
    # The submitted text, not the parser's reading of it.
    assert statement["sql"] == submitted and statement["analysis_text_differs"] is False
    assert (statement["score"], statement["risk_level"], statement["confidence"]) == (100, "CRITICAL", "LOW")


@pytest.mark.parametrize("sql", [
    "SELECT 1 DROP TABLE t",                          # a missing ";" before a write
    "SELECT * FROM orders WHERE id = 1 DELETE FROM orders",
    "SELECT * FROM orders WHERE id = 1 ) DELETE FROM orders",
    "DELETE FROM orders WHERE",                       # truncated
    "DELETE FROM orders WHERE id = 5 OR",
    "UPDATE orders SET status = 'x' WHERE id IN (1, 2",
])
def test_sql_that_cannot_be_read_in_full_is_not_analysed(sql):
    data = validate(sql)
    assert data["statement_count"] == 1
    assert_not_analysed(data["statements"][0], sql)
    assert (data["overall_score"], data["overall_risk_level"], data["overall_confidence"]) == (100, "CRITICAL", "LOW")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders INTO OUTFILE '/tmp/x'",     # writes a file on the server host
    "SELECT id FROM orders WHERE id = 1 INTO @a",
    "LOAD DATA INFILE '/tmp/x' REPLACE INTO TABLE orders",
    "DROP TABLE IF EXISTS a, b",
])
def test_valid_mysql_the_parser_reads_only_partly_is_not_analysed(sql):
    # Valid MySQL that this sqlglot cannot read in full: never scored from the part it read
    # (a SELECT without its INTO OUTFILE, a DROP of only the first table).
    statement = validate(sql)["statements"][0]
    if strictly_readable(sql):
        # A newer sqlglot may read it in full; it is then analysed as written.
        assert statement["analysis_status"] == "analysed" and statement["analysis_text_differs"] is False
        if sql.startswith("DROP TABLE"):
            # Every target is named, never only the first (sqlglot 30.21.0 reads this in full).
            assert statement["tables"] == ["a", "b"]
    else:
        assert_not_analysed(statement, sql)


GOOD = ["SELECT * FROM orders WHERE id = 1", "DELETE FROM orders WHERE id = 2"]


@pytest.mark.parametrize("bad", ["SELECT 1 DROP TABLE t", "DELETE FROM orders WHERE", "foo bar baz"])
@pytest.mark.parametrize("position", [0, 1, 2])
def test_an_unreadable_statement_anywhere_keeps_its_place_and_the_batch_is_not_reassuring(bad, position):
    statements = list(GOOD)
    statements.insert(position, bad)
    data = validate("; ".join(statements))
    assert data["statement_count"] == 3
    assert [s["sql"] for s in data["statements"]] == statements
    for index, statement in enumerate(data["statements"]):
        if index == position:
            assert_not_analysed(statement, bad)
        else:
            # Read in full on its own: analysed, with its own confidence.
            assert statement["analysis_status"] == "analysed" and statement["analysis_text_differs"] is False
            assert statement["confidence"] in ("HIGH", "LIMITED")
    assert (data["overall_score"], data["overall_risk_level"], data["overall_confidence"]) == (100, "CRITICAL", "LOW")


def test_statements_that_cannot_be_attributed_fail_closed_as_one_unreadable_result():
    # An unterminated string: the script cannot be split into statements reliably.
    sql = "SELECT * FROM orders WHERE id = 1; SELECT 'abc; DROP TABLE t"
    data = validate(sql)
    assert data["statement_count"] == 1
    assert_not_analysed(data["statements"][0], sql)


def test_no_statement_is_ever_scored_from_a_partial_reading():
    from backend.parser import parse_sql

    for sql in ["SELECT 1 DROP TABLE t", "SELECT 1; DELETE FROM orders WHERE",
                "SELECT * FROM orders INTO OUTFILE '/x'; SELECT 1", "BEGIN DELETE FROM t; END"]:
        assert not any(facts.lossy_parse for facts in parse_sql(sql)), sql


@pytest.mark.parametrize("sql", ["", "   \n\t ", "-- note\n/* block */ ;\n# hash"])
def test_input_without_statements_is_refused(sql):
    # Contract: nothing to assess is an error, never a LOW risk or an invented confidence
    # (it used to return 0 statements with overall risk LOW).
    assert validate(sql) == {"error": "no_statements"}
