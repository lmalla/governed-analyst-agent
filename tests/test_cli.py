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
