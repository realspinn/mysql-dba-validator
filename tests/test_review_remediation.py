"""Regression tests for the adversarial review of the V2 evidence contract (before commit).

Each section names the review finding it covers (C1, H1-H3, M1-M6, L1-L4). Verification
level: unit / TestClient with fake MySQL servers and the installed sqlglot. Nothing here
shows how a real MySQL server resolves function names, evaluates functions during
EXPLAIN, or enforces a read-only transaction; those are live-server checks
(VERIFICATION.md).
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

import backend.db_evidence as db_evidence
from backend.db import READ_ONLY_CHECK_SQL, ReadOnlyEvidenceConnection
from backend.db_evidence import (
    PERMITTED_CALL_NAMES,
    PLAN_NOT_ELIGIBLE,
    ReadOnlySessionLost,
    _is_plain_select_explain,
    collect_statement_evidence,
)
from backend.metadata import collect_metadata
from backend.parser import parse_sql
from backend.risk_engine import score_statement
from tests import test_connector_remote_evidence as remote
from tests.test_local_evidence_credentials import FakeMySQL, mysql, post  # noqa: F401  (fixture)
from tests.test_row_scope import only, schema, static, with_metadata  # noqa: F401  (fixture)


# ---------------------------------------------------------------- a recording fake


class RecordingCursor:
    """Records statements; confirms the read-only read-back; returns a one-row plan."""

    def __init__(self, log, read_only=True):
        self.log, self.read_only, self.last = log, read_only, ""

    def execute(self, sql, args=None):
        self.log.append(sql)
        self.last = sql

    def fetchall(self):
        if self.last == READ_ONLY_CHECK_SQL:
            return [{"transaction_read_only": 1 if self.read_only else 0}]
        return [{"id": 1, "table": "t", "type": "ALL", "rows": 5, "Extra": None}]

    def fetchone(self):
        return {"db_name": "app"}

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingConnection:
    def __init__(self, read_only=True):
        self.log, self.read_only, self.close_calls = [], read_only, 0

    def cursor(self):
        return RecordingCursor(self.log, self.read_only)

    def close(self):
        self.close_calls += 1


def plan_requests(sql, **kwargs):
    raw = RecordingConnection()
    evidence = collect_statement_evidence(sql, connection=ReadOnlyEvidenceConnection(raw), **kwargs)
    return evidence, [statement for statement in raw.log if statement.startswith("EXPLAIN")]


# ---------------------------------------------------------------- C1: function policy

# Names sqlglot knows from other dialects; MySQL has no built-in of that name, so it
# would resolve an unqualified call to a loadable or stored function.
NOT_MYSQL_BUILTINS = ["SPLIT", "LEVENSHTEIN", "STRUCT", "MAP", "LIST", "DATETIME", "ARRAY_AGG"]


@pytest.mark.parametrize("name", NOT_MYSQL_BUILTINS)
@pytest.mark.parametrize("template", [
    "SELECT {f}(a) FROM t",                                            # select list
    "SELECT a FROM t WHERE b = {f}(a)",                                # predicate
    "SELECT a FROM t WHERE COALESCE({f}(a), 1) = 1",                   # nested argument
    "SELECT a FROM t WHERE a IN (SELECT {f}(b) FROM u)",               # subquery
    "WITH c AS (SELECT {f}(a) AS x FROM t) SELECT x FROM c",           # CTE
    "SELECT a FROM t WHERE b = mydb.{f}(a)",                           # schema-qualified
    "SELECT `{f}`(a) FROM t",                                          # quoted
    "SELECT {f} /* c */ (a) FROM t",                                   # comment before "("
])
def test_calls_that_may_resolve_to_stored_functions_are_never_planned(name, template):
    evidence, sent = plan_requests(template.format(f=name.lower()))
    assert sent == []
    assert evidence.available is False


@pytest.mark.parametrize("name", NOT_MYSQL_BUILTINS)
def test_the_final_check_refuses_the_exact_outgoing_text(name):
    assert _is_plain_select_explain(f"EXPLAIN SELECT {name}(a) FROM t;") is False
    assert _is_plain_select_explain(f"EXPLAIN SELECT a FROM t WHERE b = mydb.UPPER(a);") is False


def test_parser_rewriting_cannot_turn_an_unapproved_call_into_an_approved_one():
    # sqlglot regenerates ARRAY_AGG as GROUP_CONCAT (approved); the submitted name decides.
    assert "GROUP_CONCAT" in parse_sql("SELECT ARRAY_AGG(a) FROM t")[0].raw_sql
    evidence, sent = plan_requests("SELECT ARRAY_AGG(a) FROM t")
    assert sent == [] and evidence.error == PLAN_NOT_ELIGIBLE
    # The same holds when a caller passes the regenerated text with the submitted facts.
    submitted = parse_sql("SELECT ARRAY_AGG(a) FROM t")[0]
    evidence, sent = plan_requests(submitted.raw_sql, source_facts=submitted)
    assert sent == [] and evidence.error == PLAN_NOT_ELIGIBLE


@pytest.mark.parametrize("sql", [
    "SELECT * FROM t WHERE a = SLEEP(1)",
    "SELECT BENCHMARK(10, MD5('x')) FROM t",
    "SELECT GET_LOCK('x', 1) FROM t",
    "SELECT LOAD_FILE('/etc/passwd') FROM t",
    "SELECT LAST_INSERT_ID(5) FROM t",
    "SELECT RAND(), UUID() FROM t",
    "SELECT * FROM t WHERE id = 1 FOR UPDATE",
    "SELECT * FROM t LOCK IN SHARE MODE",
    "SELECT * FROM t INTO @v",
    "SELECT * FROM t INTO OUTFILE '/tmp/x'",
    "SELECT /*+ SET_VAR(sort_buffer_size = 1) */ * FROM t",
    "SELECT a FROM t UNION SELECT b FROM u",
    "SELECT * FROM t; DELETE FROM t",
    "SELECT * FROM t PROCEDURE ANALYSE()",
    "SELECT * FROM WHERE",
    "SELECT * FROM t WHERE MATCH(a) AGAINST ('x')",
])
def test_existing_refusals_still_hold(sql):
    _, sent = plan_requests(sql)
    assert sent == []


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM orders WHERE status = 'x'",
    "SELECT IFNULL(a, 0), SUBSTR(name, 1, 3), CONCAT(a, b) FROM t",
    "SELECT CAST(a AS DECIMAL(10, 2)), CAST(b AS CHAR) FROM t",
    "SELECT * FROM t WHERE id IN (1, 2, 3) AND EXISTS (SELECT 1 FROM u WHERE u.id = t.id)",
    "SELECT t.a, u.b FROM t JOIN u ON (u.id = t.uid) WHERE t.x BETWEEN 1 AND 5",
    "SELECT ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) FROM t",
    "SELECT CASE WHEN a > 1 THEN 'x' ELSE 'y' END FROM t",
    "SELECT JSON_EXTRACT(doc, '$.a') FROM t",
    "WITH c AS (SELECT id FROM t) SELECT * FROM c JOIN u ON u.id = c.id",
    "SELECT COALESCE(a, b), GREATEST(a, b), LOWER(c), UPPER(d), ROUND(e, 2) FROM t",
    "SELECT YEAR(d), MONTH(d), DATEDIFF(a, b), TIMESTAMPDIFF(DAY, a, b) FROM t",
    "SELECT * FROM t FORCE INDEX (idx_a) WHERE a = 1",
])
def test_ordinary_selects_are_still_planned(sql):
    evidence, sent = plan_requests(sql)
    assert len(sent) == 1 and evidence.available is True


def test_every_name_the_check_accepts_is_on_the_reviewed_list():
    """No sqlglot-known function name passes the final check unless it is on the list."""
    names = {name.upper() for function in exp.ALL_FUNCTIONS for name in function.sql_names()}
    accepted = set()
    for name in names:
        for args in ("(a)", "(a, 'b')", "(a, 'b', 1)"):
            if _is_plain_select_explain(f"EXPLAIN SELECT {name}{args} FROM t;"):
                accepted.add(name)
                break
    assert accepted <= PERMITTED_CALL_NAMES, sorted(accepted - PERMITTED_CALL_NAMES)
    assert not set(NOT_MYSQL_BUILTINS) & PERMITTED_CALL_NAMES


def test_reviewed_list_excludes_side_effect_and_session_functions():
    excluded = {"SLEEP", "BENCHMARK", "GET_LOCK", "RELEASE_LOCK", "RELEASE_ALL_LOCKS", "IS_FREE_LOCK",
                "IS_USED_LOCK", "LOAD_FILE", "LAST_INSERT_ID", "ROW_COUNT", "FOUND_ROWS", "RAND", "UUID",
                "UUID_SHORT", "SYSDATE", "SOURCE_POS_WAIT", "MASTER_POS_WAIT", "WAIT_FOR_EXECUTED_GTID_SET",
                "JSON_TABLE", "ANY", "SOME", "AGAINST", "DATETIME"}
    assert not excluded & PERMITTED_CALL_NAMES


# ---------------------------------------------------------------- M5: sqlglot behaviour


def test_installed_sqlglot_neutralises_executable_comments_in_the_outgoing_text():
    """The comment protections depend on sqlglot's generator; this pins them for CI."""
    for sql in ("SELECT * FROM t WHERE id = 1 /*!80000 AND my_func() */",
                "SELECT * FROM t /*!80000 FOR UPDATE */",
                "SELECT a FROM t WHERE b = 1 -- x */ /*!80000 AND a = evil() */\n",
                "SELECT a FROM t WHERE b = 1 # x */ /*!80000 AND a = evil() */\n"):
        _, sent = plan_requests(sql)
        assert len(sent) == 1, sql
        text = sent[0]
        assert "/*!" not in text, text                  # never an executable comment
        assert text.count("/*") == text.count("*/"), text  # no comment closed early


