# Progress board

Our stand-up. One row per piece of work, grouped by the challenge's Levels and scenarios
(CHALLENGE.md section 4). IDs `T0`-`T15` are the project plan's work breakdown; sub-items
are ours. Points are the rubric line the item serves (CHALLENGE.md section 9).

Status: `done` · `doing` · `todo` · `blocked` · `cut`. Owner is a role until names are
filled in: **D** data and detectors, **A** agent and policy, **F** frontend, **S** story,
Level 3 and submission.

Feature freeze Sat 11:00 CET · holdout release Sat ~11:00 · code freeze Sat 14:00 CET
(CHALLENGE.md; our plan said 15:00, see Q1) · submit by 13:30 if Q1 stays at 14:00.

## Foundation (serves every Level)

| ID | Item | Owner | Status | Points |
|---|---|---|---|---|
| T0 | Data fetched with checksums, CHALLENGE.md, dataset README and DECISION_CONTRACT.md read | S | done | all |
| T0 | Open questions Q1-Q12 sent to the mentor on Discord | S | todo | all |
| T1 | Package skeleton, loader, schema and units, blank = missing, known-at helpers | D | done | Reproducibility 10 |
| T1 | Data audit for quickstart and dev (`make audit`, `reports/audit/01_data_audit.md`) | D | done | Robustness 7 |
| T1 | `plato40 run <data_dir> --out decisions.jsonl` writes a valid empty file | D | done | Reproducibility 10 |
| T1 | Contract validator (`plato40 validate`; sample file passes) | D | done | Reproducibility 10 |
| T1 | Hand-written sample `decisions.jsonl` (one per scenario) handed to F | A | todo | Demo 8 |
| T1 | Known-at discipline: signals timed when known; roster absences, bookings by `created_utc` | D | done | Robustness 7 |
| T2 | Room state machine from events (occupied, dirty, cleaning, clean); telemetry cross-check open | D | doing | Constraints 8 |
| T2 | Staff and robot availability from roster, as known at decision time | D | done | Constraints 8 |
| T6 | Episodes: one `episode_id` per incident, closed after 48 h quiet; priority fixed per scenario | D/A | done | Prioritisation 8 |
| T6 | Spam and false-alarm counter printed after every run (dev normal stretches) | D | todo | Detection 10 |
| T7 | Policy layer: target-aware levels (cost = fixed list, no euro amounts; vacant inspection autonomous) | A | done | Safe autonomy 10 |
| T7 | Violation check over every decision (`plato40 validate`) | A | done | Safe autonomy 10 |
| T8 | Planner: action chain per episode in the 14-verb vocabulary | A | todo | Coordination 8 |
| T8 | Mock executor: effects labelled `simulated`, `expected_outcome`, `verification_plan` | A | todo | Limits and verification 5 |
| T13 | README, LICENSE, `.env.example`, `fetch_data.sh`, ADR-001, ADR-002 | S | done | Reproducibility 10 |
| T13 | README run command and architecture updated once `run` exists | S | todo | Reproducibility 10 |
| T14 | Fresh-clone rehearsal with quickstart as the pretend holdout | D | todo | Reproducibility 10 |
| T14 | Tag `pre-holdout`, push (needs the go to push) | D | todo | Reproducibility 10 |
| T14 | Holdout run unchanged, commit `decisions_holdout.jsonl` | D | todo | Reproducibility 10 |

## Level 1: operate and detect (mandatory)

### S01 The Perfect Checkout

| ID | Item | Owner | Status | Points |
|---|---|---|---|---|
| T3 | Signals located: early checkout, early check-in requests, cleaning 45 min median (p90 60) | D | done | Detection 10 |
| T3 | 07:00 day plan: load = turnovers x 45 min / cleaner minutes to 15:00; >= 0.75 at risk; acts before 18 of 20 real dev waits (was 9) | D | done | Detection 10 |
| T3 | Detector: requests and waiting events; ready time = queue x 60 min vs requested time | D | done | Detection 10 |
| T3 | Look-alikes: robot `completed` is not readiness; room ready when guest arrives = no action | D | done | Robustness 7 |
| T8 | Actions: cleaning at checkout, robot pass, one room offer (approval), on-call (approval), guest notice | A | done | Coordination 8 |
| T7 | Rule: never `release_room` unless clean (vacant + human cleaning completed) | A | done | Safe autonomy 10 |
| T10 | Guest message in the guest's language, deadline honoured | A | todo | Guest comms 6 |

### S02 The Hidden Water Leak

| ID | Item | Owner | Status | Points |
|---|---|---|---|---|
| T4 | Signals located: night floor, burst volume, main-minus-rooms night gap; 4/4 and 9/9 orders covered | D | done | Detection 10 |
| T4 | Detector A: continuous night flow, two nights in a row (audit implication 1) | D | done | Detection 10 |
| T4 | Detector B: burst volume at night, plus sustained high flow at any hour (2 h at 10 L per 15 min) | D | done | Detection 10 |
| T4 | Detector C: night gap above normal p95 (implication 3); never daily gap totals | D | done | Detection 10 |
| T4 | Look-alikes: resets masked, incomplete nights give no verdict, outages one flag per episode | D | done | Robustness 7 |
| T8 | Actions: graded chain, consent request before entering an occupied room, never shut off water | A | done | Safe autonomy 10 |

