# OSAM for PLATO-40

OSAM is a deterministic operations pipeline for PLATO-40, the fictional 40-room
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
products and are used here only to refer to the challenge. OSAM is not a Rhomberg
product.

## Setup

Requirements: Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked
```

## Environment

- Python 3.12 (`.python-version`; `pyproject.toml` requires `>=3.12,<3.13`) and
  [uv](https://docs.astral.sh/uv/). Dependencies are locked in `uv.lock`:
  `uv sync --locked` installs exactly those versions. Developed and run on Linux.
- The scored run needs no network and no API key: the rules decide, and LLM answers are
  read from a committed cache. `uv sync --locked` does not install the `anthropic` client;
  `uv sync --locked --extra llm` does, and it is needed only for live calls on a cache miss
  (`PLATO40_LLM=on`). `scripts/holdout.sh` and `scripts/regenerate.sh` run that sync
  themselves when an `ANTHROPIC_API_KEY` is set (in the environment or `.env`).
- Environment variables: see [.env.example](.env.example) (no secrets in it) and
  [Models and LLM settings](#models-and-llm-settings) below. Copy it to `.env`, which is
  gitignored; never commit a key.
- `make check` runs ruff and mypy --strict.

## Deliverables

Each file in the repository root is made by one command on one dataset directory:

| File | Command |
|---|---|
| `decisions.jsonl` (dev) | `uv run plato40 run data/PLATO40_v2_dev --out decisions.jsonl --llm-cache llm_cache` |
| `decisions_holdout.jsonl` | `scripts/holdout.sh <holdout_dir>` ([docs/HOLDOUT.md](docs/HOLDOUT.md)): one run, a rerun from the cache that must be byte-identical, and the contract check |
| `insights.md` | `uv run python scripts/insights.py data/PLATO40_v2_dev --out insights.md --llm-cache llm_cache --supervisor decisions.supervisor.jsonl` |
| tag `pre-holdout` | set before the holdout release; `decisions.jsonl` and `insights.md` reproduce byte for byte at it |

`plato40 validate <decisions.jsonl> <data_dir>` checks any of them against the decision
contract and the autonomy policy. One command rebuilds all of them, each LLM run followed by a
cached rerun that must be byte-identical:

```bash
make all DEV=data/PLATO40_v2_dev QS=data/PLATO40_v2_quickstart HOLDOUT=<holdout_dir>
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

Attribution: "PLATO-40 synthetic dataset, Proximata Hackathon '26 – Rhomberg track".
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
uv run plato40 ui decisions.jsonl decisions.trace.jsonl --out decisions.html \
    --data <data_dir> --llm-cache llm_cache --supervisor decisions.supervisor.jsonl \
    --rerun decisions.rerun.json --days 2026-01-25 2026-03-20   # plus the Year dashboard and
                              # the Day replay of the listed local days (demo page)
uv run python -m plato40.ui.copilot decisions.html decisions.jsonl decisions.trace.jsonl
                              # the page on http://127.0.0.1:8040 with the LLM tab live
uv run plato40 bus <data_dir> --lane s03 --out out/  # one lane (s03-s08) -> bus -> arbiter (docs/BUS.md)
uv run plato40 snapshot <data_dir> --at 2026-05-09T11:00Z --assets R209 HP1  # supervisor context
uv run python scripts/rolling_medians.py <data_dir> out/rolling_medians.json  # past-only medians, every room and 15 min
uv run python scripts/build_r102_check.py <data_dir> out/rolling_medians.json out/r102_day1.json  # one room, one day check
uv run python scripts/insights.py <data_dir> --out insights.md --llm-cache <cache_dir> \
    --supervisor <out>.supervisor.jsonl   # Level 3 report; the log makes it describe that run
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
uv sync --locked --extra llm                          # installs anthropic (live calls only)
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
| Copilot on the page (LLM tab): answers the manager's questions from a finished run's output rows through four read-only tools cut at the page clock; never writes a decision | `claude-sonnet-5-5` | `copilot/1`, tools + one retry | none | `ui/copilot.py`, `ui/ui_ask.js` |

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
  ui/            operations console and demo page (page code, views, data extract): one
                  self-contained HTML per run; separate from the pipeline on purpose;
                  copilot.py serves it locally with the LLM tab (reads output rows only)
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
  guest-message model only phrases language, and its outputs are cached.
- [ADR-002](docs/adr/ADR-002-missing-and-known-at.md): blank means missing, never zero;
  decisions use only information known at that time.
- [ADR-003](docs/adr/ADR-003-supervisor-llm.md): the rules decide first; a supervisor
  LLM may add at most 2 checked, grounded flags or requests per review, never changes or
  removes a rule's decision, and is cached like guest messages.

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
- The data is a replay: no decision changes later readings, so every effect of an action is
  labelled simulated, and outcomes are judged against events and work orders people wrote.
- A guest's language is that of their latest request known at the time; bookings carry no
  language, so a guest who never wrote gets English.
- The data's only costs are hourly electricity prices. The one saving OSAM reports is the
  battery plan's, simulated against no battery at the published prices; no other saving is
  claimed. "Notable cost" in the autonomy rules is a fixed list of actions
  (`src/plato40/policy.py`), not an amount.

### Threshold provenance: water leaks (S02)

Every threshold lives in `src/plato40/settings.py` with its unit and the measurement behind
it in the docstring. The S02 values, their basis in the data audit
(`reports/audit/01_data_audit.md`, sections 5, 6 and 8) and the practice they follow:

