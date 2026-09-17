import asyncio
import json

import pytest
from claude_agent_sdk import ResultMessage
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
