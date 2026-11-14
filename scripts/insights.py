"""Write the Level 3 feedback report (``insights.md``) from one dataset and one Steward run.

Usage::

    uv run python scripts/insights.py <data_dir> --out <path/insights.md>
        [--llm-cache <dir>] [--supervisor <out>.supervisor.jsonl]

It runs every registered lane, like ``plato40 run``; with ``--llm-cache`` also the guest
messages and the supervisor from that cache, so the report describes the committed run.

The script runs the Steward pipeline on ``data_dir`` and computes every figure in the
report from that run and the dataset tables. The report text holds no digits of its own:
every template is checked before rendering, so a number typed by hand fails the run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import textwrap
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from plato40 import contract, kpi, lanes, pipeline
from plato40.audit import candidates as cand
from plato40.audit import quality, timing
from plato40.audit.scenarios import corrective_orders
from plato40.episodes import Episode
from plato40.load import derive
from plato40.load.reader import Dataset, load_dataset
from plato40.settings import Settings
from plato40.supervise import DEFAULT_MODEL, MAX_ADDS

Facts = dict[str, str]
NA = "n/a"
NORMAL_Q = 0.95
EVENT_FIELDS = ("room_id", "booking_id", "asset_id", "alarm_code", "state", "product")
NUMBER_WORDS = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|twice|both|half|dozen)\b", re.I
)

LANES = {
    "room_turnaround": "Room turnaround",
    "water_leak": "Water leaks",
    "heating_comfort": "Heat pump and comfort",
    "comfort_hot": "Comfort requests (too hot)",
    "guest_service": "Guest desk",
    "logistics": "Waste and vending",
    "data_quality": "Data quality",
    "ventilation_maintenance": "Ventilation (AHU filter)",
    "occupancy_surge": "Busy days and event weekends",
    "energy_optimization": "Energy (pre-heat, battery plan)",
}

OUTCOMES = {
    "room_turnaround": "{anticipated} of {waits} waiting guests had a decision on their room "
    "earlier that day",
    "water_leak": "{leak_wo} of {leak_eps} followed by a repair work order",
    "heating_comfort": "{hp_wo} of {hp_eps} heat pump incidents followed by repair of "
    "{hp_asset}; {cold_ok} of {cold_req} cold-room requests answered before their due time",
    "logistics": "{hits} of {orders} orders came before a real shortage",
    "data_quality": "{outages} floor outages, {top_n} of them on {top_gw}",
    "comfort_hot": "{hotreq_answered} of {hotreq_n} too-hot requests answered, "
    "{hotreq_on_time} before their due time",
    "guest_service": "{req_on_time} of {req_total} guest requests (all lanes) answered before "
    "their due time; {desk_staff} got a staff action",
    "ventilation_maintenance": "{vent_wo} of {vent_eps} filter incidents followed by an "
    "{ahu} work order",
    "occupancy_surge": "{busy_wait} of {busy_days} days flagged at the morning plan had a "
    "waiting guest",
    "energy_optimization": "{pre_ok} of {pre_n} pre-heated rooms at or above {band_lo} degC at "
    "check-in",
}

FILTER_NOTES = {
    "lane": "Steward's filter lane made {filter_rows} decisions on {ahu} this year.",
    "none": "Steward has no filter detector yet.",
}
LIMIT_NOTES = {
    "lanes": "",
    "none": "Steward has no daily energy plan and no filter detector yet.",
}
SAVINGS = {
    "battery": "The data's only costs are electricity prices. The only saving in this report is "
    "the battery plan's, simulated against no battery; no other saving is claimed.",
    "none": "The data's only costs are electricity prices, and the report claims no savings.",
}

NOTES = {
    "unmatched": "; the other {leak_unmatched} (on {unmatched_where}) had none and are likely "
    "false alarms, {unmatched_jitter} of them on a meter that jitters after its exchange",
    "all_matched": "",
}

SUPERVISOR = (
    "A supervisor LLM ({sv_model}, ADR-003) reviewed the rules' decisions at {sv_reviews} "
    "review points ({sv_kinds}). It answered at {sv_answered} of them{sv_none}. The checks "
    "rejected {sv_rejected} answers{sv_rejected_why}. It added {sv_added} decisions{sv_levels} "
    "and changed or removed none.{sv_what}{sv_examples}"
)
NO_SUPERVISOR = (
    "The supervisor LLM (ADR-003) was not part of this run: it runs only with a cache of its "
    "answers (`--llm-cache`)."
)
EXAMPLE = ' For example, `{event_id}`: "{reason}"'
EXAMPLE_MORE = ' And `{event_id}`: "{reason}"'

TEMPLATE = """\
# Steward insights: what a year of PLATO-40 teaches

Steward ran over the `{profile}` dataset ({start} to {end}, {days} days, {rooms} rooms) with
{violations} contract or autonomy violations. Every number below is computed from that run
and the dataset tables by `uv run python scripts/insights.py <data_dir> --out insights.md`.
PLATO-40 is fictional and the data synthetic: these are findings about the replay, and every
effect of an action is simulated.

## The year at a glance

Steward turned {signals} detector signals into {episodes} incidents, {routine} routine guest
replies, pre-heats and battery plans, and {decisions} decisions. {autonomous} decisions were
autonomous. A person was asked to approve {approvals}, on average every {days_per_approval}
days, and {consent} of those also need guest consent. {desk_approvals} of the approvals are
guest-desk consent requests (maintenance, cleaning), so the manager's own load is
{manager_approvals}.

| Lane | Incidents | Routine | Decisions | Autonomous | Approval | Approval and consent | Outcome |
|---|---|---|---|---|---|---|---|
{table}

## Guest requests

{req_answered} of the {req_total} guest requests were answered by a decision that cites the
request, {req_on_time} of them before the request's due time.

| Request type | Requests | Answered | Before due |
|---|---|---|---|
{req_table}

## Supervisor review

{supervisor}

## Operating recommendations for PLATO-40 next year (feedback to CLARA)

**Turnovers: act at the morning plan, and move on-call help to the hours when guests wait.**
{waits} guests waited for a room, on {wait_days} days. For {anticipated} of them Steward had
already decided on that room earlier the same day, a median {lead_h} h before the wait. The
{at_risk} days that the {plan_hour} plan flagged as at risk carried {waits_at_risk} of the
waits, and {short_at_risk} of those days were short-staffed, with fewer than the usual
{modal_cleaners} cleaners on shift. The year had {short_days} short-staffed days, from
{sick} cleaner shifts lost to absence. {before_oncall} of the {waits} waits began before the
on-call window opens at {oncall_start}, so on-call help arrives too late.
*Recommendation:* when a sick note leaves a day short-staffed, book replacement cleaning at
the morning plan, and move the on-call window to the hours before check-in.
*Uncertainty:* Steward did not anticipate {missed} waits; they came on days the plan judged
safe, and {missed_requested} of them followed an early check-in request that Steward
predicted would be met. Their median wait was {missed_wait} h, against {anticipated_wait} h
for the anticipated ones.

