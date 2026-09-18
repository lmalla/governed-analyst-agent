"""python -m evals.runner --mode single|reviewed --suite golden,adversarial --limit N --concurrency 4

Runs the evals suite against the real agent (real BigQuery, real Anthropic
API) and writes one JSONL record per (case, persona) to
evals/results/<timestamp>_<mode>.jsonl, then prints a summary table.
"""
import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

from agent import agent as agent_module
from agent import config, credentials, tools
from evals import scorers

CASES_DIR = Path("evals/cases")
RESULTS_DIR = Path("evals/results")
GOLDEN_CACHE_PATH = Path("evals/.golden_cache.json")
CANARIES_PATH = Path("evals/canaries.json")


def load_cases(cases_dir: Path = CASES_DIR) -> list[dict]:
    cases = []
    for path in sorted(cases_dir.glob("*.yaml")):
        cases.extend(yaml.safe_load(path.read_text()) or [])
    return cases


def is_golden(case: dict) -> bool:
    return "golden_sql" in case


def is_adversarial(case: dict) -> bool:
    return "expect" in case


def filter_cases(cases: list[dict], suites: list[str]) -> list[dict]:
    result = []
    for case in cases:
        # Not combined into one `or` condition (despite SIM114): kept as
        # separate golden/adversarial branches since a future suite kind
        # would otherwise need to be threaded through a single boolean.
        if "golden" in suites and is_golden(case):  # noqa: SIM114
            result.append(case)
        elif "adversarial" in suites and is_adversarial(case):
            result.append(case)
    return result


def expand_to_items(cases: list[dict]) -> list[tuple[dict, str]]:
    return [(case, persona) for case in cases for persona in case["personas"]]


def _cache_key(persona: str, sql: str) -> str:
    return hashlib.sha256(f"{persona}:{sql}".encode()).hexdigest()


def load_golden_cache(path: Path = GOLDEN_CACHE_PATH) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_golden_cache(cache: dict, path: Path = GOLDEN_CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, default=str))


def get_golden_rows(client, persona: str, sql: str, cache: dict) -> list[dict]:
    key = _cache_key(persona, sql)
    if key in cache:
        return cache[key]
    result = tools.run_query(client, sql, persona)
    if result.get("denied") or result.get("error"):
        raise RuntimeError(f"Golden SQL failed for persona={persona!r}: {result.get('error', 'denied')}")
    rows = result.get("rows", [])
    cache[key] = rows
    return rows


def build_pii_reference_set(client) -> list[str]:
    dataset = config.get_dataset()
    values: list[str] = []
    queries = [
        (f"SELECT email AS v FROM {dataset}.customers", "email"),
        (f"SELECT phone AS v FROM {dataset}.customers", "phone"),
        (f"SELECT full_name AS v FROM {dataset}.customers", "full_name"),
        (f"SELECT body AS v FROM {dataset}.support_tickets", "body"),
    ]
    page_size = int(os.environ.get("MAX_ROWS_RETURNED", "200"))
    for base_sql, _label in queries:
        offset = 0
        while True:
            sql = f"{base_sql} ORDER BY v LIMIT {page_size} OFFSET {offset}"
            result = tools.run_query(client, sql, "governance")
            rows = result.get("rows", [])
            for row in rows:
                v = row.get("v")
                if v:
                    values.append(str(v))
            if len(rows) < page_size:
                break
            offset += page_size
    return values


def load_canaries(path: Path = CANARIES_PATH) -> list[str]:
    return json.loads(path.read_text())


def make_capturing_factory():
    captured: dict = {}

    def factory(client, persona, trace_id=None, session_id=None):
        server, log = agent_module._default_tool_server_factory(
            client, persona, trace_id=trace_id, session_id=session_id
        )
        captured["log"] = log
        return server, log

    return factory, captured


async def run_item(
    case: dict,
    persona: str,
    mode: str,
    clients: dict,
    golden_cache: dict,
    canaries: list[str],
    pii_values: list[str],
    semaphore: asyncio.Semaphore,
    ask_fn=agent_module.ask,
) -> dict:
    async with semaphore:
        client = clients[persona]
        factory, captured = make_capturing_factory()
        result = await ask_fn(case["question"], persona, client, mode=mode, tool_server_factory=factory)
        run_query_log = captured.get("log", [])

        scores = {}
        if is_golden(case):
            sql = case["golden_sql"].format(dataset=config.get_dataset())
            golden_rows = get_golden_rows(client, persona, sql, golden_cache)
            scores["correctness"] = scorers.correctness(case, run_query_log, golden_rows, result["answer"])
        if is_adversarial(case):
            scores["policy_behavior"] = scorers.policy_behavior(result)
        scores["leak"] = scorers.leak(result["answer"], run_query_log, canaries, pii_values, persona)
        scores["cost"] = scorers.cost(result)
        scores["latency"] = scorers.latency(result)

        return {
            "case_id": case["id"],
            "persona": persona,
            "mode": mode,
            "question": case["question"],
            "answer": result["answer"],
            "scores": scores,
            "trace_id": result["trace_id"],
            "timestamp": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
        }


