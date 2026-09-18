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
from datetime import UTC, datetime
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
        "timestamp": datetime.now(UTC).isoformat(),
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
