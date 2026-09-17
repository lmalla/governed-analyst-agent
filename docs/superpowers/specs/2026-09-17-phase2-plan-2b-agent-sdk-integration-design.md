# Plan 2B design: Claude Agent SDK integration (`agent/agent.py`)

**Spec parent:** `BUILD_SPEC.md` §6 Phase 2, items 3 (`agent/agent.py`), and the persona/tool
constraints in §1 ("The warehouse enforces permissions, not the prompt") and §2 Rule 1
(no cloud-mutating commands run by Claude Code itself — not relevant to this plan's code,
since `agent.py` only ever queries/reads, but binding on how the plan is executed).

**Prior plan:** Plan 2A built `agent/config.py`, `agent/credentials.py`, `agent/tools.py` —
all merged to `main` (commit `4bcda0a`). This plan builds directly on top of them.

## Context

Plan 2A deliberately deferred all Claude Agent SDK research and left two items parked in
its final review, both scoped to be settled here:

1. `list_tables`/`describe_table`/`run_query` in `agent/tools.py` have inconsistent
   error-handling behavior (the first two raise uncaught exceptions on `Forbidden`; the
   third catches and returns a structured `{"denied": ...}` result).
2. BUILD_SPEC's rule "no tool takes a persona argument" must be enforced at the SDK
   tool-registration boundary — `run_query`'s `persona` parameter (added in 2A for job
   labeling) must never be exposed to the model as a settable argument.

Research into the actual `claude-agent-sdk` Python package (see below) confirms the shape
needed to build this. One flagged risk: the research subagent's report included a stale
example model ID (`claude-3-5-sonnet-20241022`) alongside otherwise-consistent API shapes.
Per BUILD_SPEC Rule 6, exact field names taken from that research are marked `# VERIFY:`
in the plan that follows this design, to be checked against installed-package reality
during implementation (Task 0 of the plan runs `pip show`/reads the installed package's
own type stubs before any code is written against it).

## Goal

Build `agent/agent.py` exposing one async function, `ask()`, that:
- takes a question, a persona, and an already-constructed `bigquery.Client` (never
  constructs one itself — same explicit-injection principle as `agent/tools.py`)
- runs the Claude Agent SDK with exactly three custom tools (`list_tables`,
  `describe_table`, `run_query`), no built-in file/shell/web tools
- supports `mode="single"` (one agent) and `mode="reviewed"` (a deterministic second
  review pass)
- returns a structured result dict matching BUILD_SPEC §6 Phase 2 item 3 exactly

## Non-goals (explicitly deferred to Plan 2C)

- `agent/telemetry.py` (OpenTelemetry / Cloud Trace) — `trace_id` in the returned result
  is `None` for this plan; the `run_query` job label stays exactly as Plan 2A left it
  (already-optional `trace_id` param, no `session_id` param).
- `agent/lineage.py` (JSONL lineage records).
- `agent/cli.py` (Typer CLI) — the plan's tests call `ask()` directly; there is no
  command-line entry point yet. The real `bigquery.Client` construction (via
  `credentials.get_credentials(persona)` + `config.get_project_id()`) is exercised only
  in tests with a fake client — 2C's CLI is what will do this for real.

## Claude Agent SDK API — self-verified against the installed package

Initial research (a subagent summarizing SDK docs) contained one real error, caught by
directly installing `claude-agent-sdk==0.2.154` into a scratch venv and reading its actual
source (`__init__.py`, `types.py`, `query.py`) rather than trusting the summary. The
corrected findings below are read from the installed package itself, not guessed:

- Package: `claude-agent-sdk` (PyPI, confirmed `0.2.154` current at design time; Python
  3.10+). Add to `pyproject.toml`.
- Custom tools: `@tool(name, description, input_schema)` decorator on an async function
  taking one `args: dict` and returning `{"content": [{"type": "text", "text": ...}], "is_error": bool}`.
  `input_schema` is a dict mapping param name to Python type (e.g. `{"sql": str}`, or `{}`
  for no params). Grouped via `create_sdk_mcp_server(name=..., version=..., tools=[...])`.
  **Confirmed safety net:** the SDK's own docstring states an exception raised inside a
  tool handler is caught and reported as an `is_error` result automatically — a tool
  wrapper that forgets to catch something does not crash the whole `query()` call. This
  plan's explicit try/except in each wrapper (see below) is still needed to control the
  *shape* of that error (the uniform envelope), not for crash-safety, which the SDK
  already provides.
