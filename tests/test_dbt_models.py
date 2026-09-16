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


def test_stg_orders_denormalizes_region_via_customer_join():
    text = (STAGING_DIR / "stg_orders.sql").read_text()
    assert "ref('raw_orders')" in text
    assert "ref('stg_customers')" in text
    assert "region" in text
    for col in ORDER_COLUMNS:
        assert col in text, f"stg_orders.sql missing reference to raw column {col}"


def test_stg_support_tickets_denormalizes_region_via_customer_join():
    text = (STAGING_DIR / "stg_support_tickets.sql").read_text()
    assert "ref('raw_support_tickets')" in text
    assert "ref('stg_customers')" in text
    assert "region" in text
    for col in TICKET_COLUMNS:
        assert col in text, f"stg_support_tickets.sql missing reference to raw column {col}"


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
    schema = yaml.safe_load((MARTS_DIR / "schema.yml").read_text())
    models_by_name = {m["name"]: m for m in schema["models"]}
    expected_pii = {
        "customers": {"full_name", "email", "phone"},
        "support_tickets": {"body"},
    }
    for model_name, pii_cols in expected_pii.items():
        columns = {c["name"]: c for c in models_by_name[model_name]["columns"]}
        for col_name in pii_cols:
            meta = columns[col_name].get("meta", {})
            assert meta.get("sensitivity") == "pii_high", (
                f"{model_name}.{col_name} should be tagged meta.sensitivity: pii_high"
            )


def test_schema_yml_has_relationships_tests_to_customers():
    schema = yaml.safe_load((MARTS_DIR / "schema.yml").read_text())
    models_by_name = {m["name"]: m for m in schema["models"]}
    for model_name in ["orders", "support_tickets"]:
        columns = {c["name"]: c for c in models_by_name[model_name]["columns"]}
        tests = columns["customer_id"].get("tests", [])
        has_relationship = any(isinstance(t, dict) and "relationships" in t for t in tests)
        assert has_relationship, f"{model_name}.customer_id missing a relationships test"


def test_schema_yml_customer_id_columns_are_not_null_and_unique_on_customers():
    schema = yaml.safe_load((MARTS_DIR / "schema.yml").read_text())
    models_by_name = {m["name"]: m for m in schema["models"]}
    columns = {c["name"]: c for c in models_by_name["customers"]["columns"]}
    tests = columns["customer_id"].get("tests", [])
    assert "not_null" in tests
    assert "unique" in tests