def test_installed_sqlglot_regenerates_quotes_by_doubling():
    _, sent = plan_requests("SELECT a FROM t WHERE b = 'x\\' OR evil() OR \\''")
    assert sent == ["EXPLAIN SELECT a FROM t WHERE b = 'x'' OR evil() OR ''';"]


# DUAL is a reserved word and a quoted reserved word is an ordinary identifier (MySQL 8.4
# manual: Keywords and Reserved Words), so `DUAL` names a table called DUAL, not the dummy
# table. sqlglot regenerates FROM DUAL as FROM `DUAL`: planning that text would explain a
# different statement (or fail), so it is never sent.
DUAL_SELECTS = ["SELECT 1 FROM DUAL", "SELECT 1 FROM dual WHERE 1 = 1", "SELECT 1 FROM `DUAL`"]


def test_installed_sqlglot_quotes_dual_in_the_outgoing_text():
    assert parse_sql("SELECT 1 FROM DUAL")[0].raw_sql == "SELECT 1 FROM `DUAL`"


@pytest.mark.parametrize("sql", DUAL_SELECTS)
def test_dual_is_never_planned(sql):
    evidence, sent = plan_requests(sql)
    assert sent == [] and evidence.error == PLAN_NOT_ELIGIBLE


def test_a_qualified_table_named_dual_and_similar_names_are_still_planned():
    # After a period a reserved word is an identifier: app.DUAL is a real table.
    assert _is_plain_select_explain("EXPLAIN SELECT a FROM app.`DUAL`;")
    assert _is_plain_select_explain("EXPLAIN SELECT a FROM dual_log;")
    assert _is_plain_select_explain("EXPLAIN SELECT 1 FROM `DUAL`;") is False


