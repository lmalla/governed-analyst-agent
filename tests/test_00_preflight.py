import re
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "00_preflight.sh"

REQUIRED_APIS = [
    "bigquery.googleapis.com",
    "bigquerydatapolicy.googleapis.com",
    "datacatalog.googleapis.com",
    "datalineage.googleapis.com",
    "dataplex.googleapis.com",
    "aiplatform.googleapis.com",
    "cloudtrace.googleapis.com",
    "logging.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
]

REQUIRED_PERSONAS = ["persona-analyst", "persona-support-east", "persona-governance"]


def read_script() -> str:
    assert SCRIPT.exists(), f"{SCRIPT} does not exist"
    return SCRIPT.read_text()


def test_uses_strict_mode():
    assert "set -euo pipefail" in read_script()


def test_enables_all_required_apis():
    text = read_script()
    missing = [api for api in REQUIRED_APIS if api not in text]
    assert not missing, f"missing API enablement: {missing}"


def test_creates_all_three_personas():
    text = read_script()
    missing = [p for p in REQUIRED_PERSONAS if p not in text]
    assert not missing, f"missing persona: {missing}"


def test_dataset_creation_is_idempotent():
    text = read_script()
    assert "bq show --dataset" in text
    assert "bq mk --dataset" in text


def test_service_account_creation_is_idempotent():
    text = read_script()
    assert "gcloud iam service-accounts describe" in text
    assert "gcloud iam service-accounts create" in text


def test_grants_baseline_iam_roles():
    text = read_script()
    assert "roles/bigquery.jobUser" in text
    assert "roles/bigquery.dataViewer" in text
    assert "roles/iam.serviceAccountTokenCreator" in text


def test_does_not_grant_fine_grained_reader_here():
    text = read_script()
    assert "categoryFineGrainedReader" not in text, (
        "governance's fine-grained PII reader grant belongs in "
        "scripts/01_policy_tags.sh (Phase 1), not here"
    )


def test_no_hardcoded_project_values():
    text = read_script()
    assert "your-project-id" not in text
    assert "${GCP_PROJECT_ID}" in text
    assert re.search(r"gserviceaccount\.com", text)


def test_fails_fast_without_env_file():
    text = read_script()
    assert ".env" in text
    assert "exit 1" in text
