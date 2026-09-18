# Plan 2C design: telemetry, lineage, and CLI

**Spec parent:** `BUILD_SPEC.md` §6 Phase 2, items 4-7 (`agent/telemetry.py`, `agent/lineage.py`,
`agent/cli.py`, unit tests), and the "Done when" criteria (one real question produces an
answer, a visible trace in Cloud Trace, a lineage record, and jobs findable by label).

**Prior plans:** Plan 2A (`agent/config.py`, `agent/credentials.py`, `agent/tools.py`) and
Plan 2B (`agent/agent.py`: tool wrapper layer + `ask()`) are both merged to `main`. This
plan is the last piece of Phase 2 — it wires the existing agent into a real, observable,
traceable CLI, and resolves the two items Plan 2B's final review parked specifically
because they needed lineage's design to exist first.

## Context

Three things are settled from brainstorming, each with a reason:

1. **Telemetry authenticates as the human, not the persona.** Cloud Trace span-writing is
   operational observability, not governed customer data — it doesn't belong inside the
   "warehouse enforces permissions, not the prompt" boundary. Using the human's own ADC
   avoids adding a new `roles/cloudtrace.agent` IAM grant to persona service accounts (a
   new cloud-mutating preflight change this plan doesn't need).
2. **`session_id` needs threading into `tools.run_query`.** BUILD_SPEC's job-label
   contract (`app`, `persona`, `trace_id`, `session_id`) has been only half-built since
   Plan 2A — `run_query` has had an optional `trace_id` param since Plan 2A's final review,
   but no `session_id` param was ever added, and `agent.py`'s tool wrapper never passed
   `trace_id` through either. This plan closes both gaps.
3. **`review_outcome` and `errors` are added to `ask()`'s result now, not left parked.**
   Both were parked in Plan 2B's final review specifically because they needed lineage's
   record shape to justify them — lineage's whole purpose is full provenance per answer,
   so writing `lineage.py` against `ask()`'s current (incomplete) result would just
   recreate the gap one file later.
4. **Lineage is JSONL-only.** BUILD_SPEC's own wording ("optionally a BigQuery table") and
   the prototype's 3-day scope both point away from a second storage backend (new table,
   schema, IAM, write path) with no current consumer.

## Goal

- `agent/tools.py`: add a `session_id` param to `run_query`, in the job labels alongside
  the existing `trace_id`.
- `agent/agent.py`: thread `trace_id`/`session_id` through the tool-wrapper closures and
  `ask()`'s signature; add `review_outcome` and `errors` to `ask()`'s returned dict.
