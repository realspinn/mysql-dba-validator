from backend.parser import parse_sql
import pytest


def test_update_with_where_is_parsed():
    facts = parse_sql("UPDATE orders SET status = 'pending' WHERE created_at <= '2026-08-19';")
    assert len(facts) == 1
    assert facts[0].statement_type == "UPDATE"
    assert facts[0].category == "WRITE"
    assert facts[0].has_where is True
    assert "created_at" in facts[0].columns_in_where


def test_update_without_where_is_parsed():
    facts = parse_sql("UPDATE orders SET status = 'pending';")
    assert facts[0].statement_type == "UPDATE"
    assert facts[0].has_where is False


def test_multi_statement_batch_is_split():
    facts = parse_sql("UPDATE users SET active = 0 WHERE id = 1; SELECT * FROM users WHERE id = 1;")
    assert [f.statement_type for f in facts] == ["UPDATE", "SELECT"]


def test_truncate_is_classified_as_ddl():
    facts = parse_sql("TRUNCATE TABLE orders;")
    assert facts[0].statement_type == "TRUNCATE"
    assert facts[0].category == "DDL"
    assert facts[0].is_full_table_ddl is True


def test_alter_drop_column_is_detected():
    facts = parse_sql("ALTER TABLE customers DROP COLUMN legacy_status;")
    assert facts[0].statement_type == "ALTER"
    assert facts[0].is_full_table_ddl is True


def test_direct_equality_has_structured_unambiguous_column_ownership():
    facts = parse_sql("UPDATE users SET active = 0 WHERE id = 10;")[0]

    assert facts.predicate_shape == "DIRECT_COLUMN_LITERAL_EQUALITY"
    assert facts.predicate_table_association == "UNAMBIGUOUS"
    assert facts.predicate_columns[0].name == "id"
    assert facts.predicate_columns[0].table is None
    assert facts.predicate_columns[0].resolved_table == "users"


def test_qualified_and_aliased_columns_preserve_ast_ownership():
    qualified = parse_sql(
        "UPDATE users SET active = 0 WHERE users.id = 10;"
    )[0]
    aliased = parse_sql(
        "UPDATE users AS u SET active = 0 WHERE u.id = 10;"
    )[0]

    assert qualified.predicate_shape == "DIRECT_COLUMN_LITERAL_EQUALITY"
    assert qualified.predicate_table_association == "UNAMBIGUOUS"
    assert qualified.predicate_columns[0].table == "users"
    assert qualified.predicate_columns[0].resolved_table == "users"

    assert aliased.predicate_shape == "DIRECT_COLUMN_LITERAL_EQUALITY"
    assert aliased.predicate_table_association == "UNAMBIGUOUS"
    assert aliased.predicate_columns[0].table == "u"
    assert aliased.predicate_columns[0].resolved_table == "users"


def test_join_predicate_is_ineligible_due_to_multiple_tables():
    facts = parse_sql(
        "UPDATE users "
        "JOIN orders ON users.id = orders.user_id "
        "SET users.active = 0 WHERE orders.id = 10;"
    )[0]

    assert facts.tables == ["users", "orders"]
    assert facts.predicate_shape == "DIRECT_COLUMN_LITERAL_EQUALITY"
    assert facts.predicate_table_association == "MULTIPLE_TABLES"
    assert facts.predicate_columns[0].table == "orders"
    assert facts.predicate_columns[0].resolved_table is None


def test_unresolved_qualifier_is_not_unambiguously_owned():
    facts = parse_sql(
        "UPDATE users SET active = 0 WHERE unknown_alias.id = 10;"
    )[0]

    assert facts.predicate_shape == "DIRECT_COLUMN_LITERAL_EQUALITY"
    assert facts.predicate_table_association == "UNRESOLVED"
    assert facts.predicate_columns[0].table == "unknown_alias"
    assert facts.predicate_columns[0].resolved_table is None


@pytest.mark.parametrize(
    ("sql", "shape"),
    [
        ("id < 10", "RANGE"),
        ("id <= 10", "RANGE"),
        ("id > 10", "RANGE"),
        ("id >= 10", "RANGE"),
        ("id BETWEEN 1 AND 10", "BETWEEN"),
        ("id IN (1, 2)", "IN"),
        ("id = 1 OR id = 2", "OR"),
        ("LOWER(id) = 'x'", "FUNCTION"),
        ("CAST(id AS CHAR) = '10'", "CAST"),
        ("id = other_id", "COLUMN_COMPARISON"),
        ("id + 1 = 10", "COMPUTED_EXPRESSION"),
        ("id IN (SELECT id FROM orders)", "SUBQUERY"),
        ("COALESCE(id, 0) = 10", "FUNCTION"),
        ("id IS NULL", "UNSUPPORTED"),
    ],
)
def test_unsupported_predicate_shapes_are_machine_readable(sql, shape):
    facts = parse_sql(f"UPDATE users SET active = 0 WHERE {sql};")[0]

    assert facts.predicate_shape == shape
    assert facts.predicate_shape != "DIRECT_COLUMN_LITERAL_EQUALITY"


