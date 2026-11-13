# Steward for PLATO-40

Steward is a deterministic operations pipeline for PLATO-40, the fictional 40-room
self-check-in hotel of the proximata Hackathon '26 Rhomberg track ("PLATO · CLARA
Future: The Autonomous Building"). It reads one dataset directory, detects operational
risks with evidence, and writes decisions under the challenge's decision contract.

Current state: S01 room turnaround, S02 water leaks and S03 heat pump degradation are
detected and acted on, with data-quality flags; both datasets validate with no
violations. `plato40 ui` writes the operations console as one HTML page; progress is in
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
uv run plato40 validate decisions.jsonl <data_dir>      # contract + autonomy policy
uv run plato40 explain decisions.trace.jsonl [episode]  # why it acted, or did not
uv run plato40 ui decisions.jsonl decisions.trace.jsonl --out decisions.html  # console page
make audit DATA=data          # regenerate reports/audit/01_data_audit.md and figures
make check                    # ruff, mypy --strict
```

The data path is always an argument; nothing in the code assumes a location. `DATA` may
be one dataset directory or a directory that contains several.

## Architecture

```
src/plato40/
  settings.py     one typed settings object: every threshold, with its unit
  load/           schema (declared columns, keys, availability column), units and
                  plausibility bounds, known_at, reader, derive (derived series)
  state.py        room state over time from lifecycle events
  staff.py        roster as known at a given time
  detect/         one module per scenario (turnaround, water, heating, data_quality);
                  each returns signals timed when their evidence existed
  episodes.py     signals grouped into one episode per incident
  plan.py         action chains per episode; S01 shares one cleaner ledger
  policy.py       autonomy levels, approval and consent flags, forbidden actions
  contract.py     decision contract checks and JSONL I/O
  pipeline.py     load -> signals -> episodes -> decisions -> policy -> checks
  trace.py        run summary, trace file and explain
  ui.py, ui.html  operations console: one self-contained HTML page per run
  audit/          one module per audit section, assembled by audit/report.py
  cli.py          plato40 run | validate | explain | audit
reports/audit/    generated report and figures (committed, never edited by hand)
docs/adr/         architecture decision records
scripts/          fetch_data.sh
```

Decisions that shape the code are recorded as ADRs:

- [ADR-001](docs/adr/ADR-001-deterministic-core.md): deterministic Python core; the
  LLM only phrases language, and its outputs are cached.
- [ADR-002](docs/adr/ADR-002-missing-and-known-at.md): blank means missing, never zero;
  decisions use only information known at that time.

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
