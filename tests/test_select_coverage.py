"""SELECT coverage audit corpus: what the parser and evidence rules establish per form.

One row per SELECT form, pinning the facts the risk assessment relies on: statement
type, analysis status, physical tables, locking read and whether a plan may be
requested. docs/select-coverage-audit.md explains each outcome. Successful parsing is
not proof of full assessment: the "partial" and "not analysed" rows are deliberate.

Verification level: unit (parser, analyzer and evidence eligibility; no database).
"""

from __future__ import annotations

import pytest
from fastapi.encoders import jsonable_encoder

import backend.main as backend_main
from backend.db import enforce_read_only_session
from backend.db_evidence import select_not_eligible_for_plan
from backend.parser import parse_sql
from tests.local_evidence import local_request
from tests.test_connector_remote_evidence import FakeConnection, FakeServer

# (sql, statement_type, analysis_status, tables, locking_read, plan request refused)
CORPUS = {
    # Supported: classified, tables and predicate read, plan eligible.
    "simple": ("SELECT id, email FROM users WHERE id = 5", "SELECT", "analysed", ["users"], False, False),
    "joins": ("SELECT u.id FROM users u JOIN orders o ON o.user_id = u.id JOIN items i ON i.order_id = o.id "
              "WHERE u.id = 5", "SELECT", "analysed", ["users", "orders", "items"], False, False),
    "nested_subquery": ("SELECT * FROM users WHERE id IN (SELECT user_id FROM orders WHERE total > "
                        "(SELECT AVG(total) FROM orders))", "SELECT", "analysed", ["users", "orders"], False, False),
    "correlated_subquery": ("SELECT u.id, (SELECT COUNT(*) FROM orders o WHERE o.user_id = u.id) FROM users u",
                            "SELECT", "analysed", ["users", "orders"], False, False),
    "cte": ("WITH recent AS (SELECT * FROM orders WHERE id > 5) SELECT * FROM recent",
            "SELECT", "analysed", ["orders"], False, False),
    "aggregate_group_having": ("SELECT user_id, SUM(total) FROM orders GROUP BY user_id HAVING SUM(total) > 100",
                               "SELECT", "analysed", ["orders"], False, False),
    "window": ("SELECT id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id) FROM orders",
               "SELECT", "analysed", ["orders"], False, False),
    "order_limit": ("SELECT * FROM orders ORDER BY id DESC LIMIT 10", "SELECT", "analysed", ["orders"], False, False),
    "exists": ("SELECT * FROM users u WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id)",
               "SELECT", "analysed", ["users", "orders"], False, False),
    "derived_table": ("SELECT t.user_id FROM (SELECT user_id, SUM(total) s FROM orders GROUP BY user_id) t "
                      "WHERE t.s > 10", "SELECT", "analysed", ["orders"], False, False),
    "lateral": ("SELECT u.id, t.n FROM users u, LATERAL (SELECT COUNT(*) n FROM orders o WHERE o.user_id = u.id) t",
                "SELECT", "analysed", ["users", "orders"], False, False),
    "hints": ("SELECT STRAIGHT_JOIN u.id FROM users u FORCE INDEX (PRIMARY) JOIN orders o ON o.user_id = u.id",
              "SELECT", "analysed", ["users", "orders"], False, False),
    "qualified": ("SELECT * FROM app.users WHERE app.users.id = 5", "SELECT", "analysed", ["app.users"], False, False),
    "dual": ("SELECT 1 FROM DUAL", "SELECT", "analysed", [], False, False),
    "dual_with_table": ("SELECT 1 FROM users, dual", "SELECT", "analysed", ["users"], False, False),
    "quoted_dual_is_a_table": ("SELECT 1 FROM `DUAL`", "SELECT", "analysed", ["DUAL"], False, False),
    "qualified_dual_is_a_table": ("SELECT 1 FROM app.DUAL", "SELECT", "analysed", ["app.DUAL"], False, False),
    "for_update": ("SELECT * FROM users WHERE id = 5 FOR UPDATE", "SELECT", "analysed", ["users"], True, False),
    "for_share": ("SELECT * FROM users FOR SHARE", "SELECT", "analysed", ["users"], True, False),
    "lock_in_share_mode": ("SELECT * FROM users WHERE id = 1 LOCK IN SHARE MODE", "SELECT", "analysed", ["users"], True, False),
    "skip_locked": ("SELECT * FROM jobs WHERE state = 'new' LIMIT 10 FOR UPDATE SKIP LOCKED",
                    "SELECT", "analysed", ["jobs"], True, False),
    # Partially assessed: read, but some evidence is deliberately not requested.
    "union": ("SELECT id FROM users UNION SELECT user_id FROM orders", "UNION", "analysed", ["users", "orders"], False, False),
    "intersect": ("SELECT id FROM users INTERSECT SELECT user_id FROM orders",
                  "INTERSECT", "analysed", ["users", "orders"], False, False),
    "except": ("SELECT id FROM users EXCEPT SELECT user_id FROM orders", "EXCEPT", "analysed", ["users", "orders"], False, False),
    "recursive_cte": ("WITH RECURSIVE seq (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 10) SELECT n FROM seq",
                      "SELECT", "analysed", [], False, True),
    "json_table": ("SELECT jt.x FROM JSON_TABLE('[1,2]', '$[*]' COLUMNS (x INT PATH '$')) AS jt",
                   "SELECT", "analysed", [], False, True),
    "partition_selection": ("SELECT * FROM orders PARTITION (p2026) WHERE id = 5", "SELECT", "analysed", ["orders"], False, True),
    "user_function": ("SELECT my_func(id) FROM users WHERE id = 5", "SELECT", "analysed", ["users"], False, True),
    "sleep": ("SELECT SLEEP(10)", "SELECT", "analysed", [], False, True),
    # Not analysed: refused as a whole, never reported as low risk.
    "into_variable": ("SELECT COUNT(*) INTO @n FROM users", "UNKNOWN", "unreadable", [], False, False),
    "into_outfile": ("SELECT * FROM users INTO OUTFILE '/tmp/users.csv'", "UNKNOWN", "unreadable", [], False, False),
    "table_statement": ("TABLE users", "TABLE", "unsupported", [], False, False),
    "values_statement": ("VALUES ROW(1, 2)", "VALUES", "unsupported", [], False, False),
}


