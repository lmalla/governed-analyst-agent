# Phase 2 Plan 2A: Warehouse Tools Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pure-Python BigQuery interaction layer the agent will use — persona credential resolution and the three warehouse tools (`list_tables`, `describe_table`, `run_query`) — with zero Claude Agent SDK dependency, so it's fully unit-testable with a mocked BigQuery client and needs no research into the Agent SDK's tool-registration API. That research and the actual agent wiring is a separate, later plan (Phase 2 Plan 2B).

**Architecture:** Two tasks. Task 1 builds `agent/config.py` (env loading, explicit persona-name → service-account-prefix registry) and `agent/credentials.py` (impersonated credentials per persona, cached). Task 2 builds `agent/tools.py` as plain functions taking a BigQuery `Client` as an explicit parameter (dependency injection, not internally constructed) — this is what makes every function trivially testable against a hand-rolled fake client with no network access. `run_query` validates the SQL is a single `SELECT`/`WITH` statement (via `sqlglot`, plus a belt-and-suspenders raw-string check `sqlglot`'s own multi-statement handling isn't confidently documented enough to rely on alone), dry-runs first for cost/table metadata, executes for real with a byte cap and job labels, and converts BigQuery's column-level-denial exception into a structured result rather than letting it raise.

**Tech Stack:** Python 3.11+, `google-auth` (`impersonated_credentials`), `google-cloud-bigquery`, `sqlglot`, `uv`/`pytest`/`ruff`.

**Spec:** `BUILD_SPEC.md` (repo root) — this plan implements §6 Phase 2 steps 1-2 (`credentials.py`, `tools.py`) and the `config.py` file named in §3's repo structure but not separately itemized in Phase 2's numbered steps. Steps 3-6 (`agent.py`, `telemetry.py`, `lineage.py`, `cli.py`) are later plans (2B, 2C).

## Global Constraints

