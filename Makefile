.PHONY: help sync lint format typecheck run validate ui audit check

DATA ?=
AUDIT_OUT ?= reports/audit
OUT ?= decisions.jsonl

help:
	@echo "make sync                 install the locked environment (uv)"
	@echo "make lint                 ruff lint and format check"
	@echo "make format               apply ruff formatting"
	@echo "make typecheck            mypy --strict on src/"
	@echo "make run DATA=<dir> [OUT=decisions.jsonl]  write decisions for one dataset"
	@echo "make validate DATA=<dir> [OUT=...]  check decisions against contract and policy"
	@echo "make ui [OUT=...]         write the operations console next to OUT (after make run)"
	@echo "make audit DATA=<path>    regenerate reports/audit from a data root"
	@echo "make check                lint + typecheck"

sync:
	uv sync --locked

lint:
	uv run ruff check src
	uv run ruff format --check src

format:
	uv run ruff format src

typecheck:
	uv run mypy

run:
	@test -n "$(DATA)" || (echo "usage: make run DATA=<data_dir>" && exit 2)
	uv run plato40 run "$(DATA)" --out "$(OUT)" --trace "$(basename $(OUT)).trace.jsonl"

validate:
	@test -n "$(DATA)" || (echo "usage: make validate DATA=<data_dir>" && exit 2)
	uv run plato40 validate "$(OUT)" "$(DATA)"

ui:
	uv run plato40 ui "$(OUT)" "$(basename $(OUT)).trace.jsonl" --out "$(basename $(OUT)).html"

audit:
	@test -n "$(DATA)" || (echo "usage: make audit DATA=<data_root>" && exit 2)
	uv run plato40 audit "$(DATA)" --out "$(AUDIT_OUT)"

check: lint typecheck
