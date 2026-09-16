# Phase 1 Plan B1: Governance Spike on customers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the full governance chain — Data Catalog taxonomy → policy tags → dbt wiring → row access policy → smoke test — end to end on the `customers` mart alone, before extending the same (proven) pattern to `orders`/`support_tickets` in a later plan (B2). This is BUILD_SPEC.md's own explicitly-flagged highest-risk new mechanism in the build.

**Architecture:** Four tasks. Task 1 (`scripts/01_policy_tags.py`) creates the taxonomy and policy tags via the Data Catalog REST API and grants `governance` fine-grained read access — a Python script, not bash, because the stable `gcloud data-catalog taxonomies` command group has no `create` subcommand (confirmed against current docs), and Python lets the IAM-merge logic be unit-tested offline the same way Plan A's BigQuery dataset-ACL fix was. Task 2 wires the resulting policy tag resource names into `schema.yml` via `dbt`'s `--vars` mechanism (not a custom file-reading macro) and verifies the wiring with an offline `dbt parse --vars` check using dummy values — this is the plan's central uncertain point, resolved empirically rather than guessed. Task 3 adds the row access policies as a dbt post-hook on `customers` so they survive `CREATE OR REPLACE`. Task 4 is the smoke test script. None of these tasks ever execute a cloud-mutating command — every script here is written by an agent and run for real only by the human.

**Tech Stack:** Python 3.11+ (`requests` for the Data Catalog REST API), dbt-bigquery (policy tags, row access policy post-hooks, `--vars`), Bash (the smoke test), `uv`/`pytest`/`ruff`.

**Spec:** `BUILD_SPEC.md` (repo root) — this plan implements §6 Phase 1 step 3 (customers-only scope) and step 4 (smoke test), consulting §5 for the taxonomy/policy-tag/row-access-policy requirements. Extending to `orders`/`support_tickets` is Plan B2, not this plan.

## Global Constraints