- `agent/telemetry.py`: an OTel tracer, console exporter for offline dev, Cloud Trace
  exporter otherwise (human's own ADC).
- `agent/lineage.py`: one JSON record per answer, appended to `lineage/records.jsonl`.
- `agent/cli.py`: a Typer `ask` command that wires all of the above together — the first
  real (non-test) construction of a `bigquery.Client` from persona-impersonated
  credentials anywhere in this codebase.
- `Makefile`: add `ask` and `test` targets (neither exists yet).
- `.env.example`: add `OTEL_EXPORTER`.

## Non-goals

- A BigQuery `agent_audit` table (cut — see Context #4).
- Persona-aware review context (the reviewer still can't judge "should *this* persona see
  this" — flagged in Plan 2B's final review as a deliberate text-only-reviewer limitation,
  not resolved here; nothing in this plan's scope needs it).
- Per-LLM-turn token attribution finer than "one `llm.turn` span per known call" — see
  Architecture §3 for why, and what would be needed to do better.
- Phase 3's eval suite, scorers, or runner — entirely out of scope for this plan.

## Claude Agent SDK / OTel packages — self-verified, not guessed

Confirmed by installing into the same scratch venv used for Plan 2B's SDK verification:

- `opentelemetry-api==1.44.0`, `opentelemetry-sdk==1.44.0` — standard OTel Python SDK.
  `ConsoleSpanExporter`/`BatchSpanProcessor` import from `opentelemetry.sdk.trace.export`;
  `TracerProvider` from `opentelemetry.sdk.trace`; `trace.set_tracer_provider(...)` and
  `trace.get_tracer(__name__)` from `opentelemetry`. This part of the API is long-stable
  and not independently re-verified beyond confirming the package installs at these
  versions — only the GCP-specific piece below carried real uncertainty.
- `opentelemetry-exporter-gcp-trace==1.15.0` — confirmed import path
  `from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter`, and its
  constructor signature read directly via `inspect.signature`:
  `CloudTraceSpanExporter(self, project_id=None, client=None, resource_regex=None)`.
  Passing `project_id=config.get_project_id()` and leaving `client=None` lets it build its
  own `google.cloud.trace_v2` client using default ADC — exactly the "human's own login,
  not persona impersonation" behavior Context #1 calls for, with no explicit credential
  plumbing needed in `telemetry.py`. `google-cloud-trace` comes in transitively through
  this package; not separately declared in `pyproject.toml`.

## Architecture

### 1. `agent/tools.py`: add `session_id` to `run_query`

Purely additive — one new optional kwarg, same pattern as the existing `trace_id`:

```python
def run_query(
    client: bigquery.Client,
    sql: str,
    persona: str,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    ...
    labels = {"app": "governed-agent", "persona": persona}
    if trace_id:
        labels["trace_id"] = trace_id
    if session_id:
        labels["session_id"] = session_id
    ...
```

No other line in `run_query` changes. Existing tests (Plan 2A/2B) are unaffected since
the new param is optional and defaults to the prior behavior.

### 2. `agent/agent.py`: thread `trace_id`/`session_id`, add `review_outcome`/`errors`

`_build_warehouse_tools`, `build_tool_server`, and `_default_tool_server_factory` each
gain `trace_id: str | None = None, session_id: str | None = None` parameters, bound via
closure exactly like `client`/`persona` already are, and passed straight through to
`tools.run_query(client, sql, persona, trace_id=trace_id, session_id=session_id)` inside
`run_query_tool`.

`_run_single` and `_run_reviewed` gain the same two parameters, passed to
`tool_server_factory(client, persona, trace_id=trace_id, session_id=session_id)`.
`_run_single`'s return shape is **unchanged** (still a 3-tuple) — it never produces a
review outcome, so there's nothing to add there.

`_run_reviewed`'s return shape changes from a 3-tuple to a 4-tuple, adding the
classified review outcome:

```python
def _classify_review_response(review_text: str) -> str:
    stripped = review_text.strip()
    if stripped.startswith("REVISED:"):
        return "revised"
    if stripped.startswith("APPROVED"):
        return "approved"
    return "unrecognized"


async def _run_reviewed(question, client, persona, max_turns, query_fn, tool_server_factory=_default_tool_server_factory, trace_id=None, session_id=None):
    draft, run_query_log, usage_1 = await _run_single(
        question, client, persona, max_turns, query_fn, tool_server_factory, trace_id, session_id
    )
    review_prompt = _build_review_prompt(question, draft, run_query_log)
    review_options = ClaudeAgentOptions(
        model=config.get_reviewer_model(), tools=[], max_turns=1,
        system_prompt=REVIEWER_SYSTEM_PROMPT,
        strict_mcp_config=True, setting_sources=[],
    )
    review_result_message = None
    async for message in query_fn(prompt=review_prompt, options=review_options):
        if isinstance(message, ResultMessage):
            review_result_message = message
    if review_result_message is None:
        return draft, run_query_log, usage_1, "review_failed"
    review_text = review_result_message.result or ""
    final_answer = _combine_draft_and_review(draft, review_text)
    combined_usage = _sum_usage(usage_1, review_result_message.usage or {})
    return final_answer, run_query_log, combined_usage, _classify_review_response(review_text)
```

`"review_failed"` is a distinct value from `"unrecognized"` — the former means the review
call itself never produced a result (the empty-stream case Plan 2B's final review fixed);
the latter means it responded, but not with a recognized `APPROVED`/`REVISED:` prefix.
Both fail safe to the draft answer (unchanged from Plan 2B); only the label differs, for
lineage's benefit.

`ask()` gains `trace_id`/`session_id` parameters (passed straight to `_run_single`/
`_run_reviewed`) and assembles the two new result fields:

```python
async def ask(question, persona, client, mode="single", query_fn=query,
               tool_server_factory=_default_tool_server_factory,
               trace_id=None, session_id=None) -> dict:
    ...
    if mode == "single":
        answer, run_query_log, usage = await _run_single(
            question, client, persona, max_turns, query_fn, tool_server_factory, trace_id, session_id
        )
        review_outcome = None
    else:
        answer, run_query_log, usage, review_outcome = await _run_reviewed(
            question, client, persona, max_turns, query_fn, tool_server_factory, trace_id, session_id
        )
    ...
    return {
        "answer": answer,
        "sql_list": [...],           # unchanged
        "job_ids": [...],            # unchanged
        "tables_referenced": [...],  # unchanged
        "bytes_processed": ...,      # unchanged
        "denials": [...],            # unchanged
        "errors": [
            entry for entry in run_query_log
            if entry["result"].get("error") is not None and not entry["result"]["denied"]
        ],
        "review_outcome": review_outcome,
        "input_tokens": ...,         # unchanged
        "output_tokens": ...,        # unchanged
        "latency_ms": ...,           # unchanged
        "trace_id": trace_id,        # now the real caller-supplied value, not always None
    }
```

`errors` distinguishes cleanly from `denials` by `tools.run_query`'s own three return
shapes (confirmed against Plan 2A/2B's merged code): success has no `error` key at all;
a permission denial has `error` set **and** `denied: True`; a non-denial failure
(`BadRequest`/`NotFound`) has `error` set **and** `denied: False`. The `errors` filter
is exactly that third case — no new information needed from `tools.py`, just a new lens
on data `run_query_log` already carries.

### 3. `agent/telemetry.py`

```python
def get_tracer():
    exporter = ConsoleSpanExporter() if os.environ.get("OTEL_EXPORTER") == "console" \
        else CloudTraceSpanExporter(project_id=config.get_project_id())
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return trace.get_tracer("governed-agent")
```

Spans are created by the **caller** (`cli.py`), not inside `agent.py` — `agent.py` stays
free of any OTel dependency, consistent with its existing design (it already takes
`trace_id` as a plain string, never an OTel span object). `cli.py`:

1. Opens one root span, `agent.session` (attrs: `persona`, `mode`, `question_hash` — a
   truncated SHA-256 hex digest of the question, never the raw text, so Cloud Trace never
   stores question content as a span attribute).
2. Extracts that span's trace ID (hex-formatted, from the span context) to pass into
   `ask(trace_id=...)`.
3. After `ask()` returns, creates **one `llm.turn` span per known call** — exactly 1 for
   `mode="single"`, exactly 2 for `mode="reviewed"` (draft, then review) — each a child of
   `agent.session`, attributed with the mode-appropriate share of `input_tokens`/
   `output_tokens` (the totals, split evenly for `reviewed` mode since `ask()`'s result
   doesn't expose true per-call token counts). This is a deliberate simplification: true
   per-call attribution would need a further `ask()` return-shape change (a list of
   per-call usage entries) that nothing in this plan's actual scope requires — BUILD_SPEC's
   bar is "a visible trace exists" with three named span types, not per-turn token
   precision, and Plan 2C is already touching `ask()`'s contract for the second time
   (session_id/trace_id, review_outcome/errors) — a third touch for span-attribution
   precision alone isn't justified by anything this plan or Phase 3 currently needs.
4. Creates one `tool.run_query` child span per query attempt, attributed with `sql`,
   `bytes` (bytes processed), `job_id`, a `rows` count, and `denied`. **Important:**
   `result["sql_list"]` cannot be safely zipped positionally against `result["job_ids"]`
   for this — `job_ids` filters out `None` entries (denied/errored queries), so the two
   lists can differ in length and order. `result["denials"]` and `result["errors"]` are
   each already a list of full `{"sql": ..., "result": {...}}` log entries (everything a
   span needs in one object); a "succeeded" entry is anything in `run_query_log` that
   isn't in either of those two — but `ask()`'s result doesn't expose `run_query_log`
   itself, only its four derived views (`sql_list`, `job_ids`, `denials`, `errors`). The
   implementation plan's task works out the exact reconstruction (most likely: treat every
   `denials`/`errors` entry as one span each, and treat every remaining `sql_list` entry —
   by exclusion — as a succeeded span using the aggregate `job_ids`/`bytes_processed`/
   `tables_referenced` fields, accepting that a succeeded span's individual byte count
   isn't separable from the total when more than one query succeeded in the same call).
   That imprecision is acceptable here for the same reason per-call token attribution is
   (end of point 3): nothing downstream needs exact per-succeeded-query byte attribution,
   and `job_ids`/`tables_referenced`/total `bytes_processed` are still fully accurate at
   the `agent.session` level.

### 4. `agent/lineage.py`

```python
def write_record(path: Path, question: str, persona: str, mode: str, session_id: str, result: dict) -> None:
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

`path` is an explicit parameter (not a hardcoded `lineage/records.jsonl` inside the
function) — same injection-for-testability principle as every prior plan's design;
`cli.py` passes the real path, tests pass a `tmp_path`-based one. `session_id` is its own
parameter, not read off `result` — `ask()`'s returned dict never echoes back the
`session_id` it was given (only `trace_id` is echoed), so `cli.py` passes the same
`session_id` value to both `agent.ask()` and `lineage.write_record()` directly.

### 5. `agent/cli.py`

```python
app = typer.Typer()

@app.command()
def ask_command(
    question: str,
    persona: str = typer.Option(...),
    mode: str = typer.Option("single"),
):
    asyncio.run(_ask_async(question, persona, mode))

async def _ask_async(question, persona, mode):
    creds = credentials.get_credentials(persona)
    client = bigquery.Client(project=config.get_project_id(), credentials=creds)
    session_id = str(uuid.uuid4())
    tracer = telemetry.get_tracer()
    with tracer.start_as_current_span("agent.session", attributes={
        "persona": persona, "mode": mode,
        "question_hash": hashlib.sha256(question.encode()).hexdigest()[:16],
    }) as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        result = await agent.ask(question, persona, client, mode=mode,
                                  trace_id=trace_id, session_id=session_id)
        # create llm.turn / tool.run_query child spans (§3) here
    lineage.write_record(Path("lineage/records.jsonl"), question, persona, mode, session_id, result)
    typer.echo(result["answer"])
    if result["denials"]:
        typer.echo(f"({len(result['denials'])} quer{'y was' if len(result['denials'])==1 else 'ies were'} denied)")
```

This is the first place in the codebase where `credentials.get_credentials(persona)` and
a real `bigquery.Client(...)` are constructed together outside of a test double — every
prior plan deliberately deferred this to "a later plan's CLI," and this is that plan.

### 6. `Makefile` / `.env.example`

```makefile
ask:
	set -a && . ./.env && set +a && uv run python -m agent.cli ask $(ARGS)

test:
	uv run pytest -q
```

(`make ask ARGS='--persona analyst "question"'` — Typer's own `--help` covers the rest;
exact invocation shape is an implementation-plan detail, not a design-level decision.)

`.env.example` gains one line: `OTEL_EXPORTER=console                # console | (unset = Cloud Trace)`.

## Testing strategy

Every test needs zero network access and zero real GCP/Anthropic credentials, same as
every prior plan:

- `tools.run_query`'s new `session_id` param: extend the existing `FakeClient`-based
  tests with a case asserting the label appears in the job config when provided, and is
  absent when not (mirroring the existing `trace_id` test pattern exactly).
- `agent.py`'s threading and new result fields: extend the existing fake-`query_fn`-based
  tests; `_classify_review_response` gets direct unit tests (all four outcomes); `errors`
  filtering gets a test mixing a denial, a non-denial error, and a success in one
  `run_query_log`.
- `telemetry.py`: tests use `OTEL_EXPORTER=console` (or directly construct a
  `TracerProvider` with an in-memory `InMemorySpanExporter` from
  `opentelemetry.sdk.trace.export.in_memory_span_exporter`, which both `opentelemetry-sdk`
  packages ship for exactly this purpose) — never a real `CloudTraceSpanExporter` call.
- `lineage.py`: tests write to a `tmp_path`-based file and read back the JSONL line(s) to
  assert the record shape.
- `cli.py`: tests exercise `_ask_async` (or an equivalent testable inner function) with a
  fake `agent.ask` and fake `credentials.get_credentials`/`bigquery.Client` construction
  point — never a real persona/GCP call. The Typer command wrapper itself (`ask_command`)
  is thin enough (parses args, calls `asyncio.run`) that it doesn't need its own test
  beyond confirming Typer wires the CLI flags to the right parameter names.
