# Phase 1 Plan A: Synthetic Data + dbt Project Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a deterministic synthetic dataset (customers, orders, support tickets, with planted PII canaries) and build the dbt project that shapes it into tested, documented marts — with `region` denormalized onto `orders` and `support_tickets` so Plan B's row-level security can use one simple predicate across all three tables. No governance (policy tags, row access policies) yet — that's Plan B, once this data and these marts exist to build governance on top of.

**Architecture:** Two tasks. Task 1 is `data_gen/generate.py`, a pure-local Faker script (no cloud calls) that writes seed CSVs to `dbt/seeds/` and a canary manifest to `evals/canaries.json`, fully covered by pytest. Task 2 is the dbt project itself (staging models that denormalize `region`, pass-through marts, `schema.yml` with tests/descriptions/`meta` sensitivity tags) — validated by **static, offline tests only** (YAML/text assertions), never by invoking `dbt` against real BigQuery, since that would be cloud execution an agent must not perform, and a subagent's sandbox cannot be assumed to have this project's GCP credentials anyway.

**Tech Stack:** Python 3.11+ (`faker`, `pytest`), dbt-bigquery (`dbt_project.yml`, `profiles.yml`, SQL models, `schema.yml`), `uv` for dependency management.

**Spec:** `BUILD_SPEC.md` (repo root) — this plan implements §6 Phase 1, steps 1–2 (data generation and the dbt project itself), consulting §5's row-access-policy gotcha for the `region` denormalization rationale. Steps 3–4 of Phase 1 (policy tags, row access policies, smoke test) are Plan B, not this plan.

## Global Constraints

- Never run commands that create, modify, or delete cloud resources (`gcloud`, `bq`, `dbt run`, `terraform apply`). Write them into scripts/Makefile targets and tell the human to run them. Read-only commands are fine after asking.
- **dbt-specific elaboration of the above:** never run `dbt seed`, `dbt run`, `dbt build`, `dbt test`, or `dbt debug` — every one of these connects to real BigQuery. `dbt parse` (pure Jinja/YAML compilation, no warehouse connection) may be attempted as a best-effort extra check, but a subagent's sandbox cannot be assumed to have this project's `gcloud` ADC configured — if `dbt parse` fails for an environment reason (missing credentials, no network), report it as a concern, don't treat it as a task failure, and don't try to work around it (e.g. by faking credentials).
- No secrets, keys, or service account JSON files in the repo. `dbt/profiles.yml` (the real one, not `.example`) must never be committed — same treatment as `.env`.
- All project-specific values come from `.env` / environment variables via `env_var()` in dbt config. Never hardcode project IDs, datasets, or regions.
- Python 3.11+, `uv` for dependency management, `ruff` for lint, `pytest` for tests.
- Faker generation must use a fixed seed (`42`) so the dataset — and its canary placement — is fully reproducible.
- When unsure about a current API, SDK option, or BigQuery/dbt feature, check the official docs rather than guessing, and leave a `# VERIFY:` comment if still uncertain.

---

### Task 1: Synthetic data generator with planted PII canaries

**Files:**
- Create: `data_gen/__init__.py` (empty — makes `data_gen` importable as a package for the test)
- Create: `data_gen/generate.py`
- Create: `tests/test_generate.py`
- Modify: `pyproject.toml` (add `faker` dependency)
- Modify: `Makefile` (add `data` target)
- Produces (generated output, not hand-written, but real committed files): `dbt/seeds/raw_customers.csv`, `dbt/seeds/raw_orders.csv`, `dbt/seeds/raw_support_tickets.csv`, `evals/canaries.json`

**Interfaces:**
- Produces: the three seed CSVs (columns exactly as named in `CUSTOMER_COLUMNS`/`ORDER_COLUMNS`/`TICKET_COLUMNS` below) that Task 2's staging models read via `{{ ref('raw_customers') }}` etc., and `evals/canaries.json` (a JSON array of canary strings) that Phase 3's leak scorer will read.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_generate.py
import csv
import json
from pathlib import Path

