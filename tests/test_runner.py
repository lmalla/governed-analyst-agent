import asyncio
import json

import pytest

from evals import runner


def _run(coro):
    return asyncio.run(coro)


# --- case loading / filtering ---

def test_load_cases_reads_all_yaml_files_in_dir(tmp_path):
    (tmp_path / "a.yaml").write_text("- id: g001\n  question: q\n  personas: [analyst]\n  golden_sql: s\n")
    (tmp_path / "b.yaml").write_text("- id: a001\n  question: q2\n  personas: [analyst]\n  expect: refusal_or_denial\n")
    cases = runner.load_cases(tmp_path)
    assert {c["id"] for c in cases} == {"g001", "a001"}


def test_is_golden_and_is_adversarial():
    assert runner.is_golden({"golden_sql": "x"}) is True
    assert runner.is_golden({"expect": "x"}) is False
    assert runner.is_adversarial({"expect": "x"}) is True
    assert runner.is_adversarial({"golden_sql": "x"}) is False


def test_filter_cases_by_suite():
    cases = [{"id": "g1", "golden_sql": "x"}, {"id": "a1", "expect": "refusal_or_denial"}]
    assert [c["id"] for c in runner.filter_cases(cases, ["golden"])] == ["g1"]
    assert [c["id"] for c in runner.filter_cases(cases, ["adversarial"])] == ["a1"]
    assert {c["id"] for c in runner.filter_cases(cases, ["golden", "adversarial"])} == {"g1", "a1"}


def test_expand_to_items_one_per_persona():
    cases = [{"id": "g1", "personas": ["analyst", "governance"]}]
    items = runner.expand_to_items(cases)
    assert [(c["id"], p) for c, p in items] == [("g1", "analyst"), ("g1", "governance")]


# --- golden cache ---

def test_golden_cache_round_trip(tmp_path):
    path = tmp_path / "cache.json"
    cache = runner.load_golden_cache(path)
    assert cache == {}
    cache["some-key"] = [{"a": 1}]
    runner.save_golden_cache(cache, path)
    reloaded = runner.load_golden_cache(path)
    assert reloaded == {"some-key": [{"a": 1}]}


def test_get_golden_rows_caches_across_calls(monkeypatch):
    calls = []
    def fake_run_query(client, sql, persona):
        calls.append(sql)
        return {"rows": [{"n": 1}]}
    monkeypatch.setattr(runner.tools, "run_query", fake_run_query)
    cache = {}
    rows1 = runner.get_golden_rows(client=object(), persona="analyst", sql="SELECT 1", cache=cache)
    rows2 = runner.get_golden_rows(client=object(), persona="analyst", sql="SELECT 1", cache=cache)
    assert rows1 == rows2 == [{"n": 1}]
    assert len(calls) == 1  # second call hit the cache, didn't call run_query again


def test_get_golden_rows_raises_and_does_not_cache_on_denied(monkeypatch):
    def fake_run_query(client, sql, persona):
        return {"denied": True, "error": "Forbidden"}
    monkeypatch.setattr(runner.tools, "run_query", fake_run_query)
    cache = {}
    with pytest.raises(RuntimeError, match="persona='analyst'"):
        runner.get_golden_rows(client=object(), persona="analyst", sql="SELECT 1", cache=cache)
    assert cache == {}  # nothing cached on failure


def test_get_golden_rows_raises_and_does_not_cache_on_error(monkeypatch):
    def fake_run_query(client, sql, persona):
        return {"error": "some backend error"}
    monkeypatch.setattr(runner.tools, "run_query", fake_run_query)
    cache = {}
    with pytest.raises(RuntimeError, match="some backend error"):
        runner.get_golden_rows(client=object(), persona="analyst", sql="SELECT 1", cache=cache)
    assert cache == {}


# --- build_pii_reference_set: pagination ---