- Never run commands that create, modify, or delete cloud resources (`gcloud`, `bq`, `dbt run`, `dbt seed`, `dbt build`, `dbt test`, `dbt debug`, or raw REST calls to Data Catalog/BigQuery). Write them into scripts/Makefile targets and tell the human to run them.
- **The one exception:** `dbt parse` (pure Jinja/YAML compilation) never opens a warehouse connection or attempts authentication, confirmed by Plan A's real run — it may be run by an agent as a required verification step, not just best-effort.
- No secrets, keys, or service account JSON files in the repo. `dbt/policy_tags.yml` is project-specific generated output and must be gitignored, same treatment as `.env`/`dbt/profiles.yml`.
- All project-specific values come from `.env` / environment variables. Never hardcode project IDs, datasets, regions, or resource names.
- Python 3.11+, `uv` for dependency management, `ruff` for lint, `pytest` for tests.
- Scripts must be idempotent (safe to re-run).
- When unsure about a current API or feature, check the official docs rather than guessing, and leave a `# VERIFY:` comment if still uncertain. (This plan already did that research for the Data Catalog REST endpoints — see Task 1 — but the exact behavior of `policy_tags:` accepting `{{ var(...) }}` in `schema.yml`, Task 2's central question, is deliberately left for empirical verification within the task.)
- Policy tags and row access policies in this plan apply **only to `customers`**, in the plain `governed_analytics` dataset (not `governed_analytics_raw`/`governed_analytics_staging`, which personas already can't reach — see Plan A's final review).

---

### Task 1: `scripts/01_policy_tags.py` — taxonomy, policy tags, governance IAM grant

**Files:**
- Create: `scripts/01_policy_tags.py`
- Create: `tests/test_policy_tags.py`
- Modify: `pyproject.toml` (add `requests` dependency)
- Modify: `.gitignore` (add `dbt/policy_tags.yml`)
- Modify: `Makefile` (add `policy-tags` target)

**Interfaces:**
- Produces: `dbt/policy_tags.yml` (gitignored, YAML content: `pii_high: "<resource name>"` / `pii_low: "<resource name>"`) that Task 2's `--vars` wiring and Makefile changes consume.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_policy_tags.py
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
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_policy_tags.py -v`
Expected: FAIL — `scripts/01_policy_tags.py` and `scripts/policy_tags_lib.py` don't exist yet.

- [ ] **Step 3: Add the dependency**

Add to `pyproject.toml`'s `dependencies` list:

```toml
dependencies = [
    "faker>=30.0",
    "dbt-bigquery>=1.8",
    "requests>=2.32",
]
```

Run: `uv sync`

- [ ] **Step 4: Write the pure merge logic (its own module so it's importable for offline testing)**

```python
# scripts/policy_tags_lib.py
"""Pure, network-free logic for scripts/01_policy_tags.py — kept in its own
module so it can be unit tested without any GCP connection."""


def merge_binding(policy: dict, role: str, member: str) -> dict:
    """Add `member` to `role`'s bindings in `policy` without duplicating an
    existing entry, and without disturbing any other role's bindings.
    Mutates and returns `policy`."""
    bindings = policy.setdefault("bindings", [])
    for binding in bindings:
        if binding.get("role") == role:
            members = binding.setdefault("members", [])
            if member not in members:
                members.append(member)
            return policy
    bindings.append({"role": role, "members": [member]})
    return policy
```

- [ ] **Step 5: Write the script**

```python
# scripts/01_policy_tags.py
"""Create the data_sensitivity taxonomy and pii_high/pii_low policy tags via
the Data Catalog REST API, grant governance fine-grained read access on
pii_high, and write the resulting resource names to dbt/policy_tags.yml.

Uses the REST API rather than the gcloud CLI: the stable
`gcloud data-catalog taxonomies` command group has no `create` subcommand
(confirmed against current docs — only `import`, which needs an
undocumented serialized-taxonomy JSON schema). The REST API's
projects.locations.taxonomies.create / .policyTags.create endpoints are
well-documented and stable. Authenticates with the human's own gcloud ADC
access token — no service account keys. Idempotent: safe to re-run.
"""
import json
import os
import subprocess
from pathlib import Path

import requests

from policy_tags_lib import merge_binding

REPO_ROOT = Path(__file__).parent.parent
POLICY_TAGS_PATH = REPO_ROOT / "dbt" / "policy_tags.yml"

API = "https://datacatalog.googleapis.com/v1"
FINE_GRAINED_READER_ROLE = "roles/datacatalog.categoryFineGrainedReader"


def _get_access_token() -> str:
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _find_by_display_name(items: list, display_name: str):
    for item in items:
        if item.get("displayName") == display_name:
            return item
    return None


def ensure_taxonomy(token: str, parent: str) -> str:
    resp = requests.get(f"{API}/{parent}/taxonomies", headers=_headers(token))
    resp.raise_for_status()
    existing = _find_by_display_name(resp.json().get("taxonomies", []), "data_sensitivity")
    if existing:
        return existing["name"]
    resp = requests.post(
        f"{API}/{parent}/taxonomies",
        headers=_headers(token),
        json={
            "displayName": "data_sensitivity",
            "activatedPolicyTypes": ["FINE_GRAINED_ACCESS_CONTROL"],
        },
    )
    resp.raise_for_status()
    return resp.json()["name"]


def ensure_policy_tag(token: str, taxonomy_name: str, display_name: str) -> str:
    resp = requests.get(f"{API}/{taxonomy_name}/policyTags", headers=_headers(token))
    resp.raise_for_status()
    existing = _find_by_display_name(resp.json().get("policyTags", []), display_name)
    if existing:
        return existing["name"]
    resp = requests.post(
        f"{API}/{taxonomy_name}/policyTags",
        headers=_headers(token),
        json={"displayName": display_name},
    )
    resp.raise_for_status()
    return resp.json()["name"]


def grant_fine_grained_reader(token: str, policy_tag_name: str, member: str) -> None:
    resp = requests.post(f"{API}/{policy_tag_name}:getIamPolicy", headers=_headers(token))
    resp.raise_for_status()
    policy = resp.json()
    updated = merge_binding(policy, FINE_GRAINED_READER_ROLE, member)
    resp = requests.post(
        f"{API}/{policy_tag_name}:setIamPolicy",
        headers=_headers(token),
        json={"policy": updated},
    )
    resp.raise_for_status()


def main() -> None:
    project_id = os.environ["GCP_PROJECT_ID"]
    location = os.environ["BQ_LOCATION"]
    parent = f"projects/{project_id}/locations/{location}"

    token = _get_access_token()

    print(f"==> Ensuring taxonomy data_sensitivity exists in {location}")
    taxonomy_name = ensure_taxonomy(token, parent)
    print(f"    {taxonomy_name}")

    print("==> Ensuring policy tag pii_high exists")
    pii_high = ensure_policy_tag(token, taxonomy_name, "pii_high")
    print(f"    {pii_high}")

    print("==> Ensuring policy tag pii_low exists")
    pii_low = ensure_policy_tag(token, taxonomy_name, "pii_low")
    print(f"    {pii_low}")

    governance_sa = f"persona-governance@{project_id}.iam.gserviceaccount.com"
    print(f"==> Granting {FINE_GRAINED_READER_ROLE} on pii_high to {governance_sa}")
    grant_fine_grained_reader(token, pii_high, f"serviceAccount:{governance_sa}")

    POLICY_TAGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    POLICY_TAGS_PATH.write_text(
        "# Generated by scripts/01_policy_tags.py — do not edit by hand, do not commit.\n"
        f'pii_high: "{pii_high}"\n'
        f'pii_low: "{pii_low}"\n'
    )
    print(f"\n==> Policy tags setup complete. Resource names written to {POLICY_TAGS_PATH}")


if __name__ == "__main__":
    main()
```

Note: `tests/test_policy_tags.py` imports `from scripts.policy_tags_lib import merge_binding` — add `scripts/__init__.py` (empty) so `scripts` is an importable package, same pattern as `data_gen/__init__.py` in Plan A. Update the import in `scripts/01_policy_tags.py` itself to `from policy_tags_lib import merge_binding` (same-directory import, since it's run as a script via `uv run python scripts/01_policy_tags.py`, not imported as a package) — the test imports it the package way, the script imports it the script way; both resolve to the same file.

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_policy_tags.py -v`
Expected: PASS (11 passed).

- [ ] **Step 7: Update .gitignore and add the Makefile target**

Add to `.gitignore`:
```
dbt/policy_tags.yml
```

Extend `Makefile`'s `.PHONY` line and add:

```makefile
policy-tags:
	set -a && . ./.env && set +a && uv run python scripts/01_policy_tags.py
```

- [ ] **Step 8: Run the full suite once and commit**

Run: `uv run pytest -v` — expect all passing (existing + 11 new).
Run: `uv run ruff check .` — expect clean.

```bash
git add scripts/01_policy_tags.py scripts/policy_tags_lib.py scripts/__init__.py tests/test_policy_tags.py pyproject.toml uv.lock .gitignore Makefile
git commit -m "feat: add policy tag taxonomy script with offline-tested IAM merge logic"
```

---

### Task 2: Wire policy tag names into schema.yml via dbt --vars

**Files:**
- Modify: `dbt/dbt_project.yml` (add `persist_docs` config for marts)
- Modify: `dbt/models/marts/schema.yml` (add `policy_tags:` config to `customers.email`/`phone`/`full_name`)
- Modify: `Makefile` (`dbt-build` target passes `--vars` from `dbt/policy_tags.yml`)
- Modify: `tests/test_dbt_models.py` (add tests for the above)

**Interfaces:**
- Consumes: `dbt/policy_tags.yml`'s `pii_high`/`pii_low` keys (produced by Task 1, at real-run time — not present when this task's own tests run, since those never touch real GCP).
- Produces: `customers.email`/`phone`/`full_name` carrying real BigQuery policy tags once run for real.

