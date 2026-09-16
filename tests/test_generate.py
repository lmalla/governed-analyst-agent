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
