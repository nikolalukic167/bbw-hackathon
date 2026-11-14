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
# The API client is an extra: a fresh clone has it only after this sync.
if [ "$llm" = on ]; then uv sync --locked --extra llm; else uv sync --locked; fi

echo "== run 1: PLATO40_LLM=$llm"
PLATO40_LLM=$llm uv run plato40 run "$data" \
  --out decisions_holdout.jsonl --trace decisions_holdout.trace.jsonl --llm-cache "$cache"

echo "== run 2: PLATO40_LLM=off, from the cache"
PLATO40_LLM=off uv run plato40 run "$data" \
  --out "$scratch/decisions_holdout.jsonl" --trace "$scratch/trace.jsonl" --llm-cache "$cache"
a=$(sha256sum decisions_holdout.jsonl | cut -d" " -f1)
b=$(sha256sum "$scratch/decisions_holdout.jsonl" | cut -d" " -f1)
same=false; [ "$a" = "$b" ] && same=true
printf '{"decisions_sha256": "%s", "rerun_sha256": "%s", "identical": %s}\n' "$a" "$b" "$same" \
  > decisions_holdout.rerun.json
cmp decisions_holdout.jsonl "$scratch/decisions_holdout.jsonl"
echo "rerun byte-identical"

uv run plato40 validate decisions_holdout.jsonl "$data"
uv run plato40 ui decisions_holdout.jsonl decisions_holdout.trace.jsonl --out decisions_holdout.html \
  --data "$data" --llm-cache "$cache" --supervisor decisions_holdout.supervisor.jsonl \
  --rerun decisions_holdout.rerun.json --days 2026-01-26 2026-06-24 2026-11-28
sha256sum decisions_holdout.jsonl
