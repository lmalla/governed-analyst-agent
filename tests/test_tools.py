import datetime
import decimal
import json

import pytest
from google.api_core.exceptions import BadRequest, Forbidden, NotFound

from agent.tools import _validate_single_select_or_with, describe_table, list_tables, run_query


class FakeTableListItem:
    def __init__(self, table_id):
        self.table_id = table_id


class FakePolicyTagList:
    def __init__(self, names):
        self.names = names


class FakeSchemaField:
    def __init__(self, name, field_type, description="", policy_tags=None):
        self.name = name
        self.field_type = field_type
        self.description = description
        self.policy_tags = policy_tags


class FakeTable:
    def __init__(self, schema):
        self.schema = schema


class FakeQueryJob:
    def __init__(
        self,
        total_bytes_processed=0,
        referenced_tables=None,
        rows=None,
        job_id="fake-job-id",
        raise_forbidden=False,
        raise_on_result=None,
    ):
        self.total_bytes_processed = total_bytes_processed
        self.referenced_tables = referenced_tables or []
        self._rows = rows or []
        self.job_id = job_id
        self._raise_forbidden = raise_forbidden
        self._raise_on_result = raise_on_result

    def result(self, max_results=None):
        if self._raise_forbidden:
            raise Forbidden("Access Denied: BigQuery column-level security policy tag")
        if self._raise_on_result is not None:
            raise self._raise_on_result
        return self._rows[:max_results] if max_results is not None else self._rows


class FakeClient:
    def __init__(
        self,
        tables=None,
        table_schema=None,
        dry_run_job=None,
        query_job=None,
        raise_on_dry_run=None,
        raise_on_query=None,
    ):
        self._tables = tables or []
        self._table_schema = table_schema or []
        self._dry_run_job = dry_run_job or FakeQueryJob()
        self._query_job = query_job or FakeQueryJob()
        self._raise_on_dry_run = raise_on_dry_run
        self._raise_on_query = raise_on_query
        self.query_calls = []

    def list_tables(self, dataset_ref):
        return self._tables

    def get_table(self, table_ref):
        return FakeTable(self._table_schema)

    def query(self, sql, job_config=None):
        self.query_calls.append((sql, job_config))
        if job_config is not None and getattr(job_config, "dry_run", False):
            if self._raise_on_dry_run is not None:
                raise self._raise_on_dry_run
            return self._dry_run_job
        if self._raise_on_query is not None:
            raise self._raise_on_query
        return self._query_job


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
    monkeypatch.setenv("BQ_DATASET", "test_dataset")
    monkeypatch.setenv("MAX_BYTES_BILLED", "100000000")
    monkeypatch.setenv("MAX_ROWS_RETURNED", "200")


def test_list_tables_returns_table_ids():
    client = FakeClient(tables=[FakeTableListItem("customers"), FakeTableListItem("orders")])
    assert list_tables(client) == ["customers", "orders"]


def test_describe_table_marks_pii_high_columns_as_restricted():
    schema = [
        FakeSchemaField("customer_id", "INTEGER"),
        FakeSchemaField(
            "email", "STRING", policy_tags=FakePolicyTagList(["projects/p/locations/us/taxonomies/t/policyTags/x"])
        ),
    ]
    client = FakeClient(table_schema=schema)
    result = describe_table(client, "customers")
    columns_by_name = {c["name"]: c for c in result["columns"]}
    assert columns_by_name["customer_id"]["restricted"] is False
    assert columns_by_name["email"]["restricted"] is True


def test_describe_table_column_with_no_policy_tags_is_not_restricted():
    schema = [FakeSchemaField("region", "STRING", policy_tags=None)]
    client = FakeClient(table_schema=schema)
    result = describe_table(client, "customers")
    assert result["columns"][0]["restricted"] is False


def test_validate_rejects_multiple_statements_separated_by_semicolon():
    with pytest.raises(ValueError, match="single SQL statement"):
        _validate_single_select_or_with("SELECT 1; DROP TABLE customers")


def test_validate_rejects_non_select_statements():
    with pytest.raises(ValueError, match="SELECT/WITH"):
        _validate_single_select_or_with("DELETE FROM customers WHERE TRUE")


def test_validate_accepts_plain_select():
    _validate_single_select_or_with("SELECT * FROM customers")  # must not raise


