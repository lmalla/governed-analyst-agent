import os
from pathlib import Path

import yaml

from data_gen.generate import CUSTOMER_COLUMNS, ORDER_COLUMNS, TICKET_COLUMNS

REPO_ROOT = Path(__file__).parent.parent
DBT_DIR = REPO_ROOT / "dbt"
STAGING_DIR = DBT_DIR / "models" / "staging"
MARTS_DIR = DBT_DIR / "models" / "marts"


def test_dbt_project_yml_is_valid_with_required_keys():
    config = yaml.safe_load((DBT_DIR / "dbt_project.yml").read_text())
    assert config["name"]
    assert config["profile"]
    assert config["model-paths"] == ["models"]
    assert config["seed-paths"] == ["seeds"]


def test_dbt_project_yml_isolates_staging_and_seeds_from_governed_dataset():
    """Raw/staging tables must land in a differently-named dataset than the
    marts, so BQ_DATASET dataViewer grants (analyst/support_east) don't also
    expose raw PII columns via raw_customers/stg_customers."""
    config = yaml.safe_load((DBT_DIR / "dbt_project.yml").read_text())
    assert config["models"]["governed_analyst_agent"]["staging"]["+schema"] == "staging"
    assert config["seeds"]["governed_analyst_agent"]["+schema"] == "raw"


def test_profiles_example_uses_env_vars_and_oauth():
    text = (DBT_DIR / "profiles.yml.example").read_text()
    assert "env_var('GCP_PROJECT_ID')" in text
    assert "env_var('BQ_DATASET')" in text
    assert "env_var('BQ_LOCATION')" in text
    assert "method: oauth" in text
    assert "type: bigquery" in text


def test_staging_models_exist():
    for name in ["stg_customers", "stg_orders", "stg_support_tickets"]:
        assert (STAGING_DIR / f"{name}.sql").exists()


def test_stg_customers_selects_all_raw_columns():
    text = (STAGING_DIR / "stg_customers.sql").read_text()
    assert "ref('raw_customers')" in text
    for col in CUSTOMER_COLUMNS:
        assert col in text, f"stg_customers.sql missing reference to raw column {col}"


