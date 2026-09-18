from pathlib import Path

from agent.config import PERSONAS
from evals.runner import is_adversarial, is_golden, load_cases

CASES_DIR = Path("evals/cases")


def test_full_suite_has_at_least_20_golden_cases():
    cases = load_cases(CASES_DIR)
    golden = [c for c in cases if is_golden(c)]
    assert len(golden) >= 20


def test_full_suite_has_at_least_10_adversarial_cases():
    cases = load_cases(CASES_DIR)
    adversarial = [c for c in cases if is_adversarial(c)]
    assert len(adversarial) >= 10


def test_every_case_has_required_fields_and_a_unique_id():
    cases = load_cases(CASES_DIR)
    seen_ids = set()
    for case in cases:
        assert "id" in case
        assert case["id"] not in seen_ids, f"duplicate case id: {case['id']}"
        seen_ids.add(case["id"])
        assert case.get("question")
        assert "personas" in case and len(case["personas"]) > 0
        assert is_golden(case) or is_adversarial(case), f"case {case['id']} is neither golden nor adversarial"


def test_golden_cases_have_a_valid_compare_mode():
    cases = load_cases(CASES_DIR)
    for case in cases:
        if is_golden(case):
            assert case.get("compare", "result_set") in ("result_set", "scalar", "llm_judge"), case["id"]


def test_every_referenced_persona_is_known():
    cases = load_cases(CASES_DIR)
    for case in cases:
        for persona in case["personas"]:
            assert persona in PERSONAS, f"case {case['id']} references unknown persona {persona!r}"


def test_golden_sql_uses_the_dataset_placeholder_not_a_hardcoded_name():
    cases = load_cases(CASES_DIR)
    for case in cases:
        if is_golden(case):
            assert "{dataset}" in case["golden_sql"], case["id"]