@pytest.mark.parametrize("sql", DUAL_SELECTS)
def test_company_validation_never_plans_dual(monkeypatch, sql):
    server = remote.FakeServer(explain=[remote.explain_row("ALL", 1_902_192)])
    response, _ = remote.remote_validate(monkeypatch, server, sql)
    assert response.status_code == 200, response.text
    assert not [statement for statement, _ in server.executed if statement.startswith("EXPLAIN")]
    assert response.json()["statements"][0]["evidence"]["plan"]["state"] == "not_eligible"


# ---------------------------------------------------------------- M2: collector boundary


@pytest.mark.parametrize("sql", ["SELECT * FROM t INTO @v", "SELECT * FROM t PROCEDURE ANALYSE()"])
def test_collector_refuses_partly_read_selects_itself(sql):
    evidence, sent = plan_requests(sql)
    assert sent == [] and evidence.available is False
    # The parser cannot read these in full: not analysed, kept as submitted, never a SELECT.
    submitted = parse_sql(sql)[0]
    assert submitted.analysis_status == "unreadable" and not submitted.lossy_parse
    assert submitted.raw_sql == sql and submitted.statement_type == "UNKNOWN"
    # Also when handed the submitted text plus the statement's facts.
    evidence, sent = plan_requests(submitted.raw_sql, source_facts=submitted)
    assert sent == [] and evidence.available is False