- [ ] **Step 1: Write the failing tests**

```python
# Add to tests/test_dbt_models.py

def test_dbt_project_yml_persists_docs_for_marts():
    config = yaml.safe_load((DBT_DIR / "dbt_project.yml").read_text())
    marts_config = config["models"]["governed_analyst_agent"]["marts"]
    assert marts_config["+persist_docs"] == {"relation": True, "columns": True}


def test_customers_pii_columns_have_policy_tags_config():
    schema = yaml.safe_load((MARTS_DIR / "schema.yml").read_text())
    models_by_name = {m["name"]: m for m in schema["models"]}
    columns = {c["name"]: c for c in models_by_name["customers"]["columns"]}
    for col_name in ["email", "phone", "full_name"]:
        policy_tags = columns[col_name]["config"].get("policy_tags", {})
        names = policy_tags.get("names", [])
        assert any("pii_high" in n for n in names), (
            f"customers.{col_name} config.policy_tags.names should reference the pii_high var"
        )


def test_makefile_dbt_build_passes_vars_from_policy_tags_yml():
    text = (REPO_ROOT / "Makefile").read_text()
    assert "policy_tags.yml" in text
    assert "--vars" in text


def test_dbt_parse_with_dummy_policy_tag_vars_succeeds():
    """dbt parse never opens a warehouse connection (confirmed in Plan A's
    real run) — it only compiles Jinja/YAML, so this is safe to run for
    real, with dummy var values standing in for the real resource names
    Task 1 would produce. This is the plan's central verification: does
    schema.yml's policy_tags config actually accept {{ var(...) }}?
    """
    import shutil
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        profiles_dir = Path(tmp)
        shutil.copy(DBT_DIR / "profiles.yml.example", profiles_dir / "profiles.yml")
        env = {
            **os.environ,
            "GCP_PROJECT_ID": "dbt-parse-check",
            "BQ_DATASET": "governed_analytics",
            "BQ_LOCATION": "US",
            "DBT_PROFILES_DIR": str(profiles_dir),
        }
        result = subprocess.run(
            [
                "dbt",
                "parse",
                "--project-dir",
                str(DBT_DIR),
                "--vars",
                '{"pii_high": "dummy_pii_high", "pii_low": "dummy_pii_low"}',
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
```

