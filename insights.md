# Steward insights: what a year of PLATO-40 teaches

Steward ran over the `dev` dataset (1 Jan 2026 to 31 Dec 2026, 365 days, 40 rooms) with 0
contract or autonomy violations. Every number below is computed from that run and the dataset
tables by `uv run python scripts/insights.py <data_dir> --out insights.md`. PLATO-40 is
fictional and the data synthetic: these are findings about the replay, and every effect of an
action is simulated.

## The year at a glance

Steward turned 2,728 detector signals into 163 incidents, 2,127 routine guest replies, pre-heats
and battery plans, and 2,942 decisions. 2,770 decisions were autonomous. A person was asked to
approve 172, on average every 2.1 days, and 127 of those also need guest consent. 116 of the
approvals are guest-desk consent requests (maintenance, cleaning), so the manager's own load is
56.

| Lane | Incidents | Decisions | Autonomous | Approval | Approval and consent | Outcome |
|---|---|---|---|---|---|---|
| Room turnaround | 53 | 224 | 209 | 15 | 0 | 21 of 24 waiting guests had a decision on their room earlier that day |
| Water leaks | 5 | 23 | 11 | 8 | 4 | 5 of 5 followed by a repair work order |
| Heat pump and comfort | 31 | 112 | 101 | 4 | 7 | 2 of 2 heat pump incidents followed by repair of HP1; 29 of 29 cold-room requests answered before their due time |
| Comfort requests (too hot) | 185 | 377 | 377 | 0 | 0 | 201 of 201 too-hot requests answered, 201 before their due time |
| Guest desk | 1,331 | 1,454 | 1,338 | 0 | 116 | 1,612 of 1,612 guest requests (all lanes) answered before their due time; 116 got a staff action |
| Waste and vending | 27 | 31 | 31 | 0 | 0 | 21 of 25 orders came before a real shortage |
| Data quality | 16 | 16 | 16 | 0 | 0 | 14 floor outages, 11 of them on GW_F3 |
| Ventilation (AHU filter) | 12 | 19 | 15 | 4 | 0 | 5 of 12 filter incidents followed by an AHU1 work order |
| Busy days and event weekends | 14 | 67 | 53 | 14 | 0 | 5 of 11 days flagged at the morning plan had a waiting guest |
| Energy (pre-heat, battery plan) | 616 | 619 | 619 | 0 | 0 | 1,253 of 1,716 pre-heated rooms at or above 20 degC at check-in |

## Guest requests

1,612 of the 1,612 guest requests were answered by a decision that cites the request, 1,612 of
them before the request's due time.

| Request type | Requests | Answered | Before due |
|---|---|---|---|
| early_checkin | 349 | 349 | 349 |
| late_checkout | 271 | 271 | 271 |
| temperature_hot | 201 | 201 | 201 |
| noise_complaint | 173 | 173 | 173 |
| wifi_help | 140 | 140 | 140 |
| extra_towels | 112 | 112 | 112 |
| arrival_delay | 87 | 87 | 87 |
| parking | 57 | 57 | 57 |
| maintenance_issue | 50 | 50 | 50 |
| cleaning_request | 44 | 44 | 44 |
| luggage_storage | 44 | 44 | 44 |
| temperature_cold | 29 | 29 | 29 |
| invoice | 19 | 19 | 19 |
| access_issue | 17 | 17 | 17 |
| waiting_for_room | 14 | 14 | 14 |
| air_quality | 4 | 4 | 4 |
| water_issue | 1 | 1 | 1 |

## Supervisor review

A supervisor LLM (claude-sonnet-5-5, ADR-003) reviewed the rules' decisions at 488 review points
(364 daily and 124 hotspot). It answered at 481 of them; 7 had no answer, all because the call
failed. The checks rejected 0 answers. It added 9 decisions (the policy set 9 autonomous) and
changed or removed none. By action: 9 flag_anomaly; by lane: 6 guest desk and 3 energy
(pre-heat, battery plan). For example, `SV-D-BUILDING-20260315T0600Z-01`: "R405 reported noise
at 22:47Z and again at 23:02Z. The first reply did not settle it, and the rules only sent a
second notify_guest. A repeat noise complaint within 15 minutes points to an ongoing disturbance
that a..." And `SV-D-BUILDING-20260323T0600Z-01`: "Three noise complaints from R308 (23:01Z),
R304 (23:44Z) and R108 (00:59Z) arrived within about two hours. The rules treated each as a
separate guest reply. R304 and R308 are both on floor 3, and R303 on the same..."

