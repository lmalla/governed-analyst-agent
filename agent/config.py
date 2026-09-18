"""Loads .env-sourced configuration and the persona registry.

An explicit dict (short persona name -> service account name prefix)
rather than a string transform (e.g. replacing underscores with hyphens) —
this avoids a subtle bug class where a future persona name doesn't map
cleanly, and makes every valid persona name grep-able in one place.
"""
import os

PERSONAS = {
    "analyst": "persona-analyst",
    "support_east": "persona-support-east",
    "governance": "persona-governance",
}


def get_project_id() -> str:
    return os.environ["GCP_PROJECT_ID"]


def get_dataset() -> str:
    return os.environ["BQ_DATASET"]


def resolve_persona_sa_email(persona: str) -> str:
    if persona not in PERSONAS:
        raise ValueError(f"Unknown persona: {persona!r}. Valid personas: {sorted(PERSONAS)}")
    sa_prefix = PERSONAS[persona]
    return f"{sa_prefix}@{get_project_id()}.iam.gserviceaccount.com"


def get_agent_model() -> str:
    return os.environ["AGENT_MODEL"]


def get_reviewer_model() -> str:
    return os.environ["REVIEWER_MODEL"]


def get_agent_max_turns() -> int:
    return int(os.environ.get("AGENT_MAX_TURNS", "8"))


def get_grader_model() -> str:
    return os.environ["GRADER_MODEL"]
