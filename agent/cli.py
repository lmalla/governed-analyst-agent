"""Typer CLI: `ask --persona <name> --mode single|reviewed "<question>"`.

This is the first place in the codebase where credentials.get_credentials()
and a real bigquery.Client are constructed together outside of a test
double -- every prior plan deliberately deferred this to "a later plan's
CLI," and this is that plan. It's also where telemetry (agent.session /
llm.turn / tool.run_query spans) and lineage (one JSONL record per answer)
get wired around a single agent.ask() call.
"""
import asyncio
import hashlib
import uuid
from pathlib import Path

import typer
from google.cloud import bigquery

from agent import agent, config, credentials, lineage, telemetry

app = typer.Typer()

LINEAGE_PATH = Path("lineage/records.jsonl")


def _question_hash(question: str) -> str:
    return hashlib.sha256(question.encode()).hexdigest()[:16]


def _build_client(persona: str) -> bigquery.Client:
    creds = credentials.get_credentials(persona)
    return bigquery.Client(project=config.get_project_id(), credentials=creds)


def _create_query_spans(tracer, result: dict) -> None:
    """One tool.run_query child span per query attempt.

    result["denials"]/result["errors"] are each already a list of full
    {"sql": ..., "result": {...}} log entries. A "succeeded" span is
    reconstructed for every sql_list entry that isn't in either of those
    two lists, using the aggregate job_ids/bytes_processed/tables_referenced
    fields -- exact per-succeeded-query byte attribution isn't separable
    when more than one query succeeded in the same call, which is an
    accepted simplification (see the design doc's Architecture §3).
    """
    denied_or_errored_sql = {e["sql"] for e in result["denials"]} | {e["sql"] for e in result["errors"]}
    for entry in result["denials"]:
        with tracer.start_as_current_span("tool.run_query", attributes={
            "sql": entry["sql"], "denied": True, "bytes": entry["result"].get("bytes_processed") or 0,
            "job_id": entry["result"].get("job_id") or "",
        }):
            pass
    for entry in result["errors"]:
        with tracer.start_as_current_span("tool.run_query", attributes={
            "sql": entry["sql"], "denied": False, "bytes": entry["result"].get("bytes_processed") or 0,
            "job_id": entry["result"].get("job_id") or "",
        }):
            pass
    for sql in result["sql_list"]:
        if sql in denied_or_errored_sql:
            continue
        # No per-query row count is available here: ask()'s result doesn't
        # expose it (only the raw run_query_log, which isn't part of the
        # public contract, has each call's "rows" list) -- bytes/job_id are
        # the aggregate totals across the whole call, same imprecision as
        # the bytes attribution above.
        with tracer.start_as_current_span("tool.run_query", attributes={
            "sql": sql, "denied": False, "bytes": result["bytes_processed"],
        }):
            pass


def _create_llm_turn_spans(tracer, mode: str, result: dict) -> None:
    call_count = 2 if mode == "reviewed" else 1
    per_call_input = result["input_tokens"] // call_count
    per_call_output = result["output_tokens"] // call_count
    for _ in range(call_count):
        with tracer.start_as_current_span("llm.turn", attributes={
            "input_tokens": per_call_input, "output_tokens": per_call_output,
        }):
            pass


async def run_ask(question: str, persona: str, mode: str) -> dict:
    """The testable core of the `ask` command -- everything ask_command()
    does, minus Typer's own arg parsing and asyncio.run() wrapper."""
    client = _build_client(persona)
    session_id = str(uuid.uuid4())
    tracer = telemetry.get_tracer()
    with tracer.start_as_current_span("agent.session", attributes={
        "persona": persona, "mode": mode, "question_hash": _question_hash(question),
    }) as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        result = await agent.ask(
            question, persona, client, mode=mode, trace_id=trace_id, session_id=session_id
        )
        _create_llm_turn_spans(tracer, mode, result)
        _create_query_spans(tracer, result)
    lineage.write_record(LINEAGE_PATH, question, persona, mode, session_id, result)
    return result


@app.command()
def ask_command(
    question: str,
    persona: str = typer.Option(..., "--persona"),
    mode: str = typer.Option("single", "--mode"),
) -> None:
    result = asyncio.run(run_ask(question, persona, mode))
    typer.echo(result["answer"])
    if result["denials"]:
        n = len(result["denials"])
        typer.echo(f"({n} quer{'y was' if n == 1 else 'ies were'} denied)")
    if result["errors"]:
        n = len(result["errors"])
        typer.echo(f"({n} quer{'y' if n == 1 else 'ies'} failed with an error)")


if __name__ == "__main__":
    app()
