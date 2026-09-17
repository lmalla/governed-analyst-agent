"""Claude Agent SDK integration: wraps agent/tools.py functions as SDK custom tools.

This module version has only the tool-wrapper layer (build_tool_server and its
helpers). mode=single/reviewed and the public ask() entry point are added on top
of this same file by a later task.
"""
import json
import time

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query, tool
from google.api_core.exceptions import BadRequest, Forbidden, NotFound
from google.cloud import bigquery

from agent import config, tools


def _wrap_tool_result(data=None, error=None) -> dict:
    """Uniform agent-facing envelope for all three warehouse tools.

    Settles Plan 2A's parked return-shape inconsistency (list_tables/describe_table
    raised uncaught exceptions on Forbidden; run_query already returned a structured
    dict) at this adapter boundary, without reshaping agent/tools.py's own already-
    reviewed, merged contract.
    """
    if error is not None:
        payload = {"ok": False, "error": error}
    else:
        payload = {"ok": True, "data": data}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
        "is_error": error is not None,
    }


def _build_warehouse_tools(client: bigquery.Client, persona: str, run_query_log: list[dict]):
    """Builds the three SdkMcpTool objects (from the @tool decorator).

    Split out from build_tool_server so tests can call each tool's .handler
    directly without going through the full MCP server/transport machinery.
    client and persona are bound via closure so neither is ever a model-settable
    tool argument (BUILD_SPEC: "No tool takes a persona argument"). run_query_log
    is a plain list the caller owns; every run_query call that actually reaches
    agent.tools.run_query appends its sql and raw result dict to it (a call
    rejected by SQL validation before reaching BigQuery is not logged, since no
    query was attempted).
    """

    @tool("list_tables", "List tables in the governed dataset.", {})
    async def list_tables_tool(args: dict) -> dict:
        try:
            data = tools.list_tables(client)
        except (Forbidden, BadRequest, NotFound) as e:
            return _wrap_tool_result(error=str(e))
        return _wrap_tool_result(data=data)

    @tool(
        "describe_table",
        "Describe a table's schema, including which columns are restricted PII.",
        {"table_name": str},
    )
    async def describe_table_tool(args: dict) -> dict:
        try:
            data = tools.describe_table(client, args["table_name"])
        except (Forbidden, BadRequest, NotFound) as e:
            return _wrap_tool_result(error=str(e))
        return _wrap_tool_result(data=data)

    @tool(
        "run_query",
        "Run a single read-only SELECT/WITH SQL query against the governed dataset.",
        {"sql": str},
    )
    async def run_query_tool(args: dict) -> dict:
        sql = args["sql"]
        try:
            result = tools.run_query(client, sql, persona)
        except ValueError as e:
            # tools.run_query raises ValueError for SQL that fails validation
            # (not a single SELECT/WITH) before it ever reaches BigQuery — not
            # logged to run_query_log, since no query was actually attempted.
            return _wrap_tool_result(error=str(e))
        run_query_log.append({"sql": sql, "result": result})
        return _wrap_tool_result(data=result)

    return [list_tables_tool, describe_table_tool, run_query_tool]


def build_tool_server(client: bigquery.Client, persona: str, run_query_log: list[dict]):
    """Builds the in-process MCP server exposing list_tables/describe_table/run_query."""
    warehouse_tools = _build_warehouse_tools(client, persona, run_query_log)
    return create_sdk_mcp_server(name="warehouse", version="1.0.0", tools=warehouse_tools)


SYSTEM_PROMPT = (
    "You are a data analyst agent with access to a governed BigQuery dataset "
    "through the list_tables, describe_table, and run_query tools. Answer the "
    "user's question using only these tools. If a tool reports a denial or "
    "restriction, never attempt to work around it or guess at the restricted "
    "data — report the denial to the user plainly, as part of your answer."
)

REVIEWER_SYSTEM_PROMPT = (
    "You are reviewing a data analyst agent's draft answer for correctness and "
    "for any restricted or PII data that should not have been included. You "
    "will be given the original question, the draft answer, and the SQL "
    "queries that were run with their results. You have no tools and cannot "
    "run new queries — review only what you are given. "
    "Respond with exactly one of two forms, and always start your response "
    "with one of these two literal tokens: "
    "'APPROVED' if the draft answer is correct and contains no restricted "
    "data, or 'REVISED: <corrected answer>' if it needs correction."
)


def _default_tool_server_factory(client: bigquery.Client, persona: str):
    """Builds a fresh tool server and the run_query_log it logs into.

    Split out as its own factory (rather than inlined in _run_single) so
    tests can inject a pre-seeded run_query_log and a trivial fake server —
    letting orchestration tests (does ask() correctly read a ResultMessage
    and assemble the structured result) run independently of the tool-
    wrapper tests Task 1 already covers.
    """
    run_query_log: list[dict] = []
    server = build_tool_server(client, persona, run_query_log)
    return server, run_query_log


