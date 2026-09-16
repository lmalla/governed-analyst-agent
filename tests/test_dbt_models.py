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