def test_new_observations_are_additive_to_existing_statement_facts():
    facts = parse_sql(
        "UPDATE orders SET status = 'pending' "
        "WHERE created_at <= '2026-08-19';"
    )[0]

    assert facts.statement_type == "UPDATE"
    assert facts.tables == ["orders"]
    assert facts.has_where is True
    assert facts.where_predicate_summary is not None
    assert facts.columns_in_where == ["created_at"]
    assert facts.has_limit is False
    assert facts.parse_error is None
    assert facts.predicate_shape == "RANGE"


def test_single_unaliased_table_has_one_binding():
    facts = parse_sql("UPDATE users SET active = 0 WHERE id = 10;")[0]

    assert [(binding.schema, binding.table, binding.alias) for binding in facts.table_bindings] == [
        (None, "users", None),
    ]
    assert facts.tables == ["users"]


def test_single_aliased_table_has_one_binding_with_alias():
    facts = parse_sql("UPDATE users AS u SET active = 0 WHERE u.id = 10;")[0]

    assert [(binding.schema, binding.table, binding.alias) for binding in facts.table_bindings] == [
        (None, "users", "u"),
    ]
    assert facts.tables == ["users"]
    assert facts.predicate_columns[0].table == "u"
    assert facts.predicate_columns[0].resolved_table == "users"


def test_two_different_table_aliases_have_two_bindings():
    facts = parse_sql(
        "UPDATE orders AS o "
        "JOIN users AS u ON o.user_id = u.id "
        "SET o.status = 'x' WHERE o.id = 10;"
    )[0]

    assert [(binding.schema, binding.table, binding.alias) for binding in facts.table_bindings] == [
        (None, "orders", "o"),
        (None, "users", "u"),
    ]
    assert facts.predicate_table_association == "MULTIPLE_TABLES"


def test_self_join_has_two_bindings_even_with_same_base_table():
    facts = parse_sql(
        "UPDATE users AS u "
        "JOIN users AS v ON u.id = v.id "
        "SET u.active = 0 WHERE u.id = 10;"
    )[0]

    assert facts.tables == ["users"]
    assert [(binding.schema, binding.table, binding.alias) for binding in facts.table_bindings] == [
        (None, "users", "u"),
        (None, "users", "v"),
    ]
    assert len(facts.table_bindings) > 1
    assert facts.predicate_shape == "DIRECT_COLUMN_LITERAL_EQUALITY"
    assert facts.predicate_table_association == "UNAMBIGUOUS"
    assert facts.predicate_columns[0].table == "u"


def test_self_join_predicate_on_second_alias_retains_alias_binding():
    facts = parse_sql(
        "UPDATE users AS u "
        "JOIN users AS v ON u.id = v.id "
        "SET v.active = 0 WHERE v.id = 10;"
    )[0]

    assert [(binding.table, binding.alias) for binding in facts.table_bindings] == [
        ("users", "u"),
        ("users", "v"),
    ]
    assert len(facts.table_bindings) == 2
    assert facts.predicate_columns[0].table == "v"
    assert facts.predicate_columns[0].resolved_table == "users"


def test_schema_qualified_table_binding_preserves_schema():
    facts = parse_sql(
        "UPDATE app.users AS u SET active = 0 WHERE u.id = 10;"
    )[0]

    assert [(binding.schema, binding.table, binding.alias) for binding in facts.table_bindings] == [
        ("app", "users", "u"),
    ]
    assert facts.tables == ["app.users"]


def test_delete_receives_its_own_table_binding_observation():
    facts = parse_sql("DELETE FROM users AS u WHERE u.id = 10;")[0]

    assert facts.statement_type == "DELETE"
    assert [(binding.schema, binding.table, binding.alias) for binding in facts.table_bindings] == [
        (None, "users", "u"),
    ]


def test_each_multi_statement_fact_receives_its_own_bindings():
    facts = parse_sql(
        "UPDATE users SET active = 0 WHERE id = 1; "
        "DELETE FROM orders AS o WHERE o.id = 2;"
    )

    assert [fact.statement_type for fact in facts] == ["UPDATE", "DELETE"]
    assert [
        [(binding.table, binding.alias) for binding in fact.table_bindings]
        for fact in facts
    ] == [[("users", None)], [("orders", "o")]]
