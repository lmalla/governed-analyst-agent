"""Writes one JSON record per answer to a JSONL file, tying together the
question, persona, mode, and the full structured result from agent.ask()
(SQL run, job IDs, tables referenced, denials, errors, review outcome, and
trace ID) so Dataplex's automatic BigQuery-job lineage can be traced back
to the prompt that caused it.

path is an explicit parameter, not a hardcoded "lineage/records.jsonl"
inside the function -- same injection-for-testability principle as every
prior plan in this project.

result is read with .get(...) rather than direct indexing so a partial
result (e.g. just {"trace_id": trace_id} when agent.ask() raised before
producing a full result dict) can still be written -- see cli.run_ask's
exception path, which writes a lineage record even on failure so no run
is ever silently missing from the audit trail.
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
    error: str | None = None,
) -> None:
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "question": question,
        "persona": persona,
        "mode": mode,
        "session_id": session_id,
        "answer": result.get("answer", ""),
        "sql_list": result.get("sql_list", []),
        "job_ids": result.get("job_ids", []),
        "tables_referenced": result.get("tables_referenced", []),
        "bytes_processed": result.get("bytes_processed", 0),
        "denials": result.get("denials", []),
        "errors": result.get("errors", []),
        "review_outcome": result.get("review_outcome", None),
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "latency_ms": result.get("latency_ms", 0),
        "trace_id": result.get("trace_id", None),
        "error": error,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")
