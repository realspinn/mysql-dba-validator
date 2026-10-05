from backend.parser import parse_sql
from backend.risk_engine import score_batch


def report(sql: str):
    return score_batch(parse_sql(sql))[0]


def test_normal_update_is_not_penalized_for_missing_limit():
    r = report("UPDATE orders SET status = 'pending' WHERE created_at <= '2026-08-19';")
    assert r.risk_level == "MEDIUM"
    assert r.score == 20
    assert not any("LIMIT" in f.label for f in r.factors)
    assert r.confidence == "LIMITED"
    assert any("row impact" in finding.lower() for finding in r.findings)


def test_key_scoped_update_gets_small_reduction():
    r = report("UPDATE users SET status = 'inactive' WHERE id = 10;")
    assert r.score == 10
    assert r.risk_level == "LOW"
    assert any("key-scoped" in f.label for f in r.factors)


def test_update_without_where_is_high_or_worse():
    r = report("UPDATE users SET status = 'inactive';")
    assert r.score >= 70
    assert r.risk_level in {"HIGH", "CRITICAL"}
    assert any("No WHERE" in f.label for f in r.factors)


def test_delete_without_where_is_high_or_worse():
    r = report("DELETE FROM users;")
    assert r.score >= 70
    assert r.risk_level in {"HIGH", "CRITICAL"}


def test_limit_is_not_a_universal_safety_rule():
    without_limit = report("DELETE FROM users WHERE status = 'inactive';")
    with_limit = report("DELETE FROM users WHERE status = 'inactive' LIMIT 100;")
    assert without_limit.score == with_limit.score


def test_drop_is_critical():
    r = report("DROP TABLE users;")
    assert r.risk_level == "CRITICAL"
    assert r.confidence == "HIGH"


def test_truncate_is_critical():
    r = report("TRUNCATE TABLE users;")
    assert r.risk_level == "CRITICAL"


def test_ddl_does_not_get_transaction_wrapping_advice():
    r = report("ALTER TABLE users ADD COLUMN last_seen DATETIME;")
    assert not any("explicit transaction" in item.lower() for item in r.checklist)


def test_dml_gets_transaction_advice():
    r = report("UPDATE users SET status = 'inactive' WHERE id = 10;")
    assert any("transaction" in item.lower() for item in r.checklist)


def test_call_warns_about_uninspected_procedure():
    r = report("CALL process_monthly_billing(2026, 8);")
    assert r.risk_level == "LOW"
    assert any("procedure definition" in item.lower() for item in r.checklist)


def test_multi_statement_batch_gets_batch_factor():
    reports = score_batch(parse_sql("UPDATE users SET active = 0 WHERE id = 1; SELECT * FROM users;"))
    assert len(reports) == 2
    assert all(any("multi-statement" in f.label.lower() for f in r.factors) for r in reports)
