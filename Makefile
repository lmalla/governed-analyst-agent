.PHONY: setup preflight lint data dbt-build policy-tags smoke ask test

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
	test -f policy_tags.yml || { echo "dbt/policy_tags.yml missing — run 'make policy-tags' first" >&2; exit 1; }; \
	DBT_VARS="$$(cat policy_tags.yml)" && \
	dbt seed --vars "$$DBT_VARS" && \
	dbt run  --vars "$$DBT_VARS" && \
	dbt test --vars "$$DBT_VARS"

policy-tags:
	set -a && . ./.env && set +a && uv run python scripts/01_policy_tags.py

smoke:
	set -a && . ./.env && set +a && bash scripts/smoke_test.sh

ask:
	set -a && . ./.env && set +a && uv run python -m agent.cli $(ARGS)

test:
	uv run pytest -q