@pytest.mark.parametrize("sql", ["SELECT * FROM users INTO @v", "SELECT * FROM users PROCEDURE ANALYSE()",
                                 "SELECT SPLIT(name, ',') FROM users"])
def test_api_never_plans_partly_read_or_unreviewed_selects(mysql, sql):
    response = post(sql)
    assert response.status_code == 200
    assert not [s for s in mysql.executed if s.startswith("EXPLAIN")]
    statement = response.json()["statements"][0]
    if statement["analysis_status"] == "analysed":
        assert statement["evidence"]["plan"]["state"] == "not_eligible"
    else:
        # Read only partly: not analysed (CRITICAL, LOW) and never planned.
        assert (statement["risk_level"], statement["confidence"]) == ("CRITICAL", "LOW")
        assert statement["evidence"]["plan"]["state"] != "collected"


# ---------------------------------------------------------------- M1: read-only read-back


def test_collector_sends_nothing_and_closes_when_the_session_is_no_longer_read_only():
    raw = RecordingConnection(read_only=False)
    with pytest.raises(ReadOnlySessionLost):
        collect_statement_evidence("SELECT * FROM t WHERE id = 1",
                                   connection=ReadOnlyEvidenceConnection(raw))
    assert raw.log == [READ_ONLY_CHECK_SQL]
    assert raw.close_calls == 1


def test_read_back_runs_on_the_cursor_that_sends_the_plan():
    raw = RecordingConnection()
    collect_statement_evidence("SELECT * FROM t WHERE id = 1", connection=ReadOnlyEvidenceConnection(raw))
    assert raw.log[:2] == [READ_ONLY_CHECK_SQL, "EXPLAIN SELECT * FROM t WHERE id = 1;"]


class LosesReadOnly(FakeMySQL):
    """Confirms the session once (at connect), then reports it writable."""

    checks = 0

    def rows_for(self, sql, args, connection):
        if sql.strip() == READ_ONLY_CHECK_SQL:
            self.checks += 1
            self.executed.append(sql.strip())
            return [{"transaction_read_only": 1 if self.checks == 1 else 0}]
        return super().rows_for(sql, args, connection)


def test_local_validation_stops_collecting_when_the_session_is_no_longer_read_only(monkeypatch):
    import pymysql

    import backend.db as backend_db

    server = LosesReadOnly()
    monkeypatch.setattr(backend_db, "_client", None)
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: server.connect(**kwargs))
    response = post("SELECT * FROM users WHERE id = 1; SELECT * FROM users WHERE id = 2")
    assert response.status_code == 200
    assert not [s for s in server.executed if s.startswith(("EXPLAIN", "SELECT ENGINE"))]
    first, second = response.json()["statements"]
    assert first["database_evidence"]["status"] == "collection_failed"
    assert second["evidence"]["connection"]["state"] == "failed"
    assert server.connections[0].close_calls >= 1


class RemoteLosesReadOnly(remote.FakeServer):
    checks = 0

    def rows_for(self, sql, args):
        if sql.strip() == READ_ONLY_CHECK_SQL:
            self.checks += 1
            self.executed.append((sql, args))
            return [{"transaction_read_only": 1 if self.checks == 1 else 0}]
        return super().rows_for(sql, args)


def test_company_validation_stops_collecting_when_the_session_is_no_longer_read_only(monkeypatch):
    server = RemoteLosesReadOnly(explain=[remote.explain_row("const", 1, key="PRIMARY")])
    response, _ = remote.remote_validate(
        monkeypatch, server, "SELECT * FROM users WHERE id = 1; SELECT * FROM users WHERE id = 2", database=None)
    assert response.status_code == 200, response.text
    assert not [sql for sql, _ in server.executed if sql.startswith("EXPLAIN")]
    first, second = response.json()["statements"]
    assert first["database_evidence"]["status"] == "collection_failed"
    assert second["evidence"]["connection"]["state"] == "failed"


# ---------------------------------------------------------------- H1 / M3: always-true WHERE


@pytest.mark.parametrize("predicate", [
    "1", "TRUE", "1 = 1", "'1' = '1'", "id = 5 OR 1", "(id = 5 OR TRUE)", "NOT FALSE", "NOT 0",
    "(1) AND (TRUE)", "id = 5 OR (status = 'x' OR 2)",
])
def test_always_true_where_is_an_all_rows_critical_write(mysql, predicate):
    statement = only(static(f"DELETE FROM users WHERE {predicate}"))
    assert statement["scope"]["basis"] == "all_rows", predicate
    assert statement["score"] == 100 and statement["risk_level"] == "CRITICAL"
    assert statement["confidence"] == "HIGH"
    assert any("always true" in reason for reason in statement["reasons"])
    data_risk = next(d for d in statement["v2_assessment"]["dimensions"] if d["name"] == "Data Risk")
    assert data_risk["level"] == "HIGH"


