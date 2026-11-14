# Team briefing: how Steward works, for the live demo

Read this once before the demo. Each section is written so that any of us can say it aloud
in two minutes. Numbers are from the committed dev run (`decisions.jsonl`, 2,989 decisions,
0 violations) unless marked quickstart. The demo click path is `docs/DEMO.md`.

Words we use: **Steward** is the product. **PLATO-40** is the building. PLATO and CLARA are
only named as the two targets of the feedback report. A **signal** is a fact a detector found
in the data. An **episode** is one incident, all its signals grouped. A **proposal** is what a
lane wants to do. A **decision** is a proposal the arbiter accepted and the policy stamped.

## 1. The decision pipeline in one breath

```
 data directory (15 files, replayed, read as known at the time)
      |
      v
 detectors ------> signals ------> episodes (one per incident, quiet 48 h closes it)
 (S01 S02 S03,                          |
  logistics, DQ)                        v
                                    planner: action chain per episode
                                        |
 lanes s03..s08 -----> proposals -------+-----> ARBITER (one ledger, one rank order)
 (one per domain,      on the bus              | contract, known-at, roster, IDs,
  replay hourly)                               | message guard, claims, needs
                                               v
                                        policy.apply: autonomy level, approval,
                                        consent (challenge section 7)
                                               |
                                               v
                                        verify: closed loop, follow-ups on a miss
                                               |
                                               v
                                        supervisor LLM: reviews, may add <= 2 checked
                                        flags or requests per review (ADR-003)
                                               |
                                               v
                                        finish: simulated label, notice priority cap
                                               |
                                               v
                                        check: contract + policy, raise on violation
                                               |
                                               v
                                        decisions.jsonl   trace.jsonl   supervisor.jsonl
```

What to say: one command, `plato40 run <data_dir>`, does all of this. Rules find and decide.
A policy stamps what a person must approve. A loop checks whether it worked. A language model
is allowed to word messages and to raise its hand, never to decide in place of a rule. The
file is checked before it is written; a violation stops the run.

Order matters, and here is why:

1. **Detectors before episodes.** Signals carry the time their evidence existed, so an
   episode starts when the building could have known, not when we ran the script.
2. **Planner and lanes before the arbiter.** The planner handles S01 and S02 and the
   data-quality flags. Lanes s03 to s08 replace the planner for their domain and all go to
   the arbiter together, so they share one cleaning ledger, one roster view and one rank
   order (water first, then turnaround, busy days, heating, filters, logistics, energy, guest
   desk).
3. **Policy after the arbiter.** A proposal never sets its own autonomy. The policy reads the
   action and its target (occupied room or not, technician or not) and sets the level.
4. **Verify after everything the rules did.** Each follow-up is a new decision at the time
   the check became possible, with only data known then, and it depends on the decision it
   checks.
5. **Supervisor last among the writers.** It sees the final rule decisions of the last 24 h and
   the context snapshot, so it reviews what we actually did.
6. **Finish and check.** The simulated label and the notice priority cap run after the
   supervisor, because its cache is keyed on the exact text it was shown. Then every row is
   checked against the contract and the policy.

## 2. The simulation: what is real, what is replayed, what is simulated

**The data is a replay.** PLATO-40 is a synthetic hotel year. Nothing we decide changes a
later reading. So:

- **Detection and decisions are real code on the given data.** Every number in a reason or
  an evidence reference can be found in the files.
- **Effects are simulated and labelled.** Every expected outcome ends with a simulated label.
  A proposed guest message is not a delivered message, and the reason text says so.
- **Outcomes are judged against what people wrote.** The closed loop and the insights read
  work orders, cleaning events and guest requests that the hotel staff recorded. "Followed by
  a repair work order" is a match in `work_orders.csv`, not an effect of our decision.

**Known-at discipline (ADR-002).** Every table has a column that says when a row became
known: readings at their timestamp, tariffs and forecasts at publication, bookings at
creation, roster absences at the time the sick note was known, work orders at creation.
Detectors, lanes, the arbiter and the context snapshot read only rows known at the decision
time. A blank reading is missing, never zero. The audit found that BMS alarms trail their
condition by about three quarters of an hour, so alarms are evidence beside our rules and
never the trigger.

**The shared ledger.** Cleaners, robots, the technician, the on-call housekeeper and rooms are
resources. A proposal that needs one makes a claim with a start and a duration. The arbiter
grants claims in the order safety, waiting guest, deadline, then lane rank, and only from the
roster as known at that time. An absent cleaner cannot be booked; the check at the end would
stop the run if it happened.

**The mock executor.** It lives in the demo page. A decision that needs a person shows Approve
and Reject buttons; the verdict is kept in the browser only, nothing is written back, and the
footer of the page says so. There is no second simulation of the building: the only building
model is the replay plus a simplified, uncalibrated thermal view used in the pre-heat rule,
and the report says it is uncalibrated.

**Determinism (ADR-001).** With the cache given, a run is byte-identical. `decisions.rerun.json`
records the hashes of the run and its rerun; the trust tile on the Year view reads that file
and never defaults to yes. Two live runs with the model on differ slightly in wording and in
what the supervisor adds; the committed run and its cache are the thing we reproduce.

## 3. Trigger catalogue

Every trigger Steward reacts to, lane by lane. A trigger is a **signal**: a condition on the
data, checked only with what was known at that moment. Signals that belong to one incident
form an **episode**, and the episode gets an ordered chain of **decisions**.

How to read the tables:

- **Condition**: when the signal fires. Each threshold is followed by its name in
  `src/plato40/settings.py` (`DetectorSettings`), or by the file it comes from.
- **Evidence**: what the signal cites, in the form `file:reference`.
- **What Steward does**: the decision chain in the order it is written.
- **Autonomy**: is set by the policy (`src/plato40/policy.py`), never by the lane. The policy
  has five rules:
  1. Entering an occupied room (cleaning, inspection, technician or contractor) needs
     approval and guest consent.
  2. A technician dispatch, a room block, a room move and a request to a manager always
     need approval.
  3. A service order needs approval, unless it is a waste pickup or a vending restock.
  4. Calling the on-call housekeeper needs approval.
  5. Everything else is autonomous.

  The rule text quoted in a row is the `rule` field of the trace.
- **Verified by**: the verification plan written on the decision, and the follow-up
  (section "Follow-ups" below) that fires when the check fails.
- **Not confused with**: the look-alike case this trigger is designed to leave alone.
- **Dev count**: from the committed dev run on main (`decisions.trace.jsonl`,
  `decisions.jsonl`). That run has 2,746 signals, 2,309 episodes and 2,989 decisions:
  - 2,817 autonomous;
  - 45 with human approval;
  - 127 with approval and guest consent.

  "Episodes" counts the episodes that this signal kind opened. A signal kind that only
  appears inside other episodes shows 0 episodes.

