import pytest

from agent.config import (
    PERSONAS,
    get_agent_max_turns,
    get_agent_model,
    get_dataset,
    get_project_id,
    get_reviewer_model,
    resolve_persona_sa_email,
)


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


def test_get_agent_model_reads_env(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    assert get_agent_model() == "claude-sonnet-5"


def test_get_agent_model_raises_when_unset(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    with pytest.raises(KeyError):
        get_agent_model()


def test_get_reviewer_model_reads_env(monkeypatch):
    monkeypatch.setenv("REVIEWER_MODEL", "claude-haiku-4-5")
    assert get_reviewer_model() == "claude-haiku-4-5"


def test_get_agent_max_turns_reads_env(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_TURNS", "12")
    assert get_agent_max_turns() == 12


def test_get_agent_max_turns_defaults_to_eight(monkeypatch):
    monkeypatch.delenv("AGENT_MAX_TURNS", raising=False)
    assert get_agent_max_turns() == 8