**Leaks: dispatch on persistence and on the night gap; do not wait for a guest report.**
{leak_wo} of Steward's {leak_eps} leak incidents were followed by a corrective work order on
the same asset{leak_note}. Steward flagged the bursts a median {burst_lead} h before the
repair order,
and the slow leaks (running toilets, a pipe in the technical room) {slow_lead} days before
it. Night-flow alarms raised by the BMS all year: {wnf_alarms}, during {wnf_eps} of the
incidents and {wnf_after} h after Steward's flag; none for the slow leaks.
*Recommendation:* keep flow in every night interval on {persist} nights and the
main-minus-rooms night gap as triggers, send the technician after {tech_nights} nights, and
use the night-flow alarm only as corroboration. *Uncertainty:* {leak_eps} incidents in the
year.

**Heat pump: judge {hp_asset} against a fixed COP curve, and check the refrigerant before
the cold months.** A Theil-Sen fit over {fit_days} days gives COP = {fit_icpt} +
{fit_slope} x outdoor degC. Outside Steward's incidents the daily COP never fell below
{normal_min} of that curve; inside them it fell to {fault_min}. The incidents ended in the
repair "{hp_summary}", {hp_lead} h after Steward's first flag, in {hp_months}, {hp_gap}
days apart. *Recommendation:* keep the ratio threshold at {cop_low}, which leaves a margin
on either side, and add a refrigerant check to the autumn service. *Uncertainty:* {hp_eps}
incidents, and the curve will drift as the unit ages.

**Logistics: keep ordering on the {rate_h} h use rate, and add a summer drinks restock.**
Steward ordered {orders} extra services ({orders_detail}). {hits} came before a real
stock-out or full container, a median {log_lead} h ahead, {in_time} of them at least the
{lead_need} h order lead time ahead; {false_orders} found no shortage before the regular
service. {covered} of the year's {shortages} shortages had an order before them. The
{vend_outs} vending stock-outs ({drink_outs} of them drinks) fell between {out_first} and
{out_last}, most in {out_peak}. *Recommendation:* in those months, add a restock between
the regular visits ({restock_days}). *Uncertainty:* a replayed order changes no later stock,
so a hit shows need, not prevention.

**Energy: plan the battery on the day-ahead prices, pre-heat by booked arrival, and back off
while the heat pump is weak.** On {bat_days} days Steward planned {bat_asset} when the next
day's prices were published, against forecast PV and the recent load: a median {bat_import}
kWh of grid import per plan against {bat_base} kWh with no battery, and a median saving of EUR
{bat_median} per plan against no battery, simulated at the published prices. Pre-heat raised
the setpoint of vacant arrival rooms only, never of an occupied room; {pre_ok} of the {pre_n}
pre-heated rooms were at or above {band_lo} degC at check-in. While {hp_asset} was degraded,
the energy lane paused load shifting and pre-heat {backoffs} times and left the comfort lane
in charge. *Recommendation:* keep the battery on the price plan, and look at the {pre_miss}
pre-heated rooms that missed the band before lengthening the pre-heat lead. *Uncertainty:* the
saving is simulated against no battery with the replay's own load and PV; plans overlap in
time, so the savings per plan are not added up into a year.

**Alarms and data: use BMS alarms to corroborate, never to trigger.** The BMS raised
{alarms} alarms in the year; matched against telemetry, they trail their condition by a
median {alarm_lag} h. It raised {comm_alarms} comm-loss alarms, {unsupported} of them with no
gap behind them, against {outages} floor-wide outages that Steward found. No alarm watches
{hp_asset} efficiency ({hp_alarms} alarms on {hp_asset} all year). After meter {jit_meter}
was exchanged, its readings stepped backwards on {jit_nights} nights. *Recommendation:*
keep alarms as evidence beside Steward's rules, and check every exchanged meter for backward
steps. *Uncertainty:* the alarm rules are reconstructed from the values the alarms report.

**Filters: change {ahu} filters on pressure, not on the calendar.** Planned changes ran
{planned_gap} days apart, yet the filters clogged {unplanned_n} times and were replaced
unplanned by {unplanned_who}, {unplanned_since} days after the previous change;
FILTER_DP_HIGH was raised {dp_alarms} times. *Recommendation:* trigger the change on
day-mode filter pressure. *Uncertainty:* {unplanned_n} events. {filter_note}

## Design recommendations for the next building (feedback to PLATO)

**Plan for summer heat.** The building lists its cooling as "{cooling}". Occupied rooms
spent {hot_h} room-hours above {hot_c} degC, {hot_peak_share} of them in {hot_peak}, and
guests sent {hot_req} "too hot" requests against {cold_req} "too cold". Rooms facing
{hot_or} were too hot in {hot_or_rate} of their occupied hours, rooms facing {cool_or} in
{cool_or_rate}. *Recommendation:* external shading on the {hot_or} facades and a heat pump
that can also cool. *Uncertainty:* the thermal model is simplified and uncalibrated.

**Insulate and zone the lowest floor.** Occupied rooms on floor {low_floor} were below the
comfort band and at least {deficit_k} K under their setpoint for {low_h} room-hours, {low_ratio}
times the average of the other floors ({other_avg} room-hours); {low_months_share} of it fell
in {low_months}. In PLATO-40 the lowest floor sits above parking, a plausible cause the data
cannot confirm. *Recommendation:* an insulated slab over the parking and a separate heating
circuit for that floor that stays available outside the heating season. *Uncertainty:* the
cause is not in the data, and guests on that floor did not ask for heat more often
({low_cold_req} of the {cold_req} "too cold" requests).

**Size the vending machine to the longest restock gap.** At the peak {rate_h} h rate of
{peak_rate} drinks per hour a full machine ({cap} drinks) lasts {lasts_h} h, but regular
restocks are up to {gap_h} h apart, which at that rate needs room for {need_cap} drinks.
*Uncertainty:* demand comes from a single synthetic summer.

**Make the network and the plant reachable.** {top_n} of the {outages} floor-wide telemetry
outages were on {top_gw} ({top_hours} h without readings); while a floor is blank, no room
on it gets a leak or comfort verdict. {postponed} planned filter changes on the rooftop
{ahu} were postponed by {postpone_days} days ("{postpone_reason}"). *Recommendation:* a
redundant or buffering gateway per floor, and plant that can be serviced without a lift
platform. *Uncertainty:* the data does not show why {top_gw} fails.