Add `import os` at the top of `tests/test_dbt_models.py` if not already present.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_dbt_models.py -v -k "persist_docs or policy_tags or dbt_build or dummy_policy_tag"`
Expected: FAIL — none of the config changes exist yet.

- [ ] **Step 3: Add persist_docs to dbt_project.yml**

```yaml
# dbt/dbt_project.yml — models.governed_analyst_agent.marts block
    marts:
      +materialized: table
      +persist_docs:
        relation: true
        columns: true
```

(Leave the rest of `dbt_project.yml`, including the `staging`/`seeds` schema isolation from Plan A's final-review fix, unchanged.)

- [ ] **Step 4: Add policy_tags to schema.yml's customers columns — as a top-level property, NOT nested under config**

**CONFIRMED (2026-09-16, verified twice independently — once by tracing dbt-core/dbt-bigquery source, once by re-deriving that trace and separately reproducing the manifest evidence in an isolated scratchpad):** `policy_tags:` nested under a column's `config:` block is a **silent no-op**. dbt-core's `ParserRef._add` (`dbt/parser/common.py`) only pulls `meta`/`tags` out of `config:`; any other nested key, including `policy_tags`, is dropped before the column ever reaches the compiled manifest. `dbt parse` reports success either way — it does not error, it just silently doesn't wire the tag. At real `dbt run` time, dbt-bigquery's `_update_column_dict` (`dbt/adapters/bigquery/impl.py`) reads `policy_tags` off the **top-level** serialized column dict, so a nested `config.policy_tags` would make it apply an *empty* list — clearing any real policy tags rather than setting them.

The working shape, in `dbt/models/marts/schema.yml` under `customers`, is `policy_tags:` as a sibling of `config:`, not inside it:

```yaml
      - name: email
        description: Customer's email address.
        config:
          meta:
            sensitivity: pii_high
            owner: governance
        policy_tags:
          - "{{ var('pii_high') }}"
```

Apply the same top-level `policy_tags:` addition to `phone` and `full_name` (their existing `config: {meta: {...}}` block stays as-is, unchanged). Do **not** add it to `support_tickets.body` yet — that column is out of scope for this customers-only spike (Plan B2 extends it).

- [ ] **Step 5: Wire --vars into the Makefile's dbt-build target**

```makefile
dbt-build:
	set -a && . ./.env && set +a && cd dbt && \
	dbt seed && \
	dbt run --vars "$$(cat policy_tags.yml)" && \
	dbt test
```

`dbt/policy_tags.yml`'s content (`pii_high: "..."` / `pii_low: "..."`, produced by Task 1's script) is valid YAML, and dbt's `--vars` flag accepts a YAML string directly — no JSON conversion needed.

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_dbt_models.py -v`
Expected: PASS (all tests, including the new `dbt parse --vars` check with dummy values). If `test_dbt_parse_with_dummy_policy_tag_vars_succeeds` fails for a reason unrelated to the `policy_tags`/`var` mechanism itself (e.g. a missing `dbt` binary or environment issue), report it as a concern — but if it fails because the YAML/Jinja shape is wrong, that's exactly the central uncertainty this task exists to resolve; fix `schema.yml`'s `policy_tags:` shape based on the actual error message rather than guessing further, and leave a `# VERIFY:` comment on it if still uncertain after your best attempt.

