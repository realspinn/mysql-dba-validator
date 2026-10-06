from backend.db_evidence import DatabaseEvidence
from backend.main import ValidateRequest, validate
from tests.local_evidence import local_request, use_fake_evidence_connection
from backend.metadata import MetadataConfig, collect_metadata
import pytest


def valid_table_row(table_rows=1):
    return {
        "ENGINE": "InnoDB",
        "TABLE_ROWS": table_rows,
        "DATA_LENGTH": 1,
        "INDEX_LENGTH": 1,
        "CREATE_OPTIONS": "",
        "UPDATE_TIME": None,
    }


def statistics_row(index_name, column_name, sequence, **overrides):
    row = {
        "INDEX_NAME": index_name,
        "COLUMN_NAME": column_name,
        "SEQ_IN_INDEX": sequence,
        "NON_UNIQUE": 1,
        "CARDINALITY": 1,
        "SUB_PART": None,
        "INDEX_TYPE": "BTREE",
    }
    row.update(overrides)
    return row


class FakeCursor:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchmany(self, size):
        return next(self.responses, [])

    def close(self):
        pass


class FakeConnection:
    def __init__(self, responses):
        self.cursor_instance = FakeCursor(responses)

    def cursor(self):
        return self.cursor_instance

    def close(self):
        pass


def metadata_rows():
    return [
        [{"ENGINE": "InnoDB", "TABLE_ROWS": 12000, "DATA_LENGTH": 100, "INDEX_LENGTH": 50, "CREATE_OPTIONS": "", "UPDATE_TIME": None}],
        [
            {"INDEX_NAME": "idx_status_user", "COLUMN_NAME": "status", "SEQ_IN_INDEX": 1, "NON_UNIQUE": 1, "CARDINALITY": 900, "SUB_PART": None, "INDEX_TYPE": "BTREE"},
            {"INDEX_NAME": "idx_status_user", "COLUMN_NAME": "user_id", "SEQ_IN_INDEX": 2, "NON_UNIQUE": 1, "CARDINALITY": 1200, "SUB_PART": 4, "INDEX_TYPE": "BTREE"},
        ],
        [
            {"COLUMN_NAME": "status", "DATA_TYPE": "varchar", "IS_NULLABLE": "NO", "COLUMN_KEY": "MUL", "ORDINAL_POSITION": 1},
        ],
    ]


def test_metadata_collection_uses_fixed_parameterized_queries_and_preserves_index_order():
    connection = FakeConnection(metadata_rows())
    result = collect_metadata(
        ["users"],
        connection=connection,
        database="app",
        config=MetadataConfig(max_tables=2),
    )
    assert result.status == "available"
    assert result.table_metadata[0]["estimated_table_rows"] == 12000
    assert result.table_metadata[0]["estimated_table_rows_semantics"] == "metadata estimate, not exact row count"
    assert result.indexes[0]["name"] == "idx_status_user"
    assert result.indexes[0]["columns"] == ["status", "user_id"]
    assert result.indexes[0]["column_details"][1]["prefix_length"] == "4"
    assert result.indexes[0]["cardinality_estimates"] == [900, 1200]
    assert len(connection.cursor_instance.calls) == 3
    for sql, params in connection.cursor_instance.calls:
        assert "information_schema" in sql
        assert "users" not in sql
        assert params[0] == "app"
        assert params[1] == "users"


def test_metadata_uses_existing_dbclient_readonly_connection_method():
    calls = []

    class Client:
        config = type("Config", (), {"database": "app"})()

        def connect_readonly(self, read_timeout):
            calls.append(read_timeout)
            return FakeConnection(metadata_rows())

    original = __import__("backend.metadata", fromlist=["get_client"]).get_client
    try:
        module = __import__("backend.metadata", fromlist=["get_client"])
        module.get_client = lambda: Client()
        result = collect_metadata(
            ["users"],
            config=MetadataConfig(query_timeout_seconds=0.75),
        )
    finally:
        module.get_client = original

    assert result.status == "available"
    assert calls == [0.75]
    assert len(result.query_states) == 3