| Threshold | Value | Measured on dev (audit) | Practice |
|---|---|---|---|
| Continuous night flow | at least 0.2 L in every 15-min night interval, 2 nights in a row | outside leaks every room has a zero night interval on 100 % of nights; running toilets draw 0.9 to 1.1 L per interval | minimum night flow analysis, the standard water-loss method: a meter that never rests at night is leaking, whatever the volume; a running toilet is commonly cited at up to a few hundred litres a day |
| Burst volume | 250 L in one room in one night window | normal nights reach at most 180 L; one bath alone draws up to 250 L | one night of flow with no rest, above anything a sleeping guest draws |
| Night gap main minus rooms | median 4 L/h above the building's own baseline (25th percentile of the last 14 normal nights, floored at zero), or 15 L/h whatever the baseline; the first 2 nights use the 15 L/h net only | 95th percentile of normal nights 1.4 to 2.0 L/h; the dev pipe leak ran at 25 to 30 L/h | district metered area night balance: supply minus the sum of consumer meters, at night; the baseline is learnt from past nights only, leak nights and blank nights never teach it |
| Sustained flow at any hour | 10 L per 15 min for 12 intervals (3 h) | heaviest guest use 2.8 L per 15 min, held at most 6 intervals; bursts run 20 h or more | a daytime burst in a vacant room would escape a night-only window |

The rule is persistence first, magnitude second: a slow leak is called on two nights of
unbroken flow, not on litres. Each value sits several times above normal operation and
several times below the incidents in the year, so the exact number is not what detects them.
Three thresholds are fixed and not refitted on the window they judge (ADR-001). The night gap
is judged against a past-only learnt baseline, so another run of the building with higher
normal losses outside the rooms learns its own normal instead of alarming every night; its
limit is that a leak under 15 L/h running from the first night is learnt as normal. On dev
and quickstart the learnt and the fixed rule give the same decisions.

## Changes after pre-holdout

The decision files were produced at the tag; nothing below touches detection, decision logic,
thresholds, the run's prompts or data processing.

- `src/plato40/ui/` (`copilot.py`, `ui_ask.js`, `ui.html`): the LLM tab. Three fixed answers
  computed in the page (what happened since a time, why an episode happened, what waits on a
  human) and a question box for a model served by a local copilot. The copilot reads the
  decisions and trace files only, through four tools that cut the rows at the page clock; an
  answer is shown only if every cited id and number is in what the tools returned. It never
  writes a decision. `ui.html` gains the tab and its key in the footer. Restyle of the page
  (every figure unchanged): IBM Carbon g10/g100 surfaces and status colours,
  Okabe-Ito lane colours keyed by issue type (`data.py`), IBM Plex fonts inlined from
  `ui/fonts/` (SIL OFL), a light/dark/auto toggle, the Year figures in one band with cards for
  the replay days, and the Day scrubber's hour labels drawn as text so they no longer stretch.
- `README.md`: this section, the copilot command, its row in the model table and the `ui/`
  line of the architecture tree.
- `docs/DEMO.md`: step 1's cue now names the lane colours of the year strip.
- `decisions.html`, `examples/quickstart/console.html`: rewritten with the restyled page from the
  same decision files.
- `decisions_holdout.html`: rewritten with the restyled page from the same holdout decision
  files, now with the Day replay of 26 Jan, 24 Jun and 28 Nov; `scripts/holdout.sh` passes
  these days to `plato40 ui` (its run, rerun and validate steps are unchanged).
- `Makefile`: `make all DEV=<dir> QS=<dir> HOLDOUT=<dir>` runs `scripts/regenerate.sh` and
  then `scripts/holdout.sh`, so one command rebuilds every committed output.

## Submission text

Paste-ready for the written submission (CHALLENGE section 10). Fill in the bracketed parts.

- **Title:** OSAM for PLATO-40
- **One-line pitch:** Rule lanes run the PLATO-40 hotel day and night, act on their own only
  where the autonomy rules allow it, cite the data behind every decision, and let a language
  model word guest messages and flag what the rules missed, never decide in their place.
- **Team:** [names and roles]
- **Problem:** Hotel operations signals sit in separate systems, arrive late, drop out or are
  wrong, and one change creates trouble elsewhere: an early arrival meets a short cleaning
  shift, a running toilet hides in normal night use, a weak heat pump meets a cold snap.
  People stitch this together by hand, and the lessons rarely reach the next building.
- **Solution:** One command turns a dataset into `decisions.jsonl`. Detectors and one rule
  lane per scenario (turnaround, leaks, heating, filters, busy days, energy, logistics, guest
  desk) write signals and proposals to a trigger bus; an arbiter checks each against the
  decision contract, what was known at the time, the staff and robot roster and the autonomy
  policy, on one shared ledger. A closed loop follows up missed outcomes. Guest messages go
  out in the guest's language under a contract that forbids invented numbers; a supervisor
  model reviews the day and may add a few checked decisions. An operations console shows
  evidence, risk, decision, autonomy and verification, and `insights.md` turns the year into
  feedback for CLARA and PLATO, every number computed by a script.
- **Stack:** Python 3.12, uv, pandas, numpy, matplotlib; Claude Haiku 5.5 (guest messages)
  and Claude Sonnet 5.5 (supervisor) through the Anthropic API, with every answer checked
  and cached; JSONL contracts; a self-contained HTML console.
- **Links:** repository [URL in the Proximata organisation], demo video [URL],
  `insights.md`, [docs/BUS.md](docs/BUS.md), [ADRs](docs/adr/).

## Licence

Code: MIT, see [LICENSE](LICENSE). Dataset: CC BY 4.0, attribution above.
