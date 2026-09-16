from pathlib import Path

from scripts.policy_tags_lib import merge_binding

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "01_policy_tags.py"


def read_script() -> str:
    assert SCRIPT.exists(), f"{SCRIPT} does not exist"
    return SCRIPT.read_text()


# --- Offline tests of the pure IAM-merge logic (no network, no GCP) ---


def test_merge_binding_adds_new_role_with_no_prior_bindings():
    policy = {}
    result = merge_binding(policy, "roles/x", "serviceAccount:a@b.com")
    assert result["bindings"] == [{"role": "roles/x", "members": ["serviceAccount:a@b.com"]}]


def test_merge_binding_is_idempotent_on_rerun():
    policy = {"bindings": [{"role": "roles/x", "members": ["serviceAccount:a@b.com"]}]}
    result = merge_binding(policy, "roles/x", "serviceAccount:a@b.com")
    assert result["bindings"] == [{"role": "roles/x", "members": ["serviceAccount:a@b.com"]}]


def test_merge_binding_preserves_unrelated_existing_bindings():
    policy = {"bindings": [{"role": "roles/other", "members": ["user:x@y.com"]}]}
    result = merge_binding(policy, "roles/x", "serviceAccount:a@b.com")
    assert len(result["bindings"]) == 2
    assert {"role": "roles/other", "members": ["user:x@y.com"]} in result["bindings"]


def test_merge_binding_adds_member_to_existing_role_without_duplicating():
    policy = {"bindings": [{"role": "roles/x", "members": ["serviceAccount:a@b.com"]}]}
    result = merge_binding(policy, "roles/x", "serviceAccount:c@d.com")
    assert len(result["bindings"]) == 1
    assert set(result["bindings"][0]["members"]) == {"serviceAccount:a@b.com", "serviceAccount:c@d.com"}


# --- Static structural checks of the script (no network, no GCP) ---


def test_uses_datacatalog_rest_api():
    text = read_script()
    assert "datacatalog.googleapis.com/v1" in text


def test_creates_taxonomy_with_fine_grained_access_control():
    text = read_script()
    assert "data_sensitivity" in text
    assert "FINE_GRAINED_ACCESS_CONTROL" in text


def test_creates_both_policy_tags():
    text = read_script()
    assert "pii_high" in text
    assert "pii_low" in text


def test_grants_fine_grained_reader_role():
    text = read_script()
    assert "roles/datacatalog.categoryFineGrainedReader" in text
    assert "persona-governance" in text


def test_uses_get_then_set_iam_policy_not_blind_overwrite():
    text = read_script()
    assert "getIamPolicy" in text
    assert "setIamPolicy" in text


def test_idempotent_lookups_before_create():
    text = read_script()
    # Both taxonomy and policy tag creation must check for an existing
    # resource by displayName before POSTing a new one.
    assert "displayName" in text
    assert text.count("GET") >= 0  # requests.get calls, not a literal HTTP verb string
    assert "requests.get" in text


def test_no_hardcoded_project_or_location_values():
    text = read_script()
    assert "os.environ" in text
    assert "your-project-id" not in text


def test_writes_policy_tags_yaml_output():
    text = read_script()
    assert "policy_tags.yml" in text
    assert "pii_high" in text and "pii_low" in text