def _strip_sql_comments(text: str) -> str:
    """Drop `--`-comment lines so substring assertions check the actual SQL
    (e.g. the SELECT list), not explanatory comments that happen to mention
    the same words."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("--"))


def test_stg_orders_denormalizes_region_via_customer_join():
    text = (STAGING_DIR / "stg_orders.sql").read_text()
    body = _strip_sql_comments(text)
    assert "ref('raw_orders')" in body
    assert "ref('stg_customers')" in body
    assert "region" in body
    assert "c.region" in body, "stg_orders.sql must select region from the joined customer"
    for col in ORDER_COLUMNS:
        assert col in body, f"stg_orders.sql missing reference to raw column {col}"


def test_stg_support_tickets_denormalizes_region_via_customer_join():
    text = (STAGING_DIR / "stg_support_tickets.sql").read_text()
    body = _strip_sql_comments(text)
    assert "ref('raw_support_tickets')" in body
    assert "ref('stg_customers')" in body
    assert "region" in body
    assert "c.region" in body, "stg_support_tickets.sql must select region from the joined customer"
    for col in TICKET_COLUMNS:
        assert col in body, f"stg_support_tickets.sql missing reference to raw column {col}"


def test_mart_models_exist_and_select_from_staging():
    marts = {
        "customers": "stg_customers",
        "orders": "stg_orders",
        "support_tickets": "stg_support_tickets",
    }
    for mart, staging in marts.items():
        path = MARTS_DIR / f"{mart}.sql"
        assert path.exists()
        assert f"ref('{staging}')" in path.read_text()


def test_schema_yml_documents_all_three_marts():
    schema = yaml.safe_load((MARTS_DIR / "schema.yml").read_text())
    model_names = {m["name"] for m in schema["models"]}
    assert model_names == {"customers", "orders", "support_tickets"}


def test_schema_yml_marks_pii_high_columns():
    # dbt 1.10+ moved column-level `meta` under a `config:` sibling of
    # `description`/`tests` (PropertyMovedToConfigDeprecation) — look it up
    # at its new location, not the pre-1.10 top-level `meta` key.
    schema = yaml.safe_load((MARTS_DIR / "schema.yml").read_text())
    models_by_name = {m["name"]: m for m in schema["models"]}
    expected_pii = {
        "customers": {"full_name", "email", "phone"},
        "support_tickets": {"body"},
    }
    for model_name, pii_cols in expected_pii.items():
        columns = {c["name"]: c for c in models_by_name[model_name]["columns"]}
        for col_name in pii_cols:
            meta = columns[col_name].get("config", {}).get("meta", {})
            assert meta.get("sensitivity") == "pii_high", (
                f"{model_name}.{col_name} should be tagged config.meta.sensitivity: pii_high"
            )


def test_schema_yml_has_relationships_tests_to_customers():
    # dbt 1.10.8+ requires generic-test parameters nested under `arguments:`
    # (MissingArgumentsPropertyInGenericTestDeprecation) — the relationships
    # test's `to`/`field` now live under arguments, not directly on the test.
    schema = yaml.safe_load((MARTS_DIR / "schema.yml").read_text())
    models_by_name = {m["name"]: m for m in schema["models"]}
    for model_name in ["orders", "support_tickets"]:
        columns = {c["name"]: c for c in models_by_name[model_name]["columns"]}
        tests = columns["customer_id"].get("tests", [])
        has_relationship = any(
            isinstance(t, dict)
            and "relationships" in t
            and "arguments" in (t["relationships"] or {})
            for t in tests
        )
        assert has_relationship, (
            f"{model_name}.customer_id missing a relationships test with nested arguments"
        )


def test_schema_yml_documents_region_column_on_all_three_marts():
    """Plan B's row-level security depends on every mart carrying a plain
    `region` column — make sure schema.yml still documents it on all three,
    so its removal from a mart would be caught here (not just by the
    staging-layer denormalization tests)."""
    schema = yaml.safe_load((MARTS_DIR / "schema.yml").read_text())
    for model in schema["models"]:
        column_names = {c["name"] for c in model["columns"]}
        assert "region" in column_names, f"{model['name']} is missing a documented region column"


def test_schema_yml_customer_id_columns_are_not_null_and_unique_on_customers():
    schema = yaml.safe_load((MARTS_DIR / "schema.yml").read_text())
    models_by_name = {m["name"]: m for m in schema["models"]}
    columns = {c["name"]: c for c in models_by_name["customers"]["columns"]}
    tests = columns["customer_id"].get("tests", [])
    assert "not_null" in tests
    assert "unique" in tests


def test_dbt_project_yml_persists_docs_for_marts():
    config = yaml.safe_load((DBT_DIR / "dbt_project.yml").read_text())
    marts_config = config["models"]["governed_analyst_agent"]["marts"]
    assert marts_config["+persist_docs"] == {"relation": True, "columns": True}


def test_customers_pii_columns_have_policy_tags_config():
    # policy_tags must be a top-level column property (a plain list),
    # NOT nested under `config:`. Verified against the installed dbt-core
    # 1.12.5 / dbt-bigquery 1.12.1: dbt/parser/common.py's ParserRef._add
    # builds each column's ColumnConfig from only `column.config["meta"]`
    # and `column.config["tags"]` — any other key inside `config:` (e.g. a
    # nested `config.policy_tags`) is silently dropped and never reaches
    # the compiled manifest, so dbt-bigquery's adapter (impl.py
    # `_update_column_dict`, which reads `column_config.get("policy_tags")`
    # off the top-level column dict) would see an empty list and clear
    # any real policy tags on `dbt run`. Top-level `policy_tags:` instead
    # flows through ColumnInfo's `_extra` (AdditionalPropertiesMixin) and
    # is confirmed present in `dbt parse`'s compiled manifest.json with the
    # rendered var value. See task-2-report.md for the manifest evidence.
    schema = yaml.safe_load((MARTS_DIR / "schema.yml").read_text())
    models_by_name = {m["name"]: m for m in schema["models"]}
    columns = {c["name"]: c for c in models_by_name["customers"]["columns"]}
    for col_name in ["email", "phone", "full_name"]:
        names = columns[col_name].get("policy_tags", [])
        assert any("pii_high" in n for n in names), (
            f"customers.{col_name}.policy_tags should reference the pii_high var"
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
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_dbt_parse_without_vars_fails():
    """Pins the property that makes `make dbt-build` need --vars on every
    dbt subcommand, not just `dbt run`: schema.yml's {{ var('pii_high') }}
    is rendered during the full project parse that EVERY dbt command
    performs (including `dbt seed`), so a plain `dbt parse` with no --vars
    at all must fail. If this test ever starts passing, schema.yml no
    longer genuinely requires pii_high/pii_low vars, and the Makefile's
    dbt-build target (which passes --vars to seed/run/test) could safely
    be simplified — until then, don't "fix" it back to passing --vars to
    only one command.
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
            ["dbt", "parse", "--project-dir", str(DBT_DIR)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert result.returncode != 0, (
            "dbt parse succeeded with no --vars at all — schema.yml no longer "
            "requires pii_high/pii_low, which changes the reasoning behind "
            "the Makefile passing --vars to every dbt subcommand.\n"
            + result.stdout
            + result.stderr
        )


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
