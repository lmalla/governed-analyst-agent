#!/usr/bin/env bash
set -euo pipefail

# scripts/smoke_test.sh
# Runs the same two queries as each of the three personas via impersonated
# service account credentials, to prove row/column-level governance is
# actually enforced by the warehouse. Customers-only for this spike.
# This is a pass/fail gate (exits non-zero on any unexpected result), not
# just a report for a human to eyeball.
#
# Expected results:
#   Region count query: analyst/governance see 3 regions, support_east sees 1.
#   Email query: only governance succeeds; analyst/support_east are denied.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$REPO_ROOT/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Copy .env.example to .env and fill in values." >&2
  exit 1
fi

set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID must be set in .env}"
: "${BQ_DATASET:?BQ_DATASET must be set in .env}"

PERSONAS=(persona-analyst persona-support-east persona-governance)
FAILURES=0

# CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT is a per-invocation environment
# override for gcloud's auth/impersonate_service_account property (bq's
# credential loader shells out to gcloud, which honors it). Process-scoped:
# unlike `gcloud config set`, it cannot leak into concurrent processes or
# persist past this one command if the script exits abnormally.
run_as_persona() {
  local persona="$1"
  local sql="$2"
  local extra_flags="${3:-}"
  local sa_email="${persona}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

  # shellcheck disable=SC2086
  CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT="${sa_email}" \
    bq query --use_legacy_sql=false --project_id="${GCP_PROJECT_ID}" ${extra_flags} "$sql"
}

is_access_denied() {
  echo "$1" | grep -qiE "access denied|permission_denied|does not have permission"
}

expected_region_count() {
  case "$1" in
    persona-support-east) echo 1 ;;
    *) echo 3 ;;
  esac
}

record_result() {
  local ok="$1"
  local message="$2"
  if [[ "$ok" == "true" ]]; then
    echo "    PASS: ${message}"
  else
    echo "    FAIL: ${message}"
    FAILURES=$((FAILURES + 1))
  fi
}

REGION_QUERY="SELECT region, COUNT(*) AS n FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.customers\` GROUP BY region"
EMAIL_QUERY="SELECT email FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.customers\` LIMIT 5"

for persona in "${PERSONAS[@]}"; do
  echo "==> [${persona}] region count query"
  region_output=""
  region_status=0
  region_output=$(run_as_persona "$persona" "$REGION_QUERY" "--format=json") || region_status=$?

  if [[ $region_status -eq 0 ]]; then
    row_count=$(echo "$region_output" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "unparseable")
    expected=$(expected_region_count "$persona")
    if [[ "$row_count" == "$expected" ]]; then
      record_result true "${persona} sees ${row_count} region(s) (expected ${expected})"
    else
      record_result false "${persona} sees ${row_count} region(s), expected ${expected}. Output: ${region_output}"
    fi
  elif is_access_denied "$region_output"; then
    record_result false "${persona} was denied the region query entirely (expected success). Output: ${region_output}"
  else
    record_result false "${persona}'s region query failed unexpectedly (not an access-denial pattern): ${region_output}"
  fi

  echo "==> [${persona}] email query"
  email_output=""
  email_status=0
  email_output=$(run_as_persona "$persona" "$EMAIL_QUERY") || email_status=$?

  if [[ "$persona" == "persona-governance" ]]; then
    if [[ $email_status -eq 0 ]]; then
      record_result true "governance can read email (expected)"
    else
      record_result false "governance was denied email access (expected success). Output: ${email_output}"
    fi
  else
    if [[ $email_status -ne 0 ]] && is_access_denied "$email_output"; then
      record_result true "${persona} denied email access (expected)"
    elif [[ $email_status -eq 0 ]]; then
      record_result false "SECURITY VIOLATION: ${persona} successfully read email (must be denied). Output: ${email_output}"
    else
      record_result false "${persona}'s email query failed unexpectedly (not an access-denial pattern): ${email_output}"
    fi
  fi
  echo
done

TOTAL=$((${#PERSONAS[@]} * 2))
PASSED=$((TOTAL - FAILURES))
echo "==> Smoke test complete: ${PASSED}/${TOTAL} checks passed"
if [[ $FAILURES -gt 0 ]]; then
  echo "    ${FAILURES} check(s) FAILED — governance is not behaving as expected." >&2
  exit 1
fi
echo "    All governance checks passed."