def test_build_pii_reference_set_paginates_through_all_rows(monkeypatch):
    monkeypatch.setenv("BQ_DATASET", "test_dataset")
    monkeypatch.setenv("MAX_ROWS_RETURNED", "2")  # small page size to make pagination easy to exercise
    calls = []

    def fake_run_query(client, sql, persona):
        calls.append(sql)
        # First page (OFFSET 0) per base query returns a full page (2 rows,
        # equal to MAX_ROWS_RETURNED), so pagination continues; the second
        # page (OFFSET 2) returns a partial page (1 row), which stops it.
        if "OFFSET 0" in sql:
            return {"rows": [{"v": "a"}, {"v": "b"}]}
        return {"rows": [{"v": "c"}]}

    monkeypatch.setattr(runner.tools, "run_query", fake_run_query)
    values = runner.build_pii_reference_set(client=object())
    # 4 source queries (email, phone, full_name, body), each paginated in 2 calls = 8 calls
    assert len(calls) == 8
    assert all("ORDER BY v" in sql for sql in calls)
    # rows collected from both pages of at least one query
    assert "a" in values and "b" in values and "c" in values


# --- capturing factory ---

def test_capturing_factory_exposes_run_query_log(monkeypatch):
    fake_log = [{"sql": "SELECT 1", "result": {"rows": [{"n": 1}]}}]
    def fake_default_factory(client, persona, trace_id=None, session_id=None):
        return {"type": "sdk", "name": "warehouse", "instance": None}, fake_log
    monkeypatch.setattr(runner.agent_module, "_default_tool_server_factory", fake_default_factory)
    factory, captured = runner.make_capturing_factory()
    factory(client=object(), persona="analyst")
    assert captured["log"] == fake_log


# --- run_item / run_all with fully faked dependencies ---

def _fake_result(**overrides):
    result = {
        "answer": "Three regions.", "sql_list": ["SELECT region FROM customers"],
        "job_ids": ["job-1"], "tables_referenced": ["proj.ds.customers"],
        "bytes_processed": 500, "denials": [], "errors": [], "review_outcome": None,
        "input_tokens": 200, "output_tokens": 40, "latency_ms": 100, "trace_id": "trace-abc",
    }
    result.update(overrides)
    return result


def test_run_item_scores_a_golden_case(monkeypatch):
    monkeypatch.setenv("BQ_DATASET", "test_dataset")  # run_item's golden branch calls config.get_dataset()
    monkeypatch.setattr(runner.tools, "run_query", lambda client, sql, persona: {"rows": [{"region": "East"}]})

    async def fake_ask(question, persona, client, mode="single", tool_server_factory=None, **kwargs):
        # Simulate the model calling run_query exactly as the real SDK loop
        # would: call the factory to get the real (uncached) run_query_log
        # list, then append an entry to it -- make_capturing_factory's
        # `captured["log"]` is that same list object, so run_item sees this
        # entry after fake_ask returns. tool_server_factory is the REAL
        # make_capturing_factory closure (not mocked); its internal call to
        # agent_module._default_tool_server_factory is safe with a fake
        # client since no tool handler is ever actually invoked here -- it
        # only builds SdkMcpTool closures, never touches the client.
        _server, log = tool_server_factory(client, persona)
        log.append({"sql": "SELECT region FROM customers", "result": {"rows": [{"region": "East"}]}})
        return _fake_result()

    case = {"id": "g001", "question": "q", "personas": ["analyst"], "golden_sql": "SELECT region FROM {dataset}.customers", "compare": "result_set"}
    record = _run(runner.run_item(
        case, "analyst", "single", clients={"analyst": object()}, golden_cache={},
        canaries=[], pii_values=[], semaphore=asyncio.Semaphore(1), ask_fn=fake_ask,
    ))
    assert record["case_id"] == "g001"
    assert record["scores"]["correctness"]["pass"] is True
    assert "policy_behavior" not in record["scores"]


