import asyncio
import json

import pytest
from claude_agent_sdk import ResultMessage
from google.api_core.exceptions import BadRequest, Forbidden, NotFound

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
    monkeypatch.setattr(
        tools_module, "run_query",
        lambda client, sql, persona, trace_id=None, session_id=None: fake_result,
    )
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
    monkeypatch.setattr(
        tools_module, "run_query",
        lambda client, sql, persona, trace_id=None, session_id=None: denied_result,
    )
    by_name, log = _tools_by_name()
    result = _run(by_name["run_query"].handler({"sql": "SELECT email FROM customers"}))
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "data": denied_result}
    assert result["is_error"] is False
    assert log == [{"sql": "SELECT email FROM customers", "result": denied_result}]


def test_run_query_tool_catches_value_error_without_logging(monkeypatch):
    def raise_value_error(client, sql, persona, trace_id=None, session_id=None):
        raise ValueError("Only SELECT/WITH statements are allowed, got Create")
    monkeypatch.setattr(tools_module, "run_query", raise_value_error)
    by_name, log = _tools_by_name()
    result = _run(by_name["run_query"].handler({"sql": "CREATE TABLE x (a INT)"}))
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert "SELECT/WITH" in payload["error"]
    assert result["is_error"] is True
    assert log == []  # never reached BigQuery, so nothing to log


def test_run_query_tool_catches_not_found_without_logging(monkeypatch):
    # tools.run_query's dry-run branch only wraps Forbidden internally, so a
    # bad table/column name (NotFound or BadRequest) from the dry run can
    # propagate out uncaught -- run_query_tool must still catch it and wrap
    # it in the uniform {"ok": false, "error": ...} envelope, same as
    # list_tables_tool/describe_table_tool already do.
    def raise_not_found(client, sql, persona, trace_id=None, session_id=None):
        raise NotFound("Not found: Table proj.ds.bogus")
    monkeypatch.setattr(tools_module, "run_query", raise_not_found)
    by_name, log = _tools_by_name()
    result = _run(by_name["run_query"].handler({"sql": "SELECT * FROM bogus"}))
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert "not found" in payload["error"].lower()
    assert result["is_error"] is True
    assert log == []  # exception was caught before a query result existed to log


def test_run_query_tool_catches_bad_request_without_logging(monkeypatch):
    def raise_bad_request(client, sql, persona, trace_id=None, session_id=None):
        raise BadRequest("Invalid column name bogus_col")
    monkeypatch.setattr(tools_module, "run_query", raise_bad_request)
    by_name, log = _tools_by_name()
    result = _run(by_name["run_query"].handler({"sql": "SELECT bogus_col FROM customers"}))
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert "invalid column" in payload["error"].lower()
    assert result["is_error"] is True
    assert log == []


def test_run_query_tool_input_schema_has_no_persona_field():
    by_name, _ = _tools_by_name()
    assert set(by_name["run_query"].input_schema) == {"sql"}


def test_run_query_tool_binds_persona_from_closure_not_args(monkeypatch):
    captured = {}
    def fake_run_query(client, sql, persona, trace_id=None, session_id=None):
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
    def factory(client, persona, trace_id=None, session_id=None):
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


# --- _combine_draft_and_review: whitespace tolerance (finding 5) ---

def test_combine_revised_tolerates_leading_whitespace():
    result = _combine_draft_and_review("3 regions.", "  \nREVISED: Actually, 2 regions.")
    assert result == "Actually, 2 regions."


# --- _build_review_prompt: prompt-injection delimiters (finding 4) ---