The LLM supervisor added 9 decisions on dev, separately from these triggers. They are left
out of the chains below.

### S01 Room turnaround (planner: `detect/turnaround.py`, `plan.py`)

Times are local (Europe/Vienna). Check-in is 15:00 and check-out 11:00 (`building.json`
operations).

| Signal | Condition | Evidence | What Steward does | Autonomy and why | Verified by | Not confused with | Dev count |
|---|---|---|---|---|---|---|---|
| `request` | **When:** a guest asks for early check-in or says they are waiting (a request, or an `arrival_waiting_room` event).<br>**Check:** the room's predicted ready time is later than the wanted time. The wanted time is the first time written in the request, else now. A room is ready `clean_room_min` = 60 min after a cleaner can start on it. | The guest request, the booking, the event that set the room's state. | Clean this room first, with a robot floor pass before the human clean, and tell the guest a ready time. The most common chain is reply, clean, robot, reply (27 of 46 episodes). If another clean room is free for the whole stay, offer it (`reassign_room`). If the guest is waiting, Steward calls the on-call housekeeper (on call 15:30-22:00 local, about `oncall_travel_min` = 45 min to arrive) when that beats the plan. If the room is only ready on a later day and nothing can be offered, Steward asks a manager. | Clean, robot and reply are autonomous by default. A room move is `reassign_room always needs approval` (8). A manager request is `request_human_approval always needs approval` (4). The on-call call is "costly": approval. | "No waiting_for_room request from the guest; escalate on one." Follow-ups: `ready_time_missed`, `ready_early`. | A request whose room will be ready in time gets an answer from S08 instead. The planner owns only real conflicts. | 70 signals, 46 episodes |
| `checkout` | The room a guest asked for is checked out within `checkout_follow_h` = 24 h of the request. Cleaning can be planned from this moment. | The checkout event. | Re-plans the clean from the actual checkout, then updates the guest. | As for `request`. | As for `request`. | A room still occupied at request time: it gets a plan only once the guest leaves. | 35 signals, 0 episodes (always inside a request episode) |
| `checkin_due` | **When:** a booked arrival reaches check-in time.<br>**Check:** its room cannot be clean the same day. No request is needed. | The booking, the event that set the room's state. | Clean the room at the first possible shift, offer a free clean room if there is one, and tell the guest the ready time. If the guest then waits, the steps of `request` apply. | Clean and reply are autonomous. A room move needs approval (2). A manager request needs approval (1). | "<room> shows cleaning_completed by <time>; if not, update the guest and escalate." Follow-ups as for `request`. | Every arrival whose room is clean in time: no signal. | 3 signals, 3 episodes |
| `ready` | A waiting guest's room has just become clean. | The waiting event, the event that set the room's state. | Tell the waiting guest the room is ready and self check-in works. | Autonomous by default. | "A check-in to <room> follows; escalate if the guest waits on." | A clean room with nobody waiting: no message. | 4 signals, 4 episodes |

### S02 Water leak (planner: `detect/water.py`, `plan.py`)

The night is 01:00-05:00 local (`NightWindow`).

| Signal | Condition | Evidence | What Steward does | Autonomy and why | Verified by | Not confused with | Dev count |
|---|---|---|---|---|---|---|---|
| `night_flow` | **When:** every 15-min interval of a room's night has at least `leak_floor_l_per_15min` = 0.2 L. A running toilet draws 0.9-1.1 L.<br>**Then:** night `leak_persist_nights` = 2 in a row: flag and inspect. Night `leak_technician_nights` = 3: repair. `leak_contractor_nights` = 2 more nights: an external plumber. | `room_water.csv:<room>:<night range>` | **Night 2:** flag (`flag_anomaly`), ask the guest for consent to enter (if occupied), inspect.<br>**Night 3:** the technician if on shift within `technician_horizon_h` = 24 h, else an external plumber (`order_service`).<br>**Later:** a plumber if the leak persists. | Flag and consent message are autonomous. Inspection and technician in an occupied room: "enters occupied <room>", approval and consent. Plumber: "contractor service", approval. | "Night minimum of the meter returns to 0 L after repair; escalate if flow continues another night or the night volume rises." Follow-ups: `repair_not_held` or `room_released`. | One night of flow is not a leak (R105 twice on dev: 2 episodes, no decision). A jittering meter is judged on hourly volumes, see `meter_jitter`. | 19 signals, 4 episodes (2 acted on) |
| `burst_flow` | A room draws at least `burst_flow_l_per_15min` = 10 L in each of `burst_flow_intervals` = 12 consecutive intervals (3 h), at any hour. | `room_water.csv:<room>:<range>` | Critical flag, block the room (out of sale), consent if occupied, inspect, repair. The technician only if on shift now, else a plumber. | Flag and inspection autonomous (vacant room). `block_room always needs approval`. Plumber: "contractor service", approval. | "Room stays blocked until the repair work order completes." Follow-up: `room_released`. | Heavy guest use holds at most 6 such intervals (dev). | 2 signals, 2 episodes |
| `burst` | A room's night volume reaches `burst_night_volume_l` = 250 L, with flow in every night interval so far. | `room_water.csv:<room>:<range>` | Joins the open burst episode (already handled by `burst_flow`). | As above. | As above. | A bath: up to 250 L in one interval between dry ones. | 2 signals, 0 episodes |
| `night_gap` | The median of the main meter minus all room meters reaches `gap_night_l_per_h` = 4 L/h over a night with at least `gap_min_coverage` = 0.75 of its intervals present. This points at pipes outside the rooms. | `water_main.csv`, `room_water.csv:all rooms` | Flag, the technician, then an external plumber. | Flag autonomous. `dispatch_technician always needs approval`. Plumber: "contractor service". | "Median night gap main minus rooms below 4 L/h after repair (the main meter also feeds common areas); escalate if it stays above another night." | Normal nights reach at most 2.0 L/h (dev). Gateway-outage nights give no verdict. | 8 signals, 1 episode |

### S03 Heating and comfort, including too hot (lane `lane_s03.py`, detector `detect/heating.py`)

