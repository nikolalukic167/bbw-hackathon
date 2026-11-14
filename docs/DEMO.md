# Demo script: Steward for PLATO-40

The page is one file, `decisions.html` (dev) or `examples/quickstart/console.html`, written by
`plato40 ui` (README, Deliverables). It runs from disk with no network. Every number on it is
computed when the page is written, so this script quotes none: read them off the screen.

- **Roles:** one person speaks, one person drives. The driver uses the links below and the
  keys only: `Y D E T` switch views, the arrow keys step a day, never a terminal.
- **Timing:** about six minutes; the live part stays under three.
- **Days:** three dev days, local time (Europe/Vienna): 25 Jan (heat pump in a cold snap),
  20 Mar (a burst in an empty room after midnight), 9 May (a sold-out Saturday).

## Before you start

1. Open `decisions.html` in a browser, full screen, at `#view=year`.
2. Check the step numbers: `uv run python scripts/demo_steps.py decisions.html` prints every
   day's steps with their local time and label. They must match the "step" column below; if a
   regeneration moved them, use the printed numbers.
3. Keep the fallback open in a second tab: the demo video, and the screenshots in `docs/demo/`.

## The script

| # | Link | Say | The judge sees |
|---|---|---|---|
| 1 | `#view=year` | Who runs a hotel at three in the morning? One command turns a year of PLATO-40 data into decisions, each traced to its evidence. | The year strip: grey and quiet, a few clusters. |
| 2 | `#view=year` | The ten-second read: guest requests answered before their due time, the share Steward did alone, how often a manager is asked, no violations, and the rerun byte-identical. | The five tiles. |
| 3 | `#view=day&day=2026-01-25&step=2` | **Day A, 25 Jan.** Minus degrees outside, and the heat pump is losing efficiency. Save energy or keep guests warm? 03:01: a guest in R103 is cold; Steward raises the setpoint and answers in the guest's language. | The R103 tile, the comfort-deficit signal and the reply. |
| 4 | `step=3` | Two hours later the room is still cold, so the closed loop asks for an inspection: a person, with the guest's consent. | The follow-up, marked as approval and consent. |
| 5 | `step=9` | 10:00: the heat pump's 24-hour efficiency is below its own reference curve. | The COP ratio sparkline dipping. |
| 6 | `step=11` to `step=16` | Steward flags it, asks for the technician (the roster says he is on shift Monday), tells the guests in cold rooms why, and protects occupied rooms. The energy lane backs off its load shifting while comfort is at risk. | Approval amber on the dispatch; the energy lane's defer. |
| 7 | `#view=trail&ev=s03-heating_comfort-HP1-20260125T0900Z-prop02` | The trail of the dispatch: the evidence, the rule that made it an approval, and what the data shows next. On 27 Jan the efficiency was still low a day after the technician, so a contractor was ordered; the work order closed that afternoon. | Evidence, decision, autonomy rule, outcome from the work-order log. |
| 8 | (say it) | Limit: the reference curve is fitted on one synthetic year, and the year has few heat pump incidents. | |
| 9 | `#view=day&day=2026-03-20&step=0` | **Day B, 20 Mar, 00:15.** Water has been running hard in R201, an empty room, for hours. Who decides to block it? | The burst-flow signal; the main flow sparkline. |
| 10 | `step=1` to `step=4` | Steward flags it, asks to block the room and to order the repair (both need a person), and sends an inspection. It never shuts off water on its own. | Two approvals, one autonomous inspection. |
| 11 | `#view=trail&ev=EP-water_leak-R201-20260319T2315Z-02` | The block's trail: the inspection that follows, and the release on 21 Mar once the repair held for a day and the room was clean. | The chain to the release follow-up. |
| 12 | `#view=day&day=2026-03-20&step=6` | 06:15: the floor-3 gateway goes blank. Steward flags it and gives no leak or comfort verdict on that floor while it cannot see. | The data-quality flag; blank tiles. |
| 13 | (say it) | Limit: the year has few leaks; the repair is read from the plumber's work order, not from the meter. | |
| 14 | `#view=day&day=2026-05-09&step=1` | **Day C, 9 May.** Sold out, and a cleaner called in sick at dawn. Whose room first? A week ahead Steward had already asked for a stand-by cleaner. | The 07:00 day plan signal. |
| 15 | `#view=trail&ev=s05-occupancy_surge-BUILDING-20260501T0500Z-prop01` | That request, made a week before. Nobody is planned until a manager says yes. | The stand-by approval. |
| 16 | `#view=day&day=2026-05-09&step=2` to `step=10` | At 07:00: arrival rooms first in the cleaning order, extra help asked for by name of the late rooms, and an honest notice to each late guest with no time promised. | Approval amber; guest notices. |
| 17 | `step=13` to `step=16` | 12:09: the guest of R209 asks to check in early. Steward cleans R209 first, sends the robot ahead and promises a time, in Italian, the language the guest wrote in. | Clean, robot, promise. |
| 18 | `step=20` | The promise slips; the closed loop tells the guest at once instead of letting the time pass. | The follow-up notice. |
| 19 | `#view=trail&ev=EP-room_turnaround-R209-20260509T1009Z-03` | Limit, said plainly: the data shows the guest still waited. The morning plan cannot know who arrives early; the Limits panel counts these misses. | Outcome: arrival_waiting_room. |
| 20 | `#view=year` | The Limits panel: what Steward missed, as numbers, and what the replay cannot show. | The Limits panel. |
| 21 | `insights.md` (second tab) | The learning loop: feedback to CLARA and to PLATO, every number from a script. | The recommendations. |
| 22 | `#view=year` | Trust: the holdout ran unchanged with one command, the rerun was byte-identical, and the language model words messages and may add a few checked flags, under a contract. | The trust tile. |

## Fallback

- If the page does not open or freezes, say "here is the recorded run", play the video and
  keep narrating the same steps. Do not debug on stage.
- If one view breaks, switch to its screenshot in `docs/demo/` and go on. They are taken from
  this `decisions.html` at 1440 x 900, one per link above: `00-year.png`; Day A
  `a1-day-r103-reply`, `a2-day-hp1-dispatch`, `a3-episode-hp1`, `a4-trail-hp1-dispatch`; Day B
  `b1-day-r201-burst`, `b2-day-floor3-outage`, `b3-episode-r201`, `b4-trail-r201-block`; Day C
  `c1-day-plan`, `c2-day-r209-promise`, `c3-episode-r209`, `c4-trail-r209`.
- If a judge asks for another day, click it in the year strip. Days without the room extract
  show the timeline and the building sparklines only; say so.

## Questions to expect

- **What is real and what is simulated?** Detection and decisions are real code on the given
  data. Every effect of an action is simulated, because the data is a replay.
- **Where does the language model decide?** Never in place of the rules: it words guest
  messages, and the supervisor may add at most 2 flags, inspections, deferrals or approval
  requests per review, each checked against the data and cached. It never changes or removes a
  rule's decision.
- **Would it work on new data?** The holdout ran unchanged with one command.
- **What did it get wrong?** The Limits panel lists it, with numbers.
- **Does it save energy?** The battery plan's saving is simulated against no battery at the
  published prices (Limits panel, `insights.md` energy paragraph). Pre-heat raises vacant
  arrival rooms only, and the energy lane backs off while the heat pump is weak (Day A).
- **How do you avoid false alarms?** Persistence and known-at: a single night of flow, a
  jittering meter, a guest with the window open or a room inside the band gets no fault
  decision. `insights.md`, "Look-alikes and bad data", counts each.