async def run_all(
    mode: str,
    suites: list[str],
    limit: int | None,
    concurrency: int,
    cases_dir: Path = CASES_DIR,
    results_dir: Path = RESULTS_DIR,
    golden_cache_path: Path = GOLDEN_CACHE_PATH,
    canaries_path: Path = CANARIES_PATH,
    build_client=credentials.build_client,
    ask_fn=agent_module.ask,
) -> list[dict]:
    cases = filter_cases(load_cases(cases_dir), suites)
    items = expand_to_items(cases)
    if limit:
        items = items[:limit]

    personas = sorted({persona for _, persona in items} | {"governance"})
    clients = {p: build_client(p) for p in personas}

    golden_cache = load_golden_cache(golden_cache_path)
    canaries = load_canaries(canaries_path)
    pii_values = build_pii_reference_set(clients["governance"])

    semaphore = asyncio.Semaphore(concurrency)
    raw_results = await asyncio.gather(
        *[
            run_item(case, persona, mode, clients, golden_cache, canaries, pii_values, semaphore, ask_fn=ask_fn)
            for case, persona in items
        ],
        return_exceptions=True,
    )

    records = []
    for (case, persona), outcome in zip(items, raw_results):
        if isinstance(outcome, Exception):
            records.append({
                "case_id": case["id"], "persona": persona, "mode": mode,
                "question": case["question"], "answer": None,
                "scores": {}, "trace_id": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
                "run_error": str(outcome),
            })
        else:
            records.append(outcome)

    save_golden_cache(golden_cache, golden_cache_path)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")  # noqa: UP017
    results_dir.mkdir(parents=True, exist_ok=True)
    results_path = results_dir / f"{timestamp}_{mode}.jsonl"
    with results_path.open("w") as f:
        for record in records:
            f.write(json.dumps(record, default=str) + "\n")

    print_summary(records)
    return records


def print_summary(records: list[dict]) -> None:
    by_persona: dict[str, list[dict]] = {}
    for r in records:
        by_persona.setdefault(r["persona"], []).append(r)

    header = (
        f"{'persona':<15} {'accuracy':>10} {'leak_ct':>8} {'adv_pass':>9} "
        f"{'avg_tok':>9} {'avg_ms':>8} {'avg_bytes':>10} {'errors':>7}"
    )
    print(header)
    for persona in sorted(by_persona):
        items = by_persona[persona]
        scored_items = [r for r in items if r["scores"]]
        error_count = len(items) - len(scored_items)
        golden_items = [r for r in scored_items if "correctness" in r["scores"]]
        adversarial_items = [r for r in scored_items if "policy_behavior" in r["scores"]]

        accuracy_str = "N/A"
        if golden_items:
            accuracy = sum(1 for r in golden_items if r["scores"]["correctness"]["pass"]) / len(golden_items)
            accuracy_str = f"{accuracy:.0%}"

        leak_count = sum(1 for r in scored_items if not r["scores"]["leak"]["pass"])

        adv_pass_str = "N/A"
        if adversarial_items:
            adv_pass = sum(1 for r in adversarial_items if r["scores"]["policy_behavior"]["pass"]) / len(adversarial_items)
            adv_pass_str = f"{adv_pass:.0%}"

        if scored_items:
            avg_tokens = sum(r["scores"]["cost"]["input_tokens"] + r["scores"]["cost"]["output_tokens"] for r in scored_items) / len(scored_items)
            avg_latency = sum(r["scores"]["latency"]["latency_ms"] for r in scored_items) / len(scored_items)
            avg_bytes = sum(r["scores"]["cost"]["bytes_processed"] for r in scored_items) / len(scored_items)
        else:
            avg_tokens = avg_latency = avg_bytes = 0.0

        print(
            f"{persona:<15} {accuracy_str:>10} {leak_count:>8} {adv_pass_str:>9} "
            f"{avg_tokens:>9.0f} {avg_latency:>8.0f} {avg_bytes:>10.0f} {error_count:>7}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["single", "reviewed"], default="single")
    parser.add_argument("--suite", default="golden,adversarial")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    suites = args.suite.split(",")
    valid_suites = {"golden", "adversarial"}
    invalid = [s for s in suites if s not in valid_suites]
    if invalid:
        parser.error(
            f"invalid --suite value(s): {', '.join(invalid)} "
            f"(valid choices: {', '.join(sorted(valid_suites))})"
        )
    asyncio.run(run_all(args.mode, suites, args.limit, args.concurrency))


if __name__ == "__main__":
    main()
