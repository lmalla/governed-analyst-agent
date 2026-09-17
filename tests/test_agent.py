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
