.PHONY: install test lint typecheck check run

install:
	cd server && pip install -e ".[dev]"

test:
	cd server && pytest

lint:
	cd server && ruff check . && ruff format --check .

typecheck:
	cd server && mypy src

check: lint typecheck test

run:
	cd server && uvicorn app.main:app --reload --port 8000