@pytest.mark.parametrize("name", list(CORPUS))
def test_select_form_outcome(name):
    sql, statement_type, status, tables, locking, plan_refused = CORPUS[name]
    facts = parse_sql(sql)
    assert len(facts) == 1
    f = facts[0]
    assert (f.statement_type, f.analysis_status, f.tables, f.locking_read) == (statement_type, status, tables, locking)
    assert select_not_eligible_for_plan(f) is plan_refused


class NoDualServer(FakeServer):
    """information_schema has no table called DUAL, as on a real server."""

    def rows_for(self, sql, args):
        if "information_schema" in sql and args and str(args[1]).upper() == "DUAL":
            self.executed.append((sql, args))
            return []
        return super().rows_for(sql, args)


@pytest.mark.parametrize("sql", ["SELECT 1 FROM DUAL", "SELECT NOW() FROM dual WHERE 1 = 1"])
def test_dual_is_not_looked_up_or_reported_as_a_missing_table(monkeypatch, sql):
    # Before the fix, metadata asked information_schema for a table named DUAL, reported
    # it as not found and capped confidence at LIMITED.
    server = NoDualServer(explain=[])
    connection = enforce_read_only_session(FakeConnection(server))
    monkeypatch.setattr(backend_main, "open_evidence_connection", lambda profile, database, **_: (connection, None))
    statement = jsonable_encoder(backend_main.validate(local_request(sql, database="appdb")))["statements"][0]
    assert not any(args and str(args[1]).upper() == "DUAL" for _, args in server.executed)
    assert statement["evidence"]["metadata"].get("tables") in (None, [])
    assert statement["confidence"] == "HIGH"