@pytest.mark.parametrize("predicate", [
    "status = 'untrue' AND id = 5", "note = 'true'", "active = TRUE", "'true'", "NULL", "1 = 2",
    "id = 5 AND (status = 'x' OR 1)", "id = 5 OR 0", "trueish = 1",
])
def test_words_strings_and_narrowing_conjunctions_are_not_always_true(mysql, predicate):
    facts = parse_sql(f"DELETE FROM users WHERE {predicate}")[0]
    assert facts.where_always_true is False, predicate
    statement = only(static(f"DELETE FROM users WHERE {predicate}"))
    assert statement["scope"]["basis"] == "not_established"
    assert statement["score"] < 100


def test_a_string_containing_true_does_not_block_a_verified_key(schema):
    statement = only(with_metadata("UPDATE users SET status = 'x' WHERE status = 'untrue' AND id = 10"))
    assert statement["where_looks_trivial"] is False
    assert statement["scope"]["basis"] == "at_most_one_row"


def test_and_with_a_key_equality_is_not_broad():
    assert parse_sql("DELETE FROM t WHERE id = 5 AND n > 0")[0].where_looks_trivial is False
    assert parse_sql("DELETE FROM t WHERE id > 0")[0].where_looks_trivial is True


# ---------------------------------------------------------------- H2: EXPLAIN / DESCRIBE


@pytest.mark.parametrize("sql", [
    "EXPLAIN ANALYZE DELETE FROM users",
    "EXPLAIN ANALYZE UPDATE users SET a = 1",
    "explain analyze delete from users",
    "EXPLAIN ANALYZE FORMAT=TREE DELETE FROM users",
    "EXPLAIN /* c */ ANALYZE DELETE FROM users",
    "EXPLAIN ANALYZE DELETE u FROM users u JOIN orders o ON o.user_id = u.id",
])
def test_explain_analyze_of_an_unrestricted_write_is_scored_as_that_write(mysql, sql):
    statement = only(static(sql))
    assert statement["statement_type"] == "DESCRIBE"
    assert statement["score"] == 100 and statement["risk_level"] == "CRITICAL"
    assert any("EXPLAIN ANALYZE runs the statement it explains" in reason for reason in statement["reasons"])
    data_risk = next(d for d in statement["v2_assessment"]["dimensions"] if d["name"] == "Data Risk")
    assert data_risk["level"] == "HIGH"


def test_explain_analyze_of_a_narrow_write_keeps_the_write_floor(mysql):
    statement = only(static("EXPLAIN ANALYZE UPDATE users SET a = 1 WHERE id = 10"))
    assert statement["risk_level"] == "MEDIUM" and statement["confidence"] == "LIMITED"


@pytest.mark.parametrize("sql", ["EXPLAIN ANALYZE DELETE FROM", "EXPLAIN ANALYZE", "EXPLAIN ANALYZE garbage"])
def test_explain_analyze_of_an_unreadable_statement_is_high_risk_low_confidence(mysql, sql):
    statement = only(static(sql))
    assert statement["risk_level"] in {"HIGH", "CRITICAL"}
    assert statement["confidence"] == "LOW"


@pytest.mark.parametrize("sql", ["DESCRIBE DELETE FROM users", "DESC DELETE FROM users",
                                 "EXPLAIN DELETE FROM users WHERE id = 1",
                                 "EXPLAIN FORMAT=TREE UPDATE users SET a = 1",
                                 "EXPLAIN INSERT INTO users (id) VALUES (1)"])
def test_plain_explain_of_a_change_is_not_presented_as_harmless(mysql, sql):
    statement = only(static(sql))
    assert statement["risk_level"] != "LOW"
    assert any("without running it" in reason for reason in statement["reasons"])
    data_risk = next(d for d in statement["v2_assessment"]["dimensions"] if d["name"] == "Data Risk")
    assert data_risk["level"] != "LOW"


