# Trigger bus v1: how S01–S03 triggers talk to the arbiter

One append-only JSONL file (`bus.jsonl`). Triggers write **signals** and **proposals** to their
own lane, the arbiter writes **verdicts**, and only the arbiter writes `decisions.jsonl`.
Contract code: `src/plato40/bus.py`. LLM contract: `src/plato40/llm_guard.py`.
Golden examples, all generated from a real quickstart run: `examples/bus/s01.bus.jsonl`
(copy this one), `s02.bus.jsonl`, `s03.bus.jsonl` and the matching `*.decisions.jsonl`.

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
| `proposal` | trigger or LLM | `v lane id row t source episode_id signal_ids intent action assets priority evidence reason` | `resource_id guest_request_ids message claims cause confidence expected_outcome verification_plan` |
| `verdict` | arbiter only | `v lane id row t proposal_id accepted why` | `event_id` (when accepted) |

`source` is `rules` or `llm`. `claims` is a list of `{res, id, from, min}` with `res` in
`cleaner robot technician on_call room`: what the proposal needs from the shared ledger.
The arbiter grants claims in this order: safety, then a waiting guest, then a deadline, then
comfort, then logistics. A claim that does not fit is a rejected verdict, with `why`.

IDs: `<lane>-<episode without EP->-sigNN`, `-propNN`, and `<proposal id>-v` for the verdict.

## Enums per lane

`kind` = what the trigger saw; `intent` = why it proposes; `action` = the contract verb.
*Italic* = recommended, not emitted by the code at `97b00cc` yet.

| lane | issue_type | kind | intent | action | cause |
|---|---|---|---|---|---|
| **s01** | `room_turnaround` | `request checkin_due overstay checkout ready day_plan` | `clean_first robot_prepass promise_ready_time notify_ready_now notify_day_delay offer_alternative_room withdraw_offer escalate_waiting_guest request_extra_cleaning` | `schedule_cleaning dispatch_robot notify_guest reassign_room request_human_approval release_room defer_action` | none |
| **s02** | `water_leak` | `night_flow burst burst_flow night_gap night_blank` | `flag_suspected_leak inspect_room inspect_pipes ask_entry_consent block_room dispatch_plumber` *`flag_meter_artefact watch_next_night`* | `request_inspection dispatch_technician block_room notify_guest flag_anomaly request_human_approval defer_action` | *`room_leak pipe_leak guest_use cleaning_use meter_artefact meter_exchange unknown`* |
| **s03** | `heating_comfort` | `cop_low` *`comfort_deficit cold_forecast price_peak`* | `flag_hp_degradation protect_occupied_rooms service_heat_pump escalate_contractor notify_guest_comfort` *`preheat_before_cold shift_load_off_peak flag_sensor_gap`* | `adjust_room_setpoint optimize_energy dispatch_technician order_service notify_guest flag_anomaly request_human_approval defer_action` | *`hp_degradation cold_snap sensor_gap unknown`* |

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
  - no banned claims (`guarantee`, `confirmed`, `kostenlos`, …).
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

## How to add your trigger (S02, S03)

1. Emit `signal` rows: your lane, a `kind` from the table, `facts` with everything the planner
   and the message need, and evidence refs.
2. Emit `proposal` rows: one per action, `signal_ids` set, `intent` and `action` from your lane,
   and `claims` if you need staff, a robot or TECH_1. For `notify_guest`, use one proposal per
   guest, with the language from `guest_requests.csv` for that booking (known at `t`).
3. Never write `decisions.jsonl`, `autonomy` or the approval flags.
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

## Known gaps in the examples (current code at 97b00cc, not the target)

- S01 `notify_day_delay` and every S03 `notify_guest_comfort` are one English line for many rooms.
  The target is one proposal per guest, in the guest's language, before the request's `response_due_utc`.
- The S02 example episode (R104 burst) has no guest message; R305 (from 20 Jan) shows `ask_entry_consent`.
