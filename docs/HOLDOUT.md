# Holdout runbook

The holdout archive is released Sat 14 Nov ~11:00 on Discord, and the code freeze is at 14:00.
The pipeline runs unchanged.

1. Check the archive against the published checksum:
   ```bash
   cd data && sha256sum -c SHA256SUMS.txt --ignore-missing && cd ..
   ```
2. Extract it. 7z asks for the password, and the headers are encrypted, so the folder name
   is only known afterwards:
   ```bash
   7z x data/PLATO40_v2_holdout_encrypted.7z -odata/holdout
   find data/holdout -name building.json      # its folder is <holdout_dir>
   ```
3. Run:
   ```bash
   scripts/holdout.sh <holdout_dir>
   ```
   The script:
   - runs once with `PLATO40_LLM=on` when `.env` holds a key, writing the cache to
     `llm_cache_holdout/`;
   - reruns with the LLM off from that cache, records both hashes in
     `decisions_holdout.rerun.json`, and stops unless the output is byte-identical;
   - validates against the contract and policy;
   - writes `decisions_holdout.html`, the console and demo page (`--data` gives it the KPIs,
     the year strip and the day replay; the trust tile reads `rerun.json`).
4. Commit `decisions_holdout.jsonl`, `decisions_holdout.trace.jsonl`,
   `decisions_holdout.supervisor.jsonl`, `decisions_holdout.rerun.json`,
   `decisions_holdout.html` and `llm_cache_holdout/`. Never commit `data/`.

Rehearsed on quickstart as the pretend holdout, from a fresh worktree with the committed code:

| Step | Result |
|---|---|
| Sync | `uv sync --locked --extra llm` by the script itself (the API client is an extra) |
| LLM run | 263 decisions, 0 rejected proposals, 40 supervisor reviews |
| Rerun from the cache | byte-identical, recorded in `decisions_holdout.rerun.json` |
| Validation | 0 violations |
| Page | `decisions_holdout.html` with Year, Day, Episode and Trail |
| Script wall time | 135 s |

A dev run takes 37 s with the LLM off; the full dev and quickstart regeneration with the LLM on
(`scripts/regenerate.sh`) took 20 min. Two live runs differ slightly (message wording, and the
supervisor added 2 decisions in one run and 3 in another): determinism holds for a run and its
cache, which is what the holdout procedure records.

## If something fails

| Failure | Fallback |
|---|---|
| API down or no key | Runs with templates (`guest messages: {'template': n}`); the output is still valid. |
| A lane raises | `uv run plato40 run <holdout_dir> --lanes s03 ...`, which drops the lanes from the list. `--lanes` alone runs the planner only. Justify the change under "Changes after pre-holdout" in the README. |
| The arbiter or a follow-up raises | `--no-follow-ups`, then `--lanes` alone. Justify it in the README as above. |