## Operating recommendations for PLATO-40 next year (feedback to CLARA)

**Turnovers: act at the morning plan, and move on-call help to the hours when guests wait.** 24
guests waited for a room, on 16 days. For 21 of them Steward had already decided on that room
earlier the same day, a median 6.5 h before the wait. The 11 days that the 07:00 plan flagged as
at risk carried 13 of the waits, and 3 of those days were short-staffed, with fewer than the
usual 2 cleaners on shift. The year had 15 short-staffed days, from 24 cleaner shifts lost to
absence. 22 of the 24 waits began before the on-call window opens at 15:30, so on-call help
arrives too late. *Recommendation:* when a sick note leaves a day short-staffed, book
replacement cleaning at the morning plan, and move the on-call window to the hours before
check-in. *Uncertainty:* Steward did not anticipate 3 waits; they came on days the plan judged
safe, and 1 of them followed an early check-in request that Steward predicted would be met.
Their median wait was 0.50 h, against 1.00 h for the anticipated ones.

**Leaks: dispatch on persistence and on the night gap; do not wait for a guest report.** 5 of
Steward's 5 leak incidents were followed by a corrective work order on the same asset. Steward
flagged the bursts a median 21.0 h before the repair order, and the slow leaks (running toilets,
a pipe in the technical room) 6 to 7 days before it. Night-flow alarms raised by the BMS all
year: 1, during 1 of the incidents and 2.5 h after Steward's flag; none for the slow leaks.
*Recommendation:* keep flow in every night interval on 2 nights and the main-minus-rooms night
gap as triggers, send the technician after 3 nights, and use the night-flow alarm only as
corroboration. *Uncertainty:* 5 incidents in the year.

**Heat pump: judge HP1 against a fixed COP curve, and check the refrigerant before the cold
months.** A Theil-Sen fit over 365 days gives COP = 2.78 + 0.068 x outdoor degC. Outside
Steward's incidents the daily COP never fell below 0.96 of that curve; inside them it fell to
0.81. The incidents ended in the repair "Heat pump service: refrigerant leak sealed and
recharged", 46 to 48 h after Steward's first flag, in January and December, 315 days apart.
*Recommendation:* keep the ratio threshold at 0.88, which leaves a margin on either side, and
add a refrigerant check to the autumn service. *Uncertainty:* 2 incidents, and the curve will
drift as the unit ages.

**Logistics: keep ordering on the 96 h use rate, and add a summer drinks restock.** Steward
ordered 25 extra services (24 for VEND1, 1 for WASTE_MAIN). 21 came before a real stock-out or
full container, a median 31.8 h ahead, 21 of them at least the 24 h order lead time ahead; 4
found no shortage before the regular service. 21 of the year's 23 shortages had an order before
them. The 20 vending stock-outs (20 of them drinks) fell between April and September, most in
July (6). *Recommendation:* in those months, add a restock between the regular visits (Mon and
Thu). *Uncertainty:* a replayed order changes no later stock, so a hit shows need, not
prevention.

**Alarms and data: use BMS alarms to corroborate, never to trigger.** The BMS raised 464 alarms
in the year; matched against telemetry, they trail their condition by a median 0.75 h. It raised
159 comm-loss alarms, 19 of them with no gap behind them, against 14 floor-wide outages that
Steward found. No alarm watches HP1 efficiency (0 alarms on HP1 all year). After meter WM_R105
was exchanged, its readings stepped backwards on 180 nights. *Recommendation:* keep alarms as
evidence beside Steward's rules, and check every exchanged meter for backward steps.
*Uncertainty:* the alarm rules are reconstructed from the values the alarms report.

