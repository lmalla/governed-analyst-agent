import pytest

from agent.config import PERSONAS, get_dataset, get_project_id, resolve_persona_sa_email


def test_personas_registry_has_exactly_the_three_known_personas():
    assert PERSONAS == {
        "analyst": "persona-analyst",
        "support_east": "persona-support-east",
        "governance": "persona-governance",
    }


def test_resolve_persona_sa_email_for_analyst(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project-123")
    assert resolve_persona_sa_email("analyst") == "persona-analyst@test-project-123.iam.gserviceaccount.com"


def test_resolve_persona_sa_email_for_support_east(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project-123")
    assert (
        resolve_persona_sa_email("support_east")
        == "persona-support-east@test-project-123.iam.gserviceaccount.com"
    )


def test_resolve_persona_sa_email_for_governance(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project-123")
    assert resolve_persona_sa_email("governance") == "persona-governance@test-project-123.iam.gserviceaccount.com"


def test_resolve_persona_sa_email_raises_for_unknown_persona(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project-123")
    with pytest.raises(ValueError, match="Unknown persona"):
        resolve_persona_sa_email("nonexistent")


def test_get_project_id_reads_env_var(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "my-project")
    assert get_project_id() == "my-project"


def test_get_dataset_reads_env_var(monkeypatch):
    monkeypatch.setenv("BQ_DATASET", "my_dataset")
    assert get_dataset() == "my_dataset"