def test_run_item_scores_an_adversarial_case(monkeypatch):
    # No run_query_log population needed here: policy_behavior reads only
    # result["denials"], and leak scoring an empty log + a clean answer text
    # trivially passes -- fake_ask never needs to touch tool_server_factory.
    async def fake_ask(question, persona, client, mode="single", tool_server_factory=None, **kwargs):
        return _fake_result(denials=[{"sql": "SELECT email FROM customers", "result": {"denied": True}}])

    case = {"id": "a001", "question": "q", "personas": ["analyst"], "expect": "refusal_or_denial"}
    record = _run(runner.run_item(
        case, "analyst", "single", clients={"analyst": object()}, golden_cache={},
        canaries=[], pii_values=[], semaphore=asyncio.Semaphore(1), ask_fn=fake_ask,
    ))
    assert record["scores"]["policy_behavior"]["pass"] is True
    assert "correctness" not in record["scores"]


def test_run_all_writes_jsonl_and_respects_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("BQ_DATASET", "test_dataset")  # build_pii_reference_set + run_item's golden branch both call config.get_dataset()
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "spike.yaml").write_text(
        "- id: g001\n  question: q1\n  personas: [analyst, governance]\n"
        "  golden_sql: \"SELECT 1 AS n\"\n  compare: scalar\n"
    )
    results_dir = tmp_path / "results"
    cache_path = tmp_path / "cache.json"
    canaries_path = tmp_path / "canaries.json"
    canaries_path.write_text("[]")

    monkeypatch.setattr(runner.tools, "run_query", lambda client, sql, persona: {"rows": [{"n": 1}]})

    # fake_ask never touches tool_server_factory here -- this test only
    # asserts on record count / JSONL structure, not on the resulting
    # correctness score, so an empty run_query_log (the default when the
    # factory is never invoked) is fine.
    async def fake_ask(question, persona, client, mode="single", tool_server_factory=None, **kwargs):
        return _fake_result()

    def fake_build_client(persona):
        return object()

    records = _run(runner.run_all(
        mode="single", suites=["golden", "adversarial"], limit=1, concurrency=2,
        cases_dir=cases_dir, results_dir=results_dir, golden_cache_path=cache_path,
        canaries_path=canaries_path, build_client=fake_build_client, ask_fn=fake_ask,
    ))
    assert len(records) == 1  # --limit 1 truncated the 2 (case, persona) items to 1

    jsonl_files = list(results_dir.glob("*_single.jsonl"))
    assert len(jsonl_files) == 1
    lines = jsonl_files[0].read_text().splitlines()
    assert len(lines) == 1
    written = json.loads(lines[0])
    assert written["case_id"] == "g001"


def test_run_all_isolates_a_failing_item_and_still_writes_the_others(tmp_path, monkeypatch):
    # One item raises (simulating fix #1's RuntimeError from a denied/errored
    # golden query, or any other transient failure); run_all must not let
    # asyncio.gather's default fail-fast behavior discard the other item's
    # completed work.
    monkeypatch.setenv("BQ_DATASET", "test_dataset")
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "spike.yaml").write_text(
        "- id: g001\n  question: q1\n  personas: [analyst]\n"
        "  golden_sql: \"SELECT 1 AS n\"\n  compare: scalar\n"
        "- id: g002\n  question: q2\n  personas: [analyst]\n"
        "  golden_sql: \"SELECT 1 AS n\"\n  compare: scalar\n"
    )
    results_dir = tmp_path / "results"
    cache_path = tmp_path / "cache.json"
    canaries_path = tmp_path / "canaries.json"
    canaries_path.write_text("[]")

    monkeypatch.setattr(runner.tools, "run_query", lambda client, sql, persona: {"rows": [{"n": 1}]})

    async def fake_ask(question, persona, client, mode="single", tool_server_factory=None, **kwargs):
        if question == "q1":
            raise RuntimeError("simulated failure for g001")
        _server, log = tool_server_factory(client, persona)
        log.append({"sql": "SELECT 1 AS n", "result": {"rows": [{"n": 1}]}})
        return _fake_result()

    def fake_build_client(persona):
        return object()

    records = _run(runner.run_all(
        mode="single", suites=["golden", "adversarial"], limit=None, concurrency=2,
        cases_dir=cases_dir, results_dir=results_dir, golden_cache_path=cache_path,
        canaries_path=canaries_path, build_client=fake_build_client, ask_fn=fake_ask,
    ))
    assert len(records) == 2  # both items produced a record despite one raising

    by_case = {r["case_id"]: r for r in records}
    failed = by_case["g001"]
    assert failed["scores"] == {}
    assert failed["answer"] is None
    assert failed["trace_id"] is None
    assert "simulated failure for g001" in failed["run_error"]
    assert failed["persona"] == "analyst"
    assert failed["mode"] == "single"
    assert failed["question"] == "q1"

    ok = by_case["g002"]
    assert ok["scores"]["correctness"]["pass"] is True
    assert "run_error" not in ok

    jsonl_files = list(results_dir.glob("*_single.jsonl"))
    lines = jsonl_files[0].read_text().splitlines()
    assert len(lines) == 2  # the successful item's record was still written to disk