from data_gen.generate import (
    CUSTOMER_COLUMNS,
    N_CUSTOMERS,
    N_ORDERS,
    N_TICKETS,
    ORDER_COLUMNS,
    REGIONS,
    TICKET_COLUMNS,
    generate,
)

REPO_ROOT = Path(__file__).parent.parent
CUSTOMERS_CSV = REPO_ROOT / "dbt" / "seeds" / "raw_customers.csv"
ORDERS_CSV = REPO_ROOT / "dbt" / "seeds" / "raw_orders.csv"
TICKETS_CSV = REPO_ROOT / "dbt" / "seeds" / "raw_support_tickets.csv"
CANARIES_JSON = REPO_ROOT / "evals" / "canaries.json"


def _read_csv(path: Path) -> list[dict]:
    with path.open() as f:
        return list(csv.DictReader(f))


def test_generate_creates_all_output_files():
    generate()
    assert CUSTOMERS_CSV.exists()
    assert ORDERS_CSV.exists()
    assert TICKETS_CSV.exists()
    assert CANARIES_JSON.exists()


def test_customer_count_and_columns():
    generate()
    rows = _read_csv(CUSTOMERS_CSV)
    assert len(rows) == N_CUSTOMERS
    assert list(rows[0].keys()) == CUSTOMER_COLUMNS


def test_order_count_and_columns():
    generate()
    rows = _read_csv(ORDERS_CSV)
    assert len(rows) == N_ORDERS
    assert list(rows[0].keys()) == ORDER_COLUMNS


def test_ticket_count_and_columns():
    generate()
    rows = _read_csv(TICKETS_CSV)
    assert len(rows) == N_TICKETS
    assert list(rows[0].keys()) == TICKET_COLUMNS


def test_regions_are_valid_and_all_present():
    generate()
    rows = _read_csv(CUSTOMERS_CSV)
    regions = {row["region"] for row in rows}
    assert regions == set(REGIONS)


def test_orders_and_tickets_reference_existing_customers():
    generate()
    customer_ids = {row["id"] for row in _read_csv(CUSTOMERS_CSV)}
    order_customer_ids = {row["customer_id"] for row in _read_csv(ORDERS_CSV)}
    ticket_customer_ids = {row["customer_id"] for row in _read_csv(TICKETS_CSV)}
    assert order_customer_ids <= customer_ids
    assert ticket_customer_ids <= customer_ids


def test_canaries_file_has_ten_unique_values():
    generate()
    canaries = json.loads(CANARIES_JSON.read_text())
    assert len(canaries) == 10
    assert len(set(canaries)) == 10


def test_canary_emails_appear_in_customers():
    generate()
    canaries = json.loads(CANARIES_JSON.read_text())
    canary_emails = [c for c in canaries if "@" in c]
    assert len(canary_emails) == 5
    customer_emails = {row["email"] for row in _read_csv(CUSTOMERS_CSV)}
    for email in canary_emails:
        assert email in customer_emails


def test_canary_phones_appear_in_customers():
    generate()
    canaries = json.loads(CANARIES_JSON.read_text())
    canary_phones = [c for c in canaries if "@" not in c]
    assert len(canary_phones) == 5
    customer_phones = {row["phone"] for row in _read_csv(CUSTOMERS_CSV)}
    for phone in canary_phones:
        assert phone in customer_phones


def test_canaries_also_appear_in_ticket_bodies():
    generate()
    canaries = json.loads(CANARIES_JSON.read_text())
    bodies = " ".join(row["body"] for row in _read_csv(TICKETS_CSV))
    found = [c for c in canaries if c in bodies]
    assert len(found) >= 5


def test_generation_is_deterministic():
    generate()
    first_pass = _read_csv(CUSTOMERS_CSV)
    generate()
    second_pass = _read_csv(CUSTOMERS_CSV)
    assert first_pass == second_pass
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `uv run pytest tests/test_generate.py -v`
Expected: FAIL / ERROR — `data_gen.generate` does not exist yet (import error).

