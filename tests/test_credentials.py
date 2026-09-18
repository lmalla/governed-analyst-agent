import pytest
from google.auth import impersonated_credentials
from google.auth.credentials import AnonymousCredentials

from agent import credentials as credentials_module
from agent.credentials import build_client, get_credentials


@pytest.fixture(autouse=True)
def clear_credentials_cache():
    credentials_module._credentials_cache.clear()
    yield
    credentials_module._credentials_cache.clear()


@pytest.fixture(autouse=True)
def mock_google_auth_default(monkeypatch):
    # AnonymousCredentials is a real, lightweight google-auth class meant
    # exactly for this kind of stand-in use — safer than a bare Mock, which
    # risks failing an isinstance/type check inside impersonated_credentials.
    monkeypatch.setattr(
        credentials_module,
        "google_auth_default",
        lambda: (AnonymousCredentials(), "fake-source-project"),
    )


@pytest.fixture(autouse=True)
def env_project(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project-123")


def test_get_credentials_returns_impersonated_credentials_instance():
    creds = get_credentials("analyst")
    assert isinstance(creds, impersonated_credentials.Credentials)


def test_get_credentials_raises_for_unknown_persona():
    with pytest.raises(ValueError, match="Unknown persona"):
        get_credentials("nonexistent")


def test_get_credentials_caches_per_persona():
    first = get_credentials("analyst")
    second = get_credentials("analyst")
    assert first is second


def test_get_credentials_different_personas_get_different_objects():
    analyst_creds = get_credentials("analyst")
    governance_creds = get_credentials("governance")
    assert analyst_creds is not governance_creds


def test_build_client_returns_bigquery_client_for_correct_project():
    client = build_client("analyst")
    assert client.project == "test-project-123"
