.PHONY: help sync lint format typecheck test audit check

DATA ?=
QUICKSTART ?=
AUDIT_OUT ?= reports/audit

help:
	@echo "make sync                 install the locked environment (uv)"
	@echo "make lint                 ruff lint and format check"
	@echo "make format               apply ruff formatting"
	@echo "make typecheck            mypy --strict on src/"
	@echo "make test [QUICKSTART=<dir>]  pytest; with QUICKSTART also the rerun check"
	@echo "make audit DATA=<path>    regenerate reports/audit from a data root"
	@echo "make check                lint + typecheck + test"

sync:
	uv sync --locked

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

format:
	uv run ruff format src tests

typecheck:
	uv run mypy

test:
	PLATO40_QUICKSTART="$(QUICKSTART)" uv run pytest

audit:
	@test -n "$(DATA)" || (echo "usage: make audit DATA=<data_root>" && exit 2)
	uv run plato40 audit "$(DATA)" --out "$(AUDIT_OUT)"

check: lint typecheck test
