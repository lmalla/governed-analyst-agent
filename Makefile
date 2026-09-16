.PHONY: setup preflight lint data dbt-build policy-tags smoke

setup:
	uv sync --group dev

preflight:
	bash scripts/00_preflight.sh

lint:
	uv run ruff check .
	shellcheck scripts/*.sh

data:
	uv run python data_gen/generate.py

dbt-build:
	set -a && . ./.env && set +a && cd dbt && \
	dbt seed && \
	dbt run --vars "$$(cat policy_tags.yml)" && \
	dbt test

policy-tags:
	set -a && . ./.env && set +a && uv run python scripts/01_policy_tags.py

smoke:
	set -a && . ./.env && set +a && bash scripts/smoke_test.sh