def test_print_summary_handles_empty_and_mixed_records(capsys):
    records = [
        {"persona": "analyst", "scores": {
            "correctness": {"pass": True}, "leak": {"pass": True},
            "cost": {"input_tokens": 100, "output_tokens": 10, "bytes_processed": 200},
            "latency": {"latency_ms": 50},
        }},
        {"persona": "analyst", "scores": {
            "policy_behavior": {"pass": False}, "leak": {"pass": False},
            "cost": {"input_tokens": 50, "output_tokens": 5, "bytes_processed": 100},
            "latency": {"latency_ms": 30},
        }},
    ]
    runner.print_summary(records)
    out = capsys.readouterr().out
    assert "analyst" in out
    assert "100%" in out  # correctness accuracy: 1/1 golden items passed
    assert "0%" in out    # adversarial pass rate: 0/1 passed


def test_print_summary_excludes_failed_items_from_averages_and_counts_them_as_errors(capsys):
    records = [
        {"persona": "analyst", "scores": {
            "correctness": {"pass": True}, "leak": {"pass": True},
            "cost": {"input_tokens": 100, "output_tokens": 10, "bytes_processed": 200},
            "latency": {"latency_ms": 50},
        }},
        # A failed item from run_all's exception-isolation path: scores == {}.
        {"persona": "analyst", "scores": {}, "answer": None, "run_error": "boom"},
    ]
    runner.print_summary(records)
    out = capsys.readouterr().out
    assert "errors" in out
    lines = [line for line in out.splitlines() if line.startswith("analyst")]
    assert len(lines) == 1
    assert lines[0].split()[-1] == "1"  # 1 error out of 2 items for this persona
    assert "100%" in out  # accuracy computed only from the 1 scored item, not diluted by the failed one


def test_print_summary_handles_persona_with_all_items_failed(capsys):
    # Every item for this persona failed (scores == {}) -- averages must not
    # raise ZeroDivisionError, and the persona should still show up with a
    # full error count.
    records = [
        {"persona": "analyst", "scores": {}, "answer": None, "run_error": "boom"},
    ]
    runner.print_summary(records)  # must not raise
    out = capsys.readouterr().out
    assert "analyst" in out
    lines = [line for line in out.splitlines() if line.startswith("analyst")]
    assert lines[0].split()[-1] == "1"


# --- main(): --suite validation ---

def test_main_rejects_invalid_suite_value(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["runner.py", "--suite", "goldenn"])
    with pytest.raises(SystemExit):
        runner.main()
    err = capsys.readouterr().err
    assert "goldenn" in err


def test_main_rejects_one_invalid_value_among_valid_ones(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["runner.py", "--suite", "golden,bogus"])
    with pytest.raises(SystemExit):
        runner.main()
    err = capsys.readouterr().err
    assert "bogus" in err


def test_main_accepts_valid_suite_values(monkeypatch):
    # Valid suite values must reach asyncio.run(run_all(...)) rather than
    # erroring out -- stub asyncio.run so this doesn't do real work.
    monkeypatch.setattr("sys.argv", ["runner.py", "--suite", "golden,adversarial"])
    called = {}
    def fake_asyncio_run(coro):
        called["ran"] = True
        coro.close()  # avoid "coroutine was never awaited" warning
    monkeypatch.setattr(runner.asyncio, "run", fake_asyncio_run)
    runner.main()
    assert called.get("ran") is True
