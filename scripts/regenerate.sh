#!/usr/bin/env bash
# Regenerate every committed output from the dev and quickstart datasets:
# decisions.jsonl with its trace, LLM cache, supervisor log and console; insights.md;
# the quickstart demo under examples/quickstart; the golden bus examples under examples/bus.
# Each LLM run is followed by a rerun from the cache that must be byte-identical.
#   scripts/regenerate.sh <dev_dir> <quickstart_dir>
set -euo pipefail

dev="${1:?usage: scripts/regenerate.sh <dev_dir> <quickstart_dir>}"
qs="${2:?usage: scripts/regenerate.sh <dev_dir> <quickstart_dir>}"
scratch=$(mktemp -d)
if [ -f .env ]; then set -a; . ./.env; set +a; fi
llm=off
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then llm=on; fi

run_twice() {  # <data> <out> <cache> <trace>
  echo "== $2: PLATO40_LLM=$llm"
  PLATO40_LLM=$llm uv run plato40 run "$1" --out "$2" --trace "$4" --llm-cache "$3"
  echo "== $2: rerun from the cache"
  PLATO40_LLM=off uv run plato40 run "$1" --out "$scratch/rerun.jsonl" --llm-cache "$3" > /dev/null
  a=$(sha256sum "$2" | cut -d" " -f1)
  b=$(sha256sum "$scratch/rerun.jsonl" | cut -d" " -f1)
  same=false; [ "$a" = "$b" ] && same=true
  printf '{"decisions_sha256": "%s", "rerun_sha256": "%s", "identical": %s}\n' "$a" "$b" "$same" \
    > "${2%.jsonl}.rerun.json"
  cmp "$2" "$scratch/rerun.jsonl"
  echo "rerun byte-identical"
  uv run plato40 validate "$2" "$1"
}

run_twice "$dev" decisions.jsonl llm_cache decisions.trace.jsonl
uv run plato40 ui decisions.jsonl decisions.trace.jsonl --out decisions.html \
  --data "$dev" --llm-cache llm_cache --supervisor decisions.supervisor.jsonl \
  --rerun decisions.rerun.json --days 2026-01-16 2026-05-09 2026-07-20
uv run python scripts/insights.py "$dev" --out insights.md --llm-cache llm_cache \
  --supervisor decisions.supervisor.jsonl

mkdir -p examples/quickstart
run_twice "$qs" examples/quickstart/decisions.jsonl examples/quickstart/llm_cache \
  examples/quickstart/decisions.trace.jsonl
uv run plato40 ui examples/quickstart/decisions.jsonl examples/quickstart/decisions.trace.jsonl \
  --out examples/quickstart/console.html --data "$qs" --llm-cache examples/quickstart/llm_cache \
  --supervisor examples/quickstart/decisions.supervisor.jsonl \
  --rerun examples/quickstart/decisions.rerun.json --days 2026-01-12 2026-01-15 2026-01-24
uv run python scripts/export_examples.py "$qs" examples/bus

sha256sum decisions.jsonl examples/quickstart/decisions.jsonl