def _build_review_prompt(question: str, draft_answer: str, run_query_log: list[dict]) -> str:
    queries_summary = json.dumps(
        [{"sql": entry["sql"], "result": entry["result"]} for entry in run_query_log],
        indent=2,
        default=str,
    )
    return (
        f"Original question: {question}\n\n"
        f"Draft answer: {draft_answer}\n\n"
        f"SQL queries run and their results:\n{queries_summary}"
    )


def _combine_draft_and_review(draft: str, review_text: str) -> str:
    if review_text.startswith("REVISED:"):
        return review_text.removeprefix("REVISED:").strip()
    return draft  # covers "APPROVED" and any unrecognized response, fail-safe to the draft


def _sum_usage(usage_a: dict, usage_b: dict) -> dict:
    return {
        "input_tokens": usage_a.get("input_tokens", 0) + usage_b.get("input_tokens", 0),
        "output_tokens": usage_a.get("output_tokens", 0) + usage_b.get("output_tokens", 0),
    }


async def _run_single(
    question: str,
    client: bigquery.Client,
    persona: str,
    max_turns: int,
    query_fn,
    tool_server_factory=_default_tool_server_factory,
):
    """Runs one drafting agent call. Returns (answer_text, run_query_log, usage)."""
    server, run_query_log = tool_server_factory(client, persona)
    options = ClaudeAgentOptions(
        model=config.get_agent_model(),
        tools=[],
        mcp_servers={"warehouse": server},
        allowed_tools=["list_tables", "describe_table", "run_query"],
        max_turns=max_turns,
        system_prompt=SYSTEM_PROMPT,
    )
    result_message = None
    async for message in query_fn(prompt=question, options=options):
        if isinstance(message, ResultMessage):
            result_message = message
    return result_message.result or "", run_query_log, result_message.usage or {}


async def _run_reviewed(
    question: str,
    client: bigquery.Client,
    persona: str,
    max_turns: int,
    query_fn,
    tool_server_factory=_default_tool_server_factory,
):
    """Runs the drafting call, then a deterministic, tools-disabled review call.

    Review always happens for mode="reviewed" — it is not left to the drafting
    agent's discretion whether to invoke a reviewer.
    """
    draft, run_query_log, usage_1 = await _run_single(
        question, client, persona, max_turns, query_fn, tool_server_factory
    )
    review_prompt = _build_review_prompt(question, draft, run_query_log)
    review_options = ClaudeAgentOptions(
        model=config.get_reviewer_model(),
        tools=[],
        max_turns=1,
        system_prompt=REVIEWER_SYSTEM_PROMPT,
    )
    review_result_message = None
    async for message in query_fn(prompt=review_prompt, options=review_options):
        if isinstance(message, ResultMessage):
            review_result_message = message
    final_answer = _combine_draft_and_review(draft, review_result_message.result or "")
    combined_usage = _sum_usage(usage_1, review_result_message.usage or {})
    return final_answer, run_query_log, combined_usage


async def ask(
    question: str,
    persona: str,
    client: bigquery.Client,
    mode: str = "single",
    query_fn=query,
    tool_server_factory=_default_tool_server_factory,
) -> dict:
    if persona not in config.PERSONAS:
        raise ValueError(f"Unknown persona: {persona!r}. Valid personas: {sorted(config.PERSONAS)}")
    if mode not in ("single", "reviewed"):
        raise ValueError(f"Unknown mode: {mode!r}. Valid modes: 'single', 'reviewed'")

    max_turns = config.get_agent_max_turns()
    start = time.monotonic()
    if mode == "single":
        answer, run_query_log, usage = await _run_single(
            question, client, persona, max_turns, query_fn, tool_server_factory
        )
    else:
        answer, run_query_log, usage = await _run_reviewed(
            question, client, persona, max_turns, query_fn, tool_server_factory
        )
    latency_ms = int((time.monotonic() - start) * 1000)

    return {
        "answer": answer,
        "sql_list": [entry["sql"] for entry in run_query_log],
        "job_ids": [
            entry["result"]["job_id"] for entry in run_query_log
            if entry["result"]["job_id"] is not None
        ],
        "tables_referenced": sorted(
            {t for entry in run_query_log for t in entry["result"]["tables_referenced"]}
        ),
        "bytes_processed": sum(
            entry["result"]["bytes_processed"] or 0 for entry in run_query_log
        ),
        "denials": [entry for entry in run_query_log if entry["result"]["denied"]],
        # VERIFY: ResultMessage.usage is an untyped dict (claude_agent_sdk 0.2.154's
        # own type hint is dict[str, Any]) — input_tokens/output_tokens assumed to
        # match the direct Anthropic Messages API's standard usage keys. .get(...)
        # degrades to 0 rather than raising if that assumption is wrong; confirm
        # against a real call during human-run real-world verification.
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "latency_ms": latency_ms,
        "trace_id": None,  # wired in Plan 2C once agent/telemetry.py exists
    }
