"""SELECT coverage audit corpus: what the parser and evidence rules establish per form.

One row per SELECT form, pinning the facts the risk assessment relies on: statement
type, analysis status, physical tables, locking read, and what the plan collector does
with the statement. That last value comes from the collector itself, with every gate
applied: "planned" means an EXPLAIN would be sent; anything else is the refusal reason
the collector returns, and nothing is sent. docs/select-coverage-audit.md explains each
outcome. Successful parsing is not proof of full assessment: the "partial" and "not
analysed" rows are deliberate.

Verification level: unit (parser, analyzer and plan collector on a recording
connection; no database).
"""

from __future__ import annotations

import pytest
from fastapi.encoders import jsonable_encoder

import backend.main as backend_main
from backend.db import enforce_read_only_session
from backend.parser import parse_sql
from tests.local_evidence import local_request
from tests.test_connector_remote_evidence import FakeConnection, FakeServer
from tests.test_review_remediation import plan_requests

PLANNED = "planned"
NOT_ELIGIBLE = "plan_not_eligible"
NO_TABLE = "statement_has_no_identifiable_table"
NOT_A_SELECT = "unsupported_statement_type"
NOT_READ = "parse_error"

# (sql, statement_type, analysis_status, tables, locking_read, plan outcome)
CORPUS = {
    # Supported: classified, tables and predicate read, plan requested.
    "simple": ("SELECT id, email FROM users WHERE id = 5", "SELECT", "analysed", ["users"], False, PLANNED),
    "joins": ("SELECT u.id FROM users u JOIN orders o ON o.user_id = u.id JOIN items i ON i.order_id = o.id "
              "WHERE u.id = 5", "SELECT", "analysed", ["users", "orders", "items"], False, PLANNED),
    "nested_subquery": ("SELECT * FROM users WHERE id IN (SELECT user_id FROM orders WHERE total > "
                        "(SELECT AVG(total) FROM orders))", "SELECT", "analysed", ["users", "orders"], False, PLANNED),
    "correlated_subquery": ("SELECT u.id, (SELECT COUNT(*) FROM orders o WHERE o.user_id = u.id) FROM users u",
                            "SELECT", "analysed", ["users", "orders"], False, PLANNED),
    "cte": ("WITH recent AS (SELECT * FROM orders WHERE id > 5) SELECT * FROM recent",
            "SELECT", "analysed", ["orders"], False, PLANNED),
    "recursive_cte": ("WITH RECURSIVE r AS (SELECT id AS n FROM orders WHERE id = 1 UNION ALL "
                      "SELECT n + 1 FROM r WHERE n < 10) SELECT n FROM r", "SELECT", "analysed", ["orders"], False, PLANNED),
    "aggregate_group_having": ("SELECT user_id, SUM(total) FROM orders GROUP BY user_id HAVING SUM(total) > 100",
                               "SELECT", "analysed", ["orders"], False, PLANNED),
    "window": ("SELECT id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id) FROM orders",
               "SELECT", "analysed", ["orders"], False, PLANNED),
    "order_limit": ("SELECT * FROM orders ORDER BY id DESC LIMIT 10", "SELECT", "analysed", ["orders"], False, PLANNED),
    "exists": ("SELECT * FROM users u WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id)",
               "SELECT", "analysed", ["users", "orders"], False, PLANNED),
    "derived_table": ("SELECT t.user_id FROM (SELECT user_id, SUM(total) s FROM orders GROUP BY user_id) t "
                      "WHERE t.s > 10", "SELECT", "analysed", ["orders"], False, PLANNED),
    "lateral": ("SELECT u.id, t.n FROM users u, LATERAL (SELECT COUNT(*) n FROM orders o WHERE o.user_id = u.id) t",
                "SELECT", "analysed", ["users", "orders"], False, PLANNED),
    "hints": ("SELECT STRAIGHT_JOIN u.id FROM users u FORCE INDEX (PRIMARY) JOIN orders o ON o.user_id = u.id",
              "SELECT", "analysed", ["users", "orders"], False, PLANNED),
    "qualified": ("SELECT * FROM app.users WHERE app.users.id = 5", "SELECT", "analysed", ["app.users"], False, PLANNED),
    "qualified_dual_is_a_table": ("SELECT 1 FROM app.DUAL", "SELECT", "analysed", ["app.DUAL"], False, PLANNED),
    "dual": ("SELECT 1 FROM DUAL", "SELECT", "analysed", [], False, NO_TABLE),
    # Partially assessed: read and scored, but no plan is requested.
    "for_update": ("SELECT * FROM users WHERE id = 5 FOR UPDATE", "SELECT", "analysed", ["users"], True, NOT_ELIGIBLE),
    "for_share": ("SELECT * FROM users FOR SHARE", "SELECT", "analysed", ["users"], True, NOT_ELIGIBLE),
    "lock_in_share_mode": ("SELECT * FROM users WHERE id = 1 LOCK IN SHARE MODE",
                           "SELECT", "analysed", ["users"], True, NOT_ELIGIBLE),
    "skip_locked": ("SELECT * FROM jobs WHERE state = 'new' LIMIT 10 FOR UPDATE SKIP LOCKED",
                    "SELECT", "analysed", ["jobs"], True, NOT_ELIGIBLE),
    "union": ("SELECT id FROM users UNION SELECT user_id FROM orders",
              "UNION", "analysed", ["users", "orders"], False, NOT_A_SELECT),
    "intersect": ("SELECT id FROM users INTERSECT SELECT user_id FROM orders",
                  "INTERSECT", "analysed", ["users", "orders"], False, NOT_A_SELECT),
    "except": ("SELECT id FROM users EXCEPT SELECT user_id FROM orders",
               "EXCEPT", "analysed", ["users", "orders"], False, NOT_A_SELECT),
    "recursive_cte_column_list": ("WITH RECURSIVE r (n) AS (SELECT id FROM orders WHERE id = 1 UNION ALL "
                                  "SELECT n + 1 FROM r WHERE n < 10) SELECT n FROM r",
                                  "SELECT", "analysed", ["orders"], False, NOT_ELIGIBLE),
    "json_table": ("SELECT u.id, jt.x FROM users u, JSON_TABLE(u.tags, '$[*]' COLUMNS (x INT PATH '$')) AS jt",
                   "SELECT", "analysed", ["users"], False, NOT_ELIGIBLE),
    "partition_selection": ("SELECT * FROM orders PARTITION (p2026) WHERE id = 5",
                            "SELECT", "analysed", ["orders"], False, NOT_ELIGIBLE),
    "user_function": ("SELECT my_func(id) FROM users WHERE id = 5", "SELECT", "analysed", ["users"], False, NOT_ELIGIBLE),
    "sleep": ("SELECT * FROM users WHERE id = SLEEP(1)", "SELECT", "analysed", ["users"], False, NOT_ELIGIBLE),
    "dual_with_table": ("SELECT 1 FROM users, dual", "SELECT", "analysed", ["users"], False, NOT_ELIGIBLE),
    "quoted_dual_is_a_table": ("SELECT 1 FROM `DUAL`", "SELECT", "analysed", ["DUAL"], False, NOT_ELIGIBLE),
    # Not analysed: refused as a whole, never reported as low risk.
    "into_variable": ("SELECT COUNT(*) INTO @n FROM users", "UNKNOWN", "unreadable", [], False, NOT_READ),
    "into_outfile": ("SELECT * FROM users INTO OUTFILE '/tmp/users.csv'", "UNKNOWN", "unreadable", [], False, NOT_READ),
    "table_statement": ("TABLE users", "TABLE", "unsupported", [], False, NOT_READ),
    "values_statement": ("VALUES ROW(1, 2)", "VALUES", "unsupported", [], False, NOT_READ),
}


@pytest.mark.parametrize("name", list(CORPUS))
def test_select_form_outcome(name):
    sql, statement_type, status, tables, locking, plan = CORPUS[name]
    facts = parse_sql(sql)
    assert len(facts) == 1
    f = facts[0]
    assert (f.statement_type, f.analysis_status, f.tables, f.locking_read) == (statement_type, status, tables, locking)
    evidence, sent = plan_requests(sql)
    if plan == PLANNED:
        assert len(sent) == 1 and evidence.error is None
    else:
        assert sent == [] and (evidence.error or "").startswith(plan)


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