| Signal | Condition | Evidence | What Steward does | Autonomy and why | Verified by | Not confused with | Dev count |
|---|---|---|---|---|---|---|---|
| `cop_low` | **Check:** every hour, HP1's COP over the last `cop_window_h` = 24 h is divided by the reference COP at that window's mean outdoor temperature. The reference is `cop_ref_intercept` = 2.78 + `cop_ref_slope` = 0.068 per °C, a fixed fit that is never refitted.<br>**Fires:** the ratio is below `cop_ratio_low` = 0.88 with at least `cop_min_coverage` = 0.8 of the window present. | `equipment.csv:HP1 heat_output_kw,electric_kw:<24 h>`, `weather.csv:outdoor_c:<24 h>` | Flag, then the in-house technician if on shift within `technician_horizon_h` = 24 h (else a contractor). Then a comfort-first energy plan: vacant rooms at setback, occupied rooms pre-heated within 20-23 °C, price peaks avoided only where comfort holds.<br>Each guest in a cold room is told once per episode, with notices at least `notice_gap_h` = 12 h apart. A contractor is ordered when the COP is still low `contractor_after_h` = 24 h after the technician's start.<br>Each of the 2 dev episodes had 1 flag, 1 technician, 1 contractor and 6 energy plans; the guest notices were 4 and 6. | Technician: `dispatch_technician always needs approval`. Contractor: "contractor service". The rest autonomous. | "COP ratio back above 0.88 within 24 h of the technician's start; a contractor is ordered if not." Follow-up: `service_not_restored`. | A cold snap alone: the reference already lowers the expected COP with the outdoor temperature. Normal hours stay above 0.95. | 126 signals, 2 episodes |
| `comfort_deficit` | **During a `cop_low` check:** an occupied room was in deficit in the last hour (at least `comfort_deficit_k` = 1.5 K below its setpoint, below 20 °C, window not reported open).<br>**Or:** a guest's cold complaint (`temperature_cold`). | The guest request, `room_telemetry.csv:<room> temperature_c,setpoint_c,window_open:<last hour>`, the booking | **Cold complaint:** raise the setpoint to the top of the band (23 °C) if the room is below the band and the window is not reported open, flag the room's heating if it is also below its setpoint, and reply to the guest. The chain is boost, flag, reply in 19 of 29 episodes, and boost, reply in 9. With the window open, the reply asks to close it.<br>**During a `cop_low` check:** a guest notice in the HP1 episode. | Autonomous by default. | "<room> at or above 20 degC within 2 h; otherwise inspect the room's heating (approval, guest consent)." (`room_heat_verify_h` = 2 h.) Follow-ups: `room_still_cold`, `repeat_complaint`. | A guest who chose a cool setpoint: the deficit counts only below their own setpoint. | 37 signals, 29 episodes |
| `comfort_excess` | A guest complains the room is too hot (`temperature_hot`). | The guest request, the room's last-hour readings | **Room above the band (23 °C) and setpoint above 20 °C:** lower the setpoint to 20 °C, which stops any heating, then reply.<br>The reply says the building has heating only and no cooling, and that an open window at night helps. Between `hot_day_start_h` = 8 and `hot_day_end_h` = 20 local, with the window open, it adds that closing the window keeps the heat out.<br>Lower setpoint and reply in 147 of 185 episodes; reply only in 23. | Autonomous by default. | "<room> setpoint at 20 degC at the next reading; the room cools only by ventilation, so a repeat request gets a reply, not a repair." | Never joins the HP1 episode: a hot room is not a heat pump fault. | 201 signals, 185 episodes |
| `cold_forecast` | **When:** HP1 is degraded at a forecast issue.<br>**Check:** the coldest hour of the next `cold_outlook_h` = 24 h is below `preheat_forecast_c` = -5 °C. | `weather_forecast.csv:issued <time>`, HP1 and outdoor windows | A pre-heat plan: occupied rooms towards 23 °C before the coldest hour (`optimize_energy`). | Autonomous by default. | "Occupied rooms at or above 20 degC through <hour>; escalate rooms that fall below." | A cold forecast with HP1 healthy: no signal. The normal setpoint rules cope. | 4 signals, 0 episodes (inside HP1 episodes) |
| `price_peak` | HP1 is degraded when tomorrow's tariff is published. | `tariff.csv:published <time>`, HP1 and outdoor windows | Shift HP1 load off the `price_peaks` = 3 dearest hours of the day just published, only where occupied rooms stay at or above 20 °C. | Autonomous by default. | "No occupied room below 20 degC in the shifted hours; otherwise heat at any price." | With HP1 healthy, S06 plans the battery instead; S03 does not shift load. | 6 signals, 0 episodes (inside HP1 episodes) |

### S04 AHU1 filter cascade (lane `lane_s04.py`)

The loading rate is the day median filter dp of the last 24 h minus that of the 24 h
before (`ahu_window_h` = 24). Day mode is 06:00-22:00 local by the clock
(`ahu_day_start_hour`, `ahu_day_end_hour`).

| Signal | Condition | Evidence | What Steward does | Autonomy and why | Verified by | Not confused with | Dev count |
|---|---|---|---|---|---|---|---|
| `dp_fast_rise` | **Check:** the loading rate reaches `ahu_rate_fast_pa_per_day` = 10 Pa per 24 h, which means the filter is clogging.<br>**When dp is blank:** the fan-implied dp is used instead, dp = `ahu_fan_dp_slope_pa_per_kw` = 119.2 × fan kW + `ahu_fan_dp_offset_pa` = -295.9. | `equipment.csv:AHU1 filter_dp_pa,fan_kw,airflow_m3h:<48 h>` | Flag, then a filter change. TECH_1 does it if a shift starts within `ahu_tech_horizon_h` = 24 h and S03 has no HP1 fault in the last `ahu_s03_guard_h` = 48 h; otherwise the contractor (`order_service`). An open AHU1 work order defers instead. The contractor follows `ahu_escalate_after_h` = 24 h after TECH_1's start with no reset. On dev both went to the contractor. | Flag autonomous. Contractor: "contractor service", approval. | "Day median dp below 120 Pa and fan at most 3.4 kW within 3 h of the service start; otherwise the contractor is ordered." (`ahu_reset_dp_pa`, `ahu_reset_fan_kw`, `ahu_reset_window_h`) | Normal loading is at most 3.6 Pa/day on dp; a sensor gap is not loading. | 2 signals, 2 episodes |
| `airflow_loss` | For a full day-mode hour the fan is at its ceiling (`ahu_fan_ceiling_kw` = 5.3 kW) and airflow is below `ahu_airflow_low_m3h` = 3900 m3/h. | Equipment window, AHU1 BMS alarms | A flag in the open loading episode; it opens one, with its service, if none is open. | Autonomous by default. | "Day airflow back above 3900 m3/h within 3 h of the service." | The normal day minimum is 4099 m3/h. | 2 signals, 0 episodes (inside clog episodes) |
| `dp_due` | The 24 h median day dp reaches `ahu_due_dp_pa` = 190 Pa with no episode open, so a preventive change is due. | Equipment window | Flag with the due date (`ahu_due_lead_d` = 7 days ahead), then TECH_1 at the start of his first shift on or after it. A contractor if there is none within `ahu_tech_search_d` = 14 days. A known open work order means `defer_action` instead. Chains: flag and technician (2), defer (1). | Technician: `dispatch_technician always needs approval`. | As for `dp_fast_rise`. | People ordered at 180 and 215 Pa; the airflow knee is at 245 Pa. | 3 signals, 3 episodes |
| `work_order_missed` | No reset `ahu_wo_grace_h` = 3 h after an AHU1 work order's scheduled time, so the order was postponed. | `work_orders.csv:<id>`, equipment window | Flag, then the preventive change is planned again (`defer_action` where an order is still open). | Autonomous by default. | "Reset at the work order's scheduled time; a flag follows 3 h after it with no reset." | A completed order resets dp exactly at its scheduled time. | 2 signals, 2 episodes |
| `sensor_gap` | An AHU1 dp or airflow reading, or an HP1 metric, is blank for `ahu_gap_h` = 4 h. | `equipment.csv:<asset> <metric>:<range>` | Flag. An inspection follows after `ahu_gap_inspect_h` = 24 h. A gap with loading or airflow-loss evidence joins the loading episode instead. | Autonomous by default. | "The series returns and, on return, dp is within 15 Pa of the fan-implied dp; an episode is opened if loading or airflow loss shows." | Every dev blank run is at least 2 h, so 4 h is a real gap, not a hiccup. | 5 signals, 5 episodes |

