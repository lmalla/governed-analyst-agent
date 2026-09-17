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


def test_run_as_persona_merges_stderr_only_for_denial_detection_path():
    # bq's actual error text (e.g. "Access Denied: ...") goes to stderr, not
    # stdout — the email-query path needs 2>&1 so is_access_denied() can see
    # it, or a correctly-denied query looks like an "unexpected failure"
    # (FAIL even when governance is configured perfectly).
    #
    # But bq ALSO prints an informational "WARNING: This command is using
    # service account impersonation..." notice to stderr on every call,
    # success or failure — confirmed by a real run. Merging that into a
    # --format=json-parsed query's output breaks json.load() on every
    # persona, every time (a real "cries wolf" regression observed on the
    # first real run of this script). The region-count query is never
    # expected to be denied for any persona (row access policies filter
    # rows, they don't deny the query), so it doesn't need stderr merged —
    # only the email-query path does.
    text = read_script()
    lines = [line for line in text.splitlines() if "bq query" in line and "--use_legacy_sql" in line]
    assert len(lines) == 2, f"expected exactly two bq query invocation lines, found {len(lines)}"
    with_merge = [line for line in lines if "2>&1" in line]
    without_merge = [line for line in lines if "2>&1" not in line]
    assert len(with_merge) == 1, "exactly one bq query invocation (email/denial-detection path) must merge stderr"
    assert len(without_merge) == 1, "exactly one bq query invocation (JSON-parsed region path) must NOT merge stderr"