- [ ] **Step 7: Run the full suite and commit**

Run: `uv run pytest -v` — all passing.
Run: `uv run ruff check .` — clean.

```bash
git add dbt/dbt_project.yml dbt/models/marts/schema.yml Makefile tests/test_dbt_models.py
git commit -m "feat: wire policy tag names into schema.yml via dbt --vars, verified via dbt parse"
```

---

### Task 3: Row access policy post-hook on customers

**Files:**
- Modify: `dbt/models/marts/customers.sql` (add `post_hook` config)
- Modify: `tests/test_dbt_models.py` (add tests)

**Interfaces:**
- Consumes: `USER_EMAIL`/`GCP_PROJECT_ID` from `.env` (via `env_var()`), persona service account naming convention (`persona-<name>@<project>.iam.gserviceaccount.com`) established in Phase 0.
- Produces: two row access policies (`all_rows`, `east_only`) on the real `customers` table once run for real — re-applied automatically on every `dbt run` since they're a post-hook, surviving the `CREATE OR REPLACE` that drops them each time (per BUILD_SPEC.md §5's explicit gotcha).

- [ ] **Step 1: Write the failing tests**

```python
# Add to tests/test_dbt_models.py

def test_customers_model_has_row_access_policy_post_hook():
    text = (MARTS_DIR / "customers.sql").read_text()
    assert "post_hook" in text
    assert "ROW ACCESS POLICY" in text
    assert "all_rows" in text
    assert "east_only" in text


def test_all_rows_policy_grants_analyst_governance_and_human():
    text = (MARTS_DIR / "customers.sql").read_text()
    assert "persona-analyst" in text
    assert "persona-governance" in text
    assert "env_var('USER_EMAIL')" in text
    assert "FILTER USING (TRUE)" in text


def test_east_only_policy_grants_support_east_and_filters_region():
    text = (MARTS_DIR / "customers.sql").read_text()
    assert "persona-support-east" in text
    assert "FILTER USING (region = 'East')" in text


def test_row_access_policy_uses_this_not_hardcoded_table_path():
    text = (MARTS_DIR / "customers.sql").read_text()
    assert "{{ this }}" in text
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_dbt_models.py -v -k "row_access_policy or all_rows_policy or east_only_policy"`
Expected: FAIL — `customers.sql` has no post_hook yet.

- [ ] **Step 3: Add the post-hook to customers.sql**

