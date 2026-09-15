# Phase 0: Pre-flight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scaffold the `governed-analyst-agent` repo per BUILD_SPEC.md §3, and build the idempotent `scripts/00_preflight.sh` that enables required GCP APIs, creates the BigQuery dataset, creates the three persona service accounts, and applies baseline IAM grants — with automated tests that verify structure and script content without ever touching real cloud resources.

**Architecture:** Two tasks. Task 1 creates the full repo directory tree and top-level config (pyproject.toml, .env.example, Makefile, .gitignore, README.md, BUILD_SPEC.md) with a structural pytest test. Task 2 writes `scripts/00_preflight.sh` with a pytest test that statically asserts the script's required content (APIs enabled, personas created, idempotency guards, correct IAM roles) — actual execution against GCP is a manual step for the human, never automated here.

**Tech Stack:** Bash (`set -euo pipefail`), Python 3.11+ managed with `uv`, `pytest` for tests, `ruff` + `shellcheck` for lint.

**Spec:** `BUILD_SPEC.md` (copied from `/Users/lmalla/Downloads/spec.md` in Task 1) — this plan implements BUILD_SPEC.md §3 (repo structure) and §6 Phase 0 (pre-flight), consulting §4 (.env schema) and §5 (personas/IAM) for exact values.

## Global Constraints

- Never run commands that create, modify, or delete cloud resources (`gcloud`, `bq`, `dbt run`, `terraform apply`). Write them into scripts and tell the human to run them. Read-only commands (`gcloud config list`, `bq ls`, `--dry_run`) are fine after asking.
- No secrets, keys, or service account JSON files in the repo. Auth is via `gcloud auth application-default login` plus service account impersonation only.
- All project-specific values come from `.env`. Never hardcode project IDs, emails, regions, or model IDs.
- Python 3.11+, `uv` for dependency management, `ruff` for lint, `pytest` for tests.
- Scripts must be idempotent (safe to re-run) and use `set -euo pipefail`.
- When unsure about a current API, SDK option, model ID, or BigQuery feature, check the official docs rather than guessing, and leave a `# VERIFY:` comment if still uncertain.
- Prefer shell scripts over Terraform for now.
- The `governance` persona's `roles/datacatalog.categoryFineGrainedReader` grant on the `pii_high` policy tag is **not** part of Phase 0 — the tag doesn't exist yet. It belongs in `scripts/01_policy_tags.sh` (Phase 1), per BUILD_SPEC.md §3's own comment on that script.

---

### Task 1: Scaffold repo structure and top-level config

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `Makefile`
- Create: `.gitignore`
- Create: `README.md`
- Create: `BUILD_SPEC.md` (copy of `/Users/lmalla/Downloads/spec.md`)
- Create: `tests/test_repo_structure.py`
- Create directories (with `.gitkeep` where empty): `scripts/`, `data_gen/`, `dbt/seeds/`, `dbt/models/staging/`, `dbt/models/marts/`, `agent/`, `evals/cases/`, `evals/results/`, `tests/`

