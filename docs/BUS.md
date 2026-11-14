# Trigger bus v1: how the S01–S08 triggers talk to the arbiter

One append-only JSONL file (`bus.jsonl`). Triggers write **signals** and **proposals** to their
own lane, the arbiter writes **verdicts**, and only the arbiter writes `decisions.jsonl`.
Contract code: `src/plato40/bus.py`. LLM contract: `src/plato40/llm_guard.py`.
Arbiter: `src/plato40/arbiter.py`. Native trigger lanes: `src/plato40/lane_s03.py` to
`lane_s08.py`, registered in `src/plato40/lanes.py`.
Golden examples, all generated from a real quickstart run: `examples/bus/s01.bus.jsonl`
(copy this one), `s02.bus.jsonl`, `s03.bus.jsonl` and the matching `*.decisions.jsonl`.
S01 and S02 are exported from the pipeline (`scripts/export_examples.py`); S03 is written
by its own lane and the arbiter: `plato40 bus <data_dir> --lane s03 --out examples/bus`.

```
trigger s01 ─┐  signal, proposal          verdict             decisions.jsonl
trigger s02 ─┼──────────────► bus.jsonl ──► ARBITER ────────► (contract v2, scored)
trigger s03 ─┤                    ▲           │ policy.apply sets autonomy + approval flags
LLM advisor ─┘  proposal (source=llm)         │ ledger grants claims (cleaners, robots, TECH_1)
                                              └ known-at: reads only rows with t <= now
```

## Rules (the contract; `bus.check_row` / `check_file` / `check_known_at` enforce them)

1. **Append only.** Never edit or delete a line. A correction is a new row.
2. **Your lane only.** S02 writes `lane: "s02"` rows and nothing else; enums are per lane (below).
3. **Known-at.** `t_known` is when the information became known (`status_known_utc`,
   `created_utc`, request `timestamp_utc`), not when the thing happens. A proposal at `t` may cite
   only signals with `t_known <= t` and guest requests sent at or before `t`. Future *predictions*
   (a ready time, a departure) are fine as long as they were known at `t`.
4. **Proposals never set `autonomy`, `requires_human_approval` or `requires_guest_consent`.**
   The policy sets them. A proposal carrying one is rejected.
5. **Every proposal answers at least one signal** (`signal_ids`) and cites evidence as
   `file.ext:ref` (e.g. `bookings.csv:BK000250`, `events.jsonl:E0001288`).
6. **IDs only from `building.json`** (rooms, assets, staff, robots, `BUILDING`, `MAIN_ENTRANCE`).
7. **`notify_guest` needs `message: {language, text}`**, in the guest's language (`de en fr it nl es`),
   one proposal per guest. No other action may carry a message.
8. **The arbiter copies an accepted proposal unchanged** into `decisions.jsonl` (action, assets,
   priority, evidence, resource, time); `check_bus.py` fails if anything changed.
9. Times: ISO 8601 UTC ending in `Z`, e.g. `2026-01-24T13:00Z`. Keys sorted, UTF-8.

## Row types

| row | written by | required | optional |
|---|---|---|---|
| `signal` | trigger | `v lane id row t_known issue_type kind assets evidence detail facts` | `cause` |
| `proposal` | trigger or LLM | `v lane id row t source episode_id signal_ids intent action assets priority evidence reason` | `resource_id guest_request_ids message claims needs cause confidence expected_outcome verification_plan` |
| `verdict` | arbiter only | `v lane id row t proposal_id accepted why` | `event_id` (when accepted) |