- Tool restriction: `ClaudeAgentOptions(tools=[], mcp_servers={"warehouse": server})`
  disables all built-in tools (`tools=[]` only affects built-ins, confirmed by its own
  docstring — it does not touch MCP-registered tools). **Correction from initial
  research:** `allowed_tools` takes the tool's bare `name` as given to `@tool(...)`
  (confirmed directly from `create_sdk_mcp_server`'s own docstring example:
  `allowed_tools=["add", "multiply"]` for tools named `"add"`/`"multiply"`) — there is no
  `mcp__<server>__<tool>` wildcard pattern anywhere in the installed package's source.
  This plan uses `allowed_tools=["list_tables", "describe_table", "run_query"]` (the exact
  tool names Task 1 defines) so the three custom tools execute without an interactive
  permission prompt — necessary since this SDK shells out to the `claude` CLI binary
  under the hood, and the default `permission_mode` would otherwise block on a prompt no
  one can answer in a non-interactive run.
- Model selection: `ClaudeAgentOptions(model=...)`, sourced from `config.get_agent_model()`
  for the drafting call and `config.get_reviewer_model()` for the review call (both already
  declared, currently blank, in `.env.example` — filled in by the human before running for
  real).
- `max_turns`: `ClaudeAgentOptions(max_turns=...)` — confirmed real field, `int | None`,
  sourced from `config.get_agent_max_turns()`.
- `query()`: confirmed signature `async def query(*, prompt: str | AsyncIterable[dict], options: ClaudeAgentOptions | None = None, transport=None) -> AsyncIterator[Message]` —
  stateless, one-shot, exactly matching this plan's two-independent-calls design for
  `mode="reviewed"` (no shared conversation state needed between the draft and review
  calls).
- Results: iterate the async generator; the terminal message where
  `isinstance(message, ResultMessage)` carries `.result` (`str | None`, final text),
  `.subtype` (`str`, e.g. `"success"`), `.session_id`, `.num_turns`,
  `.usage` (confirmed `dict[str, Any]` — **untyped**, passed through verbatim from the CLI,
  not a fixed dataclass). `# VERIFY:` this plan assumes the direct Anthropic Messages API's
  standard `input_tokens`/`output_tokens` keys (well-established, unlikely to differ) and
  reads them defensively via `.get(..., 0)` rather than direct indexing, so a naming
  mismatch degrades to `0` instead of crashing `ask()`. Confirming the real key names
  requires one live API call, which costs the human's `ANTHROPIC_API_KEY` budget — deferred
  to the human's real-world verification pass (same rhythm as every prior plan), not run
  by Claude Code itself.
- Runtime prerequisite (operational, not code): this SDK is a Python wrapper around a
  separately-installed `claude` CLI binary (`shutil.which("claude")`, confirmed in
  `_internal/transport/subprocess_cli.py` — not vendored inside the pip package). Since
  the human is running Claude Code itself to execute this project, that binary is already
  on their PATH; noted here only so real-run failures aren't mistaken for a code bug.

## Architecture

### 1. New `config.py` getters (small, additive — does not touch existing functions)

```python
def get_agent_model() -> str:
    return os.environ["AGENT_MODEL"]

def get_reviewer_model() -> str:
    return os.environ["REVIEWER_MODEL"]

def get_agent_max_turns() -> int:
    return int(os.environ.get("AGENT_MAX_TURNS", "8"))
```

These read the env vars `.env.example` already declares (`AGENT_MODEL`, `REVIEWER_MODEL`,
`AGENT_MAX_TURNS=8` default) — no new env vars introduced.

### 2. Tool wrapper layer — settles both items parked from Plan 2A

`build_tool_server(client: bigquery.Client, persona: str, run_query_log: list[dict]) -> McpServer`

