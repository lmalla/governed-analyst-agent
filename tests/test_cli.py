import asyncio
import json
import uuid

import pytest
import typer.testing
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent import cli


def _run(coro):
    return asyncio.run(coro)


def _in_memory_tracer():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    return tracer, exporter


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
    monkeypatch.setattr(cli.credentials, "build_client", lambda persona: object())
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
    assert record["error"] is None


def test_run_ask_passes_trace_id_and_session_id_to_agent_ask(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "LINEAGE_PATH", tmp_path / "records.jsonl")
    monkeypatch.setattr(cli.credentials, "build_client", lambda persona: object())
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


def test_run_ask_writes_lineage_record_with_error_and_reraises_on_exception(monkeypatch, tmp_path):
    """Fix #2: an exception from agent.ask() must not skip the lineage
    write -- a partial record (trace_id + error) is written, then the
    original exception still propagates."""
    lineage_path = tmp_path / "records.jsonl"
    monkeypatch.setattr(cli, "LINEAGE_PATH", lineage_path)
    monkeypatch.setattr(cli.credentials, "build_client", lambda persona: object())
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("OTEL_EXPORTER", "console")

    async def failing_ask(question, persona, client, mode="single", trace_id=None, session_id=None):
        raise RuntimeError("boom")
    monkeypatch.setattr(cli.agent, "ask", failing_ask)

    with pytest.raises(RuntimeError, match="boom"):
        _run(cli.run_ask("How many regions?", "analyst", "single"))

    lines = lineage_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["error"] == "boom"
    assert record["question"] == "How many regions?"
    # trace_id was captured from the opened span before agent.ask() raised.
    assert len(record["trace_id"]) == 32
    # everything from `result` falls back to its default since ask() never returned one.
    assert record["answer"] == ""
    assert record["sql_list"] == []


def test_run_ask_writes_lineage_record_with_error_when_client_build_fails(monkeypatch, tmp_path):
    """An exception before the span even opens (e.g. building the BigQuery
    client) must still produce a lineage record, with trace_id=None since
    no span context ever existed."""
    lineage_path = tmp_path / "records.jsonl"
    monkeypatch.setattr(cli, "LINEAGE_PATH", lineage_path)

    def failing_build_client(persona):
        raise ValueError("bad creds")
    monkeypatch.setattr(cli.credentials, "build_client", failing_build_client)
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("OTEL_EXPORTER", "console")

    with pytest.raises(ValueError, match="bad creds"):
        _run(cli.run_ask("q", "analyst", "single"))

    lines = lineage_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["error"] == "bad creds"
    assert record["trace_id"] is None


def test_run_ask_sets_aggregate_attributes_on_session_span(monkeypatch, tmp_path):
    """Fix #1: agent.session root span carries accurate aggregate totals
    (bytes_processed, job_ids, input_tokens, output_tokens) regardless of
    how many queries ran."""
    monkeypatch.setattr(cli, "LINEAGE_PATH", tmp_path / "records.jsonl")
    monkeypatch.setattr(cli.credentials, "build_client", lambda persona: object())
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")

    tracer, exporter = _in_memory_tracer()
    monkeypatch.setattr(cli.telemetry, "get_tracer", lambda: tracer)

    async def fake_ask(question, persona, client, mode="single", trace_id=None, session_id=None):
        return _fake_result(
            bytes_processed=1900, job_ids=["job-1", "job-2"], input_tokens=300, output_tokens=90,
        )
    monkeypatch.setattr(cli.agent, "ask", fake_ask)

    _run(cli.run_ask("q", "analyst", "single"))

    spans = exporter.get_finished_spans()
    session_span = next(s for s in spans if s.name == "agent.session")
    assert session_span.attributes["bytes_processed"] == 1900
    assert list(session_span.attributes["job_ids"]) == ["job-1", "job-2"]
    assert session_span.attributes["input_tokens"] == 300
    assert session_span.attributes["output_tokens"] == 90


def test_question_hash_is_stable_and_does_not_contain_raw_question():
    h1 = cli._question_hash("How many regions?")
    h2 = cli._question_hash("How many regions?")
    assert h1 == h2
    assert "regions" not in h1
    assert len(h1) == 16


