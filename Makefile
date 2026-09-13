.PHONY: format lint type-check security-check test test-coverage check check-quick

format:
	uv run ruff format .

lint:
	uv run ruff check .

type-check:
	uv run mypy src scripts

security-check:
	uv run bandit -r src -ll

test:
	uv run pytest -v

test-coverage:
	uv run pytest --cov=src --cov-report=term-missing

check: lint type-check security-check test-coverage

check-quick: lint type-check
