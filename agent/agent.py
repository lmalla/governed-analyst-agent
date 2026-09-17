"""Claude Agent SDK integration: wraps agent/tools.py functions as SDK custom tools.

This module version has only the tool-wrapper layer (build_tool_server and its
helpers). mode=single/reviewed and the public ask() entry point are added on top
of this same file by a later task.
"""
import json

from claude_agent_sdk import create_sdk_mcp_server, tool
from google.api_core.exceptions import BadRequest, Forbidden, NotFound
from google.cloud import bigquery

from agent import tools


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