`source` is `rules` or `llm`. `claims` is a list of `{res, id, from, min}` with `res` in
`cleaner robot technician on_call room`: what the proposal needs from the shared ledger.
A claim may not start before the proposal's `t`. A staffed action (`schedule_cleaning`,
`dispatch_robot`, `dispatch_technician`) with a `resource_id` but no claim on it still holds
that resource from `t` for the settings' duration. `needs` lists proposal IDs this one depends
on (a promised ready time needs its cleaning): if any of them was not accepted before it, it
is rejected too. An accepted proposal's `needs` become its decision's `dependencies`, so the
link is in `decisions.jsonl` (e.g. an S05 delay notice depends on the day plan's cleaning
order and its extra-help approval). A `message` is checked like an LLM answer against the proposal's own
`reason`: every number, date and ID in it must appear in the reason, no banned claim, an
"expected" in the reason keeps a hedge word, and the language must match every guest
request in `guest_request_ids`.
The arbiter grants claims in this order: safety, then a waiting guest, then a deadline, then
comfort, then logistics. A claim that does not fit is a rejected verdict, with `why`.

IDs: `<lane>-<episode without EP->-sigNN`, `-propNN`, and `<proposal id>-v` for the verdict.

## Enums per lane

`kind` = what the trigger saw; `intent` = why it proposes; `action` = the contract verb.
*Italic* = recommended, not emitted by the code yet (S01/S02 at `97b00cc`; S03 by `lane_s03.py`).

| lane | issue_type | kind | intent | action | cause |
|---|---|---|---|---|---|
| **s01** | `room_turnaround` | `request checkin_due overstay checkout ready day_plan` | `clean_first robot_prepass promise_ready_time notify_ready_now notify_day_delay offer_alternative_room withdraw_offer escalate_waiting_guest request_extra_cleaning` | `schedule_cleaning dispatch_robot notify_guest reassign_room request_human_approval release_room defer_action` | none |
| **s02** | `water_leak` | `night_flow burst burst_flow night_gap night_blank` | `flag_suspected_leak inspect_room inspect_pipes ask_entry_consent block_room dispatch_plumber escalate_plumber release_repaired_room` *`flag_meter_artefact watch_next_night`* | `request_inspection dispatch_technician order_service block_room release_room notify_guest flag_anomaly request_human_approval defer_action` | *`room_leak pipe_leak guest_use cleaning_use meter_artefact meter_exchange unknown`* |
| **s03** | `heating_comfort` | `cop_low comfort_deficit comfort_excess cold_forecast price_peak` | `flag_hp_degradation protect_occupied_rooms service_heat_pump escalate_contractor notify_guest_comfort preheat_before_cold shift_load_off_peak boost_room_setpoint flag_room_heating cool_room_setpoint notify_guest_heat` *`flag_sensor_gap`* | `adjust_room_setpoint optimize_energy dispatch_technician order_service notify_guest flag_anomaly request_human_approval defer_action` | `hp_degradation cold_snap sensor_gap unknown` |
| **s04** | `ventilation_maintenance` | `dp_due dp_fast_rise airflow_loss sensor_gap sensor_fault work_order_missed bms_unmatched` | `plan_filter_change change_filter flag_filter_clogging service_filter escalate_contractor flag_airflow_loss flag_sensor_gap inspect_dp_sensor flag_sensor_fault flag_missed_work_order defer_to_work_order flag_unconfirmed_alarm` | `flag_anomaly request_inspection dispatch_technician order_service defer_action` | `filter_loading filter_clogging sensor_gap sensor_fault work_order_postponed bms_unconfirmed unknown` |
| **s05** | `occupancy_surge` | `surge_forecast day_plan` | `request_standby_cleaner clean_first robot_prepass notify_day_delay request_extra_cleaning` | `schedule_cleaning dispatch_robot notify_guest request_human_approval` | `event_weekend sick_cleaner turnover_peak` |
| **s06** | `energy_optimization` | `arrival_due s03_active input_stale` | `preheat_arrival back_off_for_comfort flag_energy_data_gap` | `adjust_room_setpoint optimize_energy flag_anomaly defer_action` | `arrival s03_backoff stale_input unknown` |
| **s07** | `logistics` | `drinks_low snacks_low container_overflow_risk event_weekend_window sensor_gap sensor_jump` | `order_restock order_pickup hold_floor_dump robot_bin_run flag_logistics_data` | `order_service dispatch_robot defer_action flag_anomaly` | `demand_peak post_stockout turnover_peak event_weekend sensor_gap sensor_jump unknown` |
| **s08** | `guest_service` | `access_issue maintenance_issue water_issue air_quality cleaning_request late_checkout early_checkin noise_complaint wifi_help parking invoice luggage_storage extra_towels arrival_delay waiting_for_room` | `answer_request inspect_room order_repair clean_room` | `notify_guest request_inspection order_service schedule_cleaning` | `guest_request unknown` |

