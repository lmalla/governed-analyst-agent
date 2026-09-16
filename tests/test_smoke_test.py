from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "smoke_test.sh"


def read_script() -> str:
    assert SCRIPT.exists(), f"{SCRIPT} does not exist"
    return SCRIPT.read_text()


def test_uses_strict_mode():
    assert "set -euo pipefail" in read_script()


def test_queries_as_all_three_personas():
    text = read_script()
    for persona in ["persona-analyst", "persona-support-east", "persona-governance"]:
        assert persona in text


def test_uses_impersonate_service_account_flag():
    text = read_script()
    assert "--impersonate_service_account" in text


def test_runs_region_count_query():
    text = read_script()
    assert "SELECT region, COUNT(*)" in text or "select region, count(*)" in text.lower()


def test_runs_email_query_expecting_denial_for_non_governance():
    text = read_script()
    assert "SELECT email" in text or "select email" in text.lower()


def test_no_hardcoded_project_values():
    text = read_script()
    assert "${GCP_PROJECT_ID}" in text
    assert "your-project-id" not in text