```sql
-- dbt/models/marts/customers.sql
-- Row access policies re-applied on every `dbt run` via post-hook, since
-- CREATE OR REPLACE (this model's materialization) drops them otherwise
-- (see BUILD_SPEC.md §5). Scoped to `customers` only for this spike —
-- orders/support_tickets get the same pattern in a later plan.
{{ config(
    post_hook=[
        "CREATE OR REPLACE ROW ACCESS POLICY all_rows ON {{ this }} GRANT TO (\"serviceAccount:persona-analyst@{{ env_var('GCP_PROJECT_ID') }}.iam.gserviceaccount.com\", \"serviceAccount:persona-governance@{{ env_var('GCP_PROJECT_ID') }}.iam.gserviceaccount.com\", \"user:{{ env_var('USER_EMAIL') }}\") FILTER USING (TRUE)",
        "CREATE OR REPLACE ROW ACCESS POLICY east_only ON {{ this }} GRANT TO (\"serviceAccount:persona-support-east@{{ env_var('GCP_PROJECT_ID') }}.iam.gserviceaccount.com\") FILTER USING (region = 'East')"
    ]
) }}
select * from {{ ref('stg_customers') }}
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_dbt_models.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Best-effort real check**

Attempt the same `dbt parse` best-effort check pattern from Plan A (copy `profiles.yml.example` to `profiles.yml` temporarily, run `dbt parse --project-dir dbt --vars '{"pii_high": "dummy", "pii_low": "dummy"}'`, delete `profiles.yml` again afterward). Confirm the post-hook's nested Jinja (`{{ env_var(...) }}` calls inside the post_hook string list) renders without error. If it fails for a credential/environment reason, note as a concern, don't force it.

- [ ] **Step 6: Run the full suite and commit**

Run: `uv run pytest -v` — all passing.
Run: `uv run ruff check .` — clean.

```bash
git add dbt/models/marts/customers.sql tests/test_dbt_models.py
git commit -m "feat: add row access policy post-hook to customers mart"
```

---

### Task 4: `scripts/smoke_test.sh` — customers-only persona comparison

**Files:**
- Create: `scripts/smoke_test.sh`
- Create: `tests/test_smoke_test.py`
- Modify: `Makefile` (add `smoke` target)

**Interfaces:**
- Consumes: the three persona service account names (Phase 0), `GCP_PROJECT_ID`/`BQ_DATASET` (`.env`).
- Produces: nothing new — this is the plan's actual exit gate, run by the human.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_smoke_test.py
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "smoke_test.sh"


def read_script() -> str:
    assert SCRIPT.exists(), f"{SCRIPT} does not exist"
    return SCRIPT.read_text()


def test_uses_strict_mode():
    assert "set -euo pipefail" in read_script()


def test_queries_as_all_three_personas():
    text = read_script()
    for persona in ["persona-analyst", "persona-support-east", "persona-governance"]:
        assert persona in text


def test_uses_impersonate_service_account_flag():
    text = read_script()
    assert "--impersonate_service_account" in text


def test_runs_region_count_query():
    text = read_script()
    assert "SELECT region, COUNT(*)" in text or "select region, count(*)" in text.lower()


def test_runs_email_query_expecting_denial_for_non_governance():
    text = read_script()
    assert "SELECT email" in text or "select email" in text.lower()


def test_no_hardcoded_project_values():
    text = read_script()
    assert "${GCP_PROJECT_ID}" in text
    assert "your-project-id" not in text
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `uv run pytest tests/test_smoke_test.py -v`
Expected: FAIL — `scripts/smoke_test.sh` does not exist.

- [ ] **Step 3: Write the script**

```bash
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
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `uv run pytest tests/test_smoke_test.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Add the Makefile target**

```makefile
smoke:
	set -a && . ./.env && set +a && bash scripts/smoke_test.sh
```

- [ ] **Step 6: Run the full suite, lint, and commit**

Run: `uv run pytest -v` — all passing.
Run: `make lint` (ruff + shellcheck) — clean.

```bash
git add scripts/smoke_test.sh tests/test_smoke_test.py Makefile
git commit -m "feat: add customers-only smoke test comparing all three personas"
```

---

## Self-Review Notes

- **Spec coverage:** Task 1 covers §5's taxonomy/policy-tag/governance-grant requirements. Task 2 covers §5's "attach tags through dbt schema.yml" requirement and its `persist_docs`/VERIFY note — resolved empirically via the `dbt parse --vars` check rather than left as an open question. Task 3 covers §5's row access policy gotchas, including the `CREATE OR REPLACE` re-apply requirement, via post-hook as BUILD_SPEC.md now specifies (updated earlier this session). Task 4 covers §6 Phase 1 step 4's smoke test exactly, scoped to `customers` per this plan's spike boundary.
- **Placeholder scan:** no TBD/TODO. The one deliberately-open point (Task 2 Step 6's "if it fails, fix based on the real error, don't guess further") is explicit about what to do, not a placeholder — it's the plan's one genuinely unresolved technical question, framed as an instruction, not left blank.
- **Type/name consistency:** persona names, `GCP_PROJECT_ID`/`BQ_DATASET`/`USER_EMAIL` env var names, and the `pii_high`/`pii_low` tag names are identical across all four tasks and match BUILD_SPEC.md and Phase 0/Plan A's established conventions.
- **Verified against real docs, not guessed:** Task 1's REST API endpoints (`taxonomies.create`, `policyTags.create`, `policyTags.setIamPolicy`) were confirmed against current Google Cloud documentation before being written into this plan, specifically because Phase 0's `bq add-iam-policy-binding` mistake showed the cost of a confidently-guessed command that turned out to be wrong. The one place this plan still has a real "verify empirically" gap (Task 2's `policy_tags:` + `{{ var(...) }}` interaction) is resolved by a required, credential-free `dbt parse` check rather than deferred to a human's real run — narrowing what's left genuinely unknown until Plan B2 or a real `make dbt-build`.