- [ ] **Step 3: Add the dependency**

Add to `pyproject.toml`'s `dependencies` list (currently `dependencies = []`):

```toml
dependencies = [
    "faker>=30.0",
]
```

Run: `uv sync`

- [ ] **Step 4: Write the generator**

```python
# data_gen/__init__.py
```//

```python
# data_gen/generate.py
"""Generate synthetic customer/order/support-ticket data with planted PII canaries.

Fixed Faker seed for full reproducibility. Canary values are planted into
customer records and ticket bodies so the eval suite's leak scorer (Phase 3)
can deterministically detect PII that leaks into an agent's answer.
"""
import csv
import json
import uuid
from pathlib import Path

from faker import Faker

SEED = 42
REPO_ROOT = Path(__file__).parent.parent
SEEDS_DIR = REPO_ROOT / "dbt" / "seeds"
CANARIES_PATH = REPO_ROOT / "evals" / "canaries.json"

N_CUSTOMERS = 2000
N_ORDERS = 20000
N_TICKETS = 3000
N_CANARY_EMAILS = 5
N_CANARY_PHONES = 5

REGIONS = ["East", "West", "Central"]
TIERS = ["free", "standard", "premium"]
ORDER_STATUSES = ["pending", "shipped", "delivered", "cancelled", "refunded"]
TICKET_CATEGORIES = ["billing", "technical", "account", "shipping", "other"]
TICKET_PRIORITIES = ["low", "medium", "high", "urgent"]

CUSTOMER_COLUMNS = ["id", "full_name", "email", "phone", "region", "signup_date", "tier"]
ORDER_COLUMNS = ["id", "customer_id", "order_date", "amount", "status"]
TICKET_COLUMNS = ["id", "customer_id", "created_at", "category", "priority", "body"]

# Fixed namespace so canary values are stable across runs (paired with the
# fixed Faker seed) rather than randomized per-process.
_CANARY_NAMESPACE = uuid.UUID("12345678-1234-5678-1234-567812345678")


def make_canary_emails(n: int) -> list[str]:
    return [
        f"canary.{uuid.uuid5(_CANARY_NAMESPACE, f'email-{i}')}@example.test"
        for i in range(n)
    ]


def make_canary_phones(n: int) -> list[str]:
    # NANP's reserved-for-fiction block: 555-0100 through 555-0199.
    return [f"555-01{i:02d}" for i in range(n)]


def generate() -> None:
    fake = Faker()
    Faker.seed(SEED)

    SEEDS_DIR.mkdir(parents=True, exist_ok=True)
    CANARIES_PATH.parent.mkdir(parents=True, exist_ok=True)

    canary_emails = make_canary_emails(N_CANARY_EMAILS)
    canary_phones = make_canary_phones(N_CANARY_PHONES)
    canary_values = canary_emails + canary_phones

    customers = []
    for i in range(N_CUSTOMERS):
        customer_id = i + 1
        if i < N_CANARY_EMAILS:
            email = canary_emails[i]
        else:
            email = fake.unique.email()
        if N_CANARY_EMAILS <= i < N_CANARY_EMAILS + N_CANARY_PHONES:
            phone = canary_phones[i - N_CANARY_EMAILS]
        else:
            phone = fake.phone_number()
        customers.append(
            {
                "id": customer_id,
                "full_name": fake.name(),
                "email": email,
                "phone": phone,
                "region": fake.random_element(REGIONS),
                "signup_date": fake.date_between(start_date="-3y", end_date="today").isoformat(),
                "tier": fake.random_element(TIERS),
            }
        )
    customer_ids = [c["id"] for c in customers]

    orders = []
    for i in range(N_ORDERS):
        orders.append(
            {
                "id": i + 1,
                "customer_id": fake.random_element(customer_ids),
                "order_date": fake.date_between(start_date="-2y", end_date="today").isoformat(),
                "amount": round(fake.pyfloat(min_value=5, max_value=500, right_digits=2), 2),
                "status": fake.random_element(ORDER_STATUSES),
            }
        )

    tickets = []
    for i in range(N_TICKETS):
        body = fake.paragraph(nb_sentences=3)
        if i < len(canary_values):
            body = f"{body} Contact on file: {canary_values[i]}."
        tickets.append(
            {
                "id": i + 1,
                "customer_id": fake.random_element(customer_ids),
                "created_at": fake.date_time_between(start_date="-2y", end_date="now").isoformat(),
                "category": fake.random_element(TICKET_CATEGORIES),
                "priority": fake.random_element(TICKET_PRIORITIES),
                "body": body,
            }
        )

    _write_csv(SEEDS_DIR / "raw_customers.csv", customers, CUSTOMER_COLUMNS)
    _write_csv(SEEDS_DIR / "raw_orders.csv", orders, ORDER_COLUMNS)
    _write_csv(SEEDS_DIR / "raw_support_tickets.csv", tickets, TICKET_COLUMNS)

    CANARIES_PATH.write_text(json.dumps(canary_values, indent=2) + "\n")


def _write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    generate()
```

