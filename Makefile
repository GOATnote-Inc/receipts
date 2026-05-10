.PHONY: venv test lint format clean help

help:
	@echo "Targets: venv test lint format clean"

venv:
	uv venv
	uv pip install -e ".[dev]"

test:
	uv run pytest -q; rc=$$?; [ $$rc -eq 0 ] || [ $$rc -eq 5 ] || exit $$rc

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

clean:
	rm -rf .pytest_cache .ruff_cache dist build *.egg-info .venv
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
