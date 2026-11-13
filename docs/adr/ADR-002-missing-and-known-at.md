# ADR-002: Blank means missing; decisions use only what was known at the time

Status: accepted, 2026-11-13

## Context

The dataset README states that an empty field is a missing value and that some records
say when they became known (`published_utc`, `issued_utc`, `status_known_utc`,
`created_utc`). The holdout check penalises false alarms, and a decision that cites a
row published after the decision time is not a realistic decision.

## Decision

- The loader reads every CSV as text with `""` as the only missing marker and converts
  per declared type. A blank stays NaN/NaT/`<NA>`; it is never filled with zero, carried
  forward or interpolated by the loader.
- Each file has one declared availability column (`src/plato40/load/schema.py`): tariff
  `published_utc`, forecast `issued_utc`, roster `status_known_utc`, bookings and work
  orders `created_utc`. Measured series are known at their own timestamp.
- A decision at time `t` may read a row only if its availability time is at or before
  `t`. Each detector filters on that column directly (roster in `plato40.staff`, bookings,
  forecast and tariff in the detectors), never by effective time alone. `plato40 validate`
  also rejects a decision citing a guest request or another decision from after its own
  timestamp.
- A detector that needs a missing value lowers its confidence or defers; it does not
  issue a verdict on absent data.

## Consequences

- Detectors fire later than hindsight screens. The audit (`reports/audit`) labels its
  screens as hindsight for this reason.
- `bookings.csv` has no cancellation time, so the moment a cancellation became known is
  unknown (audit open question 2). Until answered, a cancellation is treated as known
  only at the booked arrival time. This is the conservative choice: no decision can
  benefit from a cancellation it might not have known about. The README lists it.