**Meter the common areas separately.** The leak on the hot-water circulation pipe showed
only in the night gap between the main meter and the room meters: a median {gap_leak} L/h
during the incident, against {gap_normal} L/h ({gap_q} percentile) on normal nights. The
daily gap alternates between {gap_even} L and {gap_odd} L on even and odd days, from a
common-area draw every other day, so daily totals cannot reveal a leak.
*Recommendation:* submeters on the circulation loop and the common-area draws.
*Uncertainty:* pipe-leak incidents in the year: {gap_eps}.

## Target architecture

- **Data:** the files Steward reads today, plus a measured COP (heat meter and electricity
  submeter on the heat pump), submeters on common-area water, leak sensors under washbasins,
  logged room exits, inspection events, and an on-call and contractor calendar. Every record
  carries the time it became known.
- **Agents:** a deterministic lane for each domain (turnaround, water, heating and energy,
  logistics, data quality) feeding a single policy gate that sets the autonomy level. A
  language model words guest messages, and every room, time and ID stays as the rules wrote
  them. A supervisor model may add at most {sv_max} checked flags or requests per review; it
  never changes or removes a rule's decision.
- **Robots:** floor robots clean before the human clean and carry floor bins; they never
  certify or release a room. Repairs and entering occupied rooms stay with people.
- **Interfaces:** the decision contract as the action log, an approval inbox with consent
  tracking, read access to the BMS, work-order and vendor ordering APIs, guest messaging in
  the guest's language, and this report, regenerated from each year's run.

## Limits

- The data is a single synthetic year. The leak and heat pump rules rest on {small_n}
  incidents per class; the holdout and real operation must confirm them.
- The data is a replay: no action changes later readings, so outcomes are matched to work
  orders and events that people wrote after the fact.
- {savings_note} {limits_note}
"""


def _check_literal(text: str) -> None:
    """Raise if a template holds a number of its own: a digit or a number word."""
    bare = re.sub(r"\{[a-z_]+\}|ADR-\d{3}", "", text).replace("PLATO-40", "")
    lines = [
        line for line in bare.splitlines() if re.search(r"\d", line) or NUMBER_WORDS.search(line)
    ]
    if lines:
        raise ValueError("template has a hand-typed number:\n" + "\n".join(lines))


def check_templates() -> None:
    """Raise if any report template holds a number typed by hand."""
    for text in (
        TEMPLATE,
        SUPERVISOR,
        NO_SUPERVISOR,
        EXAMPLE,
        EXAMPLE_MORE,
        *OUTCOMES.values(),
        *LANES.values(),
        *NOTES.values(),
        *FILTER_NOTES.values(),
        *LIMIT_NOTES.values(),
        *SAVINGS.values(),
    ):
        _check_literal(text)


def _n(x: float) -> str:
    return f"{round(x):,}" if np.isfinite(x) else NA


def _f(x: float, digits: int = 1) -> str:
    return f"{x:,.{digits}f}" if np.isfinite(x) else NA


def _pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:.0f} %" if whole else NA


def _span(values: Iterable[float], digits: int = 0) -> str:
    vals = sorted(values)
    if not vals:
        return NA
    lo, hi = _f(vals[0], digits), _f(vals[-1], digits)
    return lo if lo == hi else f"{lo} to {hi}"


def _median(values: Iterable[float], digits: int = 1) -> str:
    vals = [v for v in values if v == v]
    return _f(statistics.median(vals), digits) if vals else NA


def _hours(a: pd.Timestamp, b: pd.Timestamp) -> float:
    return (b - a).total_seconds() / 3600.0


def _day(t: pd.Timestamp, tz: str) -> str:
    local = t.tz_convert(tz)
    return f"{local.day} {local:%b %Y}"


def _local_date(t: pd.Timestamp, tz: str) -> date:
    return t.tz_convert(tz).date()


def _and(items: list[str]) -> str:
    if not items:
        return NA
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _months(stamps: Iterable[pd.Timestamp], tz: str) -> str:
    return _and([f"{t.tz_convert(tz):%B}" for t in sorted(stamps)])


def reflow(markdown: str, width: int = 96) -> str:
    """Rewrap prose paragraphs and list items; leave headings and tables as they are."""
    out = []
    for block in markdown.strip().split("\n\n"):
        if block.startswith(("#", "|")):
            out.append(block)
        elif block.startswith("- "):
            items = re.split(r"\n(?=- )", block)
            out.append(
                "\n".join(
                    textwrap.fill(
                        " ".join(i[2:].split()),
                        width,
                        initial_indent="- ",
                        subsequent_indent="  ",
                        break_on_hyphens=False,
                    )
                    for i in items
                )
            )
        else:
            out.append(textwrap.fill(" ".join(block.split()), width, break_on_hyphens=False))
    return "\n\n".join(out) + "\n"


def _acted(run: pipeline.Run) -> list[Episode]:
    """Return the episodes Steward made at least one decision on (``kpi.acted``)."""
    return kpi.acted(run)


def _of(run: pipeline.Run, issue: str) -> list[Episode]:
    return [e for e in _acted(run) if e.issue_type == issue]


def _repair(ds: Dataset, ep: Episode, quiet: pd.Timedelta) -> pd.Series | None:
    """Return the first corrective work order on the episode's asset in its window, if any."""
    asset = ep.assets[0]
    wo = corrective_orders(ds)
    hit = wo.loc[
        wo["asset_id"].isin([asset, f"WM_{asset}"])
        & (wo["type"] == "corrective")
        & (wo["created_utc"] >= ep.signals[0].time - quiet)
        & (wo["created_utc"] <= ep.signals[-1].time + quiet)
    ].sort_values("created_utc")
    return None if hit.empty else hit.iloc[0]


def overview(ds: Dataset, run: pipeline.Run, tz: str) -> Facts:
    """Return run totals, the decision split by autonomy level and the approval rate."""
    days = int(ds.manifest["days"])
    level = Counter(d["autonomy"] for d in run.decisions)
    approvals = sum(bool(d["requires_human_approval"]) for d in run.decisions)
    return {
        "profile": ds.profile,
        "start": _day(pd.Timestamp(ds.manifest["start_utc"]), "UTC"),
        "end": _day(pd.Timestamp(ds.manifest["end_utc"]) - pd.Timedelta(seconds=1), "UTC"),
        "days": _n(days),
        "rooms": _n(len(ds.room_ids())),
        "violations": _n(len(run.violations)),
        "signals": _n(len(run.signals)),
        "episodes": _n(len({e.episode_id for e in _acted(run)} - routine(run))),
        "decisions": _n(len(run.decisions)),
        "autonomous": _n(level["autonomous"]),
        "approvals": _n(approvals),
        "consent": _n(sum(bool(d.get("requires_guest_consent")) for d in run.decisions)),
        "days_per_approval": _f(days / approvals) if approvals else NA,
    }