@pytest.mark.parametrize(
    ("index_name", "rows", "columns"),
    [
        ("idx_a", [statistics_row("idx_a", "a", 1)], ["a"]),
        (
            "idx_ab",
            [statistics_row("idx_ab", "a", 1), statistics_row("idx_ab", "b", 2)],
            ["a", "b"],
        ),
        (
            "idx_abc",
            [
                statistics_row("idx_abc", "a", 1),
                statistics_row("idx_abc", "b", 2),
                statistics_row("idx_abc", "c", 3),
            ],
            ["a", "b", "c"],
        ),
    ],
)
def test_valid_index_sequences_are_structurally_valid(index_name, rows, columns):
    result = collect_metadata(
        ["users"],
        connection=FakeConnection([[], rows, []]),
        database="app",
    )

    index = next(index for index in result.indexes if index["name"] == index_name)
    assert index["schema"] == "app"
    assert index["table"] == "users"
    assert index["columns"] == columns
    assert index["structure_valid"] is True


@pytest.mark.parametrize(
    "rows",
    [
        [statistics_row("idx", "a", 1), statistics_row("idx", "b", 3)],
        [statistics_row("idx", "a", 1), statistics_row("idx", "b", 1)],
        [statistics_row("idx", "a", 2)],
        [statistics_row("idx", "a", 0)],
        [statistics_row("idx", "a", -1)],
        [statistics_row("idx", "a", "1")],
        [statistics_row("idx", "a", 1), statistics_row("idx", "b", None)],
    ],
)
def test_invalid_index_sequences_are_not_m2_eligible(rows):
    result = collect_metadata(
        ["users"],
        connection=FakeConnection([[], rows, []]),
        database="app",
    )

    index = next(index for index in result.indexes if index["name"] == "idx")
    assert index["structure_valid"] is False


def test_missing_sequence_field_is_malformed_and_not_returned_as_valid_index():
    rows = [statistics_row("idx", "a", 1), statistics_row("idx", "b", 2)]
    del rows[1]["SEQ_IN_INDEX"]

    result = collect_metadata(
        ["users"],
        connection=FakeConnection([[], rows, []]),
        database="app",
    )

    statistics_state = result.query_states[1]
    assert statistics_state.malformed is True
    assert statistics_state.complete is False
    assert result.indexes == []


def test_indexes_retain_schema_and_table_association():
    result = collect_metadata(
        ["schema_a.table_a", "schema_b.table_a"],
        connection=FakeConnection([
            [valid_table_row()],
            [statistics_row("idx_a", "a", 1)],
            [],
            [valid_table_row()],
            [statistics_row("idx_b", "b", 1)],
            [],
        ]),
        database="ignored",
    )

    associations = {
        (index["schema"], index["table"], index["name"])
        for index in result.indexes
    }
    assert associations == {
        ("schema_a", "table_a", "idx_a"),
        ("schema_b", "table_a", "idx_b"),
    }


def test_tables_and_statistics_use_sentinel_but_columns_do_not():
    connection = FakeConnection([
        [
            {"ENGINE": "InnoDB", "TABLE_ROWS": 1, "DATA_LENGTH": 1, "INDEX_LENGTH": 1, "CREATE_OPTIONS": "", "UPDATE_TIME": None},
            {"ENGINE": "InnoDB", "TABLE_ROWS": 1, "DATA_LENGTH": 1, "INDEX_LENGTH": 1, "CREATE_OPTIONS": "", "UPDATE_TIME": None},
            {"ENGINE": "sentinel", "TABLE_ROWS": 1, "DATA_LENGTH": 1, "INDEX_LENGTH": 1, "CREATE_OPTIONS": "", "UPDATE_TIME": None},
        ],
        [],
        [],
    ])
    result = collect_metadata(
        ["users"],
        connection=connection,
        database="app",
        config=MetadataConfig(max_metadata_rows=2),
    )
    assert [params[2] for _, params in connection.cursor_instance.calls] == [3, 3, 2]
    assert [state.retained_row_count for state in result.query_states] == [2, 0, 0]
    assert result.query_states[0].observed_row_count == 3
    assert result.query_states[0].potentially_truncated is True
    assert result.query_states[0].complete is False
    assert len(result.table_metadata) == 2
    assert all(row["engine"] != "sentinel" for row in result.table_metadata)