- Never run commands that create, modify, or delete cloud resources. This plan writes code that, when run for real by a human, queries BigQuery — but the plan's own tests must never touch real GCP (per BUILD_SPEC.md §6 Phase 2 step 7: "no cloud access needed to run pytest").
- No secrets, keys, or hardcoded project-specific values. Everything comes from `.env` / environment variables, consistent with the rest of this project.
- **Core design principle (BUILD_SPEC.md §1, do not violate):** the persona is chosen by the caller and bound to the tool's credentials in code — never selectable by the model. **No function in `tools.py` takes a persona argument for the purpose of choosing which data to access** — `run_query` takes `persona` only as a label for job attribution (logging/audit), never as something that changes which credentials get used; the `client` parameter passed in already carries the resolved persona's credentials.
- `list_tables`/`describe_table` run as the persona's own impersonated credentials, not a broader human ADC — this is a direct consequence of the core design principle, and it also means the raw/staging datasets (already inaccessible to every persona by IAM, per Phase 1) are naturally invisible without any extra filtering logic.
- Row-level denial (row access policies) is **silent** in BigQuery — a query just returns fewer/zero rows, no exception. Column-level denial (policy tags) **raises `google.api_core.exceptions.Forbidden`**. Only the latter needs exception handling in `run_query`; do not add special-case logic for row-level denial, there is nothing to catch.
- Python 3.11+, `uv` for dependency management, `ruff` for lint, `pytest` for tests.
- When unsure about a current API, check the official docs rather than guessing, and leave a `# VERIFY:` comment if still uncertain. (This plan already did that research — google-auth's `impersonated_credentials.Credentials` constructor, `google-cloud-bigquery`'s `list_tables`/`get_table`/`QueryJobConfig`/`SchemaField.policy_tags`/`PolicyTagList.names`, and `google.api_core.exceptions.Forbidden` — but `sqlglot`'s exact multi-statement-rejection behavior across versions was NOT confidently resolved by research, hence the explicit raw-string semicolon check as a non-negotiable defense layer, not just a nicety.)

---

### Task 1: `agent/config.py` + `agent/credentials.py` — persona registry and impersonated credentials

**Files:**
- Create: `agent/__init__.py` (empty — makes `agent` an importable package)
- Create: `agent/config.py`
- Create: `agent/credentials.py`
- Create: `tests/test_config.py`
- Create: `tests/test_credentials.py`
- Modify: `pyproject.toml` (add `google-auth` dependency — `google-cloud-bigquery`, needed by Task 2, transitively pulls in a compatible `google-auth`, but this task's own code imports it directly, so declare it explicitly rather than relying on a transitive pin)

**Interfaces:**
- Produces: `config.PERSONAS` (the persona registry dict), `config.resolve_persona_sa_email(persona: str) -> str`, `config.get_project_id() -> str`, `config.get_dataset() -> str`, and `credentials.get_credentials(persona: str) -> google.auth.impersonated_credentials.Credentials` — Task 2's tests use `config.get_project_id()`/`config.get_dataset()` to build expected table references, and Phase 2 Plan 2B will use `credentials.get_credentials()` to build the BigQuery client the agent's tools run against.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
import pytest

from agent.config import PERSONAS, get_dataset, get_project_id, resolve_persona_sa_email


def test_personas_registry_has_exactly_the_three_known_personas():
    assert PERSONAS == {
        "analyst": "persona-analyst",
        "support_east": "persona-support-east",
        "governance": "persona-governance",
    }


def test_resolve_persona_sa_email_for_analyst(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project-123")
    assert resolve_persona_sa_email("analyst") == "persona-analyst@test-project-123.iam.gserviceaccount.com"


def test_resolve_persona_sa_email_for_support_east(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project-123")
    assert (
        resolve_persona_sa_email("support_east")
        == "persona-support-east@test-project-123.iam.gserviceaccount.com"
    )


def test_resolve_persona_sa_email_for_governance(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project-123")
    assert resolve_persona_sa_email("governance") == "persona-governance@test-project-123.iam.gserviceaccount.com"


def test_resolve_persona_sa_email_raises_for_unknown_persona(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project-123")
    with pytest.raises(ValueError, match="Unknown persona"):
        resolve_persona_sa_email("nonexistent")


def test_get_project_id_reads_env_var(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "my-project")
    assert get_project_id() == "my-project"


def test_get_dataset_reads_env_var(monkeypatch):
    monkeypatch.setenv("BQ_DATASET", "my_dataset")
    assert get_dataset() == "my_dataset"
```

```python
# tests/test_credentials.py
import pytest
from google.auth import impersonated_credentials
from google.auth.credentials import AnonymousCredentials

from agent import credentials as credentials_module
from agent.credentials import get_credentials


@pytest.fixture(autouse=True)
def clear_credentials_cache():
    credentials_module._credentials_cache.clear()
    yield
    credentials_module._credentials_cache.clear()


@pytest.fixture(autouse=True)
def mock_google_auth_default(monkeypatch):
    # AnonymousCredentials is a real, lightweight google-auth class meant
    # exactly for this kind of stand-in use — safer than a bare Mock, which
    # risks failing an isinstance/type check inside impersonated_credentials.
    monkeypatch.setattr(
        credentials_module,
        "google_auth_default",
        lambda: (AnonymousCredentials(), "fake-source-project"),
    )


@pytest.fixture(autouse=True)
def env_project(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project-123")


def test_get_credentials_returns_impersonated_credentials_instance():
    creds = get_credentials("analyst")
    assert isinstance(creds, impersonated_credentials.Credentials)


def test_get_credentials_raises_for_unknown_persona():
    with pytest.raises(ValueError, match="Unknown persona"):
        get_credentials("nonexistent")


def test_get_credentials_caches_per_persona():
    first = get_credentials("analyst")
    second = get_credentials("analyst")
    assert first is second


def test_get_credentials_different_personas_get_different_objects():
    analyst_creds = get_credentials("analyst")
    governance_creds = get_credentials("governance")
    assert analyst_creds is not governance_creds
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_config.py tests/test_credentials.py -v`
Expected: FAIL — `agent/config.py` and `agent/credentials.py` don't exist yet.

- [ ] **Step 3: Add the dependency**

Add to `pyproject.toml`'s `dependencies` list:

```toml
dependencies = [
    "faker>=30.0",
    "dbt-bigquery>=1.8",
    "requests>=2.32",
    "google-auth>=2.35",
]
```

Run: `uv sync`

- [ ] **Step 4: Write config.py**

```python
# agent/__init__.py
```

```python
# agent/config.py
"""Loads .env-sourced configuration and the persona registry.

An explicit dict (short persona name -> service account name prefix)
rather than a string transform (e.g. replacing underscores with hyphens) —
this avoids a subtle bug class where a future persona name doesn't map
cleanly, and makes every valid persona name grep-able in one place.
"""
import os

PERSONAS = {
    "analyst": "persona-analyst",
    "support_east": "persona-support-east",
    "governance": "persona-governance",
}


def get_project_id() -> str:
    return os.environ["GCP_PROJECT_ID"]


def get_dataset() -> str:
    return os.environ["BQ_DATASET"]


def resolve_persona_sa_email(persona: str) -> str:
    if persona not in PERSONAS:
        raise ValueError(f"Unknown persona: {persona!r}. Valid personas: {sorted(PERSONAS)}")
    sa_prefix = PERSONAS[persona]
    return f"{sa_prefix}@{get_project_id()}.iam.gserviceaccount.com"
```

- [ ] **Step 5: Write credentials.py**

```python
# agent/credentials.py
"""Returns impersonated credentials for a persona, using Application
Default Credentials as the source. Cached per persona — repeated calls for
the same persona reuse the same Credentials object.
"""
from google.auth import default as google_auth_default
from google.auth import impersonated_credentials

from agent.config import resolve_persona_sa_email

# roles/bigquery.jobUser and roles/bigquery.dataViewer are the two roles
# every persona SA has (see BUILD_SPEC.md §5); this scope covers both.
BIGQUERY_SCOPES = ["https://www.googleapis.com/auth/bigquery"]

_credentials_cache: dict[str, impersonated_credentials.Credentials] = {}


def get_credentials(persona: str) -> impersonated_credentials.Credentials:
    if persona in _credentials_cache:
        return _credentials_cache[persona]

    sa_email = resolve_persona_sa_email(persona)  # raises ValueError for an unknown persona
    source_credentials, _ = google_auth_default()
    creds = impersonated_credentials.Credentials(
        source_credentials=source_credentials,
        target_principal=sa_email,
        target_scopes=BIGQUERY_SCOPES,
    )
    _credentials_cache[persona] = creds
    return creds
```

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_config.py tests/test_credentials.py -v`
Expected: PASS (13 passed).

- [ ] **Step 7: Run the full suite and commit**

Run: `uv run pytest -v` — all passing.
Run: `uv run ruff check .` — clean.

```bash
git add agent/__init__.py agent/config.py agent/credentials.py tests/test_config.py tests/test_credentials.py pyproject.toml uv.lock
git commit -m "feat: add persona registry and impersonated credentials"
```

---

### Task 2: `agent/tools.py` — list_tables, describe_table, run_query

**Files:**
- Create: `agent/tools.py`
- Create: `tests/test_tools.py`
- Modify: `pyproject.toml` (add `google-cloud-bigquery` and `sqlglot` dependencies)

**Interfaces:**
- Consumes: `config.get_project_id()`/`config.get_dataset()` (Task 1) to build fully-qualified table references. Consumes a `google.cloud.bigquery.Client` instance as an explicit parameter to every function — **never constructs a `Client` internally** — this is what makes every function testable against a fake client with no network access, and is also the seam where Plan 2B will pass in a client built from `credentials.get_credentials(persona)`.
- Produces: `list_tables(client) -> list[str]`, `describe_table(client, table_name: str) -> dict`, `run_query(client, sql: str, persona: str, trace_id: str | None = None) -> dict` — Plan 2B wraps these as Agent SDK tools; Plan 2C's `telemetry.py` supplies the real `trace_id`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tools.py
import pytest
from google.api_core.exceptions import Forbidden

from agent.tools import describe_table, list_tables, run_query, _validate_single_select_or_with


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
    real_call_sql, real_call_config = client.query_calls[1]  # [0] is the dry run
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
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_tools.py -v`
Expected: FAIL — `agent/tools.py` doesn't exist yet.

- [ ] **Step 3: Add the dependencies**

Add to `pyproject.toml`'s `dependencies` list:

```toml
dependencies = [
    "faker>=30.0",
    "dbt-bigquery>=1.8",
    "requests>=2.32",
    "google-auth>=2.35",
    "google-cloud-bigquery>=3.26",
    "sqlglot>=25.0",
]
```

Run: `uv sync`

- [ ] **Step 4: Write tools.py**

```python
# agent/tools.py
"""Plain, Agent-SDK-free warehouse tools: list_tables, describe_table,
run_query. Every function takes a BigQuery Client as an explicit
parameter rather than constructing one internally — this is what makes
them testable against a fake client with zero network access, and is the
seam where the real agent (a later plan) passes in a client built from a
persona's impersonated credentials.

Row-level denial (row access policies) is silent in BigQuery — a query
just returns fewer/zero rows. Column-level denial (policy tags) raises
google.api_core.exceptions.Forbidden. Only the latter needs handling here.
"""
import os

import sqlglot
from google.api_core.exceptions import Forbidden
from google.cloud import bigquery
from sqlglot import exp

from agent import config


def list_tables(client: bigquery.Client) -> list[str]:
    dataset_ref = f"{config.get_project_id()}.{config.get_dataset()}"
    return [t.table_id for t in client.list_tables(dataset_ref)]


def describe_table(client: bigquery.Client, table_name: str) -> dict:
    table_ref = f"{config.get_project_id()}.{config.get_dataset()}.{table_name}"
    table = client.get_table(table_ref)
    columns = []
    for field in table.schema:
        restricted = bool(field.policy_tags and field.policy_tags.names)
        columns.append(
            {
                "name": field.name,
                "type": field.field_type,
                "description": field.description or "",
                "restricted": restricted,
            }
        )
    return {"table": table_name, "columns": columns}


def _validate_single_select_or_with(sql: str) -> None:
    # Defense in depth: reject multiple statements on the raw string before
    # even parsing, regardless of sqlglot's own multi-statement handling
    # (VERIFY: not confidently documented as consistent across versions —
    # do not remove this check even if sqlglot's parser is later confirmed
    # to reject multi-statement input on its own).
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise ValueError("Only a single SQL statement is allowed.")
    try:
        parsed = sqlglot.parse_one(stripped, dialect="bigquery")
    except sqlglot.errors.ParseError as e:
        raise ValueError(f"Could not parse SQL: {e}") from e
    if not isinstance(parsed, (exp.Select, exp.With)):
        raise ValueError(f"Only SELECT/WITH statements are allowed, got {type(parsed).__name__}")


def run_query(
    client: bigquery.Client,
    sql: str,
    persona: str,
    trace_id: str | None = None,
) -> dict:
    _validate_single_select_or_with(sql)

    dry_run_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    dry_run_job = client.query(sql, job_config=dry_run_config)

    labels = {"app": "governed-agent", "persona": persona}
    if trace_id:
        labels["trace_id"] = trace_id

    max_bytes_billed = int(os.environ.get("MAX_BYTES_BILLED", "100000000"))
    max_rows_returned = int(os.environ.get("MAX_ROWS_RETURNED", "200"))

    run_config = bigquery.QueryJobConfig(
        maximum_bytes_billed=max_bytes_billed,
        labels=labels,
    )
    tables_referenced = [str(t) for t in dry_run_job.referenced_tables]

    try:
        query_job = client.query(sql, job_config=run_config)
        rows = list(query_job.result(max_results=max_rows_returned))
    except Forbidden as e:
        return {
            "denied": True,
            "error": str(e),
            "job_id": None,
            "bytes_processed": dry_run_job.total_bytes_processed,
            "tables_referenced": tables_referenced,
        }

    return {
        "denied": False,
        "rows": [dict(row) for row in rows],
        "job_id": query_job.job_id,
        "bytes_processed": query_job.total_bytes_processed,
        "tables_referenced": tables_referenced,
    }
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_tools.py -v`
Expected: PASS (16 passed).

- [ ] **Step 6: Run the full suite and commit**

Run: `uv run pytest -v` — all passing.
Run: `uv run ruff check .` — clean.

```bash
git add agent/tools.py tests/test_tools.py pyproject.toml uv.lock
git commit -m "feat: add warehouse tools (list_tables, describe_table, run_query)"
```

---

## Self-Review Notes

- **Spec coverage:** covers BUILD_SPEC.md §6 Phase 2 steps 1-2 in full, plus `config.py` from §3's repo structure. Explicitly excludes steps 3-6 (`agent.py`, `telemetry.py`, `lineage.py`, `cli.py`) — later plans.
- **Placeholder scan:** no TBD/TODO; every step has complete, real content, including the fake-client test doubles.
- **Type/name consistency:** `config.get_project_id()`/`config.get_dataset()` used identically in both `credentials.py`'s docstring context and `tools.py`'s table-reference construction. `persona` string values (`"analyst"`, `"support_east"`, `"governance"`) match `PERSONAS`' keys exactly, and match Phase 0/1's established persona naming throughout.
- **Verified against real docs, not guessed:** `impersonated_credentials.Credentials`'s constructor signature, `SchemaField.policy_tags` / `PolicyTagList.names`, `QueryJobConfig`'s `dry_run`/`maximum_bytes_billed`/`labels`, and `google.api_core.exceptions.Forbidden` were all confirmed against current docs before being written into this plan. `sqlglot`'s multi-statement behavior was NOT confidently resolved — the plan compensates with an explicit, non-removable raw-string check rather than trusting the parser alone, and this reasoning is recorded in both the Global Constraints and the code's own comment so it doesn't get "simplified away" later.
- **Testability:** every function takes its BigQuery `Client` (or, for `credentials.py`, its `google.auth.default` source) as an injectable/patchable seam, so the full suite runs with zero network access, satisfying BUILD_SPEC.md §6 Phase 2 step 7 before that step is even reached as its own task.