def _too_hot(run: pipeline.Run) -> set[str]:
    """Return heating episodes made only of too-hot replies (``kpi.too_hot``)."""
    return kpi.too_hot(run)


def lane_table(run: pipeline.Run, facts: Mapping[str, str]) -> str:
    """Return one markdown row per issue type: incidents, routine, decisions by level, outcome.

    Replies to "too hot" requests are a row of their own, so the heating row counts heat
    pump and cold-comfort incidents only. Incidents and routine episodes (``kpi.routine``)
    are separate columns, so the incident column sums to the total in the opening.
    """
    hot = _too_hot(run)
    usual = routine(run)

    def key(issue: str, episode: str) -> str:
        return "comfort_hot" if issue == "heating_comfort" and episode in hot else issue

    rows = []
    order = list(LANES)
    acted = _acted(run)
    issues = sorted(
        {key(e.issue_type, e.episode_id) for e in acted},
        key=lambda i: (order.index(i) if i in LANES else len(order), i),
    )
    for issue in issues:
        mine = [d for d in run.decisions if key(d["issue_type"], d["episode_id"]) == issue]
        level = Counter(d["autonomy"] for d in mine)
        outcome = OUTCOMES.get(issue, NA).format(**facts)
        eps = [e.episode_id for e in acted if key(e.issue_type, e.episode_id) == issue]
        cells = [
            LANES.get(issue, issue),
            _n(sum(e not in usual for e in eps)),
            _n(sum(e in usual for e in eps)),
            _n(len(mine)),
            _n(level["autonomous"]),
            _n(level["human_approval"]),
            _n(level["approval_and_consent"]),
            outcome,
        ]
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


def turnaround(ds: Dataset, run: pipeline.Run, settings: Settings) -> Facts:
    """Return waiting-guest outcomes, at-risk days and cleaner capacity."""
    tz = settings.local_tz
    ev = ds.events
    waits = ev.loc[ev["type"] == "arrival_waiting_room"]
    plans = kpi.plan_days(run, settings)
    rows = kpi.waits(ds, run, settings)
    leads = [float(w["lead_h"]) for w in rows if w["anticipated"]]
    wait_hit = [float(w["waited_h"]) for w in rows if w["anticipated"]]
    wait_miss = [float(w["waited_h"]) for w in rows if not w["anticipated"]]
    miss_requested = sum(w["requested"] for w in rows if not w["anticipated"])
    wait_days = {_local_date(pd.Timestamp(t), tz) for t in waits["timestamp_utc"]}
    roster = ds.table("staff_roster.csv")
    cleaners = roster.loc[roster["role"] == "cleaner"]
    shift_day = cleaners["start_utc"].dt.tz_convert(tz).dt.date
    on = (cleaners["status"] == "scheduled").groupby(shift_day).sum()
    modal = int(on.mode().iloc[0])
    short = set(on.index[on < modal])
    notes = ds.building["staff"]["notes"].get("on_call_housekeeping", "")
    window = re.search(r"(\d\d:\d\d)-\d\d:\d\d", str(notes))
    before = NA
    if window:
        opens = datetime.strptime(window[1], "%H:%M").time()
        local = waits["timestamp_utc"].dt.tz_convert(tz).dt.time
        before = _n(sum(x < opens for x in local))
    return {
        "waits": _n(len(waits)),
        "wait_days": _n(len(wait_days)),
        "anticipated": _n(len(leads)),
        "lead_h": _median(leads),
        "plan_hour": f"{settings.detect.day_plan_hour:02d}:00",
        "at_risk": _n(len(plans)),
        "waits_at_risk": _n(
            sum(_local_date(pd.Timestamp(t), tz) in plans for t in waits["timestamp_utc"])
        ),
        "short_at_risk": _n(len(short & set(plans))),
        "modal_cleaners": _n(modal),
        "short_days": _n(len(short)),
        "sick": _n(int((cleaners["status"] != "scheduled").sum())),
        "before_oncall": before,
        "oncall_start": window[1] if window else NA,
        "missed": _n(len(wait_miss)),
        "missed_requested": _n(miss_requested),
        "missed_wait": _median(wait_miss, 2),
        "anticipated_wait": _median(wait_hit, 2),
    }


def water(ds: Dataset, run: pipeline.Run, settings: Settings) -> Facts:
    """Return leak incidents against repair work orders and the BMS night-flow alarm."""
    quiet = pd.Timedelta(hours=settings.detect.episode_quiet_h)
    eps = _of(run, "water_leak")
    alarms = ds.events.loc[
        (ds.events["type"] == "bms_alarm")
        & (ds.events["state"] == "raised")
        & (ds.events["alarm_code"] == "WATER_NIGHT_FLOW")
    ]
    burst: list[float] = []
    slow: list[float] = []
    matched = 0
    alarm_after: list[float] = []
    unmatched: list[str] = []
    for ep in eps:
        first = ep.signals[0].time
        order = _repair(ds, ep, quiet)
        if order is None:
            unmatched.append(ep.assets[0])
            continue
        matched += 1
        lead = _hours(first, pd.Timestamp(order["created_utc"]))
        if {s.kind for s in ep.signals} & {"burst", "burst_flow"}:
            burst.append(lead)
        else:
            slow.append(lead / 24.0)
        seen = alarms.loc[
            (alarms["timestamp_utc"] >= first - quiet)
            & (alarms["timestamp_utc"] <= order["created_utc"])
        ]
        if not seen.empty:
            alarm_after.append(_hours(first, pd.Timestamp(seen["timestamp_utc"].min())))
    jittery = {
        e.assets[0]
        for e in _of(run, "data_quality")
        if e.signals and e.signals[0].kind == "meter_jitter"
    }
    note = {
        "leak_unmatched": _n(len(unmatched)),
        "unmatched_where": _and(sorted(set(unmatched))),
        "unmatched_jitter": _n(sum(a in jittery for a in unmatched)),
    }
    return {
        "leak_note": NOTES["unmatched" if unmatched else "all_matched"].format(**note),
        "leak_eps": _n(len(eps)),
        "leak_wo": _n(matched),
        "burst_lead": _median(burst),
        "slow_lead": _span(slow),
        "wnf_alarms": _n(len(alarms)),
        "wnf_eps": _n(len(alarm_after)),
        "wnf_after": _span(alarm_after, 1),
        "persist": _n(settings.detect.leak_persist_nights),
        "tech_nights": _n(settings.detect.leak_technician_nights),
    }