def test_fewer_than_sentinel_rows_are_complete_for_tables_and_statistics():
    connection = FakeConnection([
        [{"ENGINE": "InnoDB", "TABLE_ROWS": 3, "DATA_LENGTH": 1, "INDEX_LENGTH": 1, "CREATE_OPTIONS": "", "UPDATE_TIME": None}],
        [{"INDEX_NAME": "idx", "COLUMN_NAME": "id", "SEQ_IN_INDEX": 1, "NON_UNIQUE": 1, "CARDINALITY": 1, "SUB_PART": None, "INDEX_TYPE": "BTREE"}],
        [],
    ])
    result = collect_metadata(
        ["app.users"],
        connection=connection,
        database="ignored",
        config=MetadataConfig(max_metadata_rows=2),
    )
    assert result.status == "available"
    assert result.query_states[0].complete is True
    assert result.query_states[1].complete is True
    assert result.query_states[0].schema == "app"
    assert result.query_states[0].table == "users"
    assert result.query_states[1].schema == "app"
    assert result.query_states[1].table == "users"
    assert result.query_states[2].complete is False


def test_statistics_sentinel_is_not_exposed_and_is_incomplete():
    statistics = [
        {"INDEX_NAME": "idx", "COLUMN_NAME": "id", "SEQ_IN_INDEX": 1, "NON_UNIQUE": 1, "CARDINALITY": 1, "SUB_PART": None, "INDEX_TYPE": "BTREE"},
        {"INDEX_NAME": "idx", "COLUMN_NAME": "created_at", "SEQ_IN_INDEX": 2, "NON_UNIQUE": 1, "CARDINALITY": 1, "SUB_PART": None, "INDEX_TYPE": "BTREE"},
        {"INDEX_NAME": "sentinel", "COLUMN_NAME": "secret", "SEQ_IN_INDEX": 1, "NON_UNIQUE": 1, "CARDINALITY": 1, "SUB_PART": None, "INDEX_TYPE": "BTREE"},
    ]
    connection = FakeConnection([[], statistics, []])
    result = collect_metadata(
        ["users"],
        connection=connection,
        database="app",
        config=MetadataConfig(max_metadata_rows=2),
    )
    stats_state = result.query_states[1]
    assert stats_state.observed_row_count == 3
    assert stats_state.retained_row_count == 2
    assert stats_state.potentially_truncated is True
    assert stats_state.complete is False
    assert [index["name"] for index in result.indexes] == ["idx"]


def test_empty_tables_result_is_available_and_complete_but_has_no_rows():
    result = collect_metadata(["users"], connection=FakeConnection([[], [], []]), database="app")
    table_state = result.query_states[0]
    assert result.status == "available"
    assert table_state.complete is True
    assert table_state.observed_row_count == 0
    assert table_state.retained_row_count == 0
    assert result.table_metadata == []


def test_malformed_metadata_row_marks_query_incomplete():
    result = collect_metadata(
        ["users"],
        connection=FakeConnection([["malformed"], [], []]),
        database="app",
    )
    assert result.status == "available"
    assert result.query_states[0].malformed is True
    assert result.query_states[0].complete is False


