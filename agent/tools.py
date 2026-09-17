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
        # ValueError (not TypeError) is intentional: this signals an invalid
        # SQL statement, which callers and tests treat the same way as the
        # other validation failures in this function.
        raise ValueError(  # noqa: TRY004
            f"Only SELECT/WITH statements are allowed, got {type(parsed).__name__}"
        )


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
        "bytes_processed": dry_run_job.total_bytes_processed,
        "tables_referenced": tables_referenced,
    }
