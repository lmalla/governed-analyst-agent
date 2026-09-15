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
    "CLAUDE_CODE_USE_VERTEX",
    "CLOUD_ML_REGION",
    "ANTHROPIC_VERTEX_PROJECT_ID",
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
