.PHONY: setup preflight lint data

setup:
	uv sync --group dev

preflight:
	bash scripts/00_preflight.sh

lint:
	uv run ruff check .
	shellcheck scripts/*.sh

data:
	uv run python data_gen/generate.py