Two kinds are defined in `lane_s04.py` but do not fire on dev:
- `sensor_fault`: dp more than `ahu_fault_residual_pa` = 25 Pa from the fan-implied dp for `ahu_fault_h` = 2 h, or `ahu_stuck_intervals` = 8 identical readings.
- `bms_unmatched`: an AHU1 BMS alarm with no S04 episode in `ahu_alarm_quiet_h` = 24 h.

### S05 Busy days (lane `lane_s05.py`)

S01 owns one room's conflict and S05 owns the whole day. Bookings count from their creation
time, and cancelled bookings stay in, because the data has no cancellation time.

| Signal | Condition | Evidence | What Steward does | Autonomy and why | Verified by | Not confused with | Dev count |
|---|---|---|---|---|---|---|---|
| `surge_forecast` | **When:** at `day_plan_hour` = 7 local, for the day `surge_lookahead_d` = 7 days ahead.<br>**Event day:** at least `surge_occupancy` = 36 rooms booked for the night, and at least `surge_early_arrivals` = 11 arrivals booked `surge_early_days` = 14 days or more ahead.<br>**Fires:** that day, planned with one cleaner fewer than rostered, leaves an arrival room unready at check-in. | Bookings summary, `staff_roster.csv` cleaner shifts as known then | One manager request per weekend for a stand-by cleaner. Nobody is planned until it is approved. | `request_human_approval always needs approval`. | "Approval answered before <day>; on the day, every arrival room cleaning_completed before check-in." | A busy day booked late is not an event; event weekends are booked early. The two thresholds flag exactly the 3 dev event weekends. | 5 signals, 3 episodes |
| `day_plan` | **When:** at `day_plan_hour` = 7 local.<br>**Fires:** an arrival room is predicted ready after its due time (an early check-in time if one was asked, else 15:00). The plan cleans arrival rooms first, at `clean_room_min` = 60 min each, on the cleaner shifts known then, starting from `checkout_estimate_local` = 09:45 for rooms not yet free. | `staff_roster.csv` cleaner shifts, each arrival booking, the event that set each room's state | A cleaning order (arrival rooms first) and robot pre-passes for vacant arrival rooms. A manager request for extra cleaning help when an arrival room is not ready by check-in. One delay notice per late arrival guest who has no open request with S01. Over 11 episodes: 11 cleaning orders, 3 robot passes, 11 help requests, 39 guest notices. | Cleaning order, robots and notices autonomous. The help request: `request_human_approval always needs approval`. | "cleaning_completed in each arrival room by its predicted ready time + 30 min"; notices: "No arrival_waiting_room for <room> before check-in; escalate on one." | Robots shorten a clean but are not counted as capacity: no capacity is invented. | 11 signals, 11 episodes |

### S06 Energy, including the battery plan (lane `lane_s06.py`)

S06 claims no staff and no rooms and never messages guests.

| Signal | Condition | Evidence | What Steward does | Autonomy and why | Verified by | Not confused with | Dev count |
|---|---|---|---|---|---|---|---|
| `tariff_published` | Tomorrow's prices are first published and at least `battery_min_history_d` = 7 days of energy history exist. | `tariff.csv:<publication>`, `energy.csv:<now>`, `building.json:BAT1` | One battery day plan (`optimize_energy`): charge and discharge windows to the end of the published prices. It is a dynamic programme over BAT1's state of charge in `battery_soc_step_kwh` = 1 kWh steps. It works within the `building.json` limits: 10-95 kWh, 40 kW, round-trip efficiency 0.9. The PV and load forecast is the same-time-of-day mean of the last `battery_history_d` = 14 days. The saving is against no battery, simulated. | Autonomous by default. | "SOC follows the plan within 10 kWh at each window boundary; replan if import exceeds the plan by 20 %." These two values are written in the lane's text, not in `settings.py`. | Kept running while S03 handles a heat pump fault: the back-off pauses pre-heat and load shifting, not the battery. | 358 signals, 358 episodes |
| `arrival_due` | **Check:** `preheat_lead_h` = 3 h before a booked arrival.<br>**Fires:** the room is vacant, has a reading, its window is closed, it is below 20 °C, and HP1 is running. | Each arrival booking, the room readings, HP1 status | Raise the setpoint to `preheat_setpoint_c` = 21 °C. Rooms due at the same time share one decision. | Autonomous by default. | "Each room at or above 20 degC at check-in and no temperature_cold request in the first 2 h of the stay." | Skipped while S03 is active (`s03_backoff_h` = 24 h), so pre-heat never competes with a degraded heat pump. | 256 signals, 256 episodes |
| `s03_active` | The first HP1 `cop_low` verdict of an S03 episode. | HP1 and outdoor windows | Pause heat pump load shifting and arrival pre-heat (`defer_action`). The battery continues. | Autonomous by default. | "No cop_low verdict for 24 h before pre-heat resumes." | Later `cop_low` checks in the same episode do not repeat the pause. | 2 signals, 2 episodes |

One kind is defined but does not fire on dev: `input_stale`. It fires when tomorrow's tariff
is not published by `tariff_due_local_h` = 14 local, when the forecast is older than
`forecast_stale_h` = 30 h at 07:00, or when an `energy.csv` row breaks the balance or the BAT1
limits beyond `energy_balance_tol_kw` = 1 kW.