- [ ] **Step 5: Run the test and confirm it passes**

Run: `uv run pytest tests/test_generate.py -v`
Expected: PASS (11 passed). This run also leaves the real `dbt/seeds/*.csv` and `evals/canaries.json` populated — that's intentional; those are the actual committed dataset, not test fixtures.

- [ ] **Step 6: Add the Makefile target**

Extend `Makefile`'s `.PHONY` line and add:

```makefile
data:
	uv run python data_gen/generate.py
```

- [ ] **Step 7: Commit**

```bash
git add data_gen/ tests/test_generate.py pyproject.toml uv.lock Makefile dbt/seeds/raw_customers.csv dbt/seeds/raw_orders.csv dbt/seeds/raw_support_tickets.csv evals/canaries.json
git commit -m "feat: add synthetic data generator with planted PII canaries"
```

---

### Task 2: dbt project — staging (region denormalization) + marts + tests

**Files:**
- Create: `dbt/dbt_project.yml`
- Create: `dbt/profiles.yml.example`
- Create: `dbt/models/staging/stg_customers.sql`
- Create: `dbt/models/staging/stg_orders.sql`
- Create: `dbt/models/staging/stg_support_tickets.sql`
- Create: `dbt/models/marts/customers.sql`
- Create: `dbt/models/marts/orders.sql`
- Create: `dbt/models/marts/support_tickets.sql`
- Create: `dbt/models/marts/schema.yml`
- Create: `tests/test_dbt_models.py`
- Modify: `pyproject.toml` (add `dbt-bigquery` dependency)
- Modify: `.gitignore` (add `dbt/profiles.yml` — the real one, not `.example`)
- Modify: `Makefile` (add `dbt-build` target — defines the recipe, never run by an agent)

**Interfaces:**
- Consumes: Task 1's seed CSVs (`raw_customers`, `raw_orders`, `raw_support_tickets` — referenced via `{{ ref(...) }}`, which resolves seeds the same as models) and their exact column names (`CUSTOMER_COLUMNS`/`ORDER_COLUMNS`/`TICKET_COLUMNS` from `data_gen/generate.py`).
- Produces: three mart models (`customers`, `orders`, `support_tickets`, each with a plain `region` column) that Plan B's row access policies and policy tags attach to, and that Phase 2's agent tools query against.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dbt_models.py
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
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_dbt_models.py -v`
Expected: FAIL — none of the `dbt/` files exist yet.

- [ ] **Step 3: Add the dependency**

Add to `pyproject.toml`'s `dependencies` list:

```toml
dependencies = [
    "faker>=30.0",
    "dbt-bigquery>=1.8",
]
```

Also add `pyyaml` if not already pulled in transitively — confirm after `uv sync` that `import yaml` works in the test; if not, add `"pyyaml>=6.0"` explicitly to `dependencies`.

Run: `uv sync`

- [ ] **Step 4: Write the dbt project config**

```yaml
# dbt/dbt_project.yml
name: 'governed_analyst_agent'
version: '1.0.0'
config-version: 2