def test_table_metadata_eligibility_requires_exactly_one_valid_row():
    result = collect_metadata(
        ["users"],
        connection=FakeConnection([[valid_table_row(100000)], [], []]),
        database="app",
    )
    assert result.query_states[0].table_metadata_eligible is True

    for rows in [
        [],
        [valid_table_row(1), valid_table_row(2)],
        [{**valid_table_row(), "TABLE_ROWS": None}],
        [{**valid_table_row(), "TABLE_ROWS": -1}],
        [{**valid_table_row(), "TABLE_ROWS": "100000"}],
    ]:
        result = collect_metadata(
            ["users"],
            connection=FakeConnection([rows, [], []]),
            database="app",
        )
        assert result.query_states[0].table_metadata_eligible is False


def test_table_metadata_eligibility_accepts_zero_and_rejects_ambiguous_state():
    result = collect_metadata(
        ["users"],
        connection=FakeConnection([[valid_table_row(0)], [], []]),
        database="app",
    )
    assert result.query_states[0].table_metadata_eligible is True

    state = result.query_states[0]
    state.table_association_unambiguous = False
    state.table_metadata_eligible = False
    assert state.table_metadata_eligible is False


def test_metadata_empty_result_is_available_without_false_data():
    connection = FakeConnection([[], [], []])
    result = collect_metadata(["users"], connection=connection, database="app")
    assert result.status == "available"
    assert result.table_metadata == []
    assert result.indexes == []
    assert result.column_metadata == []


def test_collector_backed_m1_integration_applies_additive_adjustment(monkeypatch):
    use_fake_evidence_connection(monkeypatch)
    metadata = collect_metadata(
        ["users"],
        connection=FakeConnection([[valid_table_row(100000)], [], []]),
        database="app",
    )
    table_state = metadata.query_states[0]
    assert metadata.status == "available"
    assert table_state.query_kind == "tables"
    assert table_state.status == "available"
    assert table_state.complete is True
    assert table_state.potentially_truncated is False
    assert table_state.malformed is False
    assert table_state.table_association_unambiguous is True
    assert table_state.table_metadata_eligible is True
    assert table_state.retained_row_count == 1

    monkeypatch.setattr(
        "backend.main.collect_statement_evidence",
        lambda sql, **_kwargs: DatabaseEvidence(
            available=True,
            explain_available=True,
            estimated_rows=500,
            access_type="ALL",
            full_table_scan=True,
            tables=["users"],
        ),
    )
    monkeypatch.setattr("backend.main.collect_metadata", lambda tables, **_kwargs: metadata)

    statement = validate(
        local_request("UPDATE users SET active = 0 WHERE status = 'pending';")
    )["statements"][0]

    assert statement["static_score"] == 20
    assert statement["score"] == 40
    assert statement["static_score"] != statement["score"]
    assert statement["metadata_score_adjustment"] == 10
    assert {factor["code"] for factor in statement["metadata_factors"]} == {
        "M1_TABLE_ROWS_FULL_SCAN_WRITE",
        "M2_FULL_SCAN_NO_LEADING_INDEX",
    }
    assert len(statement["metadata_factors"]) == 2
    assert statement["high_estimated_rows_applied"] is False
    assert not any(
        factor.get("code") == "M1_TABLE_ROWS_FULL_SCAN_WRITE"
        for factor in statement["factors"]
    )
    assert statement["evidence_factors"]


