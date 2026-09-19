#!/usr/bin/env bash
set -euo pipefail

# scripts/00_preflight.sh
# Enables required GCP APIs, creates the BigQuery dataset, creates the three
# persona service accounts, and applies baseline IAM grants (BUILD_SPEC.md
# §5, §6 Phase 0). Idempotent: safe to re-run.
#
# The `governance` persona's fine-grained PII reader grant is applied later,
# in scripts/01_policy_tags.sh, once the pii_high policy tag exists.

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
: "${BQ_LOCATION:?BQ_LOCATION must be set in .env}"
: "${BQ_DATASET:?BQ_DATASET must be set in .env}"
: "${USER_EMAIL:?USER_EMAIL must be set in .env}"

for cmd in gcloud bq python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: required command '$cmd' not found on PATH." >&2
    exit 1
  fi
done

PERSONAS=(persona-analyst persona-support-east persona-governance)
APIS=(
  bigquery.googleapis.com
  bigquerydatapolicy.googleapis.com
  datacatalog.googleapis.com
  datalineage.googleapis.com
  dataplex.googleapis.com
  aiplatform.googleapis.com
  cloudtrace.googleapis.com
  logging.googleapis.com
  iam.googleapis.com
  iamcredentials.googleapis.com
)

echo "==> Enabling APIs on ${GCP_PROJECT_ID}"
gcloud services enable "${APIS[@]}" --project="${GCP_PROJECT_ID}"

echo "==> Ensuring dataset ${GCP_PROJECT_ID}:${BQ_DATASET} exists in ${BQ_LOCATION}"
if ! bq show --dataset "${GCP_PROJECT_ID}:${BQ_DATASET}" >/dev/null 2>&1; then
  bq mk --dataset --location="${BQ_LOCATION}" "${GCP_PROJECT_ID}:${BQ_DATASET}"
else
  echo "    dataset already exists, skipping"
fi

for persona in "${PERSONAS[@]}"; do
  SA_EMAIL="${persona}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

  echo "==> Ensuring service account ${SA_EMAIL}"
  if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
    gcloud iam service-accounts create "${persona}" \
      --project="${GCP_PROJECT_ID}" \
      --display-name="${persona}"
  else
    echo "    service account already exists, skipping"
  fi

  echo "==> Granting roles/bigquery.jobUser to ${SA_EMAIL}"
  gcloud projects add-iam-policy-binding "${GCP_PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/bigquery.jobUser" \
    --condition=None >/dev/null

  echo "==> Granting roles/iam.serviceAccountTokenCreator on ${SA_EMAIL} to ${USER_EMAIL}"
  gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
    --project="${GCP_PROJECT_ID}" \
    --member="user:${USER_EMAIL}" \
    --role="roles/iam.serviceAccountTokenCreator" >/dev/null
done

echo "==> Granting roles/bigquery.dataViewer on dataset ${BQ_DATASET} to all personas"
# bq's `add-iam-policy-binding` subcommand only supports tables/views, not datasets —
# dataset-level IAM must go through a read-modify-write of the dataset's access array (see
# https://docs.cloud.google.com/bigquery/docs/control-access-to-resources-iam).
# CONFIRMED (2026-09-15, project <redacted>): a real `bq show --format=prettyjson`
# on this dataset shows every persona's grant as `"role": "READER"` — this is not a stale
# spelling of roles/bigquery.dataViewer, it is what BigQuery actually stores and returns.
# Do NOT "modernize" this to roles/bigquery.dataViewer: `bq show` would still read it back
# as READER, the `if entry not in access` check below would stop matching, and every
# re-run would append a duplicate ACL entry with no error to signal it.
POLICY_JSON="$(mktemp)"
trap 'rm -f "$POLICY_JSON"' EXIT
bq show --format=prettyjson "${GCP_PROJECT_ID}:${BQ_DATASET}" > "$POLICY_JSON"
GCP_PROJECT_ID="${GCP_PROJECT_ID}" python3 - "$POLICY_JSON" "${PERSONAS[@]}" <<'PY'
import json
import os
import sys

path, personas = sys.argv[1], sys.argv[2:]
project = os.environ["GCP_PROJECT_ID"]

with open(path) as f:
    dataset = json.load(f)

access = dataset.setdefault("access", [])
for persona in personas:
    entry = {
        "role": "READER",
        "userByEmail": f"{persona}@{project}.iam.gserviceaccount.com",
    }
    if entry not in access:
        access.append(entry)

with open(path, "w") as f:
    json.dump(dataset, f)
PY
bq update --source "$POLICY_JSON" "${GCP_PROJECT_ID}:${BQ_DATASET}"

echo
echo "==> Pre-flight complete. Manual steps still required:"
echo "    1. Set a billing budget + alert for ${GCP_PROJECT_ID} in the Cloud Console."
echo "    2. Create an Anthropic API key at https://console.anthropic.com and set ANTHROPIC_API_KEY in .env (not Vertex Model Garden — that path requires business verification)."
echo "    3. Check BigQuery quota for ${GCP_PROJECT_ID} is sufficient for this prototype."
