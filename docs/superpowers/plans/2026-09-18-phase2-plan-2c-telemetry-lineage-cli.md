# Phase 2 Plan 2C: Telemetry, Lineage, and CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close out Phase 2. Thread `session_id`/`trace_id` through `agent/tools.py` and
`agent/agent.py`, add `review_outcome`/`errors` to `ask()`'s result, then build
`agent/telemetry.py` (OTel tracer), `agent/lineage.py` (JSONL provenance records), and
`agent/cli.py` (the Typer `ask` command that wires everything together and is the first
real, non-test construction of a `bigquery.Client` from persona credentials).

**Architecture:** Task 1 extends two already-merged files (`tools.py`, `agent.py`) with
purely additive, optional parameters — no existing call site breaks. Tasks 2 and 3 are
independent new files. Task 4 integrates all three into a CLI command.

**Tech Stack:** Python 3.11+, `typer`, `opentelemetry-api==1.44.0`,
`opentelemetry-sdk==1.44.0`, `opentelemetry-exporter-gcp-trace==1.15.0`, `uv`, `pytest`,
`ruff`.

**Spec:** `docs/superpowers/specs/2026-09-18-phase2-plan-2c-telemetry-lineage-cli-design.md`
(the design this plan implements), and `BUILD_SPEC.md` §6 Phase 2 items 4-7.

## Global Constraints

