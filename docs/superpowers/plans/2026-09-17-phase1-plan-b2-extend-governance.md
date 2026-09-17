# Phase 1 Plan B2: Extend Governance to orders and support_tickets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the governance mechanism Plan B1 proved on `customers` — policy tags and row access policies — to `orders` and `support_tickets`, and extend the smoke test to verify all three tables, not just `customers`. No new mechanism, no new uncertainty: Plan B1 already resolved the one genuinely uncertain question (does `schema.yml`'s `policy_tags:` work as a top-level column property with `{{ var(...) }}`) and verified it against real GCP. This plan is repetition of a proven pattern.

**Architecture:** Two tasks. Task 1 adds the row access policy post-hook (already fully generic via `{{ this }}`, near-verbatim from `customers.sql`) to `orders.sql` and `support_tickets.sql`, and adds the `policy_tags:` property to `support_tickets.body` (the only PII column in either table — `orders` has none, per BUILD_SPEC.md §5's `pii_high` list). Task 2 extends `scripts/smoke_test.sh` to check region-count governance on all three tables and PII-column governance on both `customers.email` and `support_tickets.body`, completing the real, end-to-end proof BUILD_SPEC.md §6 Phase 1 asks for.

**Tech Stack:** dbt-bigquery (SQL models, `schema.yml`), Bash (the smoke test), `uv`/`pytest`/`ruff`/`shellcheck`.

**Spec:** `BUILD_SPEC.md` (repo root) — this plan completes §6 Phase 1 steps 3-4 for all three marts (Plan B1 completed them for `customers` only), per §5's taxonomy/policy-tag/row-access-policy requirements.

## Global Constraints

- Never run commands that create, modify, or delete cloud resources (`gcloud`, `bq`, `dbt run`, `dbt seed`, `dbt build`, `dbt test`, `dbt debug`). Write them into scripts/Makefile targets and tell the human to run them.
- **The one exception:** `dbt parse` (pure Jinja/YAML compilation) never opens a warehouse connection — it may be run by an agent as a required verification step.
- All project-specific values come from `.env` / environment variables. Never hardcode project IDs, datasets, regions, or resource names.
- `orders` has no PII columns (BUILD_SPEC.md §5's `pii_high` list is customer `email`/`phone`/`full_name` and ticket `body` — nothing in `orders`) — it gets row access policies only, never `policy_tags:`. A test must assert this negative (no over-tagging), not just the positive cases.
- `policy_tags:` is a **top-level column property**, sibling to `config:`, never nested inside it — this was Plan B1's hard-won, empirically-verified finding. Do not "simplify" it back to the nested shape.
- Python 3.11+, `uv` for dependency management, `ruff` for lint, `pytest` for tests. Bash scripts use `set -euo pipefail`. Avoid bash 4+-only features (associative arrays, etc.) — macOS ships bash 3.2 by default and `#!/usr/bin/env bash` may resolve to it.

---

### Task 1: Row access policies + policy tags on orders and support_tickets

**Files:**
- Modify: `dbt/models/marts/orders.sql` (add row access policy post-hook)
- Modify: `dbt/models/marts/support_tickets.sql` (add row access policy post-hook)
- Modify: `dbt/models/marts/schema.yml` (add `policy_tags:` to `support_tickets.body`)
- Modify: `dbt/models/marts/customers.sql` (update stale comment — it currently says "orders/support_tickets get the same pattern in a later plan," which is now this plan)
- Modify: `tests/test_dbt_models.py`

**Interfaces:**
- Consumes: the proven post-hook pattern and `{{ var('pii_high') }}` wiring from Plan B1's `customers.sql`/`schema.yml`.
- Produces: nothing new for later plans — this completes Phase 1's governance requirement for all three marts.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_dbt_models.py`:

```python
def test_orders_model_has_row_access_policy_post_hook():
    text = (MARTS_DIR / "orders.sql").read_text()
    assert "post_hook" in text
    assert "ROW ACCESS POLICY" in text
    assert "all_rows" in text
    assert "east_only" in text
    assert "{{ this }}" in text


def test_orders_all_rows_policy_grants_analyst_governance_and_human():
    text = (MARTS_DIR / "orders.sql").read_text()
    assert "persona-analyst" in text
    assert "persona-governance" in text
    assert "env_var('USER_EMAIL')" in text
    assert "FILTER USING (TRUE)" in text


def test_orders_east_only_policy_grants_support_east_and_filters_region():
    text = (MARTS_DIR / "orders.sql").read_text()
    assert "persona-support-east" in text
    assert "FILTER USING (region = 'East')" in text


def test_support_tickets_model_has_row_access_policy_post_hook():
    text = (MARTS_DIR / "support_tickets.sql").read_text()
    assert "post_hook" in text
    assert "ROW ACCESS POLICY" in text
    assert "all_rows" in text
    assert "east_only" in text
    assert "{{ this }}" in text


def test_support_tickets_all_rows_policy_grants_analyst_governance_and_human():
    text = (MARTS_DIR / "support_tickets.sql").read_text()
    assert "persona-analyst" in text
    assert "persona-governance" in text
    assert "env_var('USER_EMAIL')" in text
    assert "FILTER USING (TRUE)" in text


def test_support_tickets_east_only_policy_grants_support_east_and_filters_region():
    text = (MARTS_DIR / "support_tickets.sql").read_text()
    assert "persona-support-east" in text
    assert "FILTER USING (region = 'East')" in text


def test_support_tickets_body_has_top_level_policy_tags_config():
    schema = yaml.safe_load((MARTS_DIR / "schema.yml").read_text())
    models_by_name = {m["name"]: m for m in schema["models"]}
    columns = {c["name"]: c for c in models_by_name["support_tickets"]["columns"]}
    body_col = columns["body"]
    # Top-level, NOT nested under config: — this is Plan B1's proven shape.
    assert "policy_tags" not in body_col.get("config", {}), (
        "policy_tags must NOT be nested under config: — this is a silent no-op "
        "(see Plan B1's finding). It must be a top-level column property."
    )
    names = body_col.get("policy_tags", [])
    assert any("pii_high" in n for n in names), (
        "support_tickets.body's top-level policy_tags should reference the pii_high var"
    )


def test_orders_has_no_policy_tags_anywhere():
    schema = yaml.safe_load((MARTS_DIR / "schema.yml").read_text())
    models_by_name = {m["name"]: m for m in schema["models"]}
    for column in models_by_name["orders"]["columns"]:
        assert "policy_tags" not in column, (
            f"orders.{column['name']} should not have policy_tags — orders has no PII "
            "columns per BUILD_SPEC.md §5"
        )


def test_dbt_parse_tags_support_tickets_body_with_dummy_var():
    """Extends the existing dbt-parse verification (Plan B1) to confirm
    support_tickets.body actually receives the policy tag in the compiled
    manifest, not just customers' columns."""
    import json
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
        manifest = json.loads((DBT_DIR / "target" / "manifest.json").read_text())
        node = next(
            n
            for n in manifest["nodes"].values()
            if n["name"] == "support_tickets" and n["resource_type"] == "model"
        )
        assert node["columns"]["body"]["policy_tags"] == ["dummy_pii_high"]
```

Ensure `import os` and `from pathlib import Path` are already imported at the top of `tests/test_dbt_models.py` (they should be, from Plan B1) — add them if not.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_dbt_models.py -v -k "orders_model or orders_all_rows or orders_east_only or support_tickets_model or support_tickets_all_rows or support_tickets_east_only or top_level_policy_tags or orders_has_no_policy_tags or tags_support_tickets"`
Expected: FAIL — none of the model/schema changes exist yet.

- [ ] **Step 3: Add the row access policy post-hook to orders.sql**

```sql
-- dbt/models/marts/orders.sql
-- Row access policies re-applied on every `dbt run` via post-hook, since
-- CREATE OR REPLACE (this model's materialization) drops them otherwise
-- (see BUILD_SPEC.md §5). No PII columns in this table (see BUILD_SPEC.md
-- §5's pii_high list) — row access policies only, no policy_tags.
{{ config(
    post_hook=[
        "CREATE OR REPLACE ROW ACCESS POLICY all_rows ON {{ this }} GRANT TO (\"serviceAccount:persona-analyst@{{ env_var('GCP_PROJECT_ID') }}.iam.gserviceaccount.com\", \"serviceAccount:persona-governance@{{ env_var('GCP_PROJECT_ID') }}.iam.gserviceaccount.com\", \"user:{{ env_var('USER_EMAIL') }}\") FILTER USING (TRUE)",
        "CREATE OR REPLACE ROW ACCESS POLICY east_only ON {{ this }} GRANT TO (\"serviceAccount:persona-support-east@{{ env_var('GCP_PROJECT_ID') }}.iam.gserviceaccount.com\") FILTER USING (region = 'East')"
    ]
) }}
select * from {{ ref('stg_orders') }}
```

- [ ] **Step 4: Add the row access policy post-hook to support_tickets.sql**

```sql
-- dbt/models/marts/support_tickets.sql
-- Row access policies re-applied on every `dbt run` via post-hook, since
-- CREATE OR REPLACE (this model's materialization) drops them otherwise
-- (see BUILD_SPEC.md §5).
{{ config(
    post_hook=[
        "CREATE OR REPLACE ROW ACCESS POLICY all_rows ON {{ this }} GRANT TO (\"serviceAccount:persona-analyst@{{ env_var('GCP_PROJECT_ID') }}.iam.gserviceaccount.com\", \"serviceAccount:persona-governance@{{ env_var('GCP_PROJECT_ID') }}.iam.gserviceaccount.com\", \"user:{{ env_var('USER_EMAIL') }}\") FILTER USING (TRUE)",
        "CREATE OR REPLACE ROW ACCESS POLICY east_only ON {{ this }} GRANT TO (\"serviceAccount:persona-support-east@{{ env_var('GCP_PROJECT_ID') }}.iam.gserviceaccount.com\") FILTER USING (region = 'East')"
    ]
) }}
select * from {{ ref('stg_support_tickets') }}
```

- [ ] **Step 5: Add policy_tags to support_tickets.body in schema.yml**

In `dbt/models/marts/schema.yml`, under `support_tickets`, add a **top-level** `policy_tags:` property to the `body` column (sibling of `config:`, matching Plan B1's proven shape — do NOT nest it inside `config:`):

```yaml
      - name: body
        description: Ticket body text. Contains planted canary PII for leak-detection evals.
        config:
          meta:
            sensitivity: pii_high
            owner: governance
        policy_tags:
          - "{{ var('pii_high') }}"
```

- [ ] **Step 6: Update the stale comment in customers.sql**

In `dbt/models/marts/customers.sql`, change:
```sql
-- Row access policies re-applied on every `dbt run` via post-hook, since
-- CREATE OR REPLACE (this model's materialization) drops them otherwise
-- (see BUILD_SPEC.md §5). Scoped to `customers` only for this spike —
-- orders/support_tickets get the same pattern in a later plan.
```
to:
```sql
-- Row access policies re-applied on every `dbt run` via post-hook, since
-- CREATE OR REPLACE (this model's materialization) drops them otherwise
-- (see BUILD_SPEC.md §5). orders and support_tickets have the same pattern
-- (see those models) — this was proven here first as a spike (Plan B1)
-- before extending to them (Plan B2).
```

- [ ] **Step 7: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_dbt_models.py -v`
Expected: PASS (all tests, including the new dbt-parse manifest check).

- [ ] **Step 8: Best-effort real check**

Same pattern as Plan B1: copy `dbt/profiles.yml.example` to `dbt/profiles.yml` temporarily, run `dbt parse --project-dir dbt --vars '{"pii_high": "dummy", "pii_low": "dummy"}'`, delete `dbt/profiles.yml` again afterward. Confirms all three models' post-hook Jinja and the new `policy_tags:` addition render without error.

- [ ] **Step 9: Run the full suite and commit**

Run: `uv run pytest -v` — all passing.
Run: `uv run ruff check .` — clean.

```bash
git add dbt/models/marts/orders.sql dbt/models/marts/support_tickets.sql dbt/models/marts/schema.yml dbt/models/marts/customers.sql tests/test_dbt_models.py
git commit -m "feat: extend row access policies to orders/support_tickets, policy tags to support_tickets.body"
```

---

### Task 2: Extend smoke test to all three tables

**Files:**
- Modify: `scripts/smoke_test.sh`
- Modify: `tests/test_smoke_test.py`

**Interfaces:**
- Consumes: Task 1's row access policies and policy tags (only meaningfully testable once a human runs `make dbt-build` for real — this task's tests remain static/structural, same constraint as Plan B1).

- [ ] **Step 1: Write the failing tests**

```python
# Add to tests/test_smoke_test.py

def test_checks_region_governance_on_all_three_tables():
    text = read_script()
    for table in ["customers", "orders", "support_tickets"]:
        assert table in text, f"smoke test should check {table}"


def test_checks_pii_governance_on_customers_email_and_support_tickets_body():
    text = read_script()
    assert "customers" in text and "email" in text
    assert "support_tickets" in text and "body" in text


def test_no_pii_check_attempted_on_orders():
    text = read_script()
    # orders has no PII column (see BUILD_SPEC.md §5) — the PII-check list
    # must not include it.
    pii_checks_section = text.split("PII_CHECKS")[1] if "PII_CHECKS" in text else ""
    assert "orders:" not in pii_checks_section
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_smoke_test.py -v -k "all_three_tables or pii_governance_on_customers or no_pii_check_attempted"`
Expected: FAIL — script doesn't check `orders`/`support_tickets` yet.

- [ ] **Step 3: Rewrite the script to loop over all three tables and both PII checks**

Replace `scripts/smoke_test.sh`'s query-definition and main loop sections (keep `run_as_persona_for_denial_check`, `run_as_persona_for_json`, `is_access_denied`, `expected_region_count`, `record_result` unchanged) with:

```bash
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
```

This replaces the old hardcoded `REGION_QUERY`/`EMAIL_QUERY` variables and the single-table loop entirely — remove those two variable definitions since they're no longer used.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_smoke_test.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Run the full suite, lint, and commit**

Run: `uv run pytest -v` — all passing.
Run: `make lint` (ruff + shellcheck) — clean.

```bash
git add scripts/smoke_test.sh tests/test_smoke_test.py
git commit -m "feat: extend smoke test to verify governance on orders and support_tickets"
```

---

## Self-Review Notes

- **Spec coverage:** completes BUILD_SPEC.md §6 Phase 1 steps 3-4 for all three marts. `orders`' lack of PII columns is handled correctly (row access policies only) and explicitly tested against (both the positive "has row policies" and negative "has no policy_tags" cases).
- **Placeholder scan:** no TBD/TODO; every step has complete, real content.
- **Type/name consistency:** the row access policy post-hook is byte-for-byte the same pattern across `customers.sql`, `orders.sql`, `support_tickets.sql` (only the `{{ this }}` reference differs implicitly per-model). `policy_tags:` shape matches Plan B1's proven top-level convention exactly, with an explicit test guarding against regression to the broken nested shape (the same class of bug Plan B1 found).
- **Portability note carried forward:** the smoke test rewrite explicitly avoids bash 4+ associative arrays, given the Global Constraints note about macOS's default bash 3.2.
- **Exit gate:** once a human runs `make dbt-build && make smoke` for real, a clean pass completes BUILD_SPEC.md's full Phase 1 "Done when" criterion across all three tables — not just the `customers` spike.