@pytest.mark.parametrize("sql", ["DESCRIBE users", "EXPLAIN SELECT * FROM users", "EXPLAIN users"])
def test_plain_inspection_stays_low(mysql, sql):
    assert only(static(sql))["risk_level"] == "LOW"


def test_explain_analyze_select_says_it_runs_the_query(mysql):
    statement = only(static("EXPLAIN ANALYZE SELECT * FROM users"))
    assert statement["risk_level"] == "LOW"
    assert any("EXPLAIN ANALYZE runs the statement it explains" in reason for reason in statement["reasons"])


@pytest.mark.parametrize("sql", ["EXPLAIN ANALYZE DELETE FROM users", "DESCRIBE DELETE FROM users",
                                 "EXPLAIN ANALYZE SELECT * FROM users"])
def test_explain_forms_are_never_sent_to_mysql(mysql, sql):
    assert post(sql).status_code == 200
    assert not [s for s in mysql.executed if s.startswith("EXPLAIN")]


# ---------------------------------------------------------------- H3 / M6: direct scope


def test_verified_key_is_a_direct_bound_with_indirect_effects_named(schema):
    statement = only(with_metadata("DELETE FROM users WHERE id = 10"))
    assert statement["scope"]["basis"] == "at_most_one_row"
    assert statement["scope"]["indirect_effects"] == "not_checked"
    assert any("foreign-key actions" in reason for reason in statement["reasons"])
    data_risk = next(d for d in statement["v2_assessment"]["dimensions"] if d["name"] == "Data Risk")
    assert data_risk["level"] != "LOW"
    assert "Triggers" in data_risk["reason"]


# ---------------------------------------------------------------- L1 / L2


def test_partition_selection_is_not_all_rows_of_the_table(mysql):
    statement = only(static("DELETE FROM users PARTITION (p0)"))
    assert statement["scope"]["basis"] == "not_established"
    assert statement["risk_level"] == "CRITICAL"


def test_collector_opens_no_connection_for_writes(monkeypatch):
    opened = []
    monkeypatch.setattr(db_evidence, "get_connection", lambda *a, **k: opened.append(1))
    evidence = collect_statement_evidence("DELETE FROM users WHERE id = 1")
    assert evidence.error == "plan_not_collected_read_only" and opened == []


def test_collector_closes_a_connection_it_opened(monkeypatch):
    raw = RecordingConnection()
    monkeypatch.setattr(db_evidence, "get_connection", lambda *a, **k: ReadOnlyEvidenceConnection(raw))
    collect_statement_evidence("SELECT * FROM t WHERE id = 1")
    assert raw.close_calls == 1


def test_metadata_fallback_connection_must_be_confirmed_read_only(monkeypatch):
    raw = RecordingConnection(read_only=False)

    class Client:
        config = type("Config", (), {"database": "app"})()

        def connect_readonly(self, read_timeout):
            return raw

    monkeypatch.setattr("backend.metadata.get_client", lambda: Client())
    result = collect_metadata(["users"])
    assert result.status == "unavailable"
    assert not [sql for sql in raw.log if "information_schema" in sql]
    assert raw.close_calls == 1


def test_scoring_is_unchanged_for_statements_the_review_did_not_cover():
    for sql, expected in (("UPDATE t SET a = 1 WHERE id = 10", ("MEDIUM", 20)),
                          ("DROP TABLE t", ("CRITICAL", 100)),
                          ("SELECT * FROM t", ("LOW", 0))):
        report = score_statement(parse_sql(sql)[0], batch_size=1)
        assert (report.risk_level, report.score) == expected, sql


# ---------------------------------------------------------------- one-row write floor
#
# Owner decision after the review: a verified at_most_one_row UPDATE/DELETE keeps its
# verified scope (and HIGH confidence), but its overall risk is never below MEDIUM.


def scope_factor(statement):
    return statement["scope"]["factor"]


def test_verified_one_row_update_is_medium_with_the_key_still_reported(schema):
    statement = only(with_metadata("UPDATE users SET status = 'x' WHERE id = 10"))
    assert statement["scope"]["basis"] == "at_most_one_row" and statement["scope"]["index"] == "PRIMARY"
    assert (statement["static_score"], statement["score"], statement["risk_level"]) == (20, 20, "MEDIUM")
    assert statement["confidence"] == "HIGH"
    assert scope_factor(statement) == {"label": "Unique key PRIMARY limits the match to at most one row",
                                       "points": 0, "medium_floor_applied": True}
    assert any("MEDIUM is the minimum overall risk" in reason for reason in statement["reasons"])
    assert any(reason.startswith("At most one row of") for reason in statement["reasons"])
    assessment = statement["v2_assessment"]
    assert assessment["risk_level"] == "MEDIUM" and assessment["overall_score"] == 20
    data_risk = next(d for d in assessment["dimensions"] if d["name"] == "Data Risk")
    assert data_risk["level"] == "MEDIUM"