- Every testable function takes its externally-constructed dependencies (`bigquery.Client`,
  file paths, the SDK's `query` function) as explicit parameters — never constructed
  internally. Same principle every prior plan in this project established.
- Unit tests require zero network access, zero real GCP credentials, and zero
  `ANTHROPIC_API_KEY` — `telemetry.py` tests never call the real `CloudTraceSpanExporter`;
  `lineage.py` tests write to a `tmp_path`; `cli.py` tests never construct a real
  `bigquery.Client` or call `credentials.get_credentials`.
- `agent/telemetry.py` authenticates as the human's own default credentials (ADC), never
  persona impersonation — trace/span writing is operational observability, not governed
  data access, and does not need a new IAM grant.
- `agent/agent.py` stays free of any OpenTelemetry import or dependency — spans are
  created by the caller (`cli.py`), never inside `agent.py`.
- `ruff check .` clean, full `pytest` suite passing, before each task's commit.
- The exact package names, import paths, and constructor signatures for
  `opentelemetry-exporter-gcp-trace` used below (`CloudTraceSpanExporter(project_id=None, client=None, resource_regex=None)`,
  `opentelemetry.sdk.trace.export.in_memory_span_exporter.InMemorySpanExporter`) were
  confirmed by installing the real packages into a scratch venv and reading their source
  during plan-writing (2026-09-18) — not guessed. `TracerProvider().get_tracer(name)`
  (an instance method, not the global `trace.get_tracer()`) was chosen and verified
  specifically because it never touches OpenTelemetry's global tracer-provider singleton,
  which can only be set once per process — using the global form would make `get_tracer()`
  non-idempotent across multiple test invocations in the same pytest process.
- **`CloudTraceSpanExporter` is deprecated** (confirmed while self-verifying the package;
  its own construction emits a `DeprecationWarning`). The recommended replacement
  (`OTLPSpanExporter` + explicit gRPC credential wiring) is meaningfully more complex and
  needs its own from-scratch verification. This was raised with, and confirmed by, the
  human: use the deprecated exporter deliberately for this prototype (see the note in
  Task 2's `telemetry.py` code below for the full rationale and migration-guide link) —
  do not "fix" this by swapping in `OTLPSpanExporter` without checking with the human
  first, since that's a real scope/complexity increase, not a drop-in substitution.

---

### Task 1: Thread session_id/trace_id through tools.py and agent.py; add review_outcome/errors

**Files:**
- Modify: `agent/tools.py`
- Modify: `agent/agent.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_agent.py`

**Interfaces:**
- Consumes: nothing new — extends existing, already-merged functions with optional params.
- Produces: `tools.run_query(..., session_id=None)` (new param); `agent.ask(..., trace_id=None, session_id=None) -> dict` now includes `"review_outcome"` and `"errors"` keys, and `"trace_id"` echoes the caller-supplied value instead of always `None`. Task 4 (`cli.py`) consumes this extended `ask()` signature and result shape directly.

- [ ] **Step 1: Add `session_id` to `agent/tools.py`'s `run_query`**

In `agent/tools.py`, change the `run_query` signature (currently lines 89-94):

```python
def run_query(
    client: bigquery.Client,
    sql: str,
    persona: str,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> dict:
```

And change the labels block (currently lines 116-118):

```python
    labels = {"app": "governed-agent", "persona": persona}
    if trace_id:
        labels["trace_id"] = trace_id
    if session_id:
        labels["session_id"] = session_id
```

Nothing else in the function changes.

- [ ] **Step 2: Write failing tests for `session_id`**

Append to `tests/test_tools.py`, directly after the existing
`test_run_query_omits_trace_id_label_when_not_provided` function (mirrors that test and
`test_run_query_attaches_job_labels_including_trace_id` exactly, for `session_id`):

```python
def test_run_query_attaches_session_id_label():
    client = FakeClient(query_job=FakeQueryJob(rows=[]))
    run_query(client, "SELECT 1", persona="analyst", session_id="session-xyz")
    _, real_call_config = client.query_calls[1]  # [0] is the dry run
    assert real_call_config.labels["session_id"] == "session-xyz"


def test_run_query_omits_session_id_label_when_not_provided():
    client = FakeClient(query_job=FakeQueryJob(rows=[]))
    run_query(client, "SELECT 1", persona="analyst")
    _, real_call_config = client.query_calls[1]
    assert "session_id" not in real_call_config.labels
```

- [ ] **Step 3: Run `tests/test_tools.py` to verify the new tests pass**

Run: `uv run pytest tests/test_tools.py -v`
Expected: all tests pass, including the two new ones.

- [ ] **Step 4: Update `agent/agent.py`'s tool-wrapper layer to thread `trace_id`/`session_id`**

Replace `_build_warehouse_tools` (currently lines 34-90) with:

```python
def _build_warehouse_tools(
    client: bigquery.Client,
    persona: str,
    run_query_log: list[dict],
    trace_id: str | None = None,
    session_id: str | None = None,
):
    """Builds the three SdkMcpTool objects (from the @tool decorator).

    Split out from build_tool_server so tests can call each tool's .handler
    directly without going through the full MCP server/transport machinery.
    client, persona, trace_id, and session_id are all bound via closure so
    none is ever a model-settable tool argument (BUILD_SPEC: "No tool takes
    a persona argument" -- the same reasoning applies to trace_id/session_id,
    which are caller-supplied observability metadata, not something a model
    should be able to set). run_query_log is a plain list the caller owns;
    every run_query call that actually reaches agent.tools.run_query appends
    its sql and raw result dict to it (a call rejected by SQL validation
    before reaching BigQuery is not logged, since no query was attempted).
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
            result = tools.run_query(client, sql, persona, trace_id=trace_id, session_id=session_id)
        except (Forbidden, BadRequest, NotFound, ValueError) as e:
            # ValueError: tools.run_query raises this for SQL that fails
            # validation (not a single SELECT/WITH) before it ever reaches
            # BigQuery. Forbidden/BadRequest/NotFound: the dry-run branch
            # inside tools.run_query only wraps Forbidden internally, so a
            # bad table/column name (BadRequest/NotFound) from the dry run
            # can still propagate out uncaught -- caught here for the same
            # uniform {"ok": false, "error": ...} envelope as the other two
            # tools. None of these represent a successfully attempted query,
            # so none are logged to run_query_log.
            return _wrap_tool_result(error=str(e))
        run_query_log.append({"sql": sql, "result": result})
        return _wrap_tool_result(data=result)

    return [list_tables_tool, describe_table_tool, run_query_tool]
```

(The only change from the current file: the new `trace_id`/`session_id` parameters, the
docstring line mentioning them, and `run_query_tool`'s call now passing
`trace_id=trace_id, session_id=session_id`. Everything else is byte-for-byte identical to
the current function.)

Replace `build_tool_server` (currently lines 93-96) with:

```python
def build_tool_server(
    client: bigquery.Client,
    persona: str,
    run_query_log: list[dict],
    trace_id: str | None = None,
    session_id: str | None = None,
):
    """Builds the in-process MCP server exposing list_tables/describe_table/run_query."""
    warehouse_tools = _build_warehouse_tools(
        client, persona, run_query_log, trace_id=trace_id, session_id=session_id
    )
    return create_sdk_mcp_server(name="warehouse", version="1.0.0", tools=warehouse_tools)
```

Replace `_default_tool_server_factory` (currently lines 124-135) with:

```python
def _default_tool_server_factory(
    client: bigquery.Client,
    persona: str,
    trace_id: str | None = None,
    session_id: str | None = None,
):
    """Builds a fresh tool server and the run_query_log it logs into.

    Split out as its own factory (rather than inlined in _run_single) so
    tests can inject a pre-seeded run_query_log and a trivial fake server —
    letting orchestration tests (does ask() correctly read a ResultMessage
    and assemble the structured result) run independently of the tool-
    wrapper tests Task 1 [of Plan 2B] already covers.
    """
    run_query_log: list[dict] = []
    server = build_tool_server(
        client, persona, run_query_log, trace_id=trace_id, session_id=session_id
    )
    return server, run_query_log
```

- [ ] **Step 5: Update `_run_single`/`_run_reviewed`/`ask()` to thread `trace_id`/`session_id` and add `review_outcome`/`errors`**

Add this new function directly above `_combine_draft_and_review` (currently at line 151):

```python
def _classify_review_response(review_text: str) -> str:
    stripped = review_text.strip()
    if stripped.startswith("REVISED:"):
        return "revised"
    if stripped.startswith("APPROVED"):
        return "approved"
    return "unrecognized"
```

Replace `_run_single` (currently lines 165-194) with:

```python
async def _run_single(
    question: str,
    client: bigquery.Client,
    persona: str,
    max_turns: int,
    query_fn,
    tool_server_factory=_default_tool_server_factory,
    trace_id: str | None = None,
    session_id: str | None = None,
):
    """Runs one drafting agent call. Returns (answer_text, run_query_log, usage)."""
    server, run_query_log = tool_server_factory(client, persona, trace_id=trace_id, session_id=session_id)
    options = ClaudeAgentOptions(
        model=config.get_agent_model(),
        tools=[],
        mcp_servers={"warehouse": server},
        allowed_tools=["list_tables", "describe_table", "run_query"],
        max_turns=max_turns,
        system_prompt=SYSTEM_PROMPT,
        strict_mcp_config=True,
        setting_sources=[],
    )
    result_message = None
    async for message in query_fn(prompt=question, options=options):
        if isinstance(message, ResultMessage):
            result_message = message
    if result_message is None:
        # The SDK stream ended without ever yielding a ResultMessage (empty
        # stream, transport failure, cancelled turn). Return a structured,
        # non-crashing result instead of blowing up on `.result`/`.usage`.
        return "[agent error: no result message received from the SDK]", run_query_log, {}
    return result_message.result or "", run_query_log, result_message.usage or {}
```

(Only change: the two new parameters and passing them to `tool_server_factory`.)

Replace `_run_reviewed` (currently lines 197-234) with:

```python
async def _run_reviewed(
    question: str,
    client: bigquery.Client,
    persona: str,
    max_turns: int,
    query_fn,
    tool_server_factory=_default_tool_server_factory,
    trace_id: str | None = None,
    session_id: str | None = None,
):
    """Runs the drafting call, then a deterministic, tools-disabled review call.

    Review always happens for mode="reviewed" — it is not left to the drafting
    agent's discretion whether to invoke a reviewer.
    """
    draft, run_query_log, usage_1 = await _run_single(
        question, client, persona, max_turns, query_fn, tool_server_factory,
        trace_id=trace_id, session_id=session_id,
    )
    review_prompt = _build_review_prompt(question, draft, run_query_log)
    review_options = ClaudeAgentOptions(
        model=config.get_reviewer_model(),
        tools=[],
        max_turns=1,
        system_prompt=REVIEWER_SYSTEM_PROMPT,
        strict_mcp_config=True,
        setting_sources=[],
    )
    review_result_message = None
    async for message in query_fn(prompt=review_prompt, options=review_options):
        if isinstance(message, ResultMessage):
            review_result_message = message
    if review_result_message is None:
        # The review call itself never yielded a ResultMessage. The design's
        # fail-safe-to-draft principle (an unrecognized review response keeps
        # the draft) extends to this case too: fall back to the draft answer
        # and count only the drafting call's usage. "review_failed" is a
        # distinct outcome from "unrecognized" -- the reviewer never
        # responded at all, versus responding without a recognized prefix.
        return draft, run_query_log, usage_1, "review_failed"
    review_text = review_result_message.result or ""
    final_answer = _combine_draft_and_review(draft, review_text)
    combined_usage = _sum_usage(usage_1, review_result_message.usage or {})
    return final_answer, run_query_log, combined_usage, _classify_review_response(review_text)
```

(Changes from the current function: the two new parameters passed through to
`_run_single`; the fallback return now has a 4th element `"review_failed"`; a new
`review_text` local variable is classified via `_classify_review_response` and returned
as the 4th element on the success path. `_combine_draft_and_review` itself is unchanged —
still called with the same two arguments.)

Replace `ask` (currently lines 237-285) with:

```python
async def ask(
    question: str,
    persona: str,
    client: bigquery.Client,
    mode: str = "single",
    query_fn=query,
    tool_server_factory=_default_tool_server_factory,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    if persona not in config.PERSONAS:
        raise ValueError(f"Unknown persona: {persona!r}. Valid personas: {sorted(config.PERSONAS)}")
    if mode not in ("single", "reviewed"):
        raise ValueError(f"Unknown mode: {mode!r}. Valid modes: 'single', 'reviewed'")

    max_turns = config.get_agent_max_turns()
    start = time.monotonic()
    if mode == "single":
        answer, run_query_log, usage = await _run_single(
            question, client, persona, max_turns, query_fn, tool_server_factory,
            trace_id=trace_id, session_id=session_id,
        )
        review_outcome = None
    else:
        answer, run_query_log, usage, review_outcome = await _run_reviewed(
            question, client, persona, max_turns, query_fn, tool_server_factory,
            trace_id=trace_id, session_id=session_id,
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
        "errors": [
            entry for entry in run_query_log
            if entry["result"].get("error") is not None and not entry["result"]["denied"]
        ],
        "review_outcome": review_outcome,
        # VERIFY: ResultMessage.usage is an untyped dict (claude_agent_sdk 0.2.154's
        # own type hint is dict[str, Any]) — input_tokens/output_tokens assumed to
        # match the direct Anthropic Messages API's standard usage keys. .get(...)
        # degrades to 0 rather than raising if that assumption is wrong; confirm
        # against a real call during human-run real-world verification.
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "latency_ms": latency_ms,
        "trace_id": trace_id,
    }
```

- [ ] **Step 6: Update `_seeded_factory` in `tests/test_agent.py`**

`tests/test_agent.py`'s `_seeded_factory` helper (currently lines 218-221) is called by
every existing `ask()`/`_run_single`/`_run_reviewed` test in the file, and its inner
`factory` function currently only accepts `(client, persona)`. Since `_run_single`/
`_run_reviewed` now call `tool_server_factory(client, persona, trace_id=trace_id, session_id=session_id)`
unconditionally, every one of those existing tests would break with a `TypeError` unless
this helper is updated first. Change it to:

```python
def _seeded_factory(run_query_log):
    def factory(client, persona, trace_id=None, session_id=None):
        return {"type": "sdk", "name": "warehouse", "instance": None}, run_query_log
    return factory
```

- [ ] **Step 7: Run the full suite to confirm nothing broke**

Run: `uv run pytest -q`
Expected: every pre-existing test still passes (this confirms Step 6's fix was necessary
and sufficient) — this step catches regressions before writing the new tests below.

- [ ] **Step 8: Write failing tests for the new threading and result fields**

Append to `tests/test_agent.py`:

```python
# --- trace_id/session_id threading through the tool wrapper ---

def test_run_query_tool_passes_trace_id_and_session_id_to_tools_run_query(monkeypatch):
    captured = {}
    def fake_run_query(client, sql, persona, trace_id=None, session_id=None):
        captured["trace_id"] = trace_id
        captured["session_id"] = session_id
        return {"denied": False, "rows": [], "job_id": "j", "bytes_processed": 0, "tables_referenced": []}
    monkeypatch.setattr(tools_module, "run_query", fake_run_query)
    by_name = {
        t.name: t for t in _build_warehouse_tools(
            client=object(), persona="analyst", run_query_log=[],
            trace_id="trace-abc", session_id="session-xyz",
        )
    }
    _run(by_name["run_query"].handler({"sql": "SELECT 1"}))
    assert captured["trace_id"] == "trace-abc"
    assert captured["session_id"] == "session-xyz"


# --- _classify_review_response ---

def test_classify_review_response_approved():
    from agent.agent import _classify_review_response
    assert _classify_review_response("APPROVED") == "approved"


def test_classify_review_response_revised():
    from agent.agent import _classify_review_response
    assert _classify_review_response("REVISED: fixed answer") == "revised"


def test_classify_review_response_unrecognized():
    from agent.agent import _classify_review_response
    assert _classify_review_response("I'm not sure.") == "unrecognized"


def test_classify_review_response_tolerates_leading_whitespace():
    from agent.agent import _classify_review_response
    assert _classify_review_response("  \nREVISED: fixed") == "revised"


# --- ask()'s review_outcome field ---

def test_ask_single_mode_review_outcome_is_none(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    result = _run(ask(
        "q", "analyst", client=object(), mode="single",
        query_fn=_fake_query_fn_yielding(_result_message(result="answer")),
        tool_server_factory=_seeded_factory([]),
    ))
    assert result["review_outcome"] is None


def test_ask_reviewed_mode_review_outcome_approved(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("REVIEWER_MODEL", "claude-haiku-4-5")
    draft_message = _result_message(result="3 regions.")
    review_message = _result_message(result="APPROVED")
    call_count = {"n": 0}
    async def sequenced_query_fn(*, prompt, options):
        call_count["n"] += 1
        yield draft_message if call_count["n"] == 1 else review_message
    result = _run(ask(
        "q", "analyst", client=object(), mode="reviewed",
        query_fn=sequenced_query_fn, tool_server_factory=_seeded_factory([]),
    ))
    assert result["review_outcome"] == "approved"


def test_ask_reviewed_mode_review_outcome_revised(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("REVIEWER_MODEL", "claude-haiku-4-5")
    draft_message = _result_message(result="3 regions.")
    review_message = _result_message(result="REVISED: 2 regions.")
    call_count = {"n": 0}
    async def sequenced_query_fn(*, prompt, options):
        call_count["n"] += 1
        yield draft_message if call_count["n"] == 1 else review_message
    result = _run(ask(
        "q", "analyst", client=object(), mode="reviewed",
        query_fn=sequenced_query_fn, tool_server_factory=_seeded_factory([]),
    ))
    assert result["review_outcome"] == "revised"


def test_ask_reviewed_mode_review_outcome_unrecognized(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("REVIEWER_MODEL", "claude-haiku-4-5")
    draft_message = _result_message(result="3 regions.")
    review_message = _result_message(result="I have no opinion.")
    call_count = {"n": 0}
    async def sequenced_query_fn(*, prompt, options):
        call_count["n"] += 1
        yield draft_message if call_count["n"] == 1 else review_message
    result = _run(ask(
        "q", "analyst", client=object(), mode="reviewed",
        query_fn=sequenced_query_fn, tool_server_factory=_seeded_factory([]),
    ))
    assert result["review_outcome"] == "unrecognized"


def test_ask_reviewed_mode_review_outcome_review_failed(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("REVIEWER_MODEL", "claude-haiku-4-5")
    draft_message = _result_message(result="3 regions.")
    call_count = {"n": 0}
    async def sequenced_query_fn(*, prompt, options):
        call_count["n"] += 1
        if call_count["n"] == 1:
            yield draft_message
        # second call (the review) yields nothing -- empty stream
    result = _run(ask(
        "q", "analyst", client=object(), mode="reviewed",
        query_fn=sequenced_query_fn, tool_server_factory=_seeded_factory([]),
    ))
    assert result["review_outcome"] == "review_failed"
    assert result["answer"] == "3 regions."  # fail-safe to the draft


# --- ask()'s errors field ---

def test_ask_single_mode_errors_field_separates_from_denials(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    denied_entry = {"sql": "SELECT email FROM customers", "result": {
        "denied": True, "error": "Forbidden", "job_id": None,
        "bytes_processed": None, "tables_referenced": [],
    }}
    error_entry = {"sql": "SELECT bogus_col FROM customers", "result": {
        "denied": False, "error": "Bad Request: no such column", "job_id": None,
        "bytes_processed": 100, "tables_referenced": ["p.d.customers"],
    }}
    ok_entry = {"sql": "SELECT region FROM customers", "result": {
        "denied": False, "rows": [], "job_id": "j1",
        "bytes_processed": 300, "tables_referenced": ["p.d.customers"],
    }}
    result = _run(ask(
        "q", "analyst", client=object(), mode="single",
        query_fn=_fake_query_fn_yielding(_result_message(result="done")),
        tool_server_factory=_seeded_factory([denied_entry, error_entry, ok_entry]),
    ))
    assert result["denials"] == [denied_entry]
    assert result["errors"] == [error_entry]


# --- ask()'s trace_id echoing ---

def test_ask_echoes_caller_supplied_trace_id(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    result = _run(ask(
        "q", "analyst", client=object(), mode="single",
        query_fn=_fake_query_fn_yielding(_result_message(result="answer")),
        tool_server_factory=_seeded_factory([]), trace_id="trace-abc", session_id="session-xyz",
    ))
    assert result["trace_id"] == "trace-abc"
```

- [ ] **Step 9: Run the full suite and ruff**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass, ruff clean.

- [ ] **Step 10: Commit**

```bash
git add agent/tools.py agent/agent.py tests/test_tools.py tests/test_agent.py
git commit -m "feat: thread session_id/trace_id through the agent; add review_outcome and errors to ask()'s result"
```

---

### Task 2: `agent/telemetry.py`

**Files:**
- Create: `agent/telemetry.py`
- Create: `tests/test_telemetry.py`
- Modify: `pyproject.toml` (add `opentelemetry-api`, `opentelemetry-sdk`,
  `opentelemetry-exporter-gcp-trace`)
- Modify: `.env.example` (add `OTEL_EXPORTER`)

**Interfaces:**
- Consumes: `agent.config.get_project_id()` (unchanged, from Plan 2A).
- Produces: `agent.telemetry.get_tracer() -> Tracer`. Task 4 (`cli.py`) consumes this
  directly.

- [ ] **Step 1: Add the three OpenTelemetry dependencies to `pyproject.toml`**

Add to the `[project]` section's `dependencies` list (keep every existing entry
untouched):

```toml
    "opentelemetry-api>=1.44.0",
    "opentelemetry-sdk>=1.44.0",
    "opentelemetry-exporter-gcp-trace>=1.15.0",
```

Run `uv sync` so `uv.lock` picks up the new dependencies (and their transitive
dependency `google-cloud-trace`, pulled in automatically — do not add it separately).
Stage the updated `uv.lock` alongside `pyproject.toml` in this task's commit.

- [ ] **Step 2: Add `OTEL_EXPORTER` to `.env.example`**

Add this line to `.env.example`, in the same area as the other operational env vars
(after `AGENT_MAX_TURNS` is a reasonable place):

```
OTEL_EXPORTER=console               # "console" for offline dev; unset/anything else uses Cloud Trace
```

- [ ] **Step 3: Create `agent/telemetry.py`**

```python
"""OpenTelemetry tracer setup: a console exporter for offline dev, the real
Cloud Trace exporter otherwise. Authenticates as the human's own default
credentials (ADC), never persona impersonation -- trace/span writing is
operational observability, not governed customer data, so it sits outside
the warehouse's permission boundary (BUILD_SPEC: "the warehouse enforces
permissions, not the prompt" -- traces aren't warehouse data).

Spans themselves are created by callers (agent/cli.py), never here or
inside agent/agent.py, which stays free of any OpenTelemetry dependency.

get_tracer() returns a tracer from a freshly built TracerProvider via that
provider's own .get_tracer() method, rather than going through the global
opentelemetry.trace.set_tracer_provider()/get_tracer() singleton -- the
global provider can only be set once per process, so using it here would
make repeated get_tracer() calls (e.g. across multiple pytest tests in the
same process) silently reuse whichever exporter was configured first.

NOTE (deliberate, not an oversight): opentelemetry-exporter-gcp-trace's
CloudTraceSpanExporter is deprecated as of this writing in favor of the
generic OTLPSpanExporter pointed at Cloud Trace's OTLP endpoint, which
needs explicit gRPC credential wiring (AuthMetadataPlugin + composite
credentials) and three more dependencies. For this 3-day prototype, whose
"Done when" bar (BUILD_SPEC §6 Phase 2) is just "a visible trace in Cloud
Trace" -- not a specific exporter architecture -- the deprecated exporter's
simple one-line construction was chosen over that added complexity. See
https://github.com/GoogleCloudPlatform/opentelemetry-operations-python/blob/main/MIGRATION.md
if this project ever needs to migrate off it.
"""
import os

from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from agent import config


def get_tracer(tracer_name: str = "governed-agent"):
    if os.environ.get("OTEL_EXPORTER") == "console":
        exporter = ConsoleSpanExporter()
    else:
        exporter = CloudTraceSpanExporter(project_id=config.get_project_id())
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider.get_tracer(tracer_name)
```

- [ ] **Step 4: Write failing tests**

Create `tests/test_telemetry.py`:

```python
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace import TracerProvider

from agent import telemetry


def test_get_tracer_uses_console_exporter_when_env_set(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER", "console")
    tracer = telemetry.get_tracer()
    # No exception, and the tracer is usable -- exact exporter internals
    # aren't introspectable from the tracer object itself, so this proves
    # the console path doesn't require GCP credentials/project_id at all.
    with tracer.start_as_current_span("test.span"):
        pass


def test_get_tracer_uses_cloud_trace_exporter_by_default(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER", raising=False)
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project-123")
    tracer = telemetry.get_tracer()
    # Constructing the tracer/provider/exporter must not require real GCP
    # credentials or network access -- CloudTraceSpanExporter's __init__
    # only stores config; it doesn't authenticate until a span is actually
    # exported, which this test never triggers (no span is created here).
    assert tracer is not None


def test_get_tracer_two_calls_produce_independent_tracers(monkeypatch):
    # Proves get_tracer() doesn't rely on OpenTelemetry's global
    # set_tracer_provider() singleton -- calling it twice with different
    # OTEL_EXPORTER values must not have the second call silently reuse the
    # first call's exporter (which is exactly what the global form would do).
    monkeypatch.setenv("OTEL_EXPORTER", "console")
    tracer_a = telemetry.get_tracer()
    monkeypatch.delenv("OTEL_EXPORTER", raising=False)
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project-123")
    tracer_b = telemetry.get_tracer()
    assert tracer_a is not tracer_b


def test_spans_are_captured_with_expected_attributes():
    # Exercises the actual span-creation shape cli.py (Task 4) will use,
    # via a directly-constructed TracerProvider + InMemorySpanExporter
    # (not telemetry.get_tracer() itself, since this test wants to inspect
    # captured spans, which get_tracer()'s BatchSpanProcessor doesn't
    # expose synchronously).
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("agent.session", attributes={"persona": "analyst", "mode": "single"}):
        pass
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "agent.session"
    assert spans[0].attributes["persona"] == "analyst"
    assert spans[0].attributes["mode"] == "single"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_telemetry.py -v`
Expected: all four tests pass. None requires network access or real GCP credentials —
`test_get_tracer_uses_cloud_trace_exporter_by_default` only constructs the exporter
object, it never calls `.export()` on it.

- [ ] **Step 6: Run the full suite and ruff**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass, ruff clean.

- [ ] **Step 7: Commit**

```bash
git add agent/telemetry.py tests/test_telemetry.py pyproject.toml uv.lock .env.example
git commit -m "feat: add OpenTelemetry tracer setup (console/Cloud Trace)"
```

---

### Task 3: `agent/lineage.py`

**Files:**
- Create: `agent/lineage.py`
- Create: `tests/test_lineage.py`

**Interfaces:**
- Consumes: nothing new — takes a plain `dict` shaped like `agent.ask()`'s return value.
- Produces: `agent.lineage.write_record(path, question, persona, mode, session_id, result) -> None`.
  Task 4 (`cli.py`) consumes this directly.

- [ ] **Step 1: Create `agent/lineage.py`**

```python
"""Writes one JSON record per answer to a JSONL file, tying together the
question, persona, mode, and the full structured result from agent.ask()
(SQL run, job IDs, tables referenced, denials, errors, review outcome, and
trace ID) so Dataplex's automatic BigQuery-job lineage can be traced back
to the prompt that caused it.

path is an explicit parameter, not a hardcoded "lineage/records.jsonl"
inside the function -- same injection-for-testability principle as every
prior plan in this project.
"""
import json
from datetime import datetime, timezone
from pathlib import Path


def write_record(
    path: Path,
    question: str,
    persona: str,
    mode: str,
    session_id: str,
    result: dict,
) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "question": question,
        "persona": persona,
        "mode": mode,
        "session_id": session_id,
        "answer": result["answer"],
        "sql_list": result["sql_list"],
        "job_ids": result["job_ids"],
        "tables_referenced": result["tables_referenced"],
        "denials": result["denials"],
        "errors": result["errors"],
        "review_outcome": result["review_outcome"],
        "trace_id": result["trace_id"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")
```

- [ ] **Step 2: Write failing tests**

Create `tests/test_lineage.py`:

```python
import json

from agent.lineage import write_record


def _sample_result(**overrides):
    result = {
        "answer": "3 regions.",
        "sql_list": ["SELECT region FROM customers"],
        "job_ids": ["job-1"],
        "tables_referenced": ["proj.ds.customers"],
        "denials": [],
        "errors": [],
        "review_outcome": None,
        "trace_id": "trace-abc",
    }
    result.update(overrides)
    return result


def test_write_record_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "records.jsonl"
    write_record(path, "How many regions?", "analyst", "single", "session-1", _sample_result())
    assert path.exists()


def test_write_record_writes_one_json_line_with_expected_fields(tmp_path):
    path = tmp_path / "records.jsonl"
    write_record(path, "How many regions?", "analyst", "single", "session-1", _sample_result())
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["question"] == "How many regions?"
    assert record["persona"] == "analyst"
    assert record["mode"] == "single"
    assert record["session_id"] == "session-1"
    assert record["answer"] == "3 regions."
    assert record["sql_list"] == ["SELECT region FROM customers"]
    assert record["job_ids"] == ["job-1"]
    assert record["tables_referenced"] == ["proj.ds.customers"]
    assert record["denials"] == []
    assert record["errors"] == []
    assert record["review_outcome"] is None
    assert record["trace_id"] == "trace-abc"
    assert "timestamp" in record


def test_write_record_appends_multiple_records(tmp_path):
    path = tmp_path / "records.jsonl"
    write_record(path, "Q1", "analyst", "single", "s1", _sample_result())
    write_record(path, "Q2", "governance", "reviewed", "s2", _sample_result(review_outcome="approved"))
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["question"] == "Q1"
    assert json.loads(lines[1])["question"] == "Q2"
    assert json.loads(lines[1])["review_outcome"] == "approved"


def test_write_record_serializes_denials_and_errors_lists(tmp_path):
    path = tmp_path / "records.jsonl"
    denial_entry = {"sql": "SELECT email FROM customers", "result": {"denied": True, "error": "Forbidden"}}
    write_record(path, "Q", "analyst", "single", "s1", _sample_result(denials=[denial_entry]))
    record = json.loads(path.read_text().splitlines()[0])
    assert record["denials"] == [denial_entry]
```

- [ ] **Step 3: Run the tests to verify they pass**

Run: `uv run pytest tests/test_lineage.py -v`
Expected: all four tests pass.

- [ ] **Step 4: Run the full suite and ruff**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add agent/lineage.py tests/test_lineage.py
git commit -m "feat: add lineage.py (JSONL provenance records)"
```

---

### Task 4: `agent/cli.py`, Makefile, and wiring it all together

**Files:**
- Create: `agent/cli.py`
- Create: `tests/test_cli.py`
- Modify: `pyproject.toml` (add `typer`)
- Modify: `Makefile` (add `ask` and `test` targets)

**Interfaces:**
- Consumes: `agent.ask()` (Task 1's extended signature), `agent.telemetry.get_tracer()`
  (Task 2), `agent.lineage.write_record()` (Task 3), `agent.credentials.get_credentials()`
  and `agent.config.get_project_id()`/`agent.config.PERSONAS` (unchanged, from Plan 2A).
- Produces: a runnable CLI command. Nothing later in this project consumes `cli.py`
  itself — it's Phase 2's final integration point, not a library other code imports.

- [ ] **Step 1: Add `typer` to `pyproject.toml`**

Add to the `[project]` section's `dependencies` list:

```toml
    "typer>=0.12",
```

Run `uv sync` and stage the updated `uv.lock`.

- [ ] **Step 2: Create `agent/cli.py`**

```python
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
```

- [ ] **Step 3: Write failing tests**

Create `tests/test_cli.py`. `pytest-asyncio` is NOT a project dependency (confirmed by
reading the current `pyproject.toml`) — do not add it and do not use
`@pytest.mark.asyncio`. Every async test in this project (see `tests/test_agent.py`'s
`_run` helper) instead calls `asyncio.run(...)` directly inside a plain, non-`async def`
test function. Follow that exact pattern here:

```python
import asyncio
import json
import uuid

from agent import cli


def _run(coro):
    return asyncio.run(coro)


def _fake_result(**overrides):
    result = {
        "answer": "3 regions.",
        "sql_list": ["SELECT region FROM customers"],
        "job_ids": ["job-1"],
        "tables_referenced": ["proj.ds.customers"],
        "bytes_processed": 500,
        "denials": [],
        "errors": [],
        "review_outcome": None,
        "input_tokens": 200,
        "output_tokens": 40,
        "latency_ms": 100,
        "trace_id": "trace-abc",
    }
    result.update(overrides)
    return result


def test_run_ask_writes_lineage_record_and_returns_result(monkeypatch, tmp_path):
    lineage_path = tmp_path / "records.jsonl"
    monkeypatch.setattr(cli, "LINEAGE_PATH", lineage_path)
    monkeypatch.setattr(cli, "_build_client", lambda persona: object())
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("OTEL_EXPORTER", "console")

    async def fake_ask(question, persona, client, mode="single", trace_id=None, session_id=None):
        return _fake_result()
    monkeypatch.setattr(cli.agent, "ask", fake_ask)

    result = _run(cli.run_ask("How many regions?", "analyst", "single"))

    assert result["answer"] == "3 regions."
    lines = lineage_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["question"] == "How many regions?"
    assert record["persona"] == "analyst"
    assert record["mode"] == "single"
    assert record["answer"] == "3 regions."


def test_run_ask_passes_trace_id_and_session_id_to_agent_ask(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "LINEAGE_PATH", tmp_path / "records.jsonl")
    monkeypatch.setattr(cli, "_build_client", lambda persona: object())
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("OTEL_EXPORTER", "console")

    captured = {}
    async def fake_ask(question, persona, client, mode="single", trace_id=None, session_id=None):
        captured["trace_id"] = trace_id
        captured["session_id"] = session_id
        return _fake_result()
    monkeypatch.setattr(cli.agent, "ask", fake_ask)

    _run(cli.run_ask("q", "analyst", "single"))

    # trace_id must be a real 32-hex-char OTel trace ID from the opened span,
    # not just any non-None placeholder.
    assert len(captured["trace_id"]) == 32
    int(captured["trace_id"], 16)  # raises ValueError if not valid hex
    # session_id must be a real, well-formed uuid4 string.
    assert uuid.UUID(captured["session_id"]).version == 4


def test_question_hash_is_stable_and_does_not_contain_raw_question():
    h1 = cli._question_hash("How many regions?")
    h2 = cli._question_hash("How many regions?")
    assert h1 == h2
    assert "regions" not in h1
    assert len(h1) == 16
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all three tests pass, no real BigQuery/credentials/Anthropic API access.

- [ ] **Step 5: Add `ask` and `test` targets to the `Makefile`**

Add to the `.PHONY` line (append `ask test` to the existing list) and add these two
targets, in the same style as the existing `smoke`/`dbt-build` targets (sourcing `.env`
first):

```makefile
ask:
	set -a && . ./.env && set +a && uv run python -m agent.cli ask $(ARGS)

test:
	uv run pytest -q
```

- [ ] **Step 6: Run the full suite and ruff**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all tests pass, ruff clean.

- [ ] **Step 7: Commit**

```bash
git add agent/cli.py tests/test_cli.py pyproject.toml uv.lock Makefile
git commit -m "feat: add cli.py (Typer ask command) wiring telemetry, lineage, and the agent together"
```