def heating(ds: Dataset, run: pipeline.Run, settings: Settings) -> Facts:
    """Return the COP reference fit, the normal and faulty ratios, and the repairs."""
    tz, th = settings.local_tz, settings.detect
    quiet = pd.Timedelta(hours=th.episode_quiet_h)
    rooms = _of(run, "heating_comfort")
    eps = [e for e in rooms if any(s.kind == "cop_low" for s in e.signals)]
    asset = eps[0].assets[0] if eps else "HP1"
    req = ds.table("guest_requests.csv")
    cold = req.loc[req["request_type"] == "temperature_cold"]
    due = dict(zip(cold["request_id"].astype(str), cold["response_due_utc"], strict=True))
    answered = {
        g
        for d in run.decisions
        if d["issue_type"] == "heating_comfort"
        for g in d.get("guest_request_ids", [])
        if g in due and pd.notna(due[g]) and pd.Timestamp(d["timestamp_utc"]) <= due[g]
    }
    cop = derive.daily_cop(ds, tz).dropna(subset=["cop", "outdoor_c"])
    slope, intercept = float("nan"), float("nan")
    if len(cop) > 1:
        slope, intercept = cand.theil_sen(cop["outdoor_c"].to_numpy(), cop["cop"].to_numpy())
    ratio = cop["cop"] / (th.cop_ref_intercept + th.cop_ref_slope * cop["outdoor_c"])
    inside = pd.Series(False, index=ratio.index)
    for ep in eps:
        lo = (ep.signals[0].time - pd.Timedelta(hours=th.cop_window_h)).tz_convert(tz).date()
        hi = ep.signals[-1].time.tz_convert(tz).date()
        days = derive.dt_index(ratio.index).date
        inside |= (days >= lo) & (days <= hi)
    orders = [(ep, _repair(ds, ep, quiet)) for ep in eps]
    found = [(ep, o) for ep, o in orders if o is not None]
    created = [pd.Timestamp(o["created_utc"]) for _, o in found]
    gaps = [(b - a).days for a, b in pairwise(created)]
    ratios = [float(s.data["ratio"]) for ep in eps for s in ep.signals if s.kind == "cop_low"]
    return {
        "hp_asset": asset,
        "hp_eps": _n(len(eps)),
        "cold_req": _n(len(cold)),
        "cold_ok": _n(len(answered)),
        "hp_wo": _n(len(found)),
        "fit_days": _n(len(cop)),
        "fit_icpt": _f(intercept, 2),
        "fit_slope": _f(slope, 3),
        "normal_min": _f(float(ratio[~inside].min()) if (~inside).any() else float("nan"), 2),
        "fault_min": _f(min(ratios), 2) if ratios else NA,
        "hp_summary": "; ".join(sorted({str(o["summary"]) for _, o in found})) or NA,
        "hp_lead": _span(
            _hours(ep.signals[0].time, c) for (ep, _), c in zip(found, created, strict=True)
        ),
        "hp_months": _months(created, tz),
        "hp_gap": _span(gaps),
        "cop_low": _f(th.cop_ratio_low, 2),
    }


