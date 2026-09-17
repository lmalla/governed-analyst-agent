"""Returns impersonated credentials for a persona, using Application
Default Credentials as the source. Cached per persona — repeated calls for
the same persona reuse the same Credentials object.
"""
from google.auth import default as google_auth_default
from google.auth import impersonated_credentials

from agent.config import resolve_persona_sa_email

# roles/bigquery.jobUser and roles/bigquery.dataViewer are the two roles
# every persona SA has (see BUILD_SPEC.md §5); this scope covers both.
BIGQUERY_SCOPES = ["https://www.googleapis.com/auth/bigquery"]

_credentials_cache: dict[str, impersonated_credentials.Credentials] = {}


def get_credentials(persona: str) -> impersonated_credentials.Credentials:
    if persona in _credentials_cache:
        return _credentials_cache[persona]

    sa_email = resolve_persona_sa_email(persona)  # raises ValueError for an unknown persona
    source_credentials, _ = google_auth_default()
    creds = impersonated_credentials.Credentials(
        source_credentials=source_credentials,
        target_principal=sa_email,
        target_scopes=BIGQUERY_SCOPES,
    )
    _credentials_cache[persona] = creds
    return creds