### S07 Logistics (lane `lane_s07.py`)

Service times come from `building.json`:
- the vending restock is Mon and Thu 10:00 local;
- the waste pickup is Tue and Fri 07:00 local;
- an extra service has about 24 h lead time.

| Signal | Condition | Evidence | What Steward does | Autonomy and why | Verified by | Not confused with | Dev count |
|---|---|---|---|---|---|---|---|
| `drinks_low` | **Projection:** the use rate over the last `logistics_rate_h` = 96 h projects the drinks running out before the next regular restock. Hours at zero stock and at the cap are left out of the rate.<br>**Fires:** an extra restock ordered now still arrives first, and the run-out is at most lead + `logistics_buffer_h` = 12 h away (`logistics_event_buffer_h` = 18 h on an event weekend). | `logistics.csv:VEND1/stock_drinks:<96 h>` | Order one extra restock per window (`order_service`). | Autonomous: a vending restock is named autonomous in the challenge, so the contractor rule does not apply. | "A vending_restocked event by <time> and no vending_out_of_stock before it; otherwise escalate." | A stock-out the regular restock covers in time: no order. The setting's note: on dev, 21 of 23 shortages ordered 20 h or more ahead, 1 false order. | 24 signals, 24 episodes |
| `container_overflow_risk` | The central container, simulated to the next regular pickup, reaches `logistics_cap_pct` = 100 % at the daily floor bin dump (`logistics_dump_local_h` = 14.5, i.e. 14:30 local). It fills in steps at the dump, not linearly. | `logistics.csv:WASTE_MAIN/fill_pct,WASTE_F1..F4/fill_pct:<24 h>`, departures known then | First choice: at `logistics_hold_local_h` = 13:30 local on the overflow day, hold the dump of the floor bins that would overflow it. Then, after the pickup, a robot carries them (from `logistics_robot_start_local_h` = 07:15, `logistics_bin_run_min` = 20 min per bin). If a held bin would pass `logistics_held_max_pct` = 90 %, the hold is unsafe and an extra pickup is ordered instead. Dev: hold and robot runs twice, extra pickup once. | Autonomous: waste pickup is named autonomous; robots enter no room. | "Held bins stay below 90 %, WASTE_MAIN stays below 100 % until the pickup, and the bins drop to 0 after the bin-run." | Floor bins are emptied daily and never overflow; only the central container is watched. | 3 signals, 3 episodes |

Defined but not firing on dev:
- `snacks_low` (as `drinks_low`, for snacks);
- `sensor_gap` and `sensor_jump` for logistics series. They cover a blank run of
  `logistics_gap_h` = 3 h, an hourly step of `logistics_jump_pp` = 2 with no event to explain
  it, or a value flat for `logistics_stuck_h` = 48 h.

### S08 Guest desk (lane `lane_s08.py`)

Every guest request that no other lane answers gets exactly one reply. That reply is a
`notify_guest` whose message is checked by `llm_guard` (cached model answer, else template),
plus the staff action its type asks for.
- S03 answers the requests for hot and cold.
- The S01 planner answers the turnaround requests it cites.

Each signal's evidence is `guest_requests.csv:<request id>`, and it fires at the request time,
before the request's due time. Each reply's verification plan is "No repeat of the same
request from the booking before it is due". A staff action's plan is "A staff visit or work
order for the room before the due time". A cleaning outside the cleaner shifts says it
happens "in the first cleaner shift".

| Request type | What Steward does | Autonomy and why | Dev count |
|---|---|---|---|
| `early_checkin` | Reply. "Should be ready" only if the room is clean now, no departure from it is still due before the arrival, and S05 has not told the guest it may be late. Otherwise "a message follows". No time is quoted that the rules did not plan. | Autonomous by default. | 311 |
| `late_checkout` | Reply. Offered pending staff approval if the room has no arrival that day; otherwise luggage storage is offered. Never a promise. | Autonomous by default. | 271 |
| `noise_complaint` | Reply: staff look into the noise. | Autonomous by default. | 173 |
| `wifi_help` | Reply: staff look into the WiFi and reply. No password is invented. | Autonomous by default. | 140 |
| `extra_towels` | Reply: housekeeping brings fresh towels. | Autonomous by default. | 112 |
| `arrival_delay` | Reply: reception notes the late arrival. | Autonomous by default. | 87 |
| `parking` | Reply: reception replies with the parking details. | Autonomous by default. | 57 |
| `maintenance_issue` | Reply, then a repair order (`order_service`). | Reply autonomous. Repair: "enters occupied <room>; contractor service", approval and consent (all 50). | 50 |
| `cleaning_request` | Reply, then a cleaning (`schedule_cleaning`). Outside the shifts it says from when. | Reply autonomous. Cleaning: "enters occupied <room>", approval and consent (all 44). | 44 |
| `luggage_storage` | Reply: reception explains luggage storage. | Autonomous by default. | 44 |
| `invoice` | Reply: reception prepares the invoice and replies. | Autonomous by default. | 19 |
| `access_issue` | Reply, then an inspection. | Reply autonomous. Inspection: "enters occupied <room>", approval and consent. | 17 |
| `air_quality` | Reply, then an inspection of the air in the room. | As `access_issue`. | 4 |
| `waiting_for_room` | Reply: staff check the room and message the guest. A waiting guest whose room is late is the S01 planner's (`request`). | Autonomous by default. | 2 |
| `water_issue` | Reply, then an inspection for a running toilet or leak. | As `access_issue`. | 1 |

S08 total on dev: 1,332 requests. Each one got a reply, plus 116 staff actions: 50 repairs, 44
cleanings and 22 inspections.

What it is kept apart from: a request is never answered twice. S08 skips the types S03 owns,
and the requests the planner already cites.

### DQ Data quality (`detect/data_quality.py`, planned in `plan.py`)

