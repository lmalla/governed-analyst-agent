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


def test_uses_process_scoped_impersonation_env_var():
    # CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT is process-scoped: unlike
    # `gcloud config set auth/impersonate_service_account` (global config
    # state), it cannot leak into concurrent processes or persist past a
    # single command if the script exits abnormally.
    text = read_script()
    assert "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT" in text
    # The old global-config-mutating invocations must be gone (a mention of
    # `gcloud config set` in an explanatory comment about why it was
    # replaced is fine; the actual command invocations are not).
    assert "gcloud config set auth/impersonate_service_account" not in text
    assert "gcloud config unset auth/impersonate_service_account" not in text
    assert "trap" not in text
    assert "cleanup_impersonation" not in text


def test_has_access_denial_detection():
    text = read_script()
    assert "is_access_denied" in text
    assert "access denied|permission_denied|does not have permission" in text


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


def test_is_a_real_pass_fail_gate():
    # The script must actually verify results, not just print them for a
    # human to eyeball: it must track failures and exit non-zero when any
    # governance check doesn't match the expected result.
    text = read_script()
    assert "FAILURES" in text
    assert "exit 1" in text


def test_region_query_result_is_compared_against_expected_count():
    # A broken row access policy must produce a visible FAIL from the
    # script itself, not just an eyeballed discrepancy.
    text = read_script()
    assert "expected_region_count" in text
    assert "row_count" in text


def test_email_query_success_message_is_persona_specific():
    # persona-analyst successfully reading email (a total security
    # collapse) must be flagged as a violation, not reported with a
    # generic success message that reads as reassuring regardless of
    # which persona actually succeeded.
    text = read_script()
    assert "SECURITY VIOLATION" in text


def test_run_as_persona_merges_stderr_into_captured_output():
    # bq's actual error text (e.g. "Access Denied: ...") goes to stderr,
    # not stdout. Without 2>&1 on the bq query invocation, a correctly
    # denied query's $output is empty in run_as_persona's caller, so
    # is_access_denied() can never see the denial text, and the check
    # falls through to "unexpected failure" — a FAIL even when governance
    # is configured perfectly. Regression test for that bug.
    text = read_script()
    lines = [line for line in text.splitlines() if "bq query" in line and "--use_legacy_sql" in line]
    assert lines, "expected a bq query invocation line in the script"
    for line in lines:
        assert "2>&1" in line, f"bq query invocation must merge stderr into stdout: {line!r}"
