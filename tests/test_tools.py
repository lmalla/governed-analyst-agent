import pytest
from google.api_core.exceptions import Forbidden

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
    ):
        self.total_bytes_processed = total_bytes_processed
        self.referenced_tables = referenced_tables or []
        self._rows = rows or []
        self.job_id = job_id
        self._raise_forbidden = raise_forbidden

    def result(self, max_results=None):
        if self._raise_forbidden:
            raise Forbidden("Access Denied: BigQuery column-level security policy tag")
        return self._rows[:max_results] if max_results is not None else self._rows


class FakeClient:
    def __init__(self, tables=None, table_schema=None, dry_run_job=None, query_job=None):
        self._tables = tables or []
        self._table_schema = table_schema or []
        self._dry_run_job = dry_run_job or FakeQueryJob()
        self._query_job = query_job or FakeQueryJob()
        self.query_calls = []

    def list_tables(self, dataset_ref):
        return self._tables

    def get_table(self, table_ref):
        return FakeTable(self._table_schema)

    def query(self, sql, job_config=None):
        self.query_calls.append((sql, job_config))
        if job_config is not None and getattr(job_config, "dry_run", False):
            return self._dry_run_job
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