### S03 Energy vs Comfort

| ID | Item | Owner | Status | Points |
|---|---|---|---|---|
| T5 | Signals located: COP vs outdoor (dev Theil-Sen curve), comfort deficit, lockouts | D | done | Detection 10 |
| T5 | Detector: hourly 24 h COP vs fixed dev reference, ratio < 0.88; 46-52 h before each HP1 work order | D | done | Detection 10 |
| T5 | Comfort deficit as second signal inside HP1 episodes only (May/Aug/Sep deficits raise nothing, Q6) | D | done | Robustness 7 |
| T5 | Forecast cold snap and price peaks joined by `issued_utc` / `published_utc` | D | done | Trade-offs 6 |
| T8 | Actions: flag, technician or contractor (approval), comfort-first energy plan, guest notices <= 1 per 12 h | A | done | Trade-offs 6 |
| T7 | Rule: comfort band never breached for an energy metric (needs the daily energy plan) | A | todo | Safe autonomy 10 |

## Level 2: act autonomously (advanced)

| ID | Item | Scenario | Owner | Status | Points |
|---|---|---|---|---|---|
| T9 | Filter detector: day-mode dp, fan power as the second signal through gaps (implication 6) | S04 | D | todo | Detection 10 |
| T9 | Data gap reported as `data_quality`, never as recovery; technician availability known-at | S04 | D/A | todo | Robustness 7 |
| T9 | Actions: `defer_action` on thin evidence, `order_service` / `dispatch_technician` (approval) | S04 | A | todo | Coordination 8 |
| T9 | Weekend plan re-run whenever a roster row becomes known (implication 10) | S05 | A | todo | Coordination 8 |
| T9 | Capacity from present cleaners, on-call 15:30-22:00, two robots; never invented | S05 | A | todo | Constraints 8 |
| T10 | Staggered check-in times sent to guests | S05 | A | todo | Guest comms 6 |
| T9 | Daily `optimize_energy`: battery and pre-heat against published prices and PV | Energy | A | todo | Trade-offs 6 |
| T9 | Waste pickup before the container fills; robots carry bins if Q10 allows | Logistics | A | todo | Coordination 8 |
| T9 | Vending restock before stock-out (first on the cut list) | Logistics | A | todo | Coordination 8 |

## Level 3: learn and feed back

| ID | Item | Owner | Status | Points |
|---|---|---|---|---|
| T12 | Candidate findings from the audit: July overheating (no cooling), BMS alarm tuning, F3 gateway, May/Aug/Sep comfort deficits, cleaning capacity on surge weekends | S | done | Feedback 10 |
| T12 | `insights.py`: every number in `insights.md` produced by a script | S | todo | Feedback 10 |
| T12 | Insight: recurring F3 gateway outages (count from the outage episodes) | S | todo | Feedback 10 |
| T12 | CLARA recommendations with evidence and uncertainty | S | todo | Feedback 10 |
| T12 | PLATO design recommendations with evidence and uncertainty | S | todo | Feedback 10 |
| T12 | Target architecture: one diagram plus one page | S | todo | Architecture 5 |
| T12 | Limits section | S | todo | Limits 5 |

## Demo and submission

| ID | Item | Owner | Status | Points |
|---|---|---|---|---|
| T11 | Floor-plan twin (4 x 10 rooms) with time scrubber, reading `decisions.jsonl` | F | todo | Demo 8 |
| T11 | Decision feed with autonomy badge, evidence on click, approval queue | F | todo | Trail 7 |
| T11 | Run summary, `--trace` and `plato40 explain` (terminal trail; HTML timeline reads the trace) | D | done | Trail 7 |
| T15 | 3-minute video, end to end | S/F | todo | Demo 8 |
| T15 | Written submission text and form | S | todo | all |
| T15 | Repo moved to the Proximata organisation and made public (needs the go) | S | todo | all |

## Open questions for the mentor

| Q | Question | Affects | Status |
|---|---|---|---|
| Q1 | Code freeze 14:00 (CHALLENGE.md) or 15:00? | schedule | open |
| Q2 | When is a cancellation known? ADR-002 assumes the booked arrival time | S01, S05 | open |
| Q3 | How does the check match a decision to an incident; do routine decisions count as spam? | all | open |
| Q4 | Holdout: same building, length and season as dev? | T14 | open |
| Q5 | Are the alarm lag and the 19 unsupported comm-loss alarms intended? | S02, S04 | open |
| Q6 | Why do occupied rooms fall below setpoint mostly in May, August, September? | S03, Level 3 | open |
| Q7 | Is HP1 COP meant to be derived (declared metric `cop` never appears)? | S03 | open |
| Q8 | `inspection_required` never occurs: is a completed cleaning enough to release? | S01 | open |
| Q9 | May decisions name `MUNICIPAL_SERVICE`, `VENDOR_EXT`, `housekeeping`? | S05, logistics | open |
| Q10 | May robots carry waste bins? | logistics, S05 | open |
| Q11 | Is the on-call housekeeper always available, and does calling them need approval? | S05 | open |
| Q12 | Which attribution string exactly? | README | open |
