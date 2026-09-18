import json

from agent.lineage import write_record


def _sample_result(**overrides):
    result = {
        "answer": "3 regions.",
        "sql_list": ["SELECT region FROM customers"],
        "job_ids": ["job-1"],
        "tables_referenced": ["proj.ds.customers"],
        "bytes_processed": 900,
        "denials": [],
        "errors": [],
        "review_outcome": None,
        "input_tokens": 200,
        "output_tokens": 40,
        "latency_ms": 1234,
        "trace_id": "trace-abc",
    }
    result.update(overrides)
    return result


def test_write_record_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "records.jsonl"
    write_record(
        path=path, question="How many regions?", persona="analyst", mode="single",
        session_id="session-1", result=_sample_result(),
    )
    assert path.exists()


def test_write_record_writes_one_json_line_with_expected_fields(tmp_path):
    path = tmp_path / "records.jsonl"
    write_record(
        path=path, question="How many regions?", persona="analyst", mode="single",
        session_id="session-1", result=_sample_result(),
    )
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
    write_record(
        path=path, question="Q1", persona="analyst", mode="single",
        session_id="s1", result=_sample_result(),
    )
    write_record(
        path=path, question="Q2", persona="governance", mode="reviewed",
        session_id="s2", result=_sample_result(review_outcome="approved"),
    )
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["question"] == "Q1"
    assert json.loads(lines[1])["question"] == "Q2"
    assert json.loads(lines[1])["review_outcome"] == "approved"


def test_write_record_serializes_denials_and_errors_lists(tmp_path):
    path = tmp_path / "records.jsonl"
    denial_entry = {"sql": "SELECT email FROM customers", "result": {"denied": True, "error": "Forbidden"}}
    write_record(
        path=path, question="Q", persona="analyst", mode="single",
        session_id="s1", result=_sample_result(denials=[denial_entry]),
    )
    record = json.loads(path.read_text().splitlines()[0])
    assert record["denials"] == [denial_entry]


def test_write_record_includes_new_aggregate_and_error_fields(tmp_path):
    path = tmp_path / "records.jsonl"
    write_record(
        path=path, question="Q", persona="analyst", mode="single",
        session_id="s1", result=_sample_result(),
    )
    record = json.loads(path.read_text().splitlines()[0])
    assert record["bytes_processed"] == 900
    assert record["input_tokens"] == 200
    assert record["output_tokens"] == 40
    assert record["latency_ms"] == 1234
    assert record["error"] is None


def test_write_record_does_not_raise_on_minimal_result_with_error(tmp_path):
    """A partial result (e.g. from an exception path that never got past
    building the trace_id) must still produce a written record, with every
    missing field falling back to its documented default instead of a
    KeyError."""
    path = tmp_path / "records.jsonl"
    write_record(
        path=path, question="Q", persona="analyst", mode="single",
        session_id="s1", result={"trace_id": "abc"}, error="boom",
    )
    record = json.loads(path.read_text().splitlines()[0])
    assert record["trace_id"] == "abc"
    assert record["error"] == "boom"
    assert record["answer"] == ""
    assert record["sql_list"] == []
    assert record["job_ids"] == []
    assert record["tables_referenced"] == []
    assert record["denials"] == []
    assert record["errors"] == []
    assert record["bytes_processed"] == 0
    assert record["input_tokens"] == 0
    assert record["output_tokens"] == 0
    assert record["latency_ms"] == 0
    assert record["review_outcome"] is None


def test_write_record_error_defaults_to_none(tmp_path):
    path = tmp_path / "records.jsonl"
    write_record(
        path=path, question="Q", persona="analyst", mode="single",
        session_id="s1", result=_sample_result(),
    )
    raw_line = path.read_text().splitlines()[0]
    assert '"error": null' in raw_line
    record = json.loads(raw_line)
    assert record["error"] is None