| Signal | Condition | Evidence | What Steward does | Autonomy and why | Verified by | Not confused with | Dev count |
|---|---|---|---|---|---|---|---|
| `floor_outage` | All rooms of one floor are blank in room telemetry for `outage_min_intervals` = 2 consecutive intervals (the floor's gateway). | `room_telemetry.csv:<gateway> rooms:<range>` | Low flag: "Telemetry outage". The floor's rooms get no leak or comfort verdict while blank. | Autonomous by default. | "Readings from the floor resume; escalate if blank for 24 h." | One room blank: see `sensor_dead`. | 14 signals, 14 episodes |
| `sensor_dead` | One room's temperature is blank for `sensor_dead_h` = 6 h while the rest of its floor reports. | `room_telemetry.csv:<room> temperature_c:<range>` | Low flag: "Room temperature sensor dead". The room gets no comfort verdict or pre-heat while blank. | Autonomous by default. | "Readings from the room resume; inspect the sensor if not within 24 h." | A floor outage (all rooms blank). Short dropouts: dev has 19 one-room blank runs of 1 h or more, and 18 of them last 6 h or more. | 18 signals, 17 episodes |
| `meter_jitter` | A room water meter steps back (a negative step) in a night window, which happens after a meter exchange. Readings that drop by more than `meter_reset_drop_l` = 1000 L are resets, not jitter. | `room_water.csv:<room>:<night>` | Low flag: "Water meter jitter", once per run of jittering nights. Leak checks on that meter use hourly volumes while it jitters. | Autonomous by default. | "Meter readings rise monotonically again; check the meter if not." | A slow leak: the jitter cancels within an hour, so the hourly volume shows whether water really flows. | 192 signals, 2 episodes |
| `undeclared_id` | A room or asset ID appears in the data but not in `building.json`. | `<file>:<id> first row <time>`, `building.json:building` | Low flag on BUILDING: Steward makes no decision on that ID. | Autonomous by default. | "The ID is declared or its rows stop." | | 0 (none in dev or quickstart) |

### Follow-ups (`verify.py`)

A follow-up is a new decision made when a decision's check becomes possible. It uses only
data known then, and depends on the decision it checks. The kind names are those of
`kpi.FOLLOW_UP_KINDS`.

| Follow-up | When it fires | What Steward does | Autonomy | Dev count |
|---|---|---|---|---|
| `ready_early` | A guest holds a ready time, and the room's first `cleaning_completed` after that promise arrives. The guest has not checked in and has been told nothing since. | Tell the guest the room is ready now, "before the promised <time>" if it is `promise_grace_min` = 15 min or more early. | Autonomous by default. | 30 |
| `ready_time_missed` | `promise_grace_min` = 15 min after a promised ready time, the room is neither clean nor occupied, and nothing was said to it since. | Tell the guest the room is late; staff are on it and a new time follows. | Autonomous by default. | 12 |
| `room_still_cold` | `room_heat_verify_h` = 2 h after a setpoint boost, the occupied room is still below 20 °C. | Request an inspection of the room's heating. | "enters occupied <room>": approval and consent. | 7 |
| `room_released` | A leak repair's work order completed, no leak signal followed for `repair_verify_h` = 24 h, and the blocked room is clean. | Release the room (`release_room`). | Autonomous: the policy forbids release only when the room is not clean. | 2 |
| `repair_not_held` | A leak signal within `repair_verify_h` = 24 h after the repair completed. | Ask a manager to decide on a second repair (critical). | `request_human_approval always needs approval`. | 0 |
| `service_not_restored` | A `cop_low` verdict in the hour after the COP window (`cop_window_h` = 24 h) following the HP1 service. | Ask a manager to decide on a second contractor visit. | Approval. | 0 |
| `repeat_complaint` | A guest's second cold complaint in a stay (a third one is critical). | Ask a manager to decide on a staff visit or a warmer room. | Approval. | 0 |

The LLM supervisor (`supervise.py`) is not a trigger. It reviews the rules' decisions once a
day and at cross-domain hotspots, and adds at most 2 checked decisions per review. On dev it
added 9: 8 flags and 1 inspection.

## 4. Arbiter and policy: how a proposal becomes a decision

A lane writes a **signal** (what it saw, with evidence references in the form
`file.csv:ID`) and a **proposal** (what it wants to do, which signals it answers, which
resources it claims, which other proposals it needs). The arbiter does, in this order:

1. **Contract.** Required fields, enums, IDs only from `building.json`, times in UTC.
2. **Known-at.** The proposal cites only signals known at or before its own time.
3. **Dependencies.** If a proposal it needs was rejected, it is rejected too, and the
   accepted needs become the decision's `dependencies` field.
4. **Roster and claims.** The claimed cleaner, robot or technician is on a believed shift and
   not already claimed; the on-call housekeeper counts as costly.
5. **Message guard.** A guest message is checked against the proposal's own reason: every
   number, date and ID in it must appear in the reason, no banned claim, a hedge word stays
   where the reason says expected, and the language matches the guest's requests.
6. **Copy unchanged.** An accepted proposal is copied into the decision file as it was
   written; a verdict row records why each proposal was accepted or rejected.

Then **policy.apply** (challenge section 7) sets the level from the action and its target:

| Level | Examples in our run | Rule |
|---|---|---|
| Autonomous | cleaning in a vacant room, robot pass, guest notice, setpoint in a vacant room or inside the band, battery plan, restock order, flag | may run directly, logged, explainable |
| Human approval | technician, contractor, block a room, move a booked guest, on-call housekeeper | `requires_human_approval` |
| Approval and consent | entering an occupied room for cleaning, inspection or repair | both flags; consent is needed, not granted |
| Forbidden | robot into an occupied room, release of an uncleaned room, water shutoff | the run stops |

What to say: the policy is a function of the decision and the room state at that time, not a
field a lane fills in. That is why no lane, and no language model, can grant itself autonomy.

On dev, 2,817 of 2,989 decisions were autonomous; 172 asked a person, 127 of those also for
guest consent, and 116 of the approvals are guest-desk consent requests, so the manager's own
load is 56 in a year.

## 5. Supervisor and language model

**The one sentence.** The rules decide every decision; a language model is used twice on top,
both times under a contract that checks its answer against the data, and both times cached so
that a rerun without the network is byte-identical.

### The two uses

| Use | Model | Prompt version | Code |
|---|---|---|---|
| Guest messages in the guest's language | `claude-haiku-5-5` | `guest_message/4`, tool forced | `llm_guard.py`, `messages.py` |
| Supervisor review of the rules' decisions | `claude-sonnet-5-5` | `supervisor/2`, tool `auto`, one retry | `supervise.py`, `context.py` |

Neither model takes a temperature parameter, so determinism comes from the cache, not from
sampling settings. With `PLATO40_LLM=off`, the default, nothing is called: a cache miss uses
the rules' template text (messages) or adds nothing (supervisor).

### Guest-message guard

- **What it may change:** only the words. The rules hand it a map of every number, time, date
  and ID the text may contain, already written out ("14:30", "R108"). It writes one message of
  at most 400 characters in the guest's language (de, en, fr, it, nl, es).
- **What it may not change:** any number, time, date or ID that is not in that map, and any
  value the rules marked as one it must say. It may not use a banned claim in any of the six
  languages ("guarantee", "confirmed", "free of charge" and their translations), and an
  estimate must keep its hedge word ("expected", "voraussichtlich", "devrait", ...).
- **On any failure:** the rules' template text goes out instead, and the answer is written to
  `llm_cache/rejected/` and never reused.
- **The committed cache:** 1,749 checked answers (`claude-haiku-5-5`, `guest_message/4`) and 47
  rejected ones. All 47 were rejected for a banned claim: "confirm" 30, "bestätigt" 5, "free of
  charge" 4, "kostenlos" 4, "gratuit" 2, "bevestig" 1. Without the guard, those messages
  would have promised a room or a free service that only a person can grant.
- **Cache key:** a hash of model, prompt version, system prompt and the full task. Change any
  of them and the answer is asked again; nothing stale is reused.

### The supervisor

- **When it looks (review points):**
  - **daily** at 07:00 local, over the building's last 24 h;
  - **hotspot** when one asset got decisions of two or more issue types within 24 h, at the
    later decision, once per asset and local day. Energy pre-heat never makes a hotspot,
    because it touches most rooms every day.
- **What it sees:** the decisions of the last 24 h known at that time, at most 30, most urgent
  first, each reason cut to 160 characters; and a context snapshot built at the same time
  (`context.py`):
  - building: HP1 status and COP ratio, AHU1 airflow, fan power and filter pressure, the main
    water meter, energy (load, PV, battery, price now and today), the weather forecast,
    who is on shift, arrivals and departures in the next 6 h, open work orders, room
    temperatures across all 40 rooms (median, min, max, rooms below 20 and above 26 degC);
  - up to 8 assets named in the decisions: state and since when, temperature, setpoint, window,
    night water, open work orders;
  - what moved: readings that changed beyond a threshold in 15 min, 1 h or 24 h;
  - the evidence references it may cite.
  Across the 963 cached reviews, the input is a median of 10,244 characters (smallest 3,094,
  largest 19,872).
- **What it may add:** at most 2 decisions per review, and only `flag_anomaly`,
  `request_inspection`, `request_human_approval` or `defer_action`. It never changes or removes
  a rule's decision. The policy, not the model, sets autonomy and the approval and consent
  flags of what it adds.
- **The seven checks** (`supervise.py`, `_problems`). An answer is used only if:
  1. it is the review tool's shape, `{assessment, add}`, with at most 2 adds and the exact fields;
  2. each action is one of the 4 allowed;
  3. each priority is one of critical, high, medium, low;
  4. each asset exists in the building and was shown to it;
  5. each add names, in `about`, decisions it was shown;
  6. each evidence reference is copied exactly from the references it was shown;
  7. every number and ID in its reason and check appears in its input.
  Then the decision contract and the autonomy policy check each added decision like any other.
  A failing answer adds nothing and goes to `llm_cache/supervisor/rejected/`.
- **Cache:** keyed on a hash of model, prompt version, system prompt, tool schema and the full
  input. A rerun from the cache reproduces every review.
- **The log:** `decisions.supervisor.jsonl`, one row per review: kind, time, asset, source
  (`llm`, `cache` or `none`), the model's assessment, and what it added or why it added nothing.
- **Dev numbers (`decisions.supervisor.jsonl`):** 492 reviews (364 daily, 128 hotspot). 482
  answered, 10 had no answer (the call failed twice without a tool answer), 0 answers rejected
  by the checks. 473 answered reviews added nothing. 9 decisions added: 8 `flag_anomaly`, 1
  `request_inspection`, all autonomous by the policy. By lane: 6 guest desk, 1 heating comfort,
  1 energy, 1 ventilation.

### One real example, to read aloud

`SV-D-BUILDING-20260720T0500Z-01`, the daily review of 20 Jul at 07:00 local. Its assessment:

> The rules handled each hot-room complaint on its own: they lowered the setpoint and told the
> guest. They did not connect these into one building-wide heat event. All 40 rooms are above
> 26 degC, and the building has no cooling.

The decision it added (`flag_anomaly`, medium, autonomous), citing six rule decisions in
`dependencies`:

> Building-wide heat, not separate room faults: 40 of 40 rooms are above 26 degC, with a median
> of 29.27 and a maximum of 31.47. The rules answered nine complaints one room at a time by
> lowering setpoints to 20 degC. The building has heating only, so those changes cannot cool
> anything. The forecast mean for the next 24 h is 29.75 degC (min 21.39 at Tue 21 Jul 03:00
> local), so the heat will continue. Staff should review guest heat comfort for the whole
> building, especially the highest rooms (R306 at 30.97 degC), instead of handling each
> complaint separately.

Say: every number in it was in the snapshot it was given, or the check would have dropped it.
It is a pattern no single trigger sees, and it feeds the PLATO recommendation "plan for summer
heat" in `insights.md`. The second example, if asked: `SV-D-BUILDING-20261019T0500Z-01` asks
for an inspection of AHU1, because the airflow loss the filter lane flagged is still there, the
filter pressure sensor now reads null, and two preventive work orders are long overdue.

## 6. Holdout and what it proves

The holdout is a third dataset of the same shape, released after the code was tagged. The
runbook (`docs/HOLDOUT.md`) is three commands: verify the checksum, extract, run
`scripts/holdout.sh <dir>`. The script runs once with the model on, reruns from the cache
with it off, requires the two files to be byte-identical, validates, and builds the page.
What it proves: the rules were not tuned to the dev file, the thresholds carry over, and the
run is reproducible. What it does not prove: that every incident in the holdout was found;
the organisers' automated check counts that, and we say so when asked.

## 7. Questions the jury will ask

1. **Why rules and not an agent?** The rubric rewards grounded, safe, reproducible decisions.
   Rules give one answer per input, enforce known-at and the autonomy policy in code, and ran
   on the holdout unchanged. The language model is used where language is the job (messages)
   and where a pattern spans domains (the supervisor), and even there it can only add a flag
   or a request, under seven checks.
2. **Where could the model hallucinate, and why can it not reach the file?** In wording and in
   the supervisor's reasons. A message number or ID not given by the rules, a banned promise or
   a lost hedge sends the template instead (47 such answers in the cache). A supervisor add with
   an unknown asset, an uncited decision, an evidence reference not copied from its input, or a
   number not in its input is dropped (0 in the dev run). The policy sets every autonomy flag.
3. **What is simulated?** Every effect of every action. The data is a replay: no decision
   changes a later reading. Outcomes are judged against events and work orders that people
   wrote, never against our own actions. Approve and Reject on the page are a mock executor
   kept in the browser.
4. **How does the holdout prove generalisation?** The code was tagged `pre-holdout` before the
   release and run unchanged with one command, `scripts/holdout.sh <dir>`; the README lists
   changes after the tag (none at aeb08f8). [After the run: decisions, violations and the rerun
   hash from `decisions_holdout.rerun.json`.]
5. **Do you spam alerts?** Repeated signals of one incident share one episode and one decision
   chain. The dev year has 178 incidents against 2,129 routine episodes (guest replies,
   pre-heats, battery plans), and the console separates them. 42 decisions in the year are
   critical. The supervisor added 9 decisions in 492 reviews.
6. **False alarms?** Steward acts on persistence and known-at. In dev: 2 water episodes got no
   decision (one night of flow, on the meter that jitters after its exchange); 19 of 159
   comm-loss alarms had no gap behind them, and Steward raised no outage for them. Leaks: 5 of
   5 incidents were followed by a repair work order.
7. **What did you get wrong?** From the Limits panel: 3 of 24 waiting guests were not
   anticipated that morning; 7 of 24 were told a ready time after they had arrived; 463 of
   1,716 pre-heated rooms were still below the band at check-in; 4 of 25 extra orders found no
   shortage. The heat pump and leak rules rest on 2 and 5 incidents.
8. **How would you deploy it in a real PLATO building?** First in shadow mode: same pipeline,
   real feeds with their known-at times, decisions as recommendations next to the staff. Then
   connect the executor to the real systems one action at a time (BMS setpoints, robot tasks,
   orders), keep the approval inbox for anything with cost or a person entering a room, and
   regenerate the insights each season. The target architecture is in `insights.md`.
9. **The autonomy rules, by example?** From Day B: on its own, Steward flags the burst and sends
   an inspection into the empty room. Blocking R201 needs a manager (always), and so does the
   plumber (a contractor). Inspecting an occupied room needs approval and the guest's consent
   (Day A, R103). Never allowed: a robot into an occupied room, releasing a room that is not
   clean. In dev: 2,817 decisions autonomous, 45 approval, 127 approval and consent.
10. **What about costs and savings?** The data's only costs are electricity prices. The battery
    plan saves a median EUR 17.68 per plan (358 plans) against no battery, simulated at the
    published prices; no other saving is claimed. "Notable cost" in the autonomy rules is a
    fixed list of actions, not an amount.
11. **Is it deterministic?** Yes. A rerun from the cache gave the same `decisions.jsonl`, byte
    for byte (`decisions.rerun.json`, sha256 4344f8f2...). Two live calls can differ, so the
    cache, not the model, is the record.
12. **What is known-at?** Every file has one availability column (tariff published, forecast
    issued, roster known, bookings and work orders created). A decision at time t reads only
    rows known by t, and `plato40 validate` rejects one citing a later request or decision. A
    blank is missing, never zero.
13. **What do the three demo days show?** 25 Jan: a weak heat pump in a cold snap; comfort
    first, a technician by approval, the energy lane backs off. 20 Mar: a burst in an empty room
    after midnight; flag, inspect, block and repair by approval, and a blank floor that gets no
    verdict. 9 May: a sold-out Saturday with a cleaner off sick; the morning plan, extra help by
    approval, honest notices, and a promise that slipped.
14. **How fast does it answer guests?** 1,612 of 1,612 guest requests were answered by a
    decision that cites the request, all before their due time.
15. **How often is a manager disturbed?** 172 approvals in the year, 116 of them guest-desk
    consent requests, so the manager's own load is 56, about 1.1 a week.
16. **Why trust the supervisor's 9 additions?** Each names the decisions it follows up, cites
    references copied from its input, and passed the same contract and policy as the rules.
    Two of them (R202, R410) caught our own guest desk telling a guest the room was not ready
    when the ready notice (R202) or the room's state (R410) said otherwise: a real
    inconsistency the rules did not see.
17. **What if the API is down?** The run reads the cache; a miss uses the template for messages
    and adds nothing for the supervisor. The output stays valid. The 10 failed reviews in dev
    are exactly this case.
18. **What is the feedback to CLARA and PLATO?** Twelve recommendations in `insights.md` (7 for
    CLARA, 5 for PLATO), each with evidence and uncertainty; for example, move on-call help before check-in (22 of 24
    waits began before the 15:30 on-call window), and plan for summer heat (201 "too hot"
    requests against 29 "too cold").
19. **What would you build next?** Measured COP and common-area water submeters (the data
    gaps the report names), the real executor behind the approval inbox, and a season in
    shadow mode to tune thresholds on real data.
20. **Is any of this a real PLATO building?** No. PLATO-40 is the challenge's fictional
    property and the data is synthetic; Steward is our product, not a Rhomberg one.

## 8. Roles and rehearsal

Two roles, as in `docs/DEMO.md`: the **speaker** says the lines, the **driver** opens the links
and uses only the keys (`Y D E T`, arrow keys). [Names to fill.]

| DEMO beats | Driver | Speaker |
|---|---|---|
| 1-2 | Year view, then point at the five tiles | the hook, the ten-second read |
| 3-8 (Day A) | Day 25 Jan, steps 2, 3, 9, 11-16, then the HP1 trail | cold room, inspection, heat pump flag, back-off, trail, limit |
| 9-13 (Day B) | Day 20 Mar, steps 0, 1-4, the R201 trail, step 6 | burst, approvals, release, blank floor, limit |
| 14-19 (Day C) | Day 9 May, step 1, the stand-by trail, steps 2-10, 13-16, 20, the R209 trail | morning plan, stand-by, notices, early check-in, slipped promise, limit |
| 20-22 | Year view, then the `insights.md` tab, then Year again | Limits, learning loop, trust |

**Three-minute cut** if time is short: beat 1 and 2 (20 s); Day A beats 3, 6 and 7 (45 s); Day
B beats 9 to 11 (40 s); Day C beats 14, 17 and 19 (45 s); beats 20 and 22 (30 s). Skip
`insights.md` and say "the feedback report is in the repository".

**Ten-minute rehearsal checklist:**

1. Open `decisions.html` from disk (no server, no network), full screen, at `#view=year`.
2. Run `uv run python scripts/demo_steps.py decisions.html` and compare the step numbers with
   the DEMO table; fix the table, not the page.
3. Click every link in the DEMO table once: each Day step lands on the named event, and each
   trail shows its decision pinned at the top of the list.
4. Second tab: `insights.md` rendered (GitHub or an editor preview), scrolled to the CLARA
   recommendations.
5. Third tab or file viewer: `docs/demo/`, the 13 screenshots in beat order.
6. The demo video ready to play, sound off.
7. Read the example in section A aloud once, against the clock.
8. One full run of the three-minute cut, timed; then the speaker answers questions 1, 2, 7
   and 10 aloud.
9. Check the browser zoom (100 %) and that no other tab or notification can pop up.
10. Agree the hand signal for "switch to the screenshot".
