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

run_as_persona() {
  local persona="$1"
  local sql="$2"
  local sa_email="${persona}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
  bq query \
    --use_legacy_sql=false \
    --project_id="${GCP_PROJECT_ID}" \
    --impersonate_service_account="${sa_email}" \
    "$sql"
}

REGION_QUERY="SELECT region, COUNT(*) AS n FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.customers\` GROUP BY region"
EMAIL_QUERY="SELECT email FROM \`${GCP_PROJECT_ID}.${BQ_DATASET}.customers\` LIMIT 5"

for persona in "${PERSONAS[@]}"; do
  echo "==> [${persona}] region count query (expect 3 regions for analyst/governance, 1 for support_east)"
  run_as_persona "$persona" "$REGION_QUERY" || echo "    QUERY FAILED for ${persona} (unexpected for this query)"

  echo "==> [${persona}] email query (expect success only for governance, denial otherwise)"
  if run_as_persona "$persona" "$EMAIL_QUERY"; then
    echo "    SUCCEEDED — expected only for persona-governance"
  else
    echo "    DENIED — expected for analyst/support_east"
  fi
  echo
done