**Interfaces:**
- Produces: `.env.example` (the canonical list of env var names every later phase's code reads from `.env`), `Makefile` with a `setup` target, the full directory tree later phases' files land in.

- [ ] **Step 1: Bootstrap the uv project so tests can run**

Create `pyproject.toml`:

```toml
[project]
name = "governed-analyst-agent"
version = "0.1.0"
description = "Governed text-to-SQL agent prototype over BigQuery (Claude Agent SDK)."
requires-python = ">=3.11"
dependencies = []

[dependency-groups]
dev = [
    "pytest>=8.0",
    "ruff>=0.6",
]

[tool.uv]
package = false

[tool.ruff]
line-length = 100
target-version = "py311"
```

Create `.gitignore`:

```
.venv/
__pycache__/
*.pyc
.env
.ruff_cache/
.pytest_cache/
dbt/target/
dbt/dbt_packages/
dbt/logs/
evals/results/*.jsonl
lineage/*.jsonl
*.egg-info/
.DS_Store
```

Create empty `tests/` directory (needed for the test in Step 2 to have a home).

- [ ] **Step 2: Write the failing structure test**

```python
# tests/test_repo_structure.py
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

REQUIRED_FILES = [
    "README.md",
    "BUILD_SPEC.md",
    ".env.example",
    "pyproject.toml",
    "Makefile",
    ".gitignore",
]

REQUIRED_DIRS = [
    "scripts",
    "data_gen",
    "dbt",
    "dbt/seeds",
    "dbt/models/staging",
    "dbt/models/marts",
    "agent",
    "evals",
    "evals/cases",
    "evals/results",
    "tests",
]

REQUIRED_ENV_KEYS = [
    "GCP_PROJECT_ID",
    "GCP_REGION",
    "BQ_LOCATION",
    "BQ_DATASET",
    "USER_EMAIL",
    "ANTHROPIC_API_KEY",
    "AGENT_MODEL",
    "REVIEWER_MODEL",
    "GRADER_MODEL",
    "MAX_BYTES_BILLED",
    "MAX_ROWS_RETURNED",
    "AGENT_MAX_TURNS",
]


def test_required_files_exist():
    missing = [f for f in REQUIRED_FILES if not (REPO_ROOT / f).is_file()]
    assert not missing, f"missing files: {missing}"


def test_required_directories_exist():
    missing = [d for d in REQUIRED_DIRS if not (REPO_ROOT / d).is_dir()]
    assert not missing, f"missing directories: {missing}"


def test_env_example_has_required_keys():
    text = (REPO_ROOT / ".env.example").read_text()
    missing = [k for k in REQUIRED_ENV_KEYS if k not in text]
    assert not missing, f".env.example missing keys: {missing}"


def test_pyproject_declares_python_floor():
    text = (REPO_ROOT / "pyproject.toml").read_text()
    assert 'requires-python = ">=3.11"' in text


def test_makefile_has_setup_target():
    text = (REPO_ROOT / "Makefile").read_text()
    assert "setup:" in text
```

- [ ] **Step 3: Run the test and confirm it fails**

Run: `uv sync --group dev && uv run pytest tests/test_repo_structure.py -v`
Expected: FAIL — missing `README.md`, `BUILD_SPEC.md`, `.env.example`, `Makefile`, and most of `REQUIRED_DIRS`.

- [ ] **Step 4: Create the remaining scaffold**

Create the directories with a `.gitkeep` placeholder in each empty one:

```bash
mkdir -p scripts data_gen dbt/seeds dbt/models/staging dbt/models/marts agent evals/cases evals/results
touch scripts/.gitkeep data_gen/.gitkeep dbt/seeds/.gitkeep dbt/models/staging/.gitkeep dbt/models/marts/.gitkeep agent/.gitkeep evals/cases/.gitkeep evals/results/.gitkeep
```

(Every currently-empty directory gets a `.gitkeep` so it survives a fresh clone — git does not track empty directories. `dbt/seeds/` and `evals/results/` will later hold gitignored generated output, but the directory entry itself still needs a tracked file until real content lands there.)

Copy the spec:

```bash
cp /Users/lmalla/Downloads/spec.md BUILD_SPEC.md
```

Create `.env.example` (values from BUILD_SPEC.md §4, verbatim):

```bash
GCP_PROJECT_ID=your-project-id
GCP_REGION=us-central1              # region for Cloud Trace / general resources
BQ_LOCATION=US
BQ_DATASET=governed_analytics
USER_EMAIL=you@example.com          # human who impersonates personas

# Claude via the direct Anthropic API (used by the Claude Agent SDK).
# NOT Vertex AI Model Garden: that path gates Claude behind a business-
# verification form not available to individual/personal GCP projects.
# Get a key at https://console.anthropic.com — never commit it.
ANTHROPIC_API_KEY=                  # VERIFY: create at console.anthropic.com, paste here only
AGENT_MODEL=                        # VERIFY: exact current model ID, e.g. claude-sonnet-5-<version>
REVIEWER_MODEL=                     # smaller/cheaper model
GRADER_MODEL=                       # smaller/cheaper model

# Safety caps
MAX_BYTES_BILLED=100000000          # 100 MB per query
MAX_ROWS_RETURNED=200
AGENT_MAX_TURNS=8
```

Create `Makefile`:

```makefile
.PHONY: setup

setup:
	uv sync --group dev
```

Create `README.md`:

```markdown
# Governed Data Analyst Agent

A prototype governed text-to-SQL agent over BigQuery: agent orchestration
(Claude Agent SDK), evals, dbt-driven data governance, lineage, warehouse-
enforced permissioning, and OpenTelemetry observability. See `BUILD_SPEC.md`
for the full build brief.

## Setup

1. `cp .env.example .env` and fill in your values.
2. `gcloud auth application-default login`
3. `make setup`
4. `make preflight` (added in Phase 0's second task) — creates the dataset,
   persona service accounts, and baseline IAM grants.

Architecture diagram and results table land here in Phase 3.
```

- [ ] **Step 5: Run the test again and confirm it passes**

Run: `uv run pytest tests/test_repo_structure.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .env.example Makefile .gitignore README.md BUILD_SPEC.md tests/ scripts/.gitkeep data_gen/.gitkeep dbt/ agent/.gitkeep evals/
git commit -m "chore: scaffold repo structure per BUILD_SPEC.md §3"
```

---

### Task 2: `scripts/00_preflight.sh` — APIs, dataset, personas, baseline IAM

**Files:**
- Create: `scripts/00_preflight.sh`
- Test: `tests/test_00_preflight.py`
- Modify: `Makefile` (add `preflight` and `lint` targets)

**Interfaces:**
- Consumes: `.env.example` var names from Task 1 (the script reads the same names from `.env` at runtime).
- Produces: a dataset and three service accounts (`persona-analyst`, `persona-support-east`, `persona-governance`) that Phase 1's `scripts/01_policy_tags.sh` and `scripts/03_apply_row_policies.sh` assume already exist.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_00_preflight.py
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


def test_dataset_reader_grant_uses_dataset_acl_update():
    text = read_script()
    assert "bq show --format=prettyjson" in text
    assert "bq update --source" in text
    assert '"role": "READER"' in text
    # bq add-iam-policy-binding does not support datasets — must not be used for dataViewer
    assert "bq add-iam-policy-binding" not in text
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `uv run pytest tests/test_00_preflight.py -v`
Expected: FAIL — `scripts/00_preflight.sh` does not exist.

- [ ] **Step 3: Write the script**

```bash
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
# CONFIRMED (2026-09-15, project gcp-devops-476118): a real `bq show --format=prettyjson`
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
```

```bash
chmod +x scripts/00_preflight.sh
```

- [ ] **Step 4: Run the test again and confirm it passes**

Run: `uv run pytest tests/test_00_preflight.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Add Makefile targets**

Extend `Makefile`:

```makefile
.PHONY: setup preflight lint

setup:
	uv sync --group dev

preflight:
	bash scripts/00_preflight.sh

lint:
	uv run ruff check .
	shellcheck scripts/*.sh
```

- [ ] **Step 6: Lint and confirm clean**

Run: `uv run ruff check .`
Expected: `All checks passed!`

Run: `shellcheck scripts/00_preflight.sh`
Expected: no output (clean). If `shellcheck` isn't installed, ask the user before installing it (e.g. `brew install shellcheck` on macOS) rather than skipping the check.

- [ ] **Step 7: Commit**

```bash
git add scripts/00_preflight.sh tests/test_00_preflight.py Makefile
git commit -m "feat: add idempotent Phase 0 pre-flight script"
```

- [ ] **Step 8: Produce the phase-end summary for the human**

Per BUILD_SPEC.md's top instruction ("stop, summarize what was built, list the commands the human must run, and wait for confirmation"), report:

- What was built: full repo scaffold + `scripts/00_preflight.sh`.
- Commands the human must run themselves (all touch real cloud state, forbidden for the agent to run per Global Constraints):
  1. `cp .env.example .env` and fill in real values.
  2. `gcloud auth application-default login`
  3. `make setup`
  4. `make preflight`
  5. Manually: set a billing budget + alert, create an Anthropic API key at console.anthropic.com, check quota (the script prints these reminders too).
- Wait for the human to confirm the script ran successfully before Phase 1 begins.

---

## Self-Review Notes

- **Spec coverage:** Task 1 covers BUILD_SPEC.md §3 and the `.env.example` half of §4. Task 2 covers all four §6 Phase 0 bullets and the "Done when" gate (shellcheck + human confirmation). §5's fine-grained reader grant is explicitly deferred to Phase 1, matching §3's own comment on `01_policy_tags.sh` — this is a scope boundary, not a gap.
- **Placeholder scan:** no TBD/TODO; the only deferred content (agent/evals/dbt code) belongs to later phases' plans, marked with `.gitkeep` rather than fake stub code.
- **Type/name consistency:** persona names (`persona-analyst`, `persona-support-east`, `persona-governance`), env var names, and Makefile target names (`setup`, `preflight`, `lint`) are identical across both tasks and match BUILD_SPEC.md verbatim — later phases' plans should reuse these exact names.