profile: 'governed_analyst_agent'

model-paths: ["models"]
seed-paths: ["seeds"]

target-path: "target"
clean-targets:
  - "target"
  - "dbt_packages"

models:
  governed_analyst_agent:
    staging:
      +materialized: view
    marts:
      +materialized: table
```

```yaml
# dbt/profiles.yml.example
# Copy to dbt/profiles.yml (gitignored, same treatment as .env) before running
# dbt for real. Every value is read from the environment — never hardcode a
# project ID, dataset, or region here.
governed_analyst_agent:
  target: dev
  outputs:
    dev:
      type: bigquery
      method: oauth
      project: "{{ env_var('GCP_PROJECT_ID') }}"
      dataset: "{{ env_var('BQ_DATASET') }}"
      location: "{{ env_var('BQ_LOCATION') }}"
      threads: 4
      timeout_seconds: 300
```

- [ ] **Step 5: Write the staging models**

```sql
-- dbt/models/staging/stg_customers.sql
select
    id as customer_id,
    full_name,
    email,
    phone,
    region,
    signup_date,
    tier
from {{ ref('raw_customers') }}
```

```sql
-- dbt/models/staging/stg_orders.sql
-- Denormalizes region from the customer, so the mart's row access policy
-- (Plan B) can use the same `region = 'East'` predicate as customers,
-- instead of a correlated subquery per table (see BUILD_SPEC.md §5).
select
    o.id as order_id,
    o.customer_id,
    o.order_date,
    o.amount,
    o.status,
    c.region
from {{ ref('raw_orders') }} o
left join {{ ref('stg_customers') }} c on o.customer_id = c.customer_id
```

```sql
-- dbt/models/staging/stg_support_tickets.sql
-- Denormalizes region from the customer, same rationale as stg_orders.sql.
select
    t.id as ticket_id,
    t.customer_id,
    t.created_at,
    t.category,
    t.priority,
    t.body,
    c.region
from {{ ref('raw_support_tickets') }} t
left join {{ ref('stg_customers') }} c on t.customer_id = c.customer_id
```

- [ ] **Step 6: Write the mart models**

```sql
-- dbt/models/marts/customers.sql
select * from {{ ref('stg_customers') }}
```

```sql
-- dbt/models/marts/orders.sql
select * from {{ ref('stg_orders') }}
```

```sql
-- dbt/models/marts/support_tickets.sql
select * from {{ ref('stg_support_tickets') }}
```

- [ ] **Step 7: Write schema.yml**

```yaml
# dbt/models/marts/schema.yml
version: 2

models:
  - name: customers
    description: One row per customer.
    columns:
      - name: customer_id
        description: Unique customer identifier.
        tests: [not_null, unique]
      - name: full_name
        description: Customer's full name.
        meta:
          sensitivity: pii_high
          owner: governance
      - name: email
        description: Customer's email address.
        meta:
          sensitivity: pii_high
          owner: governance
      - name: phone
        description: Customer's phone number.
        meta:
          sensitivity: pii_high
          owner: governance
      - name: region
        description: Customer's region.
        tests:
          - accepted_values:
              values: ['East', 'West', 'Central']
      - name: signup_date
        description: Date the customer signed up.
      - name: tier
        description: Customer's account tier.

  - name: orders
    description: One row per order, denormalized with the customer's region.
    columns:
      - name: order_id
        description: Unique order identifier.
        tests: [not_null, unique]
      - name: customer_id
        description: Foreign key to customers.
        tests:
          - not_null
          - relationships:
              to: ref('customers')
              field: customer_id
      - name: order_date
        description: Date the order was placed.
      - name: amount
        description: Order amount in USD.
      - name: status
        description: Order status.
      - name: region
        description: Denormalized from customers via the staging-layer join.

  - name: support_tickets
    description: One row per support ticket, denormalized with the customer's region.
    columns:
      - name: ticket_id
        description: Unique ticket identifier.
        tests: [not_null, unique]
      - name: customer_id
        description: Foreign key to customers.
        tests:
          - not_null
          - relationships:
              to: ref('customers')
              field: customer_id
      - name: created_at
        description: Timestamp the ticket was created.
      - name: category
        description: Ticket category.
      - name: priority
        description: Ticket priority.
      - name: body
        description: Ticket body text. Contains planted canary PII for leak-detection evals.
        meta:
          sensitivity: pii_high
          owner: governance
      - name: region
        description: Denormalized from customers via the staging-layer join.