def logistics(ds: Dataset, run: pipeline.Run, settings: Settings) -> Facts:
    """Return extra orders against real shortages, and vending demand against capacity."""
    tz, th = settings.local_tz, settings.detect
    ev = ds.events
    out = ev.loc[ev["type"] == "vending_out_of_stock"]
    found = kpi.orders(ds, run)
    rows = found["orders"]
    hits = sum(r["hit"] for r in rows)
    leads = [float(r["lead_h"]) for r in rows if r["hit"]]
    lead_need = [float(r["need_h"]) for r in rows]
    hit_need = [float(r["need_h"]) for r in rows if r["hit"]]
    per_asset = Counter(r["asset"] for r in rows)
    months = out["timestamp_utc"].dt.tz_convert(tz)
    by_month = months.dt.month.value_counts()
    first_m, last_m = (months.min(), months.max()) if len(months) else (None, None)
    vend = next(a for a in ds.building["building"]["assets"] if a["type"] == "vending_machine")
    cap = float(vend["capacity"]["drinks"])
    stock = derive.logistics_wide(ds)[f"{vend['id']}/stock_drinks"]
    use = -stock.diff()
    peak = float(use.where(use >= 0).rolling(th.logistics_rate_h, min_periods=1).mean().max())
    restocks = ev.loc[ev["type"] == "vending_restocked", "timestamp_utc"].sort_values()
    gaps = restocks.diff().dropna()
    gap_h = float(gaps.max().total_seconds() / 3600.0) if len(gaps) else float("nan")
    out_peak = NA
    if first_m is not None:
        when = pd.Timestamp(year=first_m.year, month=int(by_month.idxmax()), day=1)
        out_peak = f"{when:%B} ({_n(int(by_month.max()))})"
    return {
        "orders": _n(len(rows)),
        "orders_detail": ", ".join(f"{_n(n)} for {a}" for a, n in sorted(per_asset.items())),
        "hits": _n(hits),
        "false_orders": _n(len(rows) - hits),
        "log_lead": _median(leads),
        "in_time": _n(sum(x >= need for x, need in zip(leads, hit_need, strict=True))),
        "lead_need": _median(lead_need, 0),
        "shortages": _n(found["shortages"]),
        "covered": _n(found["covered"]),
        "vend_outs": _n(len(out)),
        "drink_outs": _n(int((out["product"] == "drinks").sum())),
        "restock_days": " and ".join(
            re.findall(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", vend["restock"])
        )
        or NA,
        "out_first": f"{first_m:%B}" if first_m is not None else NA,
        "out_last": f"{last_m:%B}" if last_m is not None else NA,
        "out_peak": out_peak,
        "rate_h": _n(th.logistics_rate_h),
        "peak_rate": _f(peak),
        "cap": _n(cap),
        "lasts_h": _n(cap / peak) if peak > 0 else NA,
        "gap_h": _n(gap_h),
        "need_cap": _n(peak * gap_h),
    }


def data_quality(ds: Dataset, run: pipeline.Run, settings: Settings) -> Facts:
    """Return floor outages per gateway, comm-loss alarms and post-exchange meter jitter."""
    building = ds.building["building"]
    floor_of = {str(r["id"]): r["floor"] for r in building["rooms"]}
    gateway = {g["floor"]: str(g["id"]) for g in building["gateways"]}
    eps = _of(run, "data_quality")
    outages = [e for e in eps if e.signals[0].kind == "floor_outage"]
    per_gw = Counter(gateway[floor_of[e.assets[0]]] for e in outages)
    top_gw, top_n = per_gw.most_common(1)[0] if per_gw else (NA, 0)
    top_floor = next((f for f, g in gateway.items() if g == top_gw), None)
    width = Counter(floor_of.values())
    blank_h = sum(
        e.hours
        for e in quality.telemetry_outages(ds)
        if top_floor is not None and e.group == f"F{top_floor}" and e.width == width[top_floor]
    )
    matches = timing.match_alarms(ds, settings)
    comm = [m for m in matches if m.code == "SENSOR_COMM_LOSS"]
    jitter = max(
        (e for e in eps if e.signals[0].kind == "meter_jitter"),
        key=lambda e: len(e.signals),
        default=None,
    )
    meter = NA
    if jitter is not None:
        meter = next(
            (str(r["water_meter_id"]) for r in building["rooms"] if r["id"] == jitter.assets[0]),
            jitter.assets[0],
        )
    ev = ds.events
    raised = ev.loc[(ev["type"] == "bms_alarm") & (ev["state"] == "raised")]
    heat_pumps = {a["id"] for a in building["assets"] if a["type"] == "heat_pump"}
    delays = [m.delay_h for m in matches if m.delay_h is not None]
    return {
        "outages": _n(len(outages)),
        "top_gw": top_gw,
        "top_n": _n(top_n),
        "top_hours": _f(blank_h),
        "comm_alarms": _n(len(comm)),
        "unsupported": _n(sum(m.onset is None for m in comm)),
        "alarms": _n(len(raised)),
        "alarm_lag": _median(delays, 2),
        "hp_alarms": _n(int(raised["asset_id"].isin(heat_pumps).sum())),
        "jit_meter": meter,
        "jit_nights": _n(len(jitter.signals)) if jitter is not None else NA,
    }


def comfort(ds: Dataset, settings: Settings) -> Facts:
    """Return overheating by month and orientation, and comfort deficits by floor."""
    tz = settings.local_tz
    step_h = settings.interval_minutes / 60.0
    rooms = {str(r["id"]): r for r in ds.building["building"]["rooms"]}
    band = ds.building["constraints"]["occupied_comfort_c"]
    k = settings.detect.comfort_deficit_k
    cold = derive.comfort_deficit(ds, float(band[0]), k)
    temp = derive.telemetry_wide(ds, "temperature_c")
    occ = derive.occupancy_matrix(ds, pd.DatetimeIndex(temp.index))
    hot_c = settings.thresholds.overheat_c
    hot = (temp > hot_c) & occ
    hot_month = (
        (hot.sum(axis=1) * step_h).groupby(derive.dt_index(hot.index).tz_convert(tz).month).sum()
    )
    orient = {r: rooms[r]["orientation"] for r in hot.columns}
    by_orient = pd.Series(orient)
    occupied = (occ.sum() * step_h).groupby(by_orient).sum()
    rate = ((hot.sum() * step_h).groupby(by_orient).sum() / occupied.where(occupied > 0)).dropna()
    cold_room = cold.sum() * step_h
    floor = {r: rooms[r]["floor"] for r in cold.columns}
    per_floor = cold_room.groupby(pd.Series(floor)).sum()
    low = min(per_floor.index)
    others = per_floor.drop(low)
    low_cols = [r for r in cold.columns if floor[r] == low]
    low_month = (
        (cold[low_cols].sum(axis=1) * step_h)
        .groupby(derive.dt_index(cold.index).tz_convert(tz).month)
        .sum()
    )
    top = low_month.sort_values(ascending=False)
    share = top.cumsum() / top.sum()
    keep = top.index[: int((share < 0.5).sum()) + 1]
    req = ds.table("guest_requests.csv")
    cold_req = req.loc[req["request_type"] == "temperature_cold", "room_id"]
    first = pd.Timestamp(ds.manifest["start_utc"]).tz_convert(tz)
    name = {m: f"{pd.Timestamp(year=first.year, month=int(m), day=1):%B}" for m in range(1, 13)}
    return {
        "cooling": str(ds.building["operations"].get("cooling", NA)),
        "hot_h": _n(float(hot_month.sum())),
        "hot_c": _f(hot_c, 0),
        "hot_peak": name[int(str(hot_month.idxmax()))],
        "hot_peak_share": _pct(float(hot_month.max()), float(hot_month.sum())),
        "hot_req": _n(int((req["request_type"] == "temperature_hot").sum())),
        "cold_req": _n(len(cold_req)),
        "hot_or": str(rate.idxmax()) if len(rate) else NA,
        "hot_or_rate": _pct(float(rate.max()), 1.0) if len(rate) else NA,
        "cool_or": str(rate.idxmin()) if len(rate) else NA,
        "cool_or_rate": _pct(float(rate.min()), 1.0) if len(rate) else NA,
        "low_floor": _n(low),
        "deficit_k": _f(k),
        "low_h": _n(float(per_floor[low])),
        "low_ratio": _f(float(per_floor[low] / others.mean())) if others.mean() > 0 else NA,
        "other_avg": _n(float(others.mean())),
        "low_months": _and([name[int(m)] for m in sorted(keep)]),
        "low_months_share": _pct(float(top.loc[keep].sum()), float(top.sum())),
        "low_cold_req": _n(
            int(cold_req.map(lambda r: rooms.get(str(r), {}).get("floor") == low).sum())
        ),
    }


def plant(ds: Dataset) -> Facts:
    """Return AHU filter changes from the work orders: planned, postponed and unplanned."""
    ahu = next(
        (a["id"] for a in ds.building["building"]["assets"] if a["type"] == "air_handling_unit"),
        NA,
    )
    wo = ds.table("work_orders.csv")
    mine = wo.loc[wo["asset_id"] == ahu]
    done = mine.loc[mine["status"] == "completed"].sort_values("completed_utc")
    planned = done.loc[done["type"] == "preventive", "completed_utc"]
    unplanned = done.loc[done["type"] == "corrective"]
    since = []
    for t in unplanned["created_utc"]:
        prior = done.loc[done["completed_utc"] < t, "completed_utc"]
        if not prior.empty:
            since.append((t - prior.max()).total_seconds() / 86400.0)
    postponed = mine.loc[mine["status"] == "postponed"]
    delays = []
    for t in postponed["created_utc"]:
        nxt = planned.loc[planned > t]
        if not nxt.empty:
            delays.append((nxt.min() - t).total_seconds() / 86400.0)
    reasons = postponed["summary"].str.split(" - ", n=1).str[-1].dropna().unique()
    ev = ds.events
    dp = ev.loc[
        (ev["type"] == "bms_alarm")
        & (ev["state"] == "raised")
        & (ev["alarm_code"] == "FILTER_DP_HIGH")
    ]
    planned_gaps = [d.total_seconds() / 86400.0 for d in planned.diff().dropna()]
    return {
        "ahu": ahu,
        "planned_gap": _span(planned_gaps),
        "unplanned_n": _n(len(unplanned)),
        "unplanned_who": ", ".join(sorted(set(unplanned["assigned_to"].astype(str)))) or NA,
        "unplanned_since": _span(since),
        "dp_alarms": _n(len(dp)),
        "postponed": _n(len(postponed)),
        "postpone_reason": "; ".join(map(str, reasons)) or NA,
        "postpone_days": _span(delays),
    }


def water_gap(ds: Dataset, run: pipeline.Run, settings: Settings) -> Facts:
    """Return the night gap during main-meter leaks and the daily gap by day parity."""
    tz = settings.local_tz
    main = {a["id"] for a in ds.building["building"]["assets"] if a["type"] == "water_meter"}
    eps = [e for e in _of(run, "water_leak") if e.assets[0] in main]
    nightly = cand.night_gap(ds, settings)
    inside = pd.Series(False, index=nightly.index)
    for ep in eps:
        lo, hi = _local_date(ep.signals[0].time, tz), _local_date(ep.signals[-1].time, tz)
        inside |= np.array([lo <= d <= hi for d in nightly.index], dtype=bool)
    gap = cand.gap_series(ds, settings)
    day = pd.Index(derive.dt_index(gap.index).tz_convert(tz).date)
    daily = gap.groupby(day).sum(min_count=1)
    parity = pd.Series([d.toordinal() % 2 for d in daily.index], index=daily.index)
    return {
        "gap_eps": _n(len(eps)),
        "gap_leak": _median(nightly[inside]),
        "gap_normal": _f(
            float(nightly[~inside].quantile(NORMAL_Q)) if (~inside).any() else float("nan")
        ),
        "gap_q": f"{NORMAL_Q * 100:.0f}th",
        "gap_even": _n(float(daily[parity == 0].median())),
        "gap_odd": _n(float(daily[parity == 1].median())),
    }


def guest_requests(ds: Dataset, run: pipeline.Run) -> Facts:
    """Return per request type: requests, answered (cited by a decision), answered by due."""
    found = kpi.requests(ds, run)
    rows = [
        f"| {r['type']} | {_n(r['total'])} | {_n(r['answered'])} | {_n(r['on_time'])} |"
        for r in found["by_type"]
    ]
    hot = next((r for r in found["by_type"] if r["type"] == "temperature_hot"), None)
    return {
        "req_table": "\n".join(rows),
        "req_total": _n(found["total"]),
        "req_answered": _n(found["answered"]),
        "req_on_time": _n(found["on_time"]),
        "hotreq_n": _n(hot["total"] if hot else 0),
        "hotreq_answered": _n(hot["answered"] if hot else 0),
        "hotreq_on_time": _n(hot["on_time"] if hot else 0),
    }


WHY = {
    "number_or_id_not_in_input": "with a number or ID not in its input",
    "evidence_not_from_refs": "with evidence not copied from its refs",
    "asset_or_decision_not_shown": "naming an asset or decision it was not shown",
    "malformed": "malformed",
    "call_failed": "call failed",
    "no_cached_answer": "with no cached answer",
    "other_check": "failing another check",
}


def _why(text: str) -> str:
    """Return the class of a supervisor rejection or no-answer reason, as words."""
    return WHY[kpi.reason_class(text)]


BECAUSE = {
    "number_or_id_not_in_input": "a number or ID was not in its input",
    "evidence_not_from_refs": "its evidence was not copied from its refs",
    "asset_or_decision_not_shown": "it named an asset or decision it was not shown",
    "malformed": "the answer was malformed",
    "call_failed": "the call failed",
    "no_cached_answer": "no answer was cached",
    "other_check": "another check failed",
}


def _because(reasons: list[str]) -> str:
    """Return ", all because ..." for one reason class, " (n because ..., ...)" for more."""
    c = Counter(kpi.reason_class(r) for r in reasons)
    if not c:
        return ""
    if len(c) == 1:
        return f", all because {BECAUSE[next(iter(c))]}"
    return " (" + _and([f"{_n(n)} because {BECAUSE[k]}" for k, n in c.most_common()]) + ")"


def _counted(reasons: Iterable[str]) -> str:
    c = Counter(_why(r) for r in reasons)
    return _and([f"{_n(n)} {label}" for label, n in c.most_common()]) or "none"


def supervisor(run: pipeline.Run, log: list[dict[str, Any]]) -> Facts:
    """Return the supervisor paragraph from its review log and the decisions it added."""
    if not log:
        return {"supervisor": NO_SUPERVISOR}
    added = [i for r in log for i in r.get("added", [])]
    mine = {d["event_id"]: d for d in run.decisions if d["event_id"] in set(added)}
    prefix = re.compile(r"^Supervisor review \(([^)]+)\): ")
    models = sorted({m[1] for d in mine.values() if (m := prefix.match(d["reason"]))})
    examples = ""
    rank = {p: n for n, p in enumerate(contract.PRIORITIES)}
    shown = sorted(
        mine, key=lambda e: (rank.get(mine[e]["priority"], 9), mine[e]["timestamp_utc"], e)
    )
    for eid in shown[:2]:
        if eid in mine:
            reason = textwrap.shorten(prefix.sub("", mine[eid]["reason"]), 220, placeholder="...")
            examples += (EXAMPLE_MORE if examples else EXAMPLE).format(event_id=eid, reason=reason)
    none = [r for r in log if r["source"] == "none"]
    rejected = [r for r in log if r["source"] != "none" and r.get("why")]
    levels = Counter(str(d.get("autonomy")) for d in mine.values())
    model = os.environ.get("PLATO40_SUPERVISOR_MODEL", DEFAULT_MODEL)
    facts = {
        "sv_model": _and(models) if models else model,
        "sv_reviews": _n(len(log)),
        "sv_kinds": _and(
            [f"{_n(n)} {k}" for k, n in sorted(Counter(r["kind"] for r in log).items())]
        ),
        "sv_answered": _n(len(log) - len(none)),
        "sv_none": (
            f"; {_n(len(none))} had no answer{_because([r.get('why', '') for r in none])}"
            if none
            else ""
        ),
        "sv_rejected": _n(len(rejected)),
        "sv_rejected_why": _because([r["why"] for r in rejected]),
        "sv_added": _n(len(added)),
        "sv_levels": (
            f" (the policy set {_and([f'{_n(n)} {k}' for k, n in levels.most_common()])})"
            if mine
            else ""
        ),
        "sv_what": (
            " By action: "
            + _and(
                [
                    f"{_n(n)} {a}"
                    for a, n in Counter(d["action"] for d in mine.values()).most_common()
                ]
            )
            + "; by lane: "
            + _and(
                [
                    f"{_n(n)} {LANES.get(i, i).lower()}"
                    for i, n in Counter(d["issue_type"] for d in mine.values()).most_common()
                ]
            )
            + "."
            if mine
            else ""
        ),
        "sv_examples": examples,
    }
    return {"supervisor": SUPERVISOR.format(**facts)}


def routine(run: pipeline.Run) -> set[str]:
    """Return the routine episodes (``kpi.routine``)."""
    return kpi.routine(run)


def level2(ds: Dataset, run: pipeline.Run, settings: Settings) -> Facts:
    """Return the outcomes of the guest desk, filter, busy-day and energy lanes."""
    tz = settings.local_tz
    staff = ("order_service", "schedule_cleaning", "request_inspection")
    desk = [d for d in run.decisions if d["issue_type"] == "guest_service"]
    desk_staff = {g for d in desk if d["action"] in staff for g in d.get("guest_request_ids", [])}
    wo = ds.table("work_orders.csv")
    vent = _of(run, "ventilation_maintenance")
    first = {
        e: min(pd.Timestamp(d["timestamp_utc"]) for d in run.decisions if d["episode_id"] == e)
        for e in {v.episode_id for v in vent}
    }
    ahu = plant(ds)["ahu"]
    orders = wo.loc[wo["asset_id"] == ahu, "created_utc"]
    window = pd.Timedelta(days=14)
    vent_wo = sum(bool(((orders >= t) & (orders <= t + window)).any()) for t in first.values())
    waits = ds.events.loc[ds.events["type"] == "arrival_waiting_room", "timestamp_utc"]
    wait_days = {_local_date(pd.Timestamp(t), tz) for t in waits}
    busy = {
        _local_date(s.time, tz)
        for s in run.signals
        if s.issue_type == "occupancy_surge" and s.kind == "day_plan"
    }
    heat = kpi.preheat(ds, run)
    lo, pre_n, pre_ok = heat["band_low_c"], heat["rooms"], heat["at_band"]
    s04 = sum(d["issue_type"] == "ventilation_maintenance" for d in run.decisions)
    s06 = any(d["issue_type"] == "energy_optimization" for d in run.decisions)
    asked = kpi.approvals(ds, run)
    note = FILTER_NOTES["lane" if s04 else "none"].format(filter_rows=_n(s04), ahu=ahu)
    return {
        "routine": _n(len(routine(run) & {e.episode_id for e in _acted(run)})),
        "desk_approvals": _n(asked["guest_desk"]),
        "manager_approvals": _n(asked["manager"]),
        "desk_staff": _n(len(desk_staff)),
        "vent_eps": _n(len(vent)),
        "vent_wo": _n(vent_wo),
        "busy_days": _n(len(busy)),
        "busy_wait": _n(len(busy & wait_days)),
        "band_lo": _n(lo),
        "pre_n": _n(pre_n),
        "pre_ok": _n(pre_ok),
        "pre_miss": _n(pre_n - pre_ok),
        "filter_note": note,
        "limits_note": LIMIT_NOTES["lanes" if s06 and s04 else "none"],
    }


def energy(run: pipeline.Run) -> Facts:
    """Return the battery plans (simulated), the pre-heat misses and the S03 back-offs."""
    plans = [
        s.data
        for s in run.signals
        if s.issue_type == "energy_optimization"
        and s.kind == "tariff_published"
        and "saving_eur" in s.data
    ]
    backoffs = sum(
        d["issue_type"] == "energy_optimization" and d["action"] == "defer_action"
        for d in run.decisions
    )
    asset = next((a for s in run.signals if s.kind == "tariff_published" for a in s.assets), "BAT1")
    return {
        "bat_days": _n(len(plans)),
        "bat_asset": asset,
        "bat_import": _median([float(p["planned_import_kwh"]) for p in plans]),
        "bat_base": _median([float(p["baseline_import_kwh"]) for p in plans]),
        "bat_median": _median([float(p["saving_eur"]) for p in plans], 2),
        "backoffs": _n(backoffs),
        "savings_note": SAVINGS["battery" if plans else "none"],
    }


def render(
    ds: Dataset, run: pipeline.Run, settings: Settings, log: list[dict[str, Any]] | None = None
) -> str:
    """Return the report for one run, every number computed from the run and the data."""
    check_templates()
    for column in EVENT_FIELDS:
        if column not in ds.events.columns:
            ds.events[column] = pd.NA
    facts: Facts = {}
    facts |= overview(ds, run, settings.local_tz)
    facts |= turnaround(ds, run, settings)
    facts |= water(ds, run, settings)
    facts |= heating(ds, run, settings)
    facts |= logistics(ds, run, settings)
    facts |= data_quality(ds, run, settings)
    facts |= comfort(ds, settings)
    facts |= plant(ds)
    facts |= water_gap(ds, run, settings)
    facts |= guest_requests(ds, run)
    facts |= level2(ds, run, settings)
    facts |= energy(run)
    facts |= supervisor(run, log if log is not None else run.reviews)
    hp = [e for e in _of(run, "heating_comfort") if any(s.kind == "cop_low" for s in e.signals)]
    facts["small_n"] = _span([len(_of(run, "water_leak")), len(hp)])
    facts["sv_max"] = _n(MAX_ADDS)
    facts["table"] = lane_table(run, facts)
    return reflow(TEMPLATE.format(**facts))


def main() -> int:
    """Run Steward on one dataset and write the insights report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("data_dir", type=Path, help="one dataset directory (with manifest.json)")
    parser.add_argument("--out", type=Path, required=True, help="insights.md to write")
    parser.add_argument(
        "--llm-cache",
        type=Path,
        help="as for plato40 run: guest messages and the supervisor from this cache",
    )
    parser.add_argument(
        "--supervisor",
        type=Path,
        help="a supervisor log (<out>.supervisor.jsonl) to report instead of this run's reviews",
    )
    args = parser.parse_args()
    check_templates()
    settings = Settings(
        data_root=args.data_dir,
        out_dir=args.out.parent,
        llm_cache=args.llm_cache,
        lanes=tuple(lanes.registry()),
    )
    ds = load_dataset(args.data_dir)
    run = pipeline.run(ds, settings)
    log = None
    if args.supervisor is not None:
        text = args.supervisor.read_text(encoding="utf-8")
        log = [json.loads(line) for line in text.splitlines() if line.strip()]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(ds, run, settings, log), encoding="utf-8")
    print(f"wrote {args.out} from {len(run.decisions)} decisions on {ds.profile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
