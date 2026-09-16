.PHONY: setup preflight lint data dbt-build

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
	set -a && . ./.env && set +a && cd dbt && dbt seed && dbt run && dbt test