Takes the already-constructed client and persona as explicit parameters (consistent with
`tools.py`'s injection principle) plus a plain Python list the caller owns, which the
`run_query` wrapper appends every call's result dict to — this is how `ask()` later
assembles `sql_list`/`job_ids`/`tables_referenced`/`bytes_processed`/`denials` without
re-parsing model text.

Defines three `@tool`-decorated closures, one per `tools.py` function. Each closure:

1. Calls the underlying plain function (`tools.list_tables(client)`,
   `tools.describe_table(client, table_name)`, or
   `tools.run_query(client, sql, persona)` — `persona` is bound from the outer closure,
   never read from the model-provided args dict, enforcing BUILD_SPEC's "no tool takes a
   persona argument").
2. Wraps the result (or any exception) through one shared helper:

```python
def _wrap_tool_result(data=None, error=None) -> dict:
    """Uniform agent-facing envelope. Settles Plan 2A's parked return-shape
    inconsistency at this adapter boundary rather than reshaping tools.py,
    whose plain-Python contract already has its own reviewed, merged tests."""
    if error is not None:
        payload = {"ok": False, "error": error}
    else:
        payload = {"ok": True, "data": data}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
        "is_error": error is not None,
    }
```

   `list_tables`/`describe_table`'s wrappers catch `Forbidden` (and, for consistency,
   `BadRequest`/`NotFound`) around the underlying call and route through
   `_wrap_tool_result(error=str(e))` — closing the exact gap Plan 2A's final review
   found ("a persona with no dataset access hits list_tables first — today that's an
   uncaught traceback"). `run_query`'s wrapper passes its already-structured dict
   straight through as `data` (its `denied`/`error` fields survive inside `data`
   unchanged — this plan does not alter `run_query`'s own return contract, only how
   `agent.py` presents it to the model).
3. For `run_query` specifically: appends the raw (pre-envelope) result dict, plus the
   `sql` the model requested, to `run_query_log` before wrapping — this is the
   accumulation step `ask()` reads after the SDK call completes.

### 3. `mode="single"`

```python
async def _run_single(question: str, client, persona: str, max_turns: int, query_fn) -> tuple[str, list[dict], dict]:
    """Returns (answer_text, run_query_log, usage_dict)."""
    run_query_log: list[dict] = []
    server = build_tool_server(client, persona, run_query_log)
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
    # result_message.subtype == "success" expected; anything else -> answer explains the cap was hit
    ...
    return result_message.result, run_query_log, result_message.usage
```

`SYSTEM_PROMPT` (module-level constant) follows BUILD_SPEC §6 Phase 2 item 3 verbatim:
answer data questions using the tools; never attempt to bypass access errors; report
denials plainly.

### 4. `mode="reviewed"` — deterministic two-call design (per your decision)

```python
async def _run_reviewed(question, client, persona, max_turns, query_fn):
    answer, run_query_log, usage_1 = await _run_single(question, client, persona, max_turns, query_fn)
    review_prompt = _build_review_prompt(question, answer, run_query_log)  # no tool access needed by the reviewer
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
    final_answer = _combine_draft_and_review(answer, review_result_message.result)
    combined_usage = _sum_usage(usage_1, review_result_message.usage)
    return final_answer, run_query_log, combined_usage
```

The review call gets **no tool access** (`tools=[]`, no `mcp_servers`) — it reviews the
draft answer, the SQL the drafting agent ran (from `run_query_log`), and each query's
already-captured result/denial, all serialized into the review prompt as plain text. It
never re-queries BigQuery itself (per your call to keep this text-only and avoid doubling
warehouse cost/latency per answer).

`_combine_draft_and_review` uses a fixed, mechanical protocol: `REVIEWER_SYSTEM_PROMPT`
instructs the reviewer to prefix its entire response with exactly one of two literal
tokens — `APPROVED` (draft stands as-is; any text after the token is ignored) or
`REVISED:` (everything after the colon replaces the draft as the final answer). The
combination code itself is a plain string-prefix check:

```python
def _combine_draft_and_review(draft: str, review_text: str) -> str:
    if review_text.startswith("REVISED:"):
        return review_text.removeprefix("REVISED:").strip()
    return draft  # covers "APPROVED" and any unrecognized response, fail-safe to the draft
```

The *decision* of which token to emit is the reviewer LLM's judgment (that's the point of
review); the *code path* that acts on it is a deterministic prefix check, fully
unit-testable with a fake `query_fn` returning canned review text — no LLM in the loop for
the combination logic itself, consistent with this project's "leak scorer must not rely on
an LLM" ethos.

### 5. `ask()` — public entry point and structured result assembly

```python
async def ask(
    question: str,
    persona: str,
    client: bigquery.Client,
    mode: Literal["single", "reviewed"] = "single",
    query_fn=query,  # injectable; defaults to the real SDK's query()
) -> dict:
    if persona not in config.PERSONAS:
        raise ValueError(f"Unknown persona: {persona!r}")
    max_turns = config.get_agent_max_turns()
    start = time.monotonic()
    if mode == "single":
        answer, run_query_log, usage = await _run_single(question, client, persona, max_turns, query_fn)
    elif mode == "reviewed":
        answer, run_query_log, usage = await _run_reviewed(question, client, persona, max_turns, query_fn)
    else:
        raise ValueError(f"Unknown mode: {mode!r}")
    latency_ms = int((time.monotonic() - start) * 1000)

    return {
        "answer": answer,
        "sql_list": [entry["sql"] for entry in run_query_log],
        "job_ids": [entry["result"]["job_id"] for entry in run_query_log if entry["result"]["job_id"] is not None],
        "tables_referenced": sorted({t for entry in run_query_log for t in entry["result"]["tables_referenced"]}),
        "bytes_processed": sum(entry["result"]["bytes_processed"] or 0 for entry in run_query_log),
        "denials": [entry for entry in run_query_log if entry["result"]["denied"]],
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "latency_ms": latency_ms,
        "trace_id": None,  # wired in Plan 2C once agent/telemetry.py exists
    }
```

`persona not in config.PERSONAS` is validated here too (in addition to `run_query`'s own
check inside `tools.py`) so an invalid persona fails before any SDK call is made at all,
not just before the first tool call.

### 6. Testability

`ask()`'s `client` and `query_fn` parameters are both explicit injection points, so tests
never touch real BigQuery or the real Anthropic API:

- `client`: the same hand-rolled `FakeClient` test double already built in Plan 2A's
  `tests/test_tools.py` is reused here (or a thin extension of it), since `agent.py`'s
  tool wrappers call straight into the same `tools.py` functions.
- `query_fn`: tests pass a fake async generator function that yields a canned sequence of
  SDK message objects ending in a `ResultMessage`, simulating tool-call turns without any
  network access. This is the one piece of this plan's design that depends on the exact
  shape of `ResultMessage` and the message stream `query()` yields — flagged `# VERIFY:`
  in the implementation plan, to be confirmed against the installed package before the
  fake generator's shape is finalized.

## Data flow (single call, one tool use)

```
ask(question, persona, client, mode="single")
  -> _run_single()
      -> build_tool_server(client, persona, run_query_log)  # closures bind client+persona
      -> query_fn(prompt=question, options=...)             # SDK agent loop
           -> model calls mcp__warehouse__run_query(sql=...)
                -> wrapper: tools.run_query(client, sql, persona) -> {"denied": False, "rows": [...], ...}
                -> run_query_log.append({"sql": sql, "result": {...}})
                -> _wrap_tool_result(data={...}) -> {"content": [...]}  # back to model
           -> model produces final text -> ResultMessage(result=..., usage=...)
      -> returns (answer, run_query_log, usage)
  -> ask() assembles sql_list/job_ids/tables_referenced/bytes_processed/denials from run_query_log
  -> returns structured dict
```

## Error handling

- Invalid `persona`: `ValueError` raised by `ask()` before any SDK/BigQuery call.
- Invalid `mode`: `ValueError` raised by `ask()`.
- Tool-level `Forbidden`/`BadRequest`/`NotFound` (in `list_tables`/`describe_table`):
  caught inside the tool wrapper, surfaced to the model as `{"ok": False, "error": ...}`
  with `is_error: True` — never raised into the SDK's agent loop.
- `run_query`'s own denial handling (Plan 2A, already fixed in the final-review round):
  unchanged; its dict passes through as `data`.
- SDK-level failure (`ResultMessage.subtype != "success"`, e.g. `error_max_turns`): `ask()`
  still returns a structured result (not an exception) with whatever partial answer text
  is available, so a capped-out agent reports as data, not a crash — exact field mapping
  defined in the implementation plan's task.

## Dependencies

Add to `pyproject.toml`: `claude-agent-sdk` (exact version pin confirmed against PyPI at
implementation time, per BUILD_SPEC Rule 6).

## Testing strategy (BUILD_SPEC: "no cloud access needed to run pytest")

Every test in `tests/test_agent.py` uses the `FakeClient` double (BigQuery) and a fake
`query_fn` (Claude Agent SDK) — zero network access, zero `ANTHROPIC_API_KEY` requirement,
consistent with every prior plan's test suite in this project. Coverage includes: single
mode happy path, reviewed mode happy path (draft + review both exercised), persona
validation, mode validation, tool-wrapper error envelope (Forbidden from list_tables
surfaces as structured `is_error` rather than raising), `run_query_log` aggregation into
the five list/count fields, and usage/latency assembly.