def test_validate_accepts_with_cte():
    _validate_single_select_or_with("WITH x AS (SELECT 1 AS n) SELECT * FROM x")  # must not raise


def test_validate_tolerates_a_single_trailing_semicolon():
    _validate_single_select_or_with("SELECT 1;")  # must not raise


def test_run_query_rejects_invalid_sql_before_touching_the_client():
    client = FakeClient()
    with pytest.raises(ValueError):
        run_query(client, "DROP TABLE customers", persona="analyst")
    assert client.query_calls == []


def test_run_query_returns_structured_denial_on_forbidden():
    dry_run_job = FakeQueryJob(total_bytes_processed=1000, referenced_tables=["test-project.test_dataset.customers"])
    real_job = FakeQueryJob(raise_forbidden=True)
    client = FakeClient(dry_run_job=dry_run_job, query_job=real_job)
    result = run_query(client, "SELECT email FROM customers", persona="analyst")
    assert result["denied"] is True
    assert result["bytes_processed"] == 1000


def test_run_query_returns_rows_on_success():
    dry_run_job = FakeQueryJob(total_bytes_processed=500, referenced_tables=["test-project.test_dataset.customers"])
    real_job = FakeQueryJob(rows=[{"region": "East"}], job_id="job-123")
    client = FakeClient(dry_run_job=dry_run_job, query_job=real_job)
    result = run_query(client, "SELECT region FROM customers", persona="analyst")
    assert result["denied"] is False
    assert result["job_id"] == "job-123"
    assert result["bytes_processed"] == 500
    assert result["rows"] == [{"region": "East"}]


def test_run_query_attaches_job_labels_including_trace_id():
    client = FakeClient(query_job=FakeQueryJob(rows=[]))
    run_query(client, "SELECT 1", persona="analyst", trace_id="trace-abc")
    _real_call_sql, real_call_config = client.query_calls[1]  # [0] is the dry run
    assert real_call_config.labels["app"] == "governed-agent"
    assert real_call_config.labels["persona"] == "analyst"
    assert real_call_config.labels["trace_id"] == "trace-abc"


def test_run_query_omits_trace_id_label_when_not_provided():
    client = FakeClient(query_job=FakeQueryJob(rows=[]))
    run_query(client, "SELECT 1", persona="analyst")
    _, real_call_config = client.query_calls[1]
    assert "trace_id" not in real_call_config.labels


def test_run_query_caps_returned_rows(monkeypatch):
    monkeypatch.setenv("MAX_ROWS_RETURNED", "2")
    real_job = FakeQueryJob(rows=[{"n": 1}, {"n": 2}, {"n": 3}])
    client = FakeClient(query_job=real_job)
    result = run_query(client, "SELECT n FROM t", persona="analyst")
    assert len(result["rows"]) == 2


def test_run_query_sets_maximum_bytes_billed_from_env(monkeypatch):
    monkeypatch.setenv("MAX_BYTES_BILLED", "12345")
    client = FakeClient(query_job=FakeQueryJob(rows=[]))
    run_query(client, "SELECT 1", persona="analyst")
    _, real_call_config = client.query_calls[1]
    assert real_call_config.maximum_bytes_billed == 12345


# --- Fix 1: dry-run denial must not escape uncaught ---------------------


def test_run_query_returns_structured_denial_when_dry_run_raises_forbidden():
    client = FakeClient(
        raise_on_dry_run=Forbidden("Access Denied: BigQuery column-level security policy tag")
    )
    result = run_query(client, "SELECT email FROM customers", persona="analyst")
    assert result["denied"] is True
    assert result["job_id"] is None
    assert result["tables_referenced"] == []
    assert result["bytes_processed"] is None
    assert "Access Denied" in result["error"]
    # The real (non-dry-run) query must never have been attempted.
    assert len(client.query_calls) == 1


# --- Fix 2: BadRequest / NotFound on the real job must not escape -------


def test_run_query_returns_non_denied_error_on_bad_request():
    client = FakeClient(raise_on_query=BadRequest("Syntax error: bad SQL"))
    result = run_query(client, "SELECT 1", persona="analyst")
    assert result["denied"] is False
    assert "Syntax error" in result["error"]


def test_run_query_returns_non_denied_error_on_not_found():
    client = FakeClient(raise_on_query=NotFound("Table not found: nope"))
    result = run_query(client, "SELECT 1 FROM nope", persona="analyst")
    assert result["denied"] is False
    assert "Table not found" in result["error"]


