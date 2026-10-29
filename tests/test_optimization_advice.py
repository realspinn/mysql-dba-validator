"""B1: optimization advice for an analysed SELECT, advisory only.

- Four rules, each an observation from the SQL alone ("basis": "sql"): SELECT * over base
  tables, a LIKE pattern that starts with a wildcard, a filtered column inside a function or
  calculation, ORDER BY RAND(). Index advice needs a single base table; every rule has its
  preconditions.
- Statements that were not analysed, and anything but a single non-locking SELECT, get no
  advice.
- The advice never changes the rest of the result or what is sent to the database; it
  rewrites no SQL, runs nothing and has no network access.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.main
from backend.main import app
from tests.test_frontend_evidence_rendering import local_fixture
from tests.test_local_evidence_credentials import FakeMySQL, explain_row

client = TestClient(app, base_url="http://127.0.0.1:8420")
ADVISER = Path(__file__).resolve().parent.parent / "backend" / "optimization.py"
FIELDS = {"code", "basis", "title", "observation", "action", "reasoning", "caveats", "verify"}


def validate(sql: str) -> dict:
    response = client.post("/api/validate", json={"sql": sql})
    assert response.status_code == 200, response.text
    return response.json()


def advice(sql: str) -> dict:
    data = validate(sql)
    assert data["statement_count"] == 1, data
    return data["statements"][0]["optimization"]


def codes(sql: str) -> list[str]:
    return [item["code"] for item in advice(sql)["recommendations"]]


# ---------------------------------------------------------------- the rules

ADVISED = [
    ("SELECT id FROM users WHERE name LIKE '%son'", ["leading_wildcard_like"]),
    ("SELECT id FROM users WHERE name LIKE '_a%' AND id > 5", ["leading_wildcard_like"]),
    ("SELECT id FROM users WHERE (name LIKE '%x%' OR email LIKE '%@x.com')", ["leading_wildcard_like"]),
    ("SELECT id FROM orders WHERE YEAR(created_at) = 2024", ["column_in_expression"]),
    ("SELECT id FROM orders WHERE 2024 = YEAR(created_at)", ["column_in_expression"]),
    ("SELECT id FROM orders WHERE DATE(created_at) = '2024-01-01'", ["column_in_expression"]),
    ("SELECT id FROM users WHERE LOWER(email) = 'a@b.com'", ["column_in_expression"]),
    ("SELECT id FROM orders WHERE total * 1.2 > 100", ["column_in_expression"]),
    ("SELECT id FROM orders WHERE created_at + INTERVAL 1 DAY > NOW()", ["column_in_expression"]),
    ("SELECT id FROM orders WHERE YEAR(created_at) BETWEEN 2020 AND 2024", ["column_in_expression"]),
    ("SELECT id FROM orders WHERE MONTH(created_at) IN (1, 2)", ["column_in_expression"]),
    ("SELECT id FROM users WHERE UPPER(name) LIKE 'A%'", ["column_in_expression"]),
    ("SELECT id FROM docs WHERE doc->>'$.a' = 'x'", ["column_in_expression"]),
    ("SELECT id FROM users ORDER BY RAND() LIMIT 1", ["order_by_rand"]),
    ("SELECT id FROM users ORDER BY name, RAND(42)", ["order_by_rand"]),
    # ORDER BY RAND() needs no single table: it is about the sort, not a column's index.
    ("SELECT u.id FROM users u JOIN orders o ON o.user_id = u.id ORDER BY RAND() LIMIT 5", ["order_by_rand"]),
    ("SELECT id FROM users WHERE name LIKE '%a' AND YEAR(born) = 1990 ORDER BY RAND()",
     ["leading_wildcard_like", "column_in_expression", "order_by_rand"]),
    # SELECT * is about the columns returned, so joins of base tables are included.
    ("SELECT * FROM users WHERE id = 1", ["select_star"]),
    ("SELECT u.* FROM users u", ["select_star"]),
    ("SELECT o.id, u.* FROM users u JOIN orders o ON o.user_id = u.id", ["select_star"]),
    ("SELECT * FROM users, orders", ["select_star"]),
    ("SELECT `u`.* FROM shop.users AS `u`", ["select_star"]),
    # The sort is over the rows JSON_TABLE produces, so ORDER BY RAND() still applies.
    ("SELECT a FROM JSON_TABLE('[1]', '$[*]' COLUMNS (a INT PATH '$')) jt ORDER BY RAND()", ["order_by_rand"]),
    ("SELECT * FROM users WHERE name LIKE '%a' ORDER BY RAND()",
     ["select_star", "leading_wildcard_like", "order_by_rand"]),
    # MATCH ... AGAINST is left alone, other calculations beside it are not.
    ("SELECT id FROM docs WHERE MATCH(title) AGAINST('x') > 0 AND YEAR(d) = 2024", ["column_in_expression"]),
]


@pytest.mark.parametrize("sql,expected", ADVISED)
def test_a_rule_reports_the_pattern_it_finds(sql, expected):
    result = advice(sql)
    assert result["status"] == "advised"
    assert codes(sql) == expected


NONE_FOUND = [
    "SELECT id FROM users WHERE name LIKE 'son%'",               # anchored: can use an index
    "SELECT id FROM users WHERE name LIKE '%'",                  # wildcards only
    "SELECT id FROM users WHERE name NOT LIKE '%son'",           # NOT LIKE
    "SELECT id FROM users WHERE NOT (name LIKE '%son')",
    "SELECT id FROM users WHERE name LIKE '!%son' ESCAPE '!'",   # an escaped first character
    "SELECT id FROM users WHERE name LIKE '\\%son'",
    "SELECT id FROM users WHERE name LIKE CONCAT('%', 'son')",   # not a constant string
    "SELECT name LIKE '%a' FROM users",                          # not a filter
    "SELECT id FROM orders WHERE created_at > NOW() - INTERVAL 1 DAY",
    "SELECT id FROM orders WHERE created_at >= '2024-01-01' AND created_at < '2025-01-01'",
    "SELECT id FROM orders WHERE created_at = DATE('2024-01-01')",
    "SELECT id FROM orders WHERE status = UPPER('open')",        # the calculation is on the constant
    "SELECT id FROM orders WHERE total * 2 > discount",          # not compared with a constant
    "SELECT id FROM orders WHERE YEAR(created_at) BETWEEN start_year AND 2024",
    "SELECT id FROM orders WHERE (id) = 1",
    "SELECT id FROM orders WHERE status IN ('a', 'b') AND deleted_at IS NULL",
    "SELECT YEAR(created_at) FROM orders WHERE id = 1",
    "SELECT id, RAND() AS r FROM users ORDER BY id",
    "SELECT 1 ORDER BY RAND()",                                  # no table: one row, nothing to sort
    "SELECT 1 FROM DUAL ORDER BY RAND()",
    "SELECT * FROM `dual` ORDER BY RAND()",                      # reads like DUAL once regenerated
    "SELECT 1",
    # A FULLTEXT search: neither the bare column nor an index on the expression applies.
    "SELECT id FROM docs WHERE MATCH(title) AGAINST('x')",
    "SELECT id FROM docs WHERE MATCH(title) AGAINST('x' IN BOOLEAN MODE) > 0",
    # Not the outer SELECT list over base tables.
    "SELECT COUNT(*) FROM users",
    "SELECT id FROM users WHERE EXISTS (SELECT * FROM orders o WHERE o.user_id = users.id)",
    "SELECT * FROM (SELECT id, name FROM users) x",
    "WITH x AS (SELECT id FROM users) SELECT * FROM x",
    "SELECT * FROM users u JOIN (SELECT user_id FROM orders) o ON o.user_id = u.id",
    "SELECT `*` FROM t",                                         # a column named *
    "SELECT t.`*` FROM t",
    # JSON_TABLE is a table function: its columns are declared in the query and have no index.
    "SELECT * FROM JSON_TABLE('[1]', '$[*]' COLUMNS (a INT PATH '$')) jt",
    "SELECT a FROM JSON_TABLE('[\"x\"]', '$[*]' COLUMNS (a VARCHAR(9) PATH '$')) jt WHERE a LIKE '%x'",
    "SELECT a FROM JSON_TABLE('[1]', '$[*]' COLUMNS (a INT PATH '$')) jt WHERE a + 1 > 2",
]


@pytest.mark.parametrize("sql", NONE_FOUND)
def test_no_advice_without_the_pattern(sql):
    assert advice(sql) == {"status": "none_found", "recommendations": []}


# The column's table is known only when the query reads one base table.
@pytest.mark.parametrize("sql", [
    "SELECT u.id FROM users u JOIN orders o ON o.user_id = u.id WHERE u.name LIKE '%son'",
    "SELECT id FROM users, orders WHERE name LIKE '%a'",
    "SELECT id FROM users WHERE name LIKE '%son' AND id IN (SELECT user_id FROM orders)",
    "WITH x AS (SELECT * FROM users) SELECT id FROM x WHERE YEAR(born) = 1990",
    "SELECT id FROM (SELECT * FROM users) x WHERE name LIKE '%a'",
])
def test_index_advice_needs_a_single_base_table(sql):
    assert advice(sql)["status"] == "none_found"


def test_each_recommendation_explains_itself_without_claiming_a_speed_up():
    sql = "SELECT * FROM users WHERE name LIKE '%a' AND YEAR(born) = 1990 ORDER BY RAND()"
    items = advice(sql)["recommendations"]
    assert len(items) == 4
    for item in items:
        assert set(item) == FIELDS  # no rewritten SQL, no measured result
        assert item["basis"] == "sql"
        assert all(item[key] for key in FIELDS) and len(item["caveats"]) >= 2
        assert "MySQL 8.4 manual" in item["reasoning"]
        words = " ".join(str(value) for value in item.values()).lower()
        for claim in ("faster", "will improve", "guarantee", "definitely", " safe", "is slow"):
            assert claim not in words, (item["code"], claim)


def test_order_by_rand_follows_the_manual_on_filesort_with_limit():
    # LIMIT Query Optimization: "all rows that match the query without the LIMIT clause are
    # selected, and most or all of them are sorted": not every row is always sorted.
    item = advice("SELECT id FROM users ORDER BY RAND() LIMIT 1")["recommendations"][0]
    assert "are selected, and most or all of them are sorted, before LIMIT applies" in item["reasoning"]


def test_the_observation_names_only_columns_from_the_sql():
    item = advice("SELECT id FROM users WHERE (name LIKE '%x%' OR email LIKE '%@x.com')")["recommendations"][0]
    assert item["observation"] == "A LIKE pattern for `email`, `name` starts with a wildcard (% or _)."


@pytest.mark.parametrize("sql,names", [
    ("SELECT id FROM people WHERE CONCAT(first_name, ' ', last_name) = 'Ann Lee'", "`first_name`, `last_name`"),
    ("SELECT id FROM orders WHERE price + tax > 100", "`price`, `tax`"),
    ("SELECT id FROM orders WHERE COALESCE(shipped_at, created_at) > '2024-01-01'", "`created_at`, `shipped_at`"),
])
def test_every_column_inside_the_calculation_is_named(sql, names):
    item = advice(sql)["recommendations"][0]
    assert item["observation"] == (f"The WHERE clause compares {names} inside a function or "
                                   "calculation, not as the bare column.")


# ---------------------------------------------------------------- eligibility

@pytest.mark.parametrize("sql,reason", [
    ("SELECT * FROM users INTO OUTFILE '/tmp/x'", "not analysed"),
    ("SELECT id FROM users WHERE name LIKE '%a' INTO @x", "not analysed"),
    ("SET GLOBAL read_only = 0", "not analysed"),
    ("GRANT ALL ON *.* TO app", "not analysed"),
    ("SELEC id FROM users", "not analysed"),
    ("VALUES ROW(1)", "not analysed"),
    ("SELECT id FROM users WHERE name LIKE '%a' FOR UPDATE", "locking read"),
    ("SELECT id FROM users WHERE name LIKE '%a' UNION SELECT id FROM admins", "SELECT statements only"),
    ("EXPLAIN SELECT id FROM users WHERE name LIKE '%a'", "SELECT statements only"),
    ("(SELECT id FROM users WHERE name LIKE '%a')", "SELECT statements only"),
    ("UPDATE users SET a = 1 WHERE name LIKE '%a'", "SELECT statements only"),
    ("DELETE FROM users WHERE YEAR(born) = 1990", "SELECT statements only"),
])
def test_only_an_analysed_non_locking_select_is_checked(sql, reason):
    result = advice(sql)
    assert result["status"] == "not_eligible" and result["recommendations"] == []
    assert reason in result["reason"]


def test_an_incomplete_batch_stays_incomplete_beside_the_advice():
    data = validate("SELECT id FROM users WHERE name LIKE '%a'; SELEC x")
    first, second = data["statements"]
    assert first["optimization"]["status"] == "advised"
    assert second["optimization"]["status"] == "not_eligible"
    assert (data["overall_risk_level"], data["overall_confidence"]) == ("CRITICAL", "LOW")


# ---------------------------------------------------------------- separate from the assessment

INDEPENDENT = [
    "SELECT id FROM users WHERE name LIKE '%son' ORDER BY RAND() LIMIT 5",
    "SELECT id FROM orders WHERE YEAR(created_at) = 2024; UPDATE orders SET a = 1 WHERE id = 1",
    "SELECT id FROM users WHERE name LIKE '%a' FOR UPDATE; DROP TABLE t; SELEC x",
]


def without_optimization(data: dict) -> dict:
    return {**data, "statements": [{key: value for key, value in statement.items() if key != "optimization"}
                                   for statement in data["statements"]]}


def failing(_facts):
    raise RuntimeError("adviser failure")


def advising(_facts):
    return {"status": "advised", "recommendations": [{"code": "x"}]}


REPLACEMENTS = {"fails": failing, "advises": advising}


@pytest.mark.parametrize("sql", INDEPENDENT)
@pytest.mark.parametrize("replacement", REPLACEMENTS)
def test_the_advice_changes_nothing_else_in_the_result(sql, replacement, monkeypatch):
    plain = validate(sql)
    monkeypatch.setattr(backend.main, "advise", REPLACEMENTS[replacement])
    changed = validate(sql)
    assert without_optimization(changed) == without_optimization(plain)
    if replacement == "fails":
        assert all(s["optimization"] == {"status": "unavailable", "recommendations": []}
                   for s in changed["statements"])


@pytest.mark.parametrize("replacement", REPLACEMENTS)
def test_the_advice_changes_nothing_sent_to_the_database_or_scored_from_it(replacement, monkeypatch):
    # Plan-eligible (RAND() is not on the reviewed function list, so it would get no plan).
    sql = "SELECT * FROM users WHERE name LIKE '%son' ORDER BY name"

    def run():
        server = FakeMySQL(explain=[explain_row("ALL", 1_902_192, extra="Using filesort")])
        return local_fixture(sql, server), server.executed

    plain, plain_sent = run()
    assert plain["statements"][0]["evidence"]["plan"]["state"] == "collected"
    assert plain["statements"][0]["optimization"]["status"] == "advised"
    monkeypatch.setattr(backend.main, "advise", REPLACEMENTS[replacement])
    changed, changed_sent = run()
    assert changed_sent == plain_sent
    assert without_optimization(changed) == without_optimization(plain)


def test_the_adviser_has_no_database_or_network_access():
    tree = ast.parse(ADVISER.read_text(encoding="utf-8"))
    imported = {node.module if isinstance(node, ast.ImportFrom) else alias.name
                for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names}
    assert imported == {"__future__", "typing", "sqlglot", "parser"}


def test_advised_sql_is_not_logged(caplog):
    secret = "Zx9SyntheticSecret"
    with caplog.at_level(logging.DEBUG):
        result = advice(f"SELECT id FROM users WHERE name LIKE '%{secret}' AND YEAR(born) = 1990")
    assert result["status"] == "advised"
    assert not any(secret in record.getMessage() for record in caplog.records)
