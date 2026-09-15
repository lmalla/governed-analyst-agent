.PHONY: setup preflight lint

setup:
	uv sync --group dev

preflight:
	bash scripts/00_preflight.sh

lint:
	uv run ruff check .
	shellcheck scripts/*.sh