def test_run_query_returns_non_denied_error_when_result_raises_bad_request():
    real_job = FakeQueryJob(raise_on_result=BadRequest("maximum_bytes_billed exceeded"))
    client = FakeClient(query_job=real_job)
    result = run_query(client, "SELECT 1", persona="analyst")
    assert result["denied"] is False
    assert "maximum_bytes_billed" in result["error"]


# --- Fix 3: rows must be JSON-serializable -------------------------------


def test_run_query_coerces_non_json_native_types_in_rows():
    row = {
        "signup_date": datetime.date(2024, 1, 15),
        "created_at": datetime.datetime(2024, 1, 15, 12, 30, 0, tzinfo=datetime.UTC),
        "amount": decimal.Decimal("19.99"),
        "region": "East",
    }
    real_job = FakeQueryJob(rows=[row], job_id="job-json")
    client = FakeClient(query_job=real_job)
    result = run_query(client, "SELECT * FROM orders", persona="analyst")
    assert result["denied"] is False
    serialized = json.dumps(result["rows"])  # must not raise
    deserialized = json.loads(serialized)
    assert deserialized == [
        {
            "signup_date": "2024-01-15",
            "created_at": "2024-01-15T12:30:00+00:00",
            "amount": 19.99,
            "region": "East",
        }
    ]


# --- Fix 4: persona must be validated ------------------------------------


def test_run_query_rejects_unknown_persona_before_touching_the_client():
    client = FakeClient()
    with pytest.raises(ValueError, match="persona"):
        run_query(client, "SELECT 1", persona="not-a-real-persona")
    assert client.query_calls == []


# --- Fix 5: validator must accept legitimate read-only SQL forms --------


def test_validate_accepts_union_all():
    _validate_single_select_or_with("SELECT 1 AS n UNION ALL SELECT 2 AS n")  # must not raise


def test_validate_accepts_except_distinct():
    _validate_single_select_or_with("SELECT 1 AS n EXCEPT DISTINCT SELECT 2 AS n")  # must not raise


def test_validate_accepts_intersect_distinct():
    _validate_single_select_or_with("SELECT 1 AS n INTERSECT DISTINCT SELECT 2 AS n")  # must not raise


def test_validate_accepts_parenthesized_subquery():
    _validate_single_select_or_with("(SELECT 1)")  # must not raise


def test_validate_still_rejects_create():
    with pytest.raises(ValueError, match="SELECT/WITH"):
        _validate_single_select_or_with("CREATE TABLE t (a INT64)")


def test_validate_still_rejects_insert():
    with pytest.raises(ValueError, match="SELECT/WITH"):
        _validate_single_select_or_with("INSERT INTO t VALUES (1)")


def test_validate_still_rejects_merge():
    with pytest.raises(ValueError, match="SELECT/WITH"):
        _validate_single_select_or_with(
            "MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN DELETE"
        )


def test_validate_still_rejects_truncate():
    with pytest.raises(ValueError, match="SELECT/WITH"):
        _validate_single_select_or_with("TRUNCATE TABLE t")


def test_validate_still_rejects_grant():
    with pytest.raises(ValueError, match="SELECT/WITH"):
        _validate_single_select_or_with("GRANT `roles/bigquery.dataViewer` ON TABLE t TO 'user:a@b.com'")


def test_validate_still_rejects_export_data():
    with pytest.raises(ValueError, match="SELECT/WITH"):
        _validate_single_select_or_with('EXPORT DATA OPTIONS(uri="gs://x/*") AS SELECT 1')


def test_validate_still_rejects_call():
    with pytest.raises(ValueError):
        _validate_single_select_or_with("CALL myproc()")


def test_validate_still_rejects_multiple_statements_with_union():
    with pytest.raises(ValueError, match="single SQL statement"):
        _validate_single_select_or_with("SELECT 1 UNION ALL SELECT 2; DROP TABLE customers")


# --- Fix 6: the validated (stripped) text must be what's executed -------


def test_run_query_sends_stripped_sql_to_dry_run_and_real_query():
    client = FakeClient(query_job=FakeQueryJob(rows=[]))
    run_query(client, "  SELECT 1;  ", persona="analyst")
    dry_run_sql, _ = client.query_calls[0]
    real_sql, _ = client.query_calls[1]
    assert dry_run_sql == "SELECT 1"
    assert real_sql == "SELECT 1"
