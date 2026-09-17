# Phase 2 Plan 2B: Claude Agent SDK Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `agent/agent.py`, exposing `ask()`, which runs the Claude Agent SDK
against exactly three custom tools (`list_tables`, `describe_table`, `run_query` from
Plan 2A's `agent/tools.py`), in `mode="single"` or `mode="reviewed"`, and returns a
structured result matching `BUILD_SPEC.md` §6 Phase 2 item 3.

**Architecture:** A tool-wrapper layer (`_build_warehouse_tools`/`build_tool_server`)
adapts the three plain-Python `agent/tools.py` functions into Claude Agent SDK custom
tools, binding `client`/`persona` via closure so neither is ever a model-settable
argument, and translating every result (success, structured denial, or a caught
exception) into one uniform `{"ok": ..., "error"|"data": ...}` envelope. `mode="single"`
runs one SDK `query()` call; `mode="reviewed"` deterministically runs a second,
tools-disabled `query()` call that reviews the draft and applies a fixed
`APPROVED`/`REVISED:` protocol — never left to model discretion whether review happens.

**Tech Stack:** Python 3.11+, `claude-agent-sdk` (PyPI, `>=0.2.154`), `uv`, `pytest`, `ruff`.

**Spec:** `docs/superpowers/specs/2026-09-17-phase2-plan-2b-agent-sdk-integration-design.md`
(the design this plan implements), and `BUILD_SPEC.md` §6 Phase 2 item 3 (the parent spec
the design argues from).

## Global Constraints

- Every testable function takes its externally-constructed dependencies (`bigquery.Client`,
  the SDK's `query` function) as explicit parameters — never constructed internally. Same
  principle `agent/tools.py` already established in Plan 2A.
- **No tool takes a persona argument** (`BUILD_SPEC.md` §6 Phase 2 item 2, restated for
  the SDK boundary this plan builds): `persona` is bound via closure when the three tool
  functions are built, never read from the model-supplied `args` dict. Every task's tests
  must include a check that a tool's `input_schema` never contains a `persona` key.
- Unit tests require zero network access and zero `ANTHROPIC_API_KEY` — the SDK's `query`
  function and `bigquery.Client` are both injected, and tests substitute fakes for both.
  (`BUILD_SPEC.md` §6 Phase 2 item 7.)
- Model IDs are never hardcoded — always read via `config.get_agent_model()` /
  `config.get_reviewer_model()`, which source the already-declared (currently blank)
  `AGENT_MODEL`/`REVIEWER_MODEL` env vars in `.env.example`.
- `ruff check .` clean, full `pytest` suite passing, before each task's commit.
- The exact Claude Agent SDK field names and call shapes used below (`@tool`,
  `create_sdk_mcp_server`, `ClaudeAgentOptions`, `query`, `ResultMessage`,
  `McpSdkServerConfig`) were confirmed by installing `claude-agent-sdk==0.2.154` into a
  scratch venv and reading its actual source during plan-writing (2026-09-17) — not taken
  on secondhand research alone (which had one confirmed error, corrected in the design
  doc: `allowed_tools` uses bare tool names, not an `mcp__server__*` wildcard). The one
  remaining unconfirmed detail is `ResultMessage.usage`'s exact dict keys (the SDK types
  it as an untyped `dict[str, Any]`) — code below reads it defensively via `.get(..., 0)`
  and is marked `# VERIFY:` at that line; confirming the real keys needs one live,
  human-run API call and is deferred to real-world verification, not blocking this plan's
  tests (which use canned `ResultMessage` objects with known keys).

---

### Task 1: Tool wrapper layer (`agent/agent.py` — `build_tool_server` and friends)

**Files:**
- Modify: `agent/config.py` (add three getters)
- Modify: `pyproject.toml` (add `claude-agent-sdk` dependency)
- Create: `agent/agent.py` (tool-wrapper layer only; Task 2 extends this same file)
- Create: `tests/test_agent.py`

**Interfaces:**
- Consumes: `agent.tools.list_tables(client)`, `agent.tools.describe_table(client, table_name)`,
  `agent.tools.run_query(client, sql, persona)` (all from Plan 2A, already merged, signatures
  unchanged); `agent.config.PERSONAS`, `agent.config.get_project_id()`, `agent.config.get_dataset()`
  (unchanged, from Plan 2A).
- Produces: `agent.agent.build_tool_server(client, persona, run_query_log) -> McpSdkServerConfig`,
  `agent.agent._build_warehouse_tools(client, persona, run_query_log) -> list[SdkMcpTool]`
  (the split-out, directly-testable piece `build_tool_server` calls), `agent.agent._wrap_tool_result(data=None, error=None) -> dict`,
  `agent.agent.SYSTEM_PROMPT: str`. Task 2 imports and uses `build_tool_server` (via a
  factory it defines) and `SYSTEM_PROMPT`.

- [ ] **Step 1: Add three getters to `agent/config.py`**

Add below the existing `resolve_persona_sa_email` function (do not modify anything above it):

```python
def get_agent_model() -> str:
    return os.environ["AGENT_MODEL"]


def get_reviewer_model() -> str:
    return os.environ["REVIEWER_MODEL"]


def get_agent_max_turns() -> int:
    return int(os.environ.get("AGENT_MAX_TURNS", "8"))
```

- [ ] **Step 2: Write failing tests for the new config getters**

`tests/test_config.py` currently starts with:

```python
import pytest

from agent.config import PERSONAS, get_dataset, get_project_id, resolve_persona_sa_email
```

Change the second line to add the three new names (keep everything else in that import
line unchanged):

```python
from agent.config import (
    PERSONAS,
    get_agent_max_turns,
    get_agent_model,
    get_dataset,
    get_project_id,
    get_reviewer_model,
    resolve_persona_sa_email,
)
```

Then append to the end of the file (do not modify any existing test in it):

```python
def test_get_agent_model_reads_env(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    assert get_agent_model() == "claude-sonnet-5"


def test_get_agent_model_raises_when_unset(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    with pytest.raises(KeyError):
        get_agent_model()


def test_get_reviewer_model_reads_env(monkeypatch):
    monkeypatch.setenv("REVIEWER_MODEL", "claude-haiku-4-5")
    assert get_reviewer_model() == "claude-haiku-4-5"


def test_get_agent_max_turns_reads_env(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_TURNS", "12")
    assert get_agent_max_turns() == 12


def test_get_agent_max_turns_defaults_to_eight(monkeypatch):
    monkeypatch.delenv("AGENT_MAX_TURNS", raising=False)
    assert get_agent_max_turns() == 8
```

- [ ] **Step 3: Run the new config tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: if Step 1's code doesn't exist yet, the whole file fails to collect —
`ImportError: cannot import name 'get_agent_model' from 'agent.config'` — since the
import line itself now names functions that don't exist yet, not just the four new tests
individually (this is expected and correct strict-TDD behavior for a changed import line,
not a sign something's wrong). If you already applied Step 1's edit before running this,
every test in the file (existing and new) should instead PASS. Either order is fine, as
long as you end this step with all tests in the file passing once Step 1 is applied.

- [ ] **Step 4: Add the `claude-agent-sdk` dependency to `pyproject.toml`**

In the `[project]` section's `dependencies` list, add one line (keep every existing entry
untouched, including Plan 2A's `google-auth`, `google-cloud-bigquery`, `sqlglot`):

```toml
    "claude-agent-sdk>=0.2.154",
```

Run `uv sync` (or `uv lock` then `uv sync`, whichever this repo's other tasks have used)
so `uv.lock` picks up the new dependency — check `git status` afterward and stage the
updated `uv.lock` alongside `pyproject.toml` in this task's commit, same as every prior
plan's dependency-adding task in this repo.

- [ ] **Step 5: Create `agent/agent.py` with the tool wrapper layer**

```python
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
```

- [ ] **Step 6: Write failing tests for the tool wrapper layer**

Create `tests/test_agent.py`:

```python
import asyncio
import json

from google.api_core.exceptions import Forbidden, NotFound

from agent import tools as tools_module
from agent.agent import _build_warehouse_tools, _wrap_tool_result, build_tool_server


def _run(coro):
    return asyncio.run(coro)


def _tools_by_name(client=None, persona="analyst", run_query_log=None):
    if run_query_log is None:
        run_query_log = []
    if client is None:
        client = object()  # never touched directly by the wrapper layer under test
    return {t.name: t for t in _build_warehouse_tools(client, persona, run_query_log)}, run_query_log


# --- _wrap_tool_result ---

def test_wrap_tool_result_success_shape():
    result = _wrap_tool_result(data={"a": 1})
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "data": {"a": 1}}
    assert result["is_error"] is False


def test_wrap_tool_result_error_shape():
    result = _wrap_tool_result(error="denied")
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": False, "error": "denied"}
    assert result["is_error"] is True


# --- list_tables tool ---

def test_list_tables_tool_wraps_success(monkeypatch):
    monkeypatch.setattr(tools_module, "list_tables", lambda client: ["customers", "orders"])
    by_name, _ = _tools_by_name()
    result = _run(by_name["list_tables"].handler({}))
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "data": ["customers", "orders"]}
    assert result["is_error"] is False


def test_list_tables_tool_catches_forbidden(monkeypatch):
    def raise_forbidden(client):
        raise Forbidden("Access Denied: dataset governed_analytics")
    monkeypatch.setattr(tools_module, "list_tables", raise_forbidden)
    by_name, _ = _tools_by_name()
    result = _run(by_name["list_tables"].handler({}))
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert "Access Denied" in payload["error"]
    assert result["is_error"] is True


def test_list_tables_tool_input_schema_is_empty():
    by_name, _ = _tools_by_name()
    assert by_name["list_tables"].input_schema == {}


# --- describe_table tool ---

def test_describe_table_tool_wraps_success(monkeypatch):
    fake = {"table": "customers", "columns": [{"name": "email", "type": "STRING", "description": "", "restricted": True}]}
    monkeypatch.setattr(tools_module, "describe_table", lambda client, table_name: fake)
    by_name, _ = _tools_by_name()
    result = _run(by_name["describe_table"].handler({"table_name": "customers"}))
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "data": fake}


def test_describe_table_tool_catches_not_found(monkeypatch):
    def raise_not_found(client, table_name):
        raise NotFound("Table not found: bogus")
    monkeypatch.setattr(tools_module, "describe_table", raise_not_found)
    by_name, _ = _tools_by_name()
    result = _run(by_name["describe_table"].handler({"table_name": "bogus"}))
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert "not found" in payload["error"].lower()
    assert result["is_error"] is True


def test_describe_table_tool_input_schema_has_table_name():
    by_name, _ = _tools_by_name()
    assert set(by_name["describe_table"].input_schema) == {"table_name"}


# --- run_query tool ---

def test_run_query_tool_wraps_success_and_logs(monkeypatch):
    fake_result = {
        "denied": False, "rows": [{"n": 1}], "job_id": "j1",
        "bytes_processed": 100, "tables_referenced": ["p.d.customers"],
    }
    monkeypatch.setattr(tools_module, "run_query", lambda client, sql, persona: fake_result)
    by_name, log = _tools_by_name()
    result = _run(by_name["run_query"].handler({"sql": "SELECT 1"}))
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "data": fake_result}
    assert log == [{"sql": "SELECT 1", "result": fake_result}]


def test_run_query_tool_wraps_denied_result_without_marking_is_error(monkeypatch):
    # A structured denial from tools.run_query is not a tool-call failure —
    # it's a successful call that reports a denial as data, same as BUILD_SPEC's
    # "returns a clear structured error (not an exception)".
    denied_result = {
        "denied": True, "error": "Forbidden", "job_id": None,
        "bytes_processed": None, "tables_referenced": [],
    }
    monkeypatch.setattr(tools_module, "run_query", lambda client, sql, persona: denied_result)
    by_name, log = _tools_by_name()
    result = _run(by_name["run_query"].handler({"sql": "SELECT email FROM customers"}))
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "data": denied_result}
    assert result["is_error"] is False
    assert log == [{"sql": "SELECT email FROM customers", "result": denied_result}]


def test_run_query_tool_catches_value_error_without_logging(monkeypatch):
    def raise_value_error(client, sql, persona):
        raise ValueError("Only SELECT/WITH statements are allowed, got Create")
    monkeypatch.setattr(tools_module, "run_query", raise_value_error)
    by_name, log = _tools_by_name()
    result = _run(by_name["run_query"].handler({"sql": "CREATE TABLE x (a INT)"}))
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert "SELECT/WITH" in payload["error"]
    assert result["is_error"] is True
    assert log == []  # never reached BigQuery, so nothing to log


def test_run_query_tool_input_schema_has_no_persona_field():
    by_name, _ = _tools_by_name()
    assert set(by_name["run_query"].input_schema) == {"sql"}


def test_run_query_tool_binds_persona_from_closure_not_args(monkeypatch):
    captured = {}
    def fake_run_query(client, sql, persona):
        captured["persona"] = persona
        return {"denied": False, "rows": [], "job_id": "j", "bytes_processed": 0, "tables_referenced": []}
    monkeypatch.setattr(tools_module, "run_query", fake_run_query)
    by_name, _ = _tools_by_name(persona="support_east")
    _run(by_name["run_query"].handler({"sql": "SELECT 1"}))
    assert captured["persona"] == "support_east"


# --- build_tool_server ---

def test_build_tool_server_returns_sdk_mcp_server_config():
    server = build_tool_server(client=object(), persona="analyst", run_query_log=[])
    assert server["type"] == "sdk"
    assert server["name"] == "warehouse"
    assert server["instance"] is not None
```

- [ ] **Step 7: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_agent.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.agent'` (if run before
Step 5) or all PASS (if run after Step 5's file already exists) — same either-order note
as Step 3.

- [ ] **Step 8: Run the full suite and ruff**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass (Plan 2A's existing suite plus this task's new tests), ruff clean.

- [ ] **Step 9: Commit**

```bash
git add agent/config.py agent/agent.py pyproject.toml uv.lock tests/test_config.py tests/test_agent.py
git commit -m "feat: add config getters and SDK tool-wrapper layer for the agent"
```

---

### Task 2: `ask()` entry point — mode=single, mode=reviewed, structured result

**Files:**
- Modify: `agent/agent.py` (add to the file Task 1 created — do not remove or rewrite
  anything Task 1 added)
- Modify: `tests/test_agent.py` (add to the file Task 1 created)

**Interfaces:**
- Consumes: `agent.agent.build_tool_server`, `agent.agent.SYSTEM_PROMPT` (Task 1, same
  file); `agent.config.PERSONAS`, `agent.config.get_agent_model()`,
  `agent.config.get_reviewer_model()`, `agent.config.get_agent_max_turns()` (Task 1's
  config additions); `claude_agent_sdk.ClaudeAgentOptions`, `claude_agent_sdk.ResultMessage`,
  `claude_agent_sdk.query`.
- Produces: `agent.agent.ask(question, persona, client, mode="single", query_fn=query, tool_server_factory=_default_tool_server_factory) -> dict`
  — the plan's public entry point. Nothing later in this plan consumes it (Plan 2C's CLI
  will, but that's a future plan).

- [ ] **Step 1: Add the SDK imports and orchestration helpers to `agent/agent.py`**

At the top of `agent/agent.py`, extend the existing import block (do not remove Task 1's
imports): add `import time` next to the existing `import json` (both stdlib, same group),
and add `ClaudeAgentOptions`, `ResultMessage`, `query` into the existing
`from claude_agent_sdk import create_sdk_mcp_server, tool` line rather than a second import
line for the same module. Don't hand-order the merged names — run `uv run ruff check --fix .`
after this step and let it fix import ordering; only worry about it manually if `ruff check`
still complains after `--fix`.

Also add, next to the existing `from agent import tools` line:

```python
from agent import config
```

Append below `SYSTEM_PROMPT` (Task 1's last top-level definition in the file):

```python
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
```

- [ ] **Step 2: Write failing tests for the orchestration layer**

Add two lines to `tests/test_agent.py`'s existing import block (alongside Task 1's
imports, not replacing them): `import pytest` (stdlib group already has `asyncio`/`json`;
`pytest` goes in the third-party group) and `from claude_agent_sdk import ResultMessage`.
Then append to the end of the file:

```python
from agent.agent import (
    _combine_draft_and_review,
    _sum_usage,
    ask,
)


def _result_message(result="", usage=None, subtype="success"):
    return ResultMessage(
        subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="s1", result=result, usage=usage or {},
    )


def _fake_query_fn_yielding(*messages):
    async def fake_query_fn(*, prompt, options):
        for m in messages:
            yield m
    return fake_query_fn


def _seeded_factory(run_query_log):
    def factory(client, persona):
        return {"type": "sdk", "name": "warehouse", "instance": None}, run_query_log
    return factory


# --- _combine_draft_and_review ---

def test_combine_approved_keeps_draft():
    assert _combine_draft_and_review("3 regions.", "APPROVED") == "3 regions."


def test_combine_revised_replaces_draft():
    result = _combine_draft_and_review("3 regions.", "REVISED: Actually, 2 regions.")
    assert result == "Actually, 2 regions."


def test_combine_unrecognized_response_falls_back_to_draft():
    result = _combine_draft_and_review("3 regions.", "I'm not sure how to respond.")
    assert result == "3 regions."


# --- _sum_usage ---

def test_sum_usage_adds_both():
    assert _sum_usage({"input_tokens": 100, "output_tokens": 20}, {"input_tokens": 50, "output_tokens": 10}) == {
        "input_tokens": 150, "output_tokens": 30,
    }


def test_sum_usage_handles_missing_keys():
    assert _sum_usage({}, {"input_tokens": 5}) == {"input_tokens": 5, "output_tokens": 0}


# --- ask() validation ---

def test_ask_rejects_unknown_persona_without_calling_query_fn(monkeypatch):
    calls = []
    async def tracking_query_fn(*, prompt, options):
        calls.append(1)
        yield _result_message()
    with pytest.raises(ValueError, match="persona"):
        _run(ask("q", "not_a_persona", client=object(), query_fn=tracking_query_fn))
    assert calls == []


def test_ask_rejects_unknown_mode(monkeypatch):
    with pytest.raises(ValueError, match="mode"):
        _run(ask("q", "analyst", client=object(), mode="bogus", query_fn=_fake_query_fn_yielding(_result_message())))


# --- ask() mode="single" ---

def test_ask_single_mode_assembles_structured_result(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("REVIEWER_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("AGENT_MAX_TURNS", "8")

    run_query_log = [
        {"sql": "SELECT region, COUNT(*) FROM customers GROUP BY region", "result": {
            "denied": False, "rows": [{"region": "East", "n": 3}], "job_id": "j1",
            "bytes_processed": 500, "tables_referenced": ["proj.ds.customers"],
        }},
    ]
    query_fn = _fake_query_fn_yielding(_result_message(result="3 regions.", usage={"input_tokens": 200, "output_tokens": 40}))

    result = _run(ask(
        "How many regions?", "analyst", client=object(), mode="single",
        query_fn=query_fn, tool_server_factory=_seeded_factory(run_query_log),
    ))

    assert result["answer"] == "3 regions."
    assert result["sql_list"] == ["SELECT region, COUNT(*) FROM customers GROUP BY region"]
    assert result["job_ids"] == ["j1"]
    assert result["tables_referenced"] == ["proj.ds.customers"]
    assert result["bytes_processed"] == 500
    assert result["denials"] == []
    assert result["input_tokens"] == 200
    assert result["output_tokens"] == 40
    assert result["trace_id"] is None
    assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0


def test_ask_single_mode_filters_none_job_ids_and_sums_denials(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("REVIEWER_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("AGENT_MAX_TURNS", "8")

    denied_entry = {"sql": "SELECT email FROM customers", "result": {
        "denied": True, "error": "Forbidden", "job_id": None,
        "bytes_processed": None, "tables_referenced": [],
    }}
    ok_entry = {"sql": "SELECT region FROM customers", "result": {
        "denied": False, "rows": [], "job_id": "j2",
        "bytes_processed": 300, "tables_referenced": ["proj.ds.customers"],
    }}
    query_fn = _fake_query_fn_yielding(_result_message(result="Denied for email.", usage={"input_tokens": 10, "output_tokens": 5}))

    result = _run(ask(
        "Show me emails.", "analyst", client=object(), mode="single",
        query_fn=query_fn, tool_server_factory=_seeded_factory([denied_entry, ok_entry]),
    ))

    assert result["job_ids"] == ["j2"]  # the denied entry's None job_id is filtered out
    assert result["bytes_processed"] == 300  # None coerced to 0 before summing
    assert result["denials"] == [denied_entry]


# --- ask() mode="reviewed" ---

def test_ask_reviewed_mode_approved_keeps_draft(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("REVIEWER_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("AGENT_MAX_TURNS", "8")

    draft_message = _result_message(result="3 regions.", usage={"input_tokens": 200, "output_tokens": 40})
    review_message = _result_message(result="APPROVED", usage={"input_tokens": 80, "output_tokens": 5})
    call_log = []

    async def sequenced_query_fn(*, prompt, options):
        call_log.append(prompt)
        if len(call_log) == 1:
            yield draft_message
        else:
            yield review_message

    result = _run(ask(
        "How many regions?", "analyst", client=object(), mode="reviewed",
        query_fn=sequenced_query_fn, tool_server_factory=_seeded_factory([]),
    ))

    assert result["answer"] == "3 regions."
    assert result["input_tokens"] == 280  # 200 + 80
    assert result["output_tokens"] == 45  # 40 + 5
    assert len(call_log) == 2  # drafting call, then the review call — always both


def test_ask_reviewed_mode_revised_replaces_draft(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("REVIEWER_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("AGENT_MAX_TURNS", "8")

    draft_message = _result_message(result="3 regions.", usage={"input_tokens": 200, "output_tokens": 40})
    review_message = _result_message(result="REVISED: Actually 2 regions after excluding test data.", usage={"input_tokens": 80, "output_tokens": 12})
    call_count = {"n": 0}

    async def sequenced_query_fn(*, prompt, options):
        call_count["n"] += 1
        yield draft_message if call_count["n"] == 1 else review_message

    result = _run(ask(
        "How many regions?", "analyst", client=object(), mode="reviewed",
        query_fn=sequenced_query_fn, tool_server_factory=_seeded_factory([]),
    ))

    assert result["answer"] == "Actually 2 regions after excluding test data."
```

`_run` and `_seeded_factory`'s use of the `run_query_log` list are both reused from/
consistent with Task 1's helpers already in this file — no need to redefine `_run`.

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_agent.py -v`
Expected: if Step 1's code doesn't exist yet, the whole file fails to collect —
`ImportError: cannot import name 'ask' from 'agent.agent'` — since the new import
statement at the bottom names functions that don't exist yet (same collection-failure
shape as Task 1's Step 3, and equally expected/correct). If you already applied Step 1's
edit before running this, every test in the file (Task 1's and Task 2's) should PASS.

- [ ] **Step 4: Run the full suite and ruff**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add agent/agent.py tests/test_agent.py
git commit -m "feat: add ask() entry point with single/reviewed modes"
```
