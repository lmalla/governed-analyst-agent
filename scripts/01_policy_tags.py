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


def _check(resp: requests.Response) -> requests.Response:
    """Raise with Google's actual error.message from the response body,
    instead of a bare '403 Client Error: Forbidden for url: ...' — this
    script is never run by an agent to pre-diagnose issues, so the human
    needs the real reason (API not enabled, wrong location, missing
    permission, quota) in the first error, not a second round trip to
    reproduce it with -v.
    """
    if not resp.ok:
        raise RuntimeError(f"{resp.request.method} {resp.url} -> {resp.status_code}: {resp.text}")
    return resp


def _find_by_display_name(items: list, display_name: str):
    for item in items:
        if item.get("displayName") == display_name:
            return item
    return None


def ensure_taxonomy(token: str, parent: str) -> str:
    resp = _check(requests.get(f"{API}/{parent}/taxonomies", headers=_headers(token)))
    existing = _find_by_display_name(resp.json().get("taxonomies", []), "data_sensitivity")
    if existing:
        return existing["name"]
    resp = _check(
        requests.post(
            f"{API}/{parent}/taxonomies",
            headers=_headers(token),
            json={
                "displayName": "data_sensitivity",
                "activatedPolicyTypes": ["FINE_GRAINED_ACCESS_CONTROL"],
            },
        )
    )
    return resp.json()["name"]


def ensure_policy_tag(token: str, taxonomy_name: str, display_name: str) -> str:
    resp = _check(requests.get(f"{API}/{taxonomy_name}/policyTags", headers=_headers(token)))
    existing = _find_by_display_name(resp.json().get("policyTags", []), display_name)
    if existing:
        return existing["name"]
    resp = _check(
        requests.post(
            f"{API}/{taxonomy_name}/policyTags",
            headers=_headers(token),
            json={"displayName": display_name},
        )
    )
    return resp.json()["name"]


def grant_fine_grained_reader(token: str, policy_tag_name: str, member: str) -> None:
    resp = _check(requests.post(f"{API}/{policy_tag_name}:getIamPolicy", headers=_headers(token)))
    policy = resp.json()
    updated = merge_binding(policy, FINE_GRAINED_READER_ROLE, member)
    _check(
        requests.post(
            f"{API}/{policy_tag_name}:setIamPolicy",
            headers=_headers(token),
            json={"policy": updated},
        )
    )


def main() -> None:
    project_id = os.environ["GCP_PROJECT_ID"]
    # BigQuery tolerates uppercase location values (e.g. .env.example's
    # BQ_LOCATION=US), but Data Catalog resource names need the canonical
    # lowercase form, and the taxonomy must live in the same location as
    # the dataset it tags.
    location = os.environ["BQ_LOCATION"].lower()
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
