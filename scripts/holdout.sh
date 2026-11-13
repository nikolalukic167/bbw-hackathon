#!/usr/bin/env bash
# Holdout run (docs/HOLDOUT.md): run once with the LLM on, rerun from the cache with it off,
# require byte-identical output, validate, and write the console page.
#   scripts/holdout.sh <holdout_data_dir>
set -euo pipefail

data="${1:?usage: scripts/holdout.sh <holdout_data_dir>}"
test -f "$data/building.json" || { echo "no building.json in $data" >&2; exit 2; }
cache=llm_cache_holdout
scratch=$(mktemp -d)

if [ -f .env ]; then set -a; . ./.env; set +a; fi
llm=off
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then llm=on; fi

echo "== run 1: PLATO40_LLM=$llm"
PLATO40_LLM=$llm uv run plato40 run "$data" \
  --out decisions_holdout.jsonl --trace decisions_holdout.trace.jsonl --llm-cache "$cache"

echo "== run 2: PLATO40_LLM=off, from the cache"
PLATO40_LLM=off uv run plato40 run "$data" \
  --out "$scratch/decisions_holdout.jsonl" --trace "$scratch/trace.jsonl" --llm-cache "$cache"
cmp decisions_holdout.jsonl "$scratch/decisions_holdout.jsonl"
echo "rerun byte-identical"

uv run plato40 validate decisions_holdout.jsonl "$data"
uv run plato40 ui decisions_holdout.jsonl decisions_holdout.trace.jsonl --out decisions_holdout.html
sha256sum decisions_holdout.jsonl