class TestCreateQuerySpans:
    def test_denied_span_has_outcome_and_bytes_but_no_job_id(self):
        tracer, exporter = _in_memory_tracer()
        result = _fake_result(
            sql_list=["SELECT a"],
            denials=[{"sql": "SELECT a", "result": {"bytes_processed": 42, "job_id": None}}],
            errors=[],
        )
        cli._create_query_spans(tracer, result)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes["outcome"] == "denied"
        assert spans[0].attributes["bytes"] == 42
        assert "job_id" not in spans[0].attributes
        assert "denied" not in spans[0].attributes

    def test_error_span_has_outcome_and_bytes_but_no_job_id(self):
        tracer, exporter = _in_memory_tracer()
        result = _fake_result(
            sql_list=["SELECT a"],
            denials=[],
            errors=[{"sql": "SELECT a", "result": {"bytes_processed": 100, "job_id": None}}],
        )
        cli._create_query_spans(tracer, result)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes["outcome"] == "error"
        assert spans[0].attributes["bytes"] == 100
        assert "job_id" not in spans[0].attributes

    def test_single_success_span_gets_confident_bytes_and_job_id(self):
        tracer, exporter = _in_memory_tracer()
        result = _fake_result(
            sql_list=["SELECT a"], denials=[], errors=[],
            bytes_processed=900, job_ids=["job-1"],
        )
        cli._create_query_spans(tracer, result)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes["outcome"] == "success"
        assert spans[0].attributes["bytes"] == 900
        assert spans[0].attributes["job_id"] == "job-1"

    def test_multiple_success_spans_omit_bytes_and_job_id(self):
        """With 2 succeeded + 1 errored query, per-query bytes/job_id
        aren't separable from the aggregate, so neither attribute is
        guessed at on the success spans (verifies the 2x900+100=1900 vs
        900-actual double-counting bug is gone)."""
        tracer, exporter = _in_memory_tracer()
        result = _fake_result(
            sql_list=["SELECT a", "SELECT b", "SELECT c"],
            denials=[],
            errors=[{"sql": "SELECT c", "result": {"bytes_processed": 100, "job_id": None}}],
            bytes_processed=1900,  # aggregate across all 3 attempts
            job_ids=["job-1", "job-2"],
        )
        cli._create_query_spans(tracer, result)
        spans = exporter.get_finished_spans()
        success_spans = [s for s in spans if s.attributes["outcome"] == "success"]
        error_spans = [s for s in spans if s.attributes["outcome"] == "error"]
        assert len(success_spans) == 2
        assert len(error_spans) == 1
        for s in success_spans:
            assert "bytes" not in s.attributes
            assert "job_id" not in s.attributes
        # the error span's own per-entry bytes is unaffected by the aggregate.
        assert error_spans[0].attributes["bytes"] == 100

    def test_zero_success_spans_when_all_denied_or_errored(self):
        tracer, exporter = _in_memory_tracer()
        result = _fake_result(
            sql_list=["SELECT a"],
            denials=[{"sql": "SELECT a", "result": {"bytes_processed": 0, "job_id": None}}],
            errors=[],
        )
        cli._create_query_spans(tracer, result)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes["outcome"] == "denied"


class TestCreateLlmTurnSpans:
    def test_single_mode_emits_one_span(self):
        tracer, exporter = _in_memory_tracer()
        result = _fake_result(review_outcome=None)
        cli._create_llm_turn_spans(tracer, "single", result)
        assert len(exporter.get_finished_spans()) == 1

    def test_reviewed_mode_emits_two_spans_normally(self):
        tracer, exporter = _in_memory_tracer()
        result = _fake_result(review_outcome="approved")
        cli._create_llm_turn_spans(tracer, "reviewed", result)
        assert len(exporter.get_finished_spans()) == 2

    def test_reviewed_mode_with_review_failed_emits_only_one_span(self):
        """Fix #6: only the draft call happened when review_outcome is
        'review_failed', so only 1 llm.turn span should be emitted, not 2."""
        tracer, exporter = _in_memory_tracer()
        result = _fake_result(review_outcome="review_failed")
        cli._create_llm_turn_spans(tracer, "reviewed", result)
        assert len(exporter.get_finished_spans()) == 1


def test_ask_command_registered_as_ask_not_function_name():
    """Fix #5: the Typer command name is explicit ('ask'), independent of
    the Python function name (ask_command), so adding a second @app.command
    in the future can't silently rename this one to 'ask-command'."""
    commands = cli.app.registered_commands
    assert len(commands) == 1
    assert commands[0].name == "ask"

    runner = typer.testing.CliRunner()
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "ask-command" not in result.output
