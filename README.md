# Steward for PLATO-40

Steward is a deterministic operations pipeline for PLATO-40, the fictional 40-room
self-check-in hotel of the Proximata Hackathon '26 Rhomberg track ("PLATO · CLARA
Future: The Autonomous Building"). It reads one dataset directory, detects operational
risks with evidence, and writes decisions under the challenge's decision contract.

Current state: Level 1 (S01 room turnaround, S02 water leaks, S03 heating and comfort) and
Level 2 (S04 filter cascade, S05 busy days, energy pre-heat, cleaning and logistics, a guest
desk) run as rule lanes through one arbiter, with closed-loop follow-ups and data-quality
flags; a supervisor LLM may add a few checked decisions (ADR-003). Both datasets validate
with no violations. `plato40 ui` writes the operations console as one HTML page; progress is in
[docs/PROGRESS.md](docs/PROGRESS.md).

PLATO-40 is a fictional reference property. PLATO and CLARA are names of Rhomberg
products and are used here only to refer to the challenge. Steward is not a Rhomberg
product.

## Setup

Requirements: Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked
```

## Data

The PLATO-40 v2 datasets are distributed by the organisers and are not committed here
(`data/` is gitignored). Fetch and verify them with:

```bash
scripts/fetch_data.sh /path/to/Hackathon.zip          # organiser bundle on disk
PLATO40_DATA_URL=<bundle-url> scripts/fetch_data.sh   # or download it
```

The script checks each archive against its published SHA-256 sum and unpacks
`data/PLATO40_v2_quickstart/` (21 days, January) and `data/PLATO40_v2_dev/` (365 days,
2026). The two are independent synthetic runs and are never concatenated.

Attribution: PLATO-40 synthetic dataset, proximata Hackathon '26 · Rhomberg track.
Licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

## Run

```bash
uv run plato40 run <data_dir> --out decisions.jsonl --trace decisions.trace.jsonl
                              # bus lanes s03-s08 (src/plato40/lanes.py) replace the planner for
                              # their issue type; --lanes s03 ... to pick, --lanes alone for none
                              # follow-ups from verify.py; --no-follow-ups to skip them
                              # --llm-cache <dir>: guest messages and the supervisor (below);
                              # --no-supervisor to skip the supervisor
uv run plato40 validate decisions.jsonl <data_dir>      # contract + autonomy policy
uv run plato40 arbiter <data_dir> <lane.bus.jsonl>... --out decisions.jsonl --verdicts v.jsonl
                                                        # merge lane proposals (docs/BUS.md)
