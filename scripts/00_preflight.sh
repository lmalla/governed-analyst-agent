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

  echo "==> Granting roles/bigquery.dataViewer on dataset ${BQ_DATASET} to ${SA_EMAIL}"
  # VERIFY: confirm `bq add-iam-policy-binding` is the current bq CLI syntax
  # for dataset-level IAM in your installed gcloud/bq version; older versions
  # require patching the dataset ACL via `bq update` instead.
  bq add-iam-policy-binding \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/bigquery.dataViewer" \
    "${GCP_PROJECT_ID}:${BQ_DATASET}" >/dev/null

  echo "==> Granting roles/iam.serviceAccountTokenCreator on ${SA_EMAIL} to ${USER_EMAIL}"
  gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
    --project="${GCP_PROJECT_ID}" \
    --member="user:${USER_EMAIL}" \
    --role="roles/iam.serviceAccountTokenCreator" >/dev/null
done

echo
echo "==> Pre-flight complete. Manual steps still required:"
echo "    1. Set a billing budget + alert for ${GCP_PROJECT_ID} in the Cloud Console."
echo "    2. Enable Claude models for ${GCP_PROJECT_ID} in Vertex AI Model Garden (region: \${CLOUD_ML_REGION})."
echo "    3. Check BigQuery/Vertex quota for ${GCP_PROJECT_ID} is sufficient for this prototype."
