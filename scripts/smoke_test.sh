#!/usr/bin/env bash
set -euo pipefail

# scripts/smoke_test.sh
# Runs the same two queries as each of the three personas via impersonated
# service account credentials, to prove row/column-level governance is
# actually enforced by the warehouse. Customers-only for this spike.
#
# Expected results:
#   Region count query: analyst/governance see 3 regions, support_east sees 1.
#   Email query: analyst/support_east get an access-denied error,
#                governance succeeds.

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

cleanup_impersonation() {
  gcloud config unset auth/impersonate_service_account >/dev/null 2>&1 || true
}
trap cleanup_impersonation EXIT

run_as_persona() {
  local persona="$1"
  local sql="$2"
  local sa_email="${persona}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

  gcloud config set auth/impersonate_service_account "${sa_email}" >/dev/null 2>&1

  local output
  local status=0
  output=$(bq query --use_legacy_sql=false --project_id="${GCP_PROJECT_ID}" "$sql" 2>&1) || status=$?

  gcloud config unset auth/impersonate_service_account >/dev/null 2>&1

  echo "$output"
  return "$status"
}

is_access_denied() {
  echo "$1" | grep -qiE "access denied|permission_denied|does not have permission"
}

REGION_QUERY="SELECT region, COUNT(*) AS n FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.customers\` GROUP BY region"
EMAIL_QUERY="SELECT email FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.customers\` LIMIT 5"

for persona in "${PERSONAS[@]}"; do
  echo "==> [${persona}] region count query (expect 3 regions for analyst/governance, 1 for support_east)"
  region_output=""
  region_status=0
  region_output=$(run_as_persona "$persona" "$REGION_QUERY") || region_status=$?
  echo "$region_output"
  if [[ $region_status -ne 0 ]]; then
    if is_access_denied "$region_output"; then
      echo "    DENIED (access control)"
    else
      echo "    UNEXPECTED FAILURE (not an access-denial pattern) — investigate the output above"
    fi
  fi

  echo "==> [${persona}] email query (expect success only for governance, denial otherwise)"
  email_output=""
  email_status=0
  email_output=$(run_as_persona "$persona" "$EMAIL_QUERY") || email_status=$?
  if [[ $email_status -eq 0 ]]; then
    echo "    SUCCEEDED — expected only for persona-governance"
    echo "$email_output"
  elif is_access_denied "$email_output"; then
    echo "    DENIED (access control) — expected for analyst/support_east"
  else
    echo "    UNEXPECTED FAILURE (not an access-denial pattern) — investigate:"
    echo "$email_output"
  fi
  echo
done