uv run plato40 explain decisions.trace.jsonl [episode]  # why it acted, or did not
uv run plato40 ui decisions.jsonl decisions.trace.jsonl --out decisions.html  # console page
uv run plato40 bus <data_dir> --lane s03 --out out/  # one lane (s03-s08) -> bus -> arbiter (docs/BUS.md)
uv run plato40 snapshot <data_dir> --at 2026-05-09T11:00Z --assets R209 HP1  # supervisor context
uv run python scripts/rolling_medians.py <data_dir> out/rolling_medians.json  # past-only medians, every room and 15 min
uv run python scripts/build_r102_check.py <data_dir> out/rolling_medians.json out/r102_day1.json  # one room, one day check
uv run python scripts/insights.py <data_dir> --out insights.md --llm-cache <cache_dir>  # Level 3 report
make audit DATA=data          # regenerate reports/audit/01_data_audit.md and figures
make check                    # ruff, mypy --strict
```

The data path is always an argument; nothing in the code assumes a location. `DATA` may
be one dataset directory or a directory that contains several.

Guest messages in the guest's language (optional; without `--llm-cache` the run never
touches an LLM). The model only rewords and translates; every room, time and ID stays as
the rules wrote it, and any answer that breaks that keeps the rules' text
(`src/plato40/llm_guard.py`, `docs/BUS.md`):

```bash
uv sync --extra llm                                   # installs anthropic
export ANTHROPIC_API_KEY=...                          # or keep it in .env (gitignored)
PLATO40_LLM=on uv run plato40 run <data_dir> --out decisions.jsonl --llm-cache <cache_dir>
uv run plato40 run <data_dir> --out again.jsonl --llm-cache <cache_dir>   # from cache only
cmp decisions.jsonl again.jsonl                       # byte-identical
```

### Models and LLM settings

The rules decide every decision first. Two LLM uses sit on top, both optional, both
checked, and both cached so that a rerun without the network is byte-identical
([ADR-001](docs/adr/ADR-001-deterministic-core.md),
[ADR-003](docs/adr/ADR-003-supervisor-llm.md)):

| Use | Model (default) | Prompt | Env var to override | Code |
|---|---|---|---|---|
| Guest messages in the guest's language | `claude-haiku-5-5` | `guest_message/4`, tool forced | `PLATO40_LLM_MODEL` | `llm_guard.py`, `messages.py` |
| Supervisor: reviews the rules' decisions daily and at cross-domain hotspots, adds at most 2 checked decisions per review | `claude-sonnet-5-5` | `supervisor/2`, tool `auto` + one retry | `PLATO40_SUPERVISOR_MODEL` | `supervise.py`, `context.py` |

| Env var | Values | Effect |
|---|---|---|
| `ANTHROPIC_API_KEY` | key | needed only with `PLATO40_LLM=on` |
| `PLATO40_LLM` | `off` (default), `on` | `on`: a cache miss calls the model; `off`: cache only, a miss uses the template (messages) or adds nothing (supervisor) |
| `PLATO40_LLM_MODEL` | model ID | guest-message model |
| `PLATO40_SUPERVISOR_MODEL` | model ID | supervisor model |

Both run only with `--llm-cache <dir>`; `--no-supervisor` skips the supervisor. Answers
that pass every check are cached in `<dir>` (supervisor answers in `<dir>/supervisor/`);
rejected answers go to `rejected/` and are never reused. `plato40 run` also writes
`<out>.supervisor.jsonl`, one row per review: kind, time, asset, source (`cache`, `llm`,
`none`), the model's assessment, and the decisions it added or why it added none. Every
added decision's reason starts with `Supervisor review (<model>):`.

## Architecture

```
src/plato40/
  settings.py     one typed settings object: every threshold, with its unit
  load/           schema (declared columns, keys, availability column), units and
                  plausibility bounds, known_at, reader, derive (derived series)
  state.py        room state over time from lifecycle events
  staff.py        roster as known at a given time
  detect/         one module per scenario (turnaround, water, heating, logistics,
                  data_quality); each returns signals timed when their evidence existed
  episodes.py     signals grouped into one episode per incident
  plan.py         action chains per episode; S01 shares one cleaner ledger
  policy.py       autonomy levels, approval and consent flags, forbidden actions
  contract.py     decision contract checks and JSONL I/O
  pipeline.py     load -> signals -> episodes -> decisions -> policy -> checks
  bus.py          trigger bus contract: signal, proposal and verdict rows (docs/BUS.md)
  lanes.py        registry of the bus lanes plato40 run runs (lane_s03.py, ...)
  lane_s03.py     S03 trigger lane: heating and comfort signals and proposals on the bus
  lane_s04.py     S04 lane: AHU1 filter cascade, fan-implied pressure through gaps, data gaps
  lane_s05.py     S05 lane: event outlook a week ahead, 07:00 day plan, delay notices
  lane_s06.py     S06 energy lane: arrival pre-heat, S03 back-off, stale energy inputs
  lane_s07.py     S07 lane: vending restock, container projection, dump hold, robot bin runs
  lane_s08.py     S08 guest desk: one answer to every guest request no other lane answers
  arbiter.py      judges lane proposals on one shared ledger and merges them with the
                  pipeline's decisions (contract, known-at, roster, policy, message guard)
  llm_guard.py    contract around every LLM call; messages.py phrases guest messages
  context.py      known-at context snapshot of the building for the supervisor
  supervise.py    supervisor LLM: reviews the rules' decisions, adds checked ones (ADR-003)
  verify.py       closed loop: checks each outcome when due and follows up on a miss
                  (repair held, room still cold, repeat complaint, ready time missed)
  trace.py        run summary, trace file and explain
  ui.py, ui.html  operations console: one self-contained HTML page per run
  audit/          one module per audit section, assembled by audit/report.py
  cli.py          plato40 run | arbiter | bus | validate | explain | ui | snapshot | audit
reports/audit/    generated report and figures (committed, never edited by hand)
docs/adr/         architecture decision records
scripts/          fetch_data.sh, holdout.sh (docs/HOLDOUT.md), insights.py (insights.md),
                  check_bus.py, export_examples.py, demo_llm_guard.py, rolling_medians.py,
                  build_r102_check.py
```

Decisions that shape the code are recorded as ADRs:

- [ADR-001](docs/adr/ADR-001-deterministic-core.md): deterministic Python core; the
  LLM only phrases language, and its outputs are cached.
- [ADR-002](docs/adr/ADR-002-missing-and-known-at.md): blank means missing, never zero;
  decisions use only information known at that time.
- [ADR-003](docs/adr/ADR-003-supervisor-llm.md): the rules decide first; a supervisor
  LLM may add a few checked, grounded decisions, cached like guest messages.

## Assumptions

- Timestamps are UTC; local operations follow Europe/Vienna including daylight saving
  time. Night windows and shifts are computed in local time.
- A measured row is known at its own timestamp. Tariffs, forecasts, roster changes,
  bookings and work orders are known at their availability column (ADR-002).
- A cancellation is treated as known only at the booked arrival time, because
  `bookings.csv` does not say when it became known (ADR-002).
- Plausibility bounds per series are our assumptions, listed with their basis in
  `src/plato40/load/units.py`.
- The audit has no stochastic step; its output is byte-identical across runs with the
  locked environment.

## Changes after pre-holdout

None.

## Licence

Code: MIT, see [LICENSE](LICENSE). Dataset: CC BY 4.0, attribution above.
