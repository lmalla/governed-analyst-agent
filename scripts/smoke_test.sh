#!/usr/bin/env bash
set -euo pipefail

# scripts/smoke_test.sh
# Runs region-count queries against all three governed tables (customers,
# orders, support_tickets) and PII-column queries against the two tables
# that have PII columns (customers.email, support_tickets.body), as each of
# the three personas via impersonated service account credentials, to prove
# row/column-level governance is actually enforced by the warehouse.
# This is a pass/fail gate (exits non-zero on any unexpected result), not
# just a report for a human to eyeball.
#
# Expected results:
#   Region count query: analyst/governance see 3 regions, support_east sees 1.
#   PII column query: only governance succeeds; analyst/support_east are denied.

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
#
# bq ALSO prints an informational "WARNING: This command is using service
# account impersonation..." notice to stderr on every call, success or
# failure (confirmed on a real run) — separate from any real error text.
# Merging stderr is therefore only safe for output that gets pattern-matched
# (is_access_denied), not for output that gets parsed as structured data
# (--format=json), where that warning line breaks the parse every time.
run_as_persona_for_denial_check() {
  local persona="$1"
  local sql="$2"
  local sa_email="${persona}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

  # bq's actual error text (e.g. "Access Denied: ...") goes to stderr, not
  # stdout — merge it into the captured output so is_access_denied() can
  # actually see it. Without this, a correctly-denied query's real denial
  # text is lost, $output is empty, is_access_denied() evaluates false, and
  # the check falls through to "unexpected failure" — a FAIL even when
  # governance is configured perfectly.
  CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT="${sa_email}" \
    bq query --use_legacy_sql=false --project_id="${GCP_PROJECT_ID}" "$sql" 2>&1
}

run_as_persona_for_json() {
  local persona="$1"
  local sql="$2"
  local extra_flags="$3"
  local sa_email="${persona}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

  # No 2>&1 here: this output is parsed as JSON, and the query is never
  # expected to be denied for any persona (row access policies filter rows,
  # they don't deny the query) — so denial-detection isn't needed, and
  # merging stderr would only ever break the parse.
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

TABLES=(customers orders support_tickets)
# table:column pairs — orders has no PII column (BUILD_SPEC.md §5), so it's
# absent here. Plain "table:column" strings, not an associative array —
# macOS's default bash (3.2) doesn't support those.
PII_CHECKS=(
  "customers:email"
  "support_tickets:body"
)

for persona in "${PERSONAS[@]}"; do
  for table in "${TABLES[@]}"; do
    echo "==> [${persona}] ${table} region count query"
    region_query="SELECT region, COUNT(*) AS n FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.${table}\` GROUP BY region"
    region_output=""
    region_status=0
    region_output=$(run_as_persona_for_json "$persona" "$region_query" "--format=json") || region_status=$?

    if [[ $region_status -eq 0 ]]; then
      row_count=$(echo "$region_output" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "unparseable")
      expected=$(expected_region_count "$persona")
      if [[ "$row_count" == "$expected" ]]; then
        record_result true "${persona} sees ${row_count} region(s) on ${table} (expected ${expected})"
      else
        record_result false "${persona} sees ${row_count} region(s) on ${table}, expected ${expected}. Output: ${region_output}"
      fi
    elif is_access_denied "$region_output"; then
      record_result false "${persona} was denied the ${table} region query entirely (expected success). Output: ${region_output}"
    else
      record_result false "${persona}'s ${table} region query failed unexpectedly (not an access-denial pattern): ${region_output}"
    fi
  done

  for check in "${PII_CHECKS[@]}"; do
    table="${check%%:*}"
    column="${check#*:}"
    echo "==> [${persona}] ${table}.${column} query"
    pii_query="SELECT ${column} FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.${table}\` LIMIT 5"
    pii_output=""
    pii_status=0
    pii_output=$(run_as_persona_for_denial_check "$persona" "$pii_query") || pii_status=$?

    if [[ "$persona" == "persona-governance" ]]; then
      if [[ $pii_status -eq 0 ]]; then
        record_result true "governance can read ${table}.${column} (expected)"
      else
        record_result false "governance was denied ${table}.${column} access (expected success). Output: ${pii_output}"
      fi
    else
      if [[ $pii_status -ne 0 ]] && is_access_denied "$pii_output"; then
        record_result true "${persona} denied ${table}.${column} access (expected)"
      elif [[ $pii_status -eq 0 ]]; then
        record_result false "SECURITY VIOLATION: ${persona} successfully read ${table}.${column} (must be denied). Output: ${pii_output}"
      else
        record_result false "${persona}'s ${table}.${column} query failed unexpectedly (not an access-denial pattern): ${pii_output}"
      fi
    fi
  done
  echo
done

TOTAL=$((${#PERSONAS[@]} * (${#TABLES[@]} + ${#PII_CHECKS[@]})))
PASSED=$((TOTAL - FAILURES))
echo "==> Smoke test complete: ${PASSED}/${TOTAL} checks passed"
if [[ $FAILURES -gt 0 ]]; then
  echo "    ${FAILURES} check(s) FAILED — governance is not behaving as expected." >&2
  exit 1
fi
echo "    All governance checks passed."