def test_collector_backed_metadata_reaches_m1_and_m2(monkeypatch):
    use_fake_evidence_connection(monkeypatch)
    metadata = collect_metadata(
        ["users"],
        connection=FakeConnection([
            [valid_table_row(100000)],
            [statistics_row("idx_id", "id", 1)],
            [],
        ]),
        database="app",
    )
    assert metadata.query_states[0].table_metadata_eligible is True
    assert metadata.query_states[1].complete is True
    assert metadata.indexes[0]["structure_valid"] is True

    monkeypatch.setattr(
        "backend.main.collect_statement_evidence",
        lambda sql, **_kwargs: DatabaseEvidence(
            available=True,
            explain_available=True,
            estimated_rows=500,
            access_type="ALL",
            full_table_scan=True,
            tables=["users"],
        ),
    )
    monkeypatch.setattr("backend.main.collect_metadata", lambda tables, **_kwargs: metadata)

    statement = validate(
        local_request("UPDATE users SET active = 0 WHERE status = 'pending';")
    )["statements"][0]

    assert statement["metadata_score_adjustment"] == 10
    assert {factor["code"] for factor in statement["metadata_factors"]} == {
        "M1_TABLE_ROWS_FULL_SCAN_WRITE",
        "M2_FULL_SCAN_NO_LEADING_INDEX",
    }


def test_collector_backed_m1_integration_rejects_rows_below_threshold(monkeypatch):
    use_fake_evidence_connection(monkeypatch)
    metadata = collect_metadata(
        ["users"],
        connection=FakeConnection([[valid_table_row(99999)], [], []]),
        database="app",
    )
    assert metadata.query_states[0].table_metadata_eligible is True
    assert metadata.table_metadata[0]["estimated_table_rows"] == 99999

    monkeypatch.setattr(
        "backend.main.collect_statement_evidence",
        lambda sql, **_kwargs: DatabaseEvidence(
            available=True,
            explain_available=True,
            estimated_rows=500,
            access_type="ALL",
            full_table_scan=True,
            tables=["users"],
        ),
    )
    monkeypatch.setattr("backend.main.collect_metadata", lambda tables, **_kwargs: metadata)

    statement = validate(
        local_request("UPDATE users SET active = 0 WHERE status = 'pending';")
    )["statements"][0]

    assert statement["metadata_score_adjustment"] == 5
    assert [factor["code"] for factor in statement["metadata_factors"]] == [
        "M2_FULL_SCAN_NO_LEADING_INDEX"
    ]


def test_collector_backed_m1_integration_rejects_tables_sentinel_truncation(monkeypatch):
    metadata = collect_metadata(
        ["users"],
        connection=FakeConnection([[valid_table_row(100000), valid_table_row(100000)], [], []]),
        database="app",
        config=MetadataConfig(max_metadata_rows=1),
    )
    table_state = metadata.query_states[0]
    assert table_state.query_kind == "tables"
    assert table_state.potentially_truncated is True
    assert table_state.complete is False
    assert table_state.table_metadata_eligible is False

    monkeypatch.setattr(
        "backend.main.collect_statement_evidence",
        lambda sql: DatabaseEvidence(
            available=True,
            explain_available=True,
            estimated_rows=500,
            access_type="ALL",
            full_table_scan=True,
            tables=["users"],
        ),
    )
    monkeypatch.setattr("backend.main.collect_metadata", lambda tables: metadata)

    statement = validate(
        ValidateRequest(sql="UPDATE users SET active = 0 WHERE status = 'pending';")
    )["statements"][0]

    assert statement["metadata_score_adjustment"] == 0
    assert statement["metadata_factors"] == []


def test_metadata_failures_are_sanitized_and_bounded():
    class FailingCursor(FakeCursor):
        def execute(self, sql, params):
            raise RuntimeError("access denied password=secret")

    connection = FakeConnection([])
    connection.cursor_instance = FailingCursor([])
    result = collect_metadata(["users"], connection=connection, database="app")
    assert result.status == "permission_denied"
    assert "secret" not in str(result)

    too_many = collect_metadata(["a", "b"], database="app", config=MetadataConfig(max_tables=1))
    assert too_many.status == "budget_exceeded"


