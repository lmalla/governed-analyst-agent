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

from agent import agent, credentials, lineage, telemetry

app = typer.Typer()

LINEAGE_PATH = Path("lineage/records.jsonl")


def _question_hash(question: str) -> str:
    return hashlib.sha256(question.encode()).hexdigest()[:16]


def _create_query_spans(tracer, result: dict) -> None:
    """One tool.run_query child span per query attempt.

    result["denials"]/result["errors"] are each already a list of full
    {"sql": ..., "result": {...}} log entries -- each entry's own
    result["bytes_processed"] is accurate per-entry (it comes from that
    individual agent.tools.run_query call, not an aggregate), so it's
    attached as-is. job_id is never attached on denied/error spans: every
    failure path in agent/tools.py's run_query hardcodes job_id=None (a
    failed dry run or query job never has a real BigQuery job ID to report),
    so showing it there would always be empty/misleading.

    A "success" span is reconstructed for every sql_list entry that isn't in
    either of those two lists. Per-succeeded-query bytes/job_id are only
    attached when exactly one query succeeded in the whole call -- with more
    than one success, the aggregate result["bytes_processed"]/job_ids can't
    be split between individual queries, so neither attribute is guessed at
    (see the design doc's Architecture §3). Aggregate totals across the
    whole call are always available on the agent.session root span instead.
    """
    denied_or_errored_sql = {e["sql"] for e in result["denials"]} | {e["sql"] for e in result["errors"]}
    for entry in result["denials"]:
        with tracer.start_as_current_span("tool.run_query", attributes={
            "sql": entry["sql"], "outcome": "denied", "bytes": entry["result"].get("bytes_processed") or 0,
        }):
            pass
    for entry in result["errors"]:
        with tracer.start_as_current_span("tool.run_query", attributes={
            "sql": entry["sql"], "outcome": "error", "bytes": entry["result"].get("bytes_processed") or 0,
        }):
            pass
    succeeded_sql = [sql for sql in result["sql_list"] if sql not in denied_or_errored_sql]
    single_success = (
        len(result["sql_list"]) - len(result["denials"]) - len(result["errors"]) == 1
    )
    for sql in succeeded_sql:
        attributes = {"sql": sql, "outcome": "success"}
        if single_success:
            attributes["bytes"] = result["bytes_processed"]
            attributes["job_id"] = result["job_ids"][0]
        with tracer.start_as_current_span("tool.run_query", attributes=attributes):
            pass


def _create_llm_turn_spans(tracer, mode: str, result: dict) -> None:
    """One llm.turn span per model call that actually produced a result.

    mode="single" is always 1 call. mode="reviewed" is normally 2 (draft +
    review), except when the review call never yielded a ResultMessage at
    all (result["review_outcome"] == "review_failed") -- only the draft
    call happened in that case, so only 1 span is emitted.
    """
    if mode == "reviewed" and result["review_outcome"] == "review_failed":
        call_count = 1
    else:
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
    does, minus Typer's own arg parsing and asyncio.run() wrapper.

    A lineage record is written no matter how this ends: on success it
    carries the full result, and on any exception (building the client,
    getting a tracer, or agent.ask() itself) it carries whatever trace_id
    was available plus the error message, so no run is ever silently
    missing from the audit trail. The original exception is always
    re-raised afterward -- this is purely about not losing the record,
    not about suppressing the failure from run_ask's caller.
    """
    session_id = str(uuid.uuid4())
    trace_id = None
    try:
        client = credentials.build_client(persona)
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
            span.set_attributes({
                "bytes_processed": result["bytes_processed"],
                "job_ids": result["job_ids"],
                "input_tokens": result["input_tokens"],
                "output_tokens": result["output_tokens"],
            })
    except Exception as e:
        lineage.write_record(
            path=LINEAGE_PATH, question=question, persona=persona, mode=mode,
            session_id=session_id, result={"trace_id": trace_id}, error=str(e),
        )
        raise
    lineage.write_record(
        path=LINEAGE_PATH, question=question, persona=persona, mode=mode,
        session_id=session_id, result=result,
    )
    return result


@app.command("ask")
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