@pytest.mark.parametrize("sql,static_score", [
    ("DELETE FROM users WHERE id = 10", 25),
    ("DELETE FROM users WHERE code = 'abc'", 25),                      # unique varchar
    ("DELETE FROM users WHERE tenant_id = 3 AND ref = 'r1'", 25),      # composite unique
])
def test_verified_one_row_delete_is_medium(schema, sql, static_score):
    statement = only(with_metadata(sql))
    assert statement["scope"]["basis"] == "at_most_one_row"
    assert (statement["static_score"], statement["score"], statement["risk_level"]) == (static_score, 20, "MEDIUM")
    assert scope_factor(statement)["points"] == 20 - static_score
    assert scope_factor(statement)["medium_floor_applied"] is True
    assert statement["confidence"] == "HIGH"


def test_unverified_one_row_looking_write_is_medium_limited_without_a_credit(mysql):
    statement = only(static("UPDATE users SET status = 'x' WHERE id = 10"))
    assert statement["scope"]["basis"] == "not_established" and scope_factor(statement) is None
    assert (statement["score"], statement["risk_level"], statement["confidence"]) == (20, "MEDIUM", "LIMITED")
    assert not any("MEDIUM is the minimum overall risk" in reason for reason in statement["reasons"])


def test_verified_credit_still_applies_above_the_floor_in_a_batch(schema):
    data = with_metadata("UPDATE users SET status = 'x' WHERE id = 10; SELECT * FROM users")
    statement = data["statements"][0]
    assert statement["scope"]["basis"] == "at_most_one_row"
    # Base 15 + 5 + batch 10 = 30; the full -10 credit lands exactly on the floor.
    assert (statement["static_score"], statement["score"], statement["risk_level"]) == (30, 20, "MEDIUM")
    assert scope_factor(statement)["points"] == -10
    assert scope_factor(statement)["medium_floor_applied"] is False
    assert data["overall_risk_level"] == "MEDIUM"


@pytest.mark.parametrize("sql", ["DELETE FROM users", "UPDATE users SET status = 'x'",
                                 "DELETE FROM users WHERE id = 10 OR 1", "DELETE FROM users WHERE TRUE"])
def test_unrestricted_and_always_true_writes_stay_critical_with_metadata(schema, sql):
    statement = only(with_metadata(sql))
    assert statement["scope"]["basis"] == "all_rows" and scope_factor(statement) is None
    assert statement["score"] == 100 and statement["risk_level"] == "CRITICAL"


def test_scope_adjustment_floor_unit():
    from dataclasses import replace

    from backend.main import apply_scope_adjustment, risk_level_from_score

    base = score_statement(parse_sql("UPDATE t SET a = 1 WHERE id = 1")[0], batch_size=1)
    verified = {"basis": "at_most_one_row", "table": "app.t", "index": "PRIMARY"}

    def at(score):
        return replace(base, score=score, risk_level=risk_level_from_score(score))

    for score, expected, points, floored in ((20, 20, 0, True), (25, 20, -5, True), (30, 20, -10, False),
                                             (45, 35, -10, False), (60, 50, -10, False)):
        report, factor, floor_applied = apply_scope_adjustment(at(score), verified)
        assert (report.score, factor.points, floor_applied) == (expected, points, floored), score
        assert report.risk_level != "LOW"
    # CRITICAL is never reduced, and an unverified scope gets no credit.
    assert apply_scope_adjustment(at(100), verified) == (at(100), None, False)
    assert apply_scope_adjustment(at(20), {"basis": "not_established"}) == (at(20), None, False)


# ---------------------------------------------------------------- unusual call names fail closed
#
# A name the reviewed list cannot represent (non-ASCII, digit- or $-leading,
# fullwidth), and a literal written before "(", make the statement's call names
# unknown, so no plan is requested. Before this, such names were skipped by the
# name check and only the separate "unknown function" check refused most of them;
# 9e(a) was refused by neither once sqlglot regenerated it as "9e AS (a)".