**Filters: change AHU1 filters on pressure, not on the calendar.** Planned changes ran 97 days
apart, yet the filters clogged 2 times and were replaced unplanned by EXTERNAL_HVAC, 43 to 87
days after the previous change; FILTER_DP_HIGH was raised 10 times. *Recommendation:* trigger
the change on day-mode filter pressure. *Uncertainty:* 2 events. Steward's filter lane made 19
decisions on AHU1 this year.

## Design recommendations for the next building (feedback to PLATO)

**Plan for summer heat.** The building lists its cooling as "none (heating-only heat pump;
ventilation provides night flushing in summer)". Occupied rooms spent 19,308 room-hours above 26
degC, 78 % of them in July, and guests sent 201 "too hot" requests against 29 "too cold". Rooms
facing S were too hot in 10 % of their occupied hours, rooms facing N in 8 %. *Recommendation:*
external shading on the S facades and a heat pump that can also cool. *Uncertainty:* the thermal
model is simplified and uncalibrated.

**Insulate and zone the lowest floor.** Occupied rooms on floor 1 were below the comfort band
and at least 1.5 K under their setpoint for 1,034 room-hours, 3.6 times the average of the other
floors (291 room-hours); 59 % of it fell in May and August. In PLATO-40 the lowest floor sits
above parking, a plausible cause the data cannot confirm. *Recommendation:* an insulated slab
over the parking and a separate heating circuit for that floor that stays available outside the
heating season. *Uncertainty:* the cause is not in the data, and guests on that floor did not
ask for heat more often (6 of the 29 "too cold" requests).

**Size the vending machine to the longest restock gap.** At the peak 96 h rate of 1.7 drinks per
hour a full machine (100 drinks) lasts 58 h, but regular restocks are up to 97 h apart, which at
that rate needs room for 166 drinks. *Uncertainty:* demand comes from a single synthetic summer.

**Make the network and the plant reachable.** 11 of the 14 floor-wide telemetry outages were on
GW_F3 (52.8 h without readings); while a floor is blank, no room on it gets a leak or comfort
verdict. 2 planned filter changes on the rooftop AHU1 were postponed by 12 to 16 days ("rooftop
unit only reachable with lift platform, not available"). *Recommendation:* a redundant or
buffering gateway per floor, and plant that can be serviced without a lift platform.
*Uncertainty:* the data does not show why GW_F3 fails.

**Meter the common areas separately.** The leak on the hot-water circulation pipe showed only in
the night gap between the main meter and the room meters: a median 28.5 L/h during the incident,
against 1.4 L/h (95th percentile) on normal nights. The daily gap alternates between 307 L and
289 L on even and odd days, from a common-area draw every other day, so daily totals cannot
reveal a leak. *Recommendation:* submeters on the circulation loop and the common-area draws.
*Uncertainty:* pipe-leak incidents in the year: 1.

## Target architecture

- **Data:** the files Steward reads today, plus a measured COP (heat meter and electricity
  submeter on the heat pump), submeters on common-area water, leak sensors under washbasins,
  logged room exits, inspection events, and an on-call and contractor calendar. Every record
  carries the time it became known.
- **Agents:** a deterministic lane for each domain (turnaround, water, heating and energy,
  logistics, data quality) feeding a single policy gate that sets the autonomy level. A language
  model only words guest messages; every room, time and ID stays as the rules wrote them.
- **Robots:** floor robots clean before the human clean and carry floor bins; they never certify
  or release a room. Repairs and entering occupied rooms stay with people.
- **Interfaces:** the decision contract as the action log, an approval inbox with consent
  tracking, read access to the BMS, work-order and vendor ordering APIs, guest messaging in the
  guest's language, and this report, regenerated from each year's run.

## Limits

- The data is a single synthetic year. The leak and heat pump rules rest on 5 to 216 incidents
  per class; the holdout and real operation must confirm them.
- The data is a replay: no action changes later readings, so outcomes are matched to work orders
  and events that people wrote after the fact.
- The data has no costs, so the report claims no savings. Steward's energy lane pre-heats
  arrival rooms but has no battery plan yet.