def test_execute_failure_creates_failed_tables_state():
    class FailingCursor(FakeCursor):
        def execute(self, sql, params):
            raise RuntimeError("access denied password=secret")

    connection = FakeConnection([])
    connection.cursor_instance = FailingCursor([])
    result = collect_metadata(["users"], connection=connection, database="app")
    state = result.query_states[0]
    assert state.query_kind == "tables"
    assert state.status == "permission_denied"
    assert state.complete is False
    assert state.potentially_truncated is False
    assert state.observed_row_count == 0
    assert state.retained_row_count == 0
    assert "secret" not in str(result)


def test_statistics_failure_preserves_successful_tables_state():
    class SecondQueryFailsConnection:
        def __init__(self):
            self.calls = 0

        def cursor(self):
            self.calls += 1
            if self.calls == 1:
                return FakeCursor([[{"ENGINE": "InnoDB", "TABLE_ROWS": 1, "DATA_LENGTH": 1, "INDEX_LENGTH": 1, "CREATE_OPTIONS": "", "UPDATE_TIME": None}]])

            class FailingCursor(FakeCursor):
                def execute(self, sql, params):
                    raise RuntimeError("statistics permission denied")

            return FailingCursor([])

    result = collect_metadata(["users"], connection=SecondQueryFailsConnection(), database="app")
    assert result.status == "permission_denied"
    assert [state.query_kind for state in result.query_states] == ["tables", "statistics"]
    assert result.query_states[0].complete is True
    assert result.query_states[1].complete is False
    assert result.query_states[1].status == "permission_denied"


def test_connection_failure_creates_contextual_failed_state(monkeypatch):
    class Client:
        config = type("Config", (), {"database": "app"})()

        def connect_readonly(self, read_timeout):
            return None

    monkeypatch.setattr("backend.metadata.get_client", lambda: Client())
    result = collect_metadata(["users"], database="app")
    assert result.status == "unavailable"
    assert len(result.query_states) == 1
    assert result.query_states[0].query_kind == "tables"
    assert result.query_states[0].complete is False
    assert result.query_states[0].observed_row_count == 0


def test_fetch_failure_preserves_partial_prior_state_and_sanitizes_error():
    class SecondQueryFailsConnection:
        def __init__(self):
            self.calls = 0

        def cursor(self):
            self.calls += 1
            if self.calls == 1:
                return FakeCursor([[{"ENGINE": "InnoDB", "TABLE_ROWS": 1, "DATA_LENGTH": 1, "INDEX_LENGTH": 1, "CREATE_OPTIONS": "", "UPDATE_TIME": None}]])

            class FailingFetchCursor(FakeCursor):
                def fetchmany(self, size):
                    raise TimeoutError("driver timeout password=secret")

            return FailingFetchCursor([])

    result = collect_metadata(["users"], connection=SecondQueryFailsConnection(), database="app")
    assert result.status == "timeout"
    assert result.query_states[0].complete is True
    assert result.query_states[1].status == "timeout"
    assert result.query_states[1].complete is False
    assert result.query_states[1].potentially_truncated is False
    assert result.query_states[1].observed_row_count == 0
    assert result.query_states[1].retained_row_count == 0
    assert "secret" not in str(result)


def test_budget_failure_preserves_accumulated_query_states():
    connection = FakeConnection([
        [{"ENGINE": "InnoDB", "TABLE_ROWS": 1, "DATA_LENGTH": 1, "INDEX_LENGTH": 1, "CREATE_OPTIONS": "", "UPDATE_TIME": None}],
        [{"INDEX_NAME": "idx", "COLUMN_NAME": "id", "SEQ_IN_INDEX": 1, "NON_UNIQUE": 1, "CARDINALITY": 1, "SUB_PART": None, "INDEX_TYPE": "BTREE"}],
        [],
    ])
    result = collect_metadata(["users"], connection=connection, database="app", config=MetadataConfig(max_metadata_rows=1))
    assert result.status == "budget_exceeded"
    assert len(result.query_states) == 2
    assert result.query_states[0].complete is True
    assert result.query_states[1].status == "budget_exceeded"
    assert result.query_states[1].complete is False
    assert result.query_states[1].potentially_truncated is False