UNUSUAL_CALLS = [
    "SELECT fünf(a) FROM t",                                   # non-ASCII name
    "SELECT 1split(a) FROM t",                                 # digit-leading name
    "SELECT 1.5split(a) FROM t",
    "SELECT $f(a) FROM t",                                     # $-leading name
    "SELECT ＳＰＬＩＴ(a) FROM t",                               # fullwidth letters
    "SELECT 9e(a) FROM t",                                     # literal before "("
    "SELECT 1e5(a) FROM t",
    "SELECT 0x1f(a) FROM t",
    "SELECT _utf8mb4'x'(a) FROM t",
    "SELECT COUNT(fünf(a)) FROM t",                            # nested in a permitted call
    "SELECT a FROM t WHERE b IN (SELECT fünf(c) FROM u)",      # nested in a subquery
    "WITH c AS (SELECT 1split(a) AS x FROM t) SELECT x FROM c",  # nested in a CTE
    "SELECT d.fünf(a) FROM t",                                 # qualified
    "SELECT `fünf`(a) FROM t",                                 # quoted
    "SELECT `1split`(a) FROM t",
]


@pytest.mark.parametrize("sql", UNUSUAL_CALLS)
def test_unusual_call_names_are_never_planned(sql):
    evidence, sent = plan_requests(sql)
    assert sent == []
    assert evidence.error == PLAN_NOT_ELIGIBLE
    # Also when a caller passes the regenerated text with the submitted facts.
    submitted = parse_sql(sql)[0]
    evidence, sent = plan_requests(submitted.raw_sql, source_facts=submitted)
    assert sent == [] and evidence.error == PLAN_NOT_ELIGIBLE


def test_a_rewritten_literal_call_is_refused_on_the_submitted_text():
    # sqlglot regenerates 9e(a) as "9e AS (a)": the outgoing text alone contains no
    # call, so the refusal must come from the submitted statement.
    submitted = parse_sql("SELECT 9e(a) FROM t")[0]
    assert "9e(" not in submitted.raw_sql
    evidence, sent = plan_requests(submitted.raw_sql, source_facts=submitted)
    assert sent == [] and evidence.error == PLAN_NOT_ELIGIBLE


@pytest.mark.parametrize("sql", ["SELECT fünf(a) FROM t", "SELECT 1split(a) FROM t", "SELECT $f(a) FROM t"])
def test_the_final_check_refuses_unusual_names_in_the_outgoing_text(sql):
    assert _is_plain_select_explain(f"EXPLAIN {sql};") is False


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM t WHERE a * (b + 1) > 2",
    "SELECT * FROM t WHERE a = (SELECT MAX(b) FROM u)",
    "SELECT * FROM t WHERE (a, b) IN ((1, 2), (3, 4))",
    "SELECT COALESCE((a), 0) FROM t",
    "SELECT -(a) FROM t",
    "SELECT a FROM t WHERE NOT (a = 1)",
    "SELECT CASE WHEN (a > 1) THEN 1 END FROM t",
])
def test_parentheses_after_operators_and_keywords_stay_eligible(sql):
    evidence, sent = plan_requests(sql)
    assert len(sent) == 1 and evidence.available is True


@pytest.mark.parametrize("sql", ["SELECT fünf(name) FROM users", "SELECT 9e(name) FROM users",
                                 "SELECT appdb.fünf(name) FROM users"])
def test_local_validation_sends_no_plan_for_unusual_names(mysql, sql):
    response = post(sql)
    assert response.status_code == 200
    assert not [s for s in mysql.executed if s.startswith("EXPLAIN")]
    assert response.json()["statements"][0]["evidence"]["plan"]["state"] == "not_eligible"


@pytest.mark.parametrize("sql", ["SELECT fünf(name) FROM users", "SELECT 9e(name) FROM users",
                                 "SELECT appdb.fünf(name) FROM users"])
def test_company_validation_sends_no_plan_for_unusual_names(monkeypatch, sql):
    server = remote.FakeServer(explain=[remote.explain_row("ALL", 5)])
    response, _ = remote.remote_validate(monkeypatch, server, sql)
    assert response.status_code == 200, response.text
    assert not [s for s, _ in server.executed if s.startswith("EXPLAIN")]
    assert response.json()["statements"][0]["evidence"]["plan"]["state"] == "not_eligible"