def test_build_review_prompt_wraps_inputs_in_delimiter_tags():
    from agent.agent import _build_review_prompt
    run_query_log = [{"sql": "SELECT 1", "result": {"denied": False}}]
    prompt = _build_review_prompt("What is 1+1?", "It is 2.", run_query_log)
    assert "<question>" in prompt and "</question>" in prompt
    assert "<draft_answer>" in prompt and "</draft_answer>" in prompt
    assert "<query_results>" in prompt and "</query_results>" in prompt
    # the raw inputs land inside their tags
    q_start = prompt.index("<question>")
    q_end = prompt.index("</question>")
    assert "What is 1+1?" in prompt[q_start:q_end]
    d_start = prompt.index("<draft_answer>")
    d_end = prompt.index("</draft_answer>")
    assert "It is 2." in prompt[d_start:d_end]


# --- _run_single / _run_reviewed: missing ResultMessage (finding 1) ---

async def _empty_query_fn(*, prompt, options):
    return
    yield  # pragma: no cover - makes this an async generator


def test_ask_single_mode_handles_empty_stream_without_crashing(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("REVIEWER_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("AGENT_MAX_TURNS", "8")
    result = _run(ask(
        "How many regions?", "analyst", client=object(), mode="single",
        query_fn=_empty_query_fn, tool_server_factory=_seeded_factory([]),
    ))
    assert result["answer"] == "[agent error: no result message received from the SDK]"
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0


def test_ask_reviewed_mode_falls_back_to_draft_when_review_stream_is_empty(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("REVIEWER_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("AGENT_MAX_TURNS", "8")
    draft_message = _result_message(result="3 regions.", usage={"input_tokens": 200, "output_tokens": 40})
    call_count = {"n": 0}

    async def sequenced_query_fn(*, prompt, options):
        call_count["n"] += 1
        if call_count["n"] == 1:
            yield draft_message
        else:
            return
            yield  # pragma: no cover

    result = _run(ask(
        "How many regions?", "analyst", client=object(), mode="reviewed",
        query_fn=sequenced_query_fn, tool_server_factory=_seeded_factory([]),
    ))

    assert result["answer"] == "3 regions."  # fail-safe to the draft
    assert result["input_tokens"] == 200  # only the drafting call's usage counted
    assert result["output_tokens"] == 40
    assert call_count["n"] == 2  # the review call was still attempted


def test_run_single_directly_handles_empty_stream_without_crashing(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    from agent.agent import _run_single
    answer, run_query_log, usage = _run(_run_single(
        "q", client=object(), persona="analyst", max_turns=8,
        query_fn=_empty_query_fn, tool_server_factory=_seeded_factory([]),
    ))
    assert answer == "[agent error: no result message received from the SDK]"
    assert run_query_log == []
    assert usage == {}


# --- ClaudeAgentOptions governance fields (findings 2 and 6) ---

def test_ask_reviewed_mode_options_enforce_strict_governance(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("REVIEWER_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("AGENT_MAX_TURNS", "8")

    draft_message = _result_message(result="3 regions.", usage={"input_tokens": 200, "output_tokens": 40})
    review_message = _result_message(result="APPROVED", usage={"input_tokens": 80, "output_tokens": 5})
    captured_options = []

    async def capturing_query_fn(*, prompt, options):
        captured_options.append(options)
        if len(captured_options) == 1:
            yield draft_message
        else:
            yield review_message

    _run(ask(
        "How many regions?", "analyst", client=object(), mode="reviewed",
        query_fn=capturing_query_fn, tool_server_factory=_seeded_factory([]),
    ))

    assert len(captured_options) == 2
    draft_options, review_options = captured_options

    # Draft (drafting) call: has the warehouse MCP server and the three
    # allowed tools, plus both governance-hardening fields.
    assert draft_options.tools == []
    assert set(draft_options.mcp_servers) == {"warehouse"}
    assert draft_options.allowed_tools == ["list_tables", "describe_table", "run_query"]
    assert draft_options.strict_mcp_config is True
    assert draft_options.setting_sources == []

    # Review call: no mcp_servers at all -- the reviewer has no tools.
    assert review_options.tools == []
    assert not getattr(review_options, "mcp_servers", None)
    assert review_options.strict_mcp_config is True
    assert review_options.setting_sources == []


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
