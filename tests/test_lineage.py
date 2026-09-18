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