def test_total_timeout_after_operation_preserves_query_states():
    class SlowFetchCursor(FakeCursor):
        def fetchmany(self, size):
            import time
            time.sleep(0.01)
            return []

    class SlowConnection:
        def cursor(self):
            return SlowFetchCursor([[]])

    result = collect_metadata(
        ["users"],
        connection=SlowConnection(),
        database="app",
        config=MetadataConfig(query_timeout_seconds=1, total_timeout_seconds=0.001),
    )
    assert result.status == "timeout"
    assert len(result.query_states) == 1
    assert result.query_states[0].status == "timeout"
    assert result.query_states[0].complete is False
    assert result.query_states[0].potentially_truncated is False


def test_metadata_timeout_is_sanitized():
    class SlowCursor(FakeCursor):
        def fetchmany(self, size):
            import time
            time.sleep(0.01)
            return []

    connection = FakeConnection([[], [], []])
    connection.cursor_instance = SlowCursor([[], [], []])
    result = collect_metadata(
        ["users"],
        connection=connection,
        database="app",
        config=MetadataConfig(query_timeout_seconds=0.001),
    )
    assert result.status == "timeout"
    assert "password" not in str(result).lower()


def test_metadata_passes_configured_timeout_to_driver_connection(monkeypatch):
    connection = FakeConnection([[], [], []])
    calls = []

    class Client:
        config = type("Config", (), {"database": "app"})()

        def connect_readonly(self, read_timeout):
            calls.append(read_timeout)
            return connection

    monkeypatch.setattr("backend.metadata.get_client", lambda: Client())
    result = collect_metadata(
        ["users"],
        database="app",
        config=MetadataConfig(query_timeout_seconds=0.25),
    )
    assert result.status == "available"
    assert calls == [0.25]


def test_invalid_reference_and_missing_configuration_are_conservative(monkeypatch):
    assert collect_metadata(["users; DROP TABLE users"], database="app").status == "invalid_reference"
    monkeypatch.setattr("backend.metadata.get_client", lambda: type("Client", (), {
        "config": type("Config", (), {"database": None})(),
    })())
    assert collect_metadata(["users"], database=None).status == "unavailable"


def test_metadata_does_not_change_phase2_score_or_create_factors(monkeypatch):
    use_fake_evidence_connection(monkeypatch)
    monkeypatch.setattr(
        "backend.main.collect_statement_evidence",
        lambda sql, **_kwargs: DatabaseEvidence(available=False, error="unavailable"),
    )
    monkeypatch.setattr("backend.main.collect_metadata", lambda tables, **_kwargs: type("Metadata", (), {
        "status": "permission_denied",
        "warnings": ["Database metadata permission was denied."],
        "table_metadata": [],
        "indexes": [],
        "column_metadata": [],
        "available": False,
    })())
    result = validate(local_request("UPDATE users SET active = 0 WHERE status = 'pending';"))
    statement = result["statements"][0]
    assert statement["score"] == statement["static_score"] == 20
    assert statement["metadata_factors"] == []
    assert statement["metadata_score_adjustment"] == 0
    assert statement["database_evidence"]["metadata_status"] == "permission_denied"
    assert statement["confidence"] == "LIMITED"
    assert result["analysis_mode"] in {"STATIC", "STATIC_DATABASE_UNAVAILABLE"}


def test_unexpected_metadata_exception_does_not_break_validation(monkeypatch):
    monkeypatch.setattr("backend.main.collect_metadata", lambda tables: (_ for _ in ()).throw(
        RuntimeError("password=secret connection string leaked"),
    ))
    result = validate(ValidateRequest(sql="SELECT * FROM users;"))
    statement = result["statements"][0]
    assert statement["metadata_status"] == "unavailable"
    assert statement["metadata_score_adjustment"] == 0
    assert statement["metadata_factors"] == []
    assert "secret" not in str(result)