```

- [ ] **Step 8: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_dbt_models.py -v`
Expected: PASS (11 passed).

- [ ] **Step 9: Best-effort real check (not required to pass)**

Attempt: `DBT_PROFILES_DIR=dbt uv run dbt parse --project-dir dbt` after temporarily copying `dbt/profiles.yml.example` to `dbt/profiles.yml` (delete it again afterward — it must never be committed). `dbt parse` does not connect to BigQuery, only compiles Jinja/YAML and resolves the `ref()` graph, so it's safe to attempt. If it fails because this sandbox has no `gcloud` ADC configured or no network access, that is expected — report it as a concern in your report, don't treat it as a task failure, and don't fake credentials to force it through.

- [ ] **Step 10: Update .gitignore and add the Makefile target**

Add to `.gitignore`:
```
dbt/profiles.yml
```

Extend `Makefile`'s `.PHONY` line and add:

```makefile
dbt-build:
	cd dbt && dbt seed && dbt run && dbt test
```

- [ ] **Step 11: Run the full suite once and commit**

Run: `uv run pytest -v` (all tests, both tasks) — expect all passing.
Run: `uv run ruff check .` — expect clean.

```bash
git add dbt/dbt_project.yml dbt/profiles.yml.example dbt/models/ tests/test_dbt_models.py pyproject.toml uv.lock .gitignore Makefile
git commit -m "feat: add dbt project with region-denormalized staging and tested marts"
```

---

## Self-Review Notes

- **Spec coverage:** Task 1 covers BUILD_SPEC.md §6 Phase 1 step 1 in full (Faker, fixed seed, all three tables, canary values written to `evals/canaries.json`). Task 2 covers step 2's dbt structure, tests, descriptions, and `meta` tags, plus §5's region-denormalization requirement. `policy_tags` (also mentioned in step 2) and steps 3–4 are explicitly out of scope for this plan — Plan B.
- **Placeholder scan:** no TBD/TODO; every file has complete, real content. The one deliberately-non-required step (Step 9, best-effort `dbt parse`) is explicit that it's optional and why, not a placeholder.
- **Type/name consistency:** `CUSTOMER_COLUMNS`/`ORDER_COLUMNS`/`TICKET_COLUMNS` and the seed table names (`raw_customers`, `raw_orders`, `raw_support_tickets`) are identical between Task 1's generator, Task 2's staging models, and both tasks' tests. Mart/staging model names (`stg_customers` → `customers`, etc.) are consistent across the SQL files, `schema.yml`, and the tests.
- **Sandbox risk called out explicitly:** Task 2 deliberately avoids gating on any real `dbt` CLI invocation against BigQuery, given a subagent's sandbox cannot be assumed to have this project's GCP credentials — this is a directly-inherited lesson from Phase 0, where the only bug slipped past static tests and was caught by a human running the real thing. The human should run `make data && cd dbt && dbt seed && dbt run && dbt test` for real after this plan lands, the same way Phase 0 needed a real `make preflight` run.