Lane code and rank (the arbiter's order at one minute, lowest first):

| lane | scenario | code | rank |
|---|---|---|---|
| s01 | S01 room turnaround (planner, exported) | `plan.py`, `detect/turnaround.py` | 1 |
| s02 | S02 water leaks (planner, exported) | `plan.py`, `detect/water.py` | 0 |
| s03 | S03 heating and comfort | `lane_s03.py` | 2 |
| s04 | S04 AHU1 filter cascade, equipment data gaps | `lane_s04.py` | 3 |
| s05 | S05 busy days: event outlook, 07:00 day plan | `lane_s05.py` | 1.5 |
| s06 | Energy: arrival pre-heat, S03 back-off, stale inputs | `lane_s06.py` | 5 |
| s07 | Cleaning and logistics: vending, container, floor bins | `lane_s07.py` | 4 |
| s08 | Guest desk: one answer to every guest request | `lane_s08.py` | 9 (default: not in `LANE_RANK`) |

`cause` is how S02 and S03 tell a real fault from a look-alike (guest bath, cleaning, meter
swap, cold snap). A proposal with `cause: meter_artefact` should be `flag_anomaly`, never a
technician. To add an enum value: add it in `bus.LANES` in the same commit as the code that emits it.

## Worked example: S01, R108, quickstart 24 Jan (copy this shape)

Real lines from `examples/bus/s01.bus.jsonl`, shortened with `…`.

**13:00Z, the trigger sees it** (signal; `facts` = everything the planner and the LLM may use):
```json
{"v":1,"row":"signal","lane":"s01","id":"s01-room_turnaround-R108-20260124T1300Z-sig01",
 "t_known":"2026-01-24T13:00Z","issue_type":"room_turnaround","kind":"request","assets":["R108"],
 "evidence":["events.jsonl:E0001288","bookings.csv:BK000250","events.jsonl:R108 dirty at 2026-01-24T13:00Z"],
 "detail":"guest of BK000250 wants the room at 14:00 local; room is dirty; predicted ready 18:00 local without action",
 "facts":{"booking":"BK000250","language":"en","waiting":true,"vacant":true,"wanted":"2026-01-24T13:00Z",
          "ready_now":"2026-01-24T17:00Z","ready_prio":"2026-01-24T14:00Z","robot_done":true,
          "cleaners":[["CLEANER_2","2026-01-24T13:00Z","2026-01-24T14:30Z"], …],"alternatives":[], …}}
```
**13:00Z, the trigger proposes** (two proposals answer the same signal):
```json
{"v":1,"row":"proposal","lane":"s01","id":"s01-room_turnaround-R108-20260124T1300Z-prop01",
 "t":"2026-01-24T13:00Z","source":"rules","episode_id":"EP-room_turnaround-R108-20260124T1300Z",
 "signal_ids":["s01-room_turnaround-R108-20260124T1300Z-sig01"],"intent":"clean_first",
 "action":"schedule_cleaning","assets":["R108"],"priority":"critical","resource_id":"CLEANER_2",
 "claims":[{"res":"cleaner","id":"CLEANER_2","from":"2026-01-24T13:00Z","min":60}],
 "evidence":[…same three…],"reason":"Clean R108 first, now: …","confidence":0.7,
 "expected_outcome":"R108 clean by 15:00 local (simulated).",
 "verification_plan":"R108 shows cleaning_completed by 15:00 local; if not, update the guest and escalate."}
{"v":1,"row":"proposal","lane":"s01","id":"…-prop02","t":"2026-01-24T13:00Z","source":"rules",
 "intent":"promise_ready_time","action":"notify_guest","assets":["R108"],"priority":"critical",
 "message":{"language":"en","text":"R108 expected ready at 15:00 local"}, …}
```
**13:00Z, the arbiter decides** (verdict; then the decision line, the only thing scored):
```json
{"v":1,"row":"verdict","lane":"s01","id":"…-prop01-v","t":"2026-01-24T13:00Z",
 "proposal_id":"…-prop01","accepted":true,"why":"autonomous by default",
 "event_id":"EP-room_turnaround-R108-20260124T1300Z-01"}
```
```json
{"event_id":"EP-room_turnaround-R108-20260124T1300Z-01","timestamp_utc":"2026-01-24T13:00Z",
 "affected_assets":["R108"],"issue_type":"room_turnaround","action":"schedule_cleaning","priority":"critical",
 "resource_id":"CLEANER_2","autonomy":"autonomous","requires_human_approval":false,
 "requires_guest_consent":false, …}
```
The same file also shows an offer (`R308`, 8 Jan: `reassign_room` gets the verdict
`reassign_room always needs approval`, then `escalate_waiting_guest` when the guest waits) and
a day plan (`BUILDING`, 24 Jan, `kind: day_plan`, intents `clean_first`, `robot_prepass`,
`notify_day_delay`, `request_extra_cleaning`).

## LLM calls: design by contract (`llm_guard.py`)

The LLM **phrases**; the rules **decide**, and they also **render every number** (ADR-001).

- **`Task.say`** holds every token the text may contain, already rendered by the rules from
  `facts`: `{"room":"R108","ready":"15:15"}`. The LLM picks only the words around them.
- **Preconditions** (a failure raises, because it is a caller bug): language known; facts are
  JSON; `known_by <= t`; the template holds every `must_say` key.
- **Postconditions** (a failure falls back to the template, and the reason goes to the trace):
  - the answer passes the tool schema `{language, text}`;
  - same language;
  - **every number and ID in the text is a `say` value**;
  - every `must_say` value appears;
  - at most 400 characters;
  - no banned claims in any of the six languages (`guarantee`, `garanti`, `confirm`, `kostenlos`, …);
  - an estimate stays an estimate: when the rules' text says "expected", the answer keeps a
    hedge word of its language (`voraussichtlich`, `devrait`, `dovrebbe`, `verwacht`, `debería`, …).
- **Call:** output forced through one tool, so the reply is JSON. The API takes no
  temperature for these models and two calls can differ, so determinism comes from the **cache**:
  - only an answer that passes is cached, keyed by sha256(model + prompt version + full task);
  - a rejected answer goes to `rejected/` for the trace and is never reused;
  - the default is `PLATO40_LLM=off`: nothing is called, and a cache miss uses the template.
- **Holdout:**
  1. Run once with `PLATO40_LLM=on` and commit the cache.
  2. Rerun with it off.
  3. The two `decisions_holdout.jsonl` must be byte-identical.
  If the API is down, the run still finishes, with templates.

`scripts/demo_llm_guard.py` plays scripted answers against the real R108 signal:

| LLM answer | result |
|---|---|
| "ready at 15:00 and you get 20% off" | template: invented `15:00`, invented `20`, missing `15:15` |
| "your room R109 …" | template: invented id `R109` |
| German text for an `en` guest | template: language `de` is not `en` |
| "ready soon" | template: missing `ready='15:15'` |
| "guaranteed ready" | template: banned claim |
| API timeout | template |
| "Sorry for the wait: your room R108 should be ready at 15:15 local time." | **used**, cached |
| rerun with the LLM off | same text, from the cache |
| facts known at 14:00Z for a 13:41Z decision | raises |

## How to add your trigger (any lane)

1. Emit `signal` rows: your lane, a `kind` from the table, `facts` with everything the planner
   and the message need, and evidence refs.
2. Emit `proposal` rows: one per action, `signal_ids` set, `intent` and `action` from your lane,
   and `claims` if you need staff, a robot or TECH_1. For `notify_guest`, use one proposal per
   guest, with the language from `guest_requests.csv` for that booking (known at `t`).
3. Never write `decisions.jsonl`, `autonomy` or the approval flags. Link dependent proposals
   with `needs` (they become `dependencies`).
4. Check: `python scripts/check_bus.py <data_dir> <your.bus.jsonl> <decisions.jsonl>` → 0 violations.
5. Merge with the arbiter (`src/plato40/arbiter.py`):
   ```bash
   uv run plato40 arbiter <data_dir> s02.bus.jsonl s03.bus.jsonl --replace s02 s03 \
       --out decisions.jsonl --verdicts verdicts.jsonl
   ```
   It runs the pipeline, then judges every proposal in time order (within a minute by
   priority). A proposal is rejected, with the reason printed and in its verdict, when it
   breaks a rule above, when a claim overlaps a resource already granted (the pipeline's own
   cleanings, robot passes and technician jobs are on the ledger first), when the policy
   forbids it, or when the decision breaks the contract. `--replace` drops the pipeline's own
   decisions for those lanes; without it, accepted proposals are added next to them.
   One file per lane, committed by its owner: nobody writes another lane's file.
6. Register a native lane: a module `lane_sNN.py` with `rows(ds, settings, cache, call)`, its
   enums in `bus.LANES`, its rank in `arbiter.LANE_RANK` and one line in `lanes.py`, all in
   one commit. `plato40 bus <data_dir> --lane sNN --out <dir>` then runs it alone, and
   `plato40 run` runs it with the others.

## S03 lane: what it writes (`src/plato40/lane_s03.py`)

`plato40 bus <data_dir> --lane s03 --out <dir>` runs the lane, then the arbiter, writes
`<dir>/s03.bus.jsonl` and `<dir>/s03.decisions.jsonl`, and exits 1 on any bus, known-at,
decision-contract or policy violation. HP1 counts as *degraded at t* when the hourly check
at `t` floored to the hour is `cop_low`.

| kind | when (t_known) | assets | cause | proposals (intent → action) |
|---|---|---|---|---|
| `cop_low` | each hourly check with 24 h COP below `cop_ratio_low` of the reference | HP1 | `hp_degradation` | first of an episode: `flag_hp_degradation` → `flag_anomaly`; `service_heat_pump` → `dispatch_technician` with a TECH_1 claim, or `escalate_contractor` → `order_service` if no technician on shift within 24 h; `protect_occupied_rooms` → `optimize_energy`. Later: `escalate_contractor` once, 24 h after the technician's start if the COP is still low |
| `comfort_deficit` | at a `cop_low` check | the rooms | `hp_degradation` | one `notify_guest_comfort` per guest newly in deficit, once per stay and episode |
| `comfort_deficit` | at a `temperature_cold` request, rounded up to the minute (so before `response_due_utc`) | the room | `hp_degradation` while degraded, else `unknown` (`sensor_gap` with no reading in the last hour) | a reply to that guest with `guest_request_ids`; outside HP1 episodes also `boost_room_setpoint` → `adjust_room_setpoint` (room below the band, window not reported open) and `flag_room_heating` → `flag_anomaly` (also below its setpoint by `comfort_deficit_k`) |
| `comfort_excess` | at a `temperature_hot` request, rounded up to the minute | the room | `unknown` (`sensor_gap` with no reading in the last hour); never the HP1 episode | `notify_guest_heat` → `notify_guest` with `guest_request_ids`; `cool_room_setpoint` → `adjust_room_setpoint` to the band low when the room is above the band and its setpoint above the band low (the building has heating only, no cooling) |
| `cold_forecast` | each forecast issue while degraded, coldest hour of the next 24 h below `preheat_forecast_c` | HP1, BUILDING | `cold_snap` | `preheat_before_cold` → `optimize_energy` |
| `price_peak` | each tariff publication while degraded | HP1, BUILDING | none | `shift_load_off_peak` → `optimize_energy`, only where occupied rooms stay in the band |

Guest messages: one proposal per guest, language from the request, else the booking's latest
request known at `t`, else `en`. Text goes through `llm_guard.phrase` with `say = {room}`;
three templates (`hp`, `room`, `window`) in all six languages, so a cache miss or a rejected
LLM answer still gives the guest their language. A cold report with the window reported open
asks the guest to close it. A hot report gets the `hot` parts, joined in order: no cooling in
the building, the setpoint lowered (if it was), the window open at night helps, and, with the
window reported open between `hot_day_start_h` and `hot_day_end_h` local, close it while it
is hotter outside. Dev: 201 hot requests, 201 replies before their due time, 176 setpoints
lowered, 0 rejected.

Quickstart: 114 signals, 72 proposals, 72 accepted, 0 violations; byte-identical on rerun.
Real lines from `examples/bus/s03.bus.jsonl` (shortened with `…`):

```json
{"row":"proposal","lane":"s03","id":"s03-heating_comfort-HP1-20260115T1900Z-prop02",
 "t":"2026-01-15T19:00Z","intent":"service_heat_pump","action":"dispatch_technician",
 "assets":["HP1"],"priority":"high","cause":"hp_degradation","resource_id":"TECH_1",
 "claims":[{"res":"technician","id":"TECH_1","from":"2026-01-16T07:00Z","min":1440}],
 "reason":"Service HP1 (COP 0.87 of reference); technician TECH_1 on shift from Fri 16 Jan 08:00 local.", …}
{"row":"signal","lane":"s03","id":"s03-heating_comfort-HP1-20260115T1900Z-sig73",
 "t_known":"2026-01-17T18:00Z","kind":"comfort_deficit","assets":["R406"],"cause":"hp_degradation",
 "detail":"guest of BK000213 reports cold in R406 (13.8 degC, window open) while HP1 is degraded",
 "facts":{"request":"GR000051","due":"2026-01-17T18:44Z","language":"en","window_open":1.0, …}, …}
{"row":"proposal","lane":"s03","id":"s03-heating_comfort-HP1-20260115T1900Z-prop40",
 "t":"2026-01-17T18:00Z","intent":"notify_guest_comfort","action":"notify_guest","assets":["R406"],
 "guest_request_ids":["GR000051"],"message":{"language":"en","text":"Thank you for your message
 about room R406. The window sensor shows an open window; please close it so the room can warm
 up. We are also checking the heating."}, …}
```

## The arbiter (`src/plato40/arbiter.py`)

`plato40 run` runs the pipeline, then every registered lane (s03-s08, or the ones given with
`--lanes`) through one arbiter call on one shared ledger, replacing the planner's decisions of
each lane's issue type; then the closed-loop follow-ups (`src/plato40/verify.py`) and, with
`--llm-cache`, the supervisor (`src/plato40/supervise.py`, ADR-003). `plato40 arbiter` does
the same for any lane files. Proposals are judged in time order, within one minute by
priority, then lane rank (table above: s02 safety first, s06 energy last), using only rows
known at `t`.
A proposal is rejected, with the reason in its verdict, when:

1. it breaks the bus contract or known-at, or `needs` a proposal that was not accepted;
2. its guest message breaks the LLM contract (numbers and IDs from its reason, banned words,
   hedge, the guest's language);
3. a claimed person is not on shift at the claim's start (roster as known at `t`), or a
   claim overlaps time already granted (the pipeline's own cleanings, robot passes and
   technician jobs are on the ledger first; an unclaimed `resource_id` holds implicitly);
4. it sends the technician to an asset with a dispatch still open (no repair completed since);
5. the policy forbids it, or the decision breaks the decision contract.

An accepted proposal is copied unchanged into a decision with `event_id` = proposal id; the
policy then sets `autonomy` and both flags. Example rejection: `TECH_1 not on shift at
2026-01-15T07:00Z (roster known at …)`.

## Known gaps in the examples (not the target)

- The exported S01 example still shows the planner's day plan, where `notify_day_delay` is one
  English line for many rooms. In `plato40 run` the s05 lane owns the day plan and sends one
  notice per late guest.
- The S02 example episode (R104 burst) has no guest message; R305 (from 20 Jan) shows `ask_entry_consent`.
- S01 and S02 still have no native lane: their examples are exported from the planner.
- S03 `flag_sensor_gap` is not emitted; a blank room gets `cause: sensor_gap` on its reply.
