"""Action chains per episode. Approval flags are left to the policy.

Water leak (room): a slow leak waits ``leak_persist_nights`` consecutive nights, then
flags and inspects; at ``leak_technician_nights`` it requests a technician. A burst
flags, inspects and requests a technician at once, all critical. Inspecting an occupied
room first asks the guest for consent. Water is never shut off by the system.
Water leak (main-minus-rooms gap): flag, then technician, on the same night counts.
Turnaround, day plan: on a day at risk, arrival rooms first, robots, one guest notice, and
a request for extra help when over capacity. Per request: prioritise the cleaning (and a
robot pass) once the room is vacant, call the on-call housekeeper only when no cleaner
is on shift, offer a ready room (approval), and tell the guest a realistic time.
Heating: flag, technician (or contractor if none within 24 h), a comfort-first energy plan,
and at most one notice per 12 h to occupied rooms newly in comfort deficit.
Data quality: one flag per outage episode.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd

from plato40.contract import Decision
from plato40.detect.grid import local_time, stamp
from plato40.episodes import Episode, Signal
from plato40.settings import Settings
from plato40.state import RoomStates

NEXT_NIGHT = pd.Timedelta(hours=26)
NOTICE_GAP = pd.Timedelta(hours=12)
WHAT = {
    "night_flow": "continuous night flow",
    "burst": "burst volume in one night",
    "night_gap": "unmetered night flow outside the rooms",
}
VERIFY_LEAK = (
    "Night minimum of the meter returns to 0 L after repair; escalate if flow continues "
    "another night or the night volume rises."
)


class _Chain:
    """Builds the decisions of one episode with sequential IDs and dependencies."""

    def __init__(self, ep: Episode) -> None:
        self.ep = ep
        self.out: list[Decision] = []

    def add(self, s: Signal, action: str, priority: str, reason: str, **extra: Any) -> str:
        event_id = f"{self.ep.episode_id}-{len(self.out) + 1:02d}"
        decision: Decision = {
            "event_id": event_id,
            "episode_id": self.ep.episode_id,
            "timestamp_utc": stamp(s.time.ceil("min")),
            "affected_assets": list(extra.pop("assets", s.assets)),
            "issue_type": self.ep.issue_type,
            "action": action,
            "priority": priority,
            "reason": reason,
            "requires_human_approval": False,
            "evidence": list(s.evidence),
        }
        if self.out:
            decision["dependencies"] = [self.out[-1]["event_id"]]
        decision.update(extra)
        self.out.append(decision)
        return event_id


def _inspect(chain: _Chain, s: Signal, states: RoomStates, priority: str, conf: float) -> None:
    room = s.assets[0]
    if states.at(room, s.time) == "occupied":
        chain.add(
            s,
            "notify_guest",
            priority,
            f"Ask the guest in {room} for consent to enter for a leak inspection "
            "(proposed message; delivery not confirmed).",
            expected_outcome="Guest grants or refuses consent.",
            verification_plan="Inspection waits for a recorded consent; escalate if none.",
            confidence=conf,
        )
    chain.add(
        s,
        "request_inspection",
        priority,
        f"Inspect {room} for a leak: {s.detail}.",
        expected_outcome="Inspection confirms or rules out a leak (simulated).",
        verification_plan=VERIFY_LEAK,
        confidence=conf,
    )


def _water(ep: Episode, states: RoomStates, settings: Settings) -> list[Decision]:
    th = settings.detect
    chain = _Chain(ep)
    in_room = ep.assets[0] in states.rooms
    flagged = technician = False
    nights = 0
    prev: pd.Timestamp | None = None
    for s in ep.signals:
        what = WHAT[s.kind]
        if s.kind == "burst":
            if technician:
                continue
            if not flagged:
                chain.add(s, "flag_anomaly", "critical", f"{what}: {s.detail}.", confidence=0.8)
            _inspect(chain, s, states, "critical", 0.8)
            chain.add(
                s,
                "dispatch_technician",
                "critical",
                f"Repair the suspected burst: {s.detail}.",
                expected_outcome="Technician finds and repairs the leak (simulated).",
                verification_plan=VERIFY_LEAK,
                confidence=0.8,
            )
            flagged = technician = True
            continue
        nights = nights + 1 if prev is not None and s.time - prev <= NEXT_NIGHT else 1
        prev = s.time
        conf = 0.7 if in_room else 0.6
        if nights >= th.leak_persist_nights and not flagged:
            chain.add(
                s,
                "flag_anomaly",
                "medium",
                f"{what}, night {nights} in a row: {s.detail}.",
                expected_outcome="Operations see the suspected leak; no physical effect.",
                verification_plan=VERIFY_LEAK,
                confidence=conf,
            )
            if in_room:
                _inspect(chain, s, states, "medium", conf)
            flagged = True
        if nights >= th.leak_technician_nights and not technician:
            chain.add(
                s,
                "dispatch_technician",
                "high",
                f"{what} persists, night {nights} in a row: {s.detail}.",
                expected_outcome="Technician finds and repairs the leak (simulated).",
                verification_plan=VERIFY_LEAK,
                confidence=conf + 0.1,
            )
            technician = True
    return chain.out


def _local(t: pd.Timestamp | None, ref: pd.Timestamp, settings: Settings) -> str:
    return local_time(t, ref, settings.local_tz)


def _day_plan(chain: _Chain, s: Signal, settings: Settings) -> None:
    d = s.data
    prio = "high" if d["help"] else "medium"
    rooms = ", ".join(d["arrival_rooms"]) or "none"
    checkin = _local(d["checkin"], s.time, settings)
    latest = _local(d["latest"], s.time, settings)
    vacant = d["vacant_arrival"]
    verify = (
        f"cleaning_completed in every arrival room before {checkin}; re-plan on a new sick note."
    )
    if d["cleaners"]:
        chain.add(
            s,
            "schedule_cleaning",
            prio,
            f"Turnover at risk ({s.detail}). Clean arrival rooms first ({rooms}): vacant ones "
            "now, occupied ones right after checkout; then the rest.",
            assets=[*d["cleaners"], *vacant],
            expected_outcome=(
                f"Arrival rooms clean by {latest} (simulated)."
                if d["latest"]
                else "Not every arrival room can be clean before the shifts end (simulated)."
            ),
            verification_plan=verify,
            confidence=0.6,
        )
    if d["robots"]:
        chain.add(
            s,
            "dispatch_robot",
            prio,
            "Robot floor passes in arrival rooms: vacant ones now, occupied ones only after "
            "checkout; robots do not certify or release a room.",
            assets=[*d["robots"], *vacant],
            expected_outcome="Shorter human cleaning per room (simulated).",
            verification_plan="robot_floor_cleaning_completed before each human clean.",
            confidence=0.6,
        )
    if d["arrival_rooms"]:
        chain.add(
            s,
            "notify_guest",
            prio,
            f"Tell guests arriving today ({rooms}): check-in from {checkin} as booked, early "
            "check-in is not possible today"
            + (
                f"; all rooms expected ready by {latest}"
                if d["latest"]
                else "; some rooms may be ready only after check-in time"
            )
            + " (proposed message; delivery not confirmed).",
            assets=d["arrival_rooms"],
            expected_outcome="Guests do not arrive early and wait.",
            verification_plan="No arrival_waiting_room event today; escalate on one.",
            confidence=0.6,
        )
    if d["help"]:
        chain.add(
            s,
            "request_human_approval",
            "high",
            f"Cleaning workload exceeds capacity before {checkin} (load {d['load']:.2f}); "
            "approve extra cleaning help. None is assumed until approved.",
            assets=["BUILDING"],
            expected_outcome="A manager decides on extra help.",
            verification_plan=verify,
            confidence=0.6,
        )


@dataclass
class _Guest:
    """What was already done for one guest's stay within a room episode."""

    planned: bool = False
    committed: pd.Timestamp | None = None
    offered: str | None = None


class _Ledger:
    """When each cleaner and the on-call housekeeper is next free, from jobs given so far.

    Requests are planned in time order across rooms, so a promise never assumes a person
    who is already cleaning another room at that time.
    """

    def __init__(self, settings: Settings) -> None:
        self.clean = pd.Timedelta(minutes=settings.detect.clean_room_min)
        self.travel = pd.Timedelta(minutes=settings.detect.oncall_travel_min)
        self.free: dict[str, pd.Timestamp] = {}

    def cleaner(self, d: Mapping[str, Any]) -> tuple[pd.Timestamp, str] | None:
        """Return the earliest start and the cleaner on shift who is free first."""
        if d["begin"] is None or not d["cleaners"]:
            return None
        return min((max(d["begin"], self.free.get(c, d["begin"])), c) for c in d["cleaners"])

    def oncall(self, d: Mapping[str, Any]) -> tuple[pd.Timestamp, str] | None:
        """Return when the on-call housekeeper can start, travelling only if not on site."""
        who = d["oncall"]
        if who is None:
            return None
        busy = self.free.get(who)
        return (busy if busy is not None and busy > d["start"] else d["start"] + self.travel, who)

    def book(self, begin: pd.Timestamp, who: str) -> pd.Timestamp:
        self.free[who] = begin + self.clean
        return self.free[who]


def _turnaround_step(
    chain: _Chain, guest: _Guest, s: Signal, ledger: _Ledger, settings: Settings
) -> None:
    d = s.data
    room = s.assets[0]
    th = settings.detect
    lead = d["wanted"] - s.time
    urgent = lead <= pd.Timedelta(hours=th.urgent_lead_h)
    prio = "critical" if d["waiting"] else "high" if urgent else "medium"
    cleaner = ledger.cleaner(d)
    oncall = ledger.oncall(d) if d["waiting"] else None
    if oncall is not None and cleaner is not None and cleaner[0] <= oncall[0]:
        oncall = None
    if guest.committed is not None:
        promise: pd.Timestamp | None = (
            guest.committed if guest.committed > s.time else s.time + ledger.clean
        )
    elif d["in_progress"]:
        promise = d["ready_prio"]
    elif oncall is not None:
        promise = oncall[0] + ledger.clean
    else:
        promise = None if cleaner is None else cleaner[0] + ledger.clean
    ready = _local(promise, s.time, settings)
    verify = f"{room} shows cleaning_completed by {ready}; if not, update the guest and escalate."
    if d["in_progress"] and not guest.planned:
        guest.planned, guest.committed = True, promise
    if d["vacant"] and not guest.planned:
        if oncall is not None:
            begin, who = oncall
            chain.add(
                s,
                "schedule_cleaning",
                prio,
                f"No cleaner on shift and the guest is waiting; call the on-call housekeeper "
                f"for {room}: {s.detail}.",
                resource_id=who,
                expected_outcome=f"{room} clean by {ready} (simulated).",
                verification_plan=verify,
                confidence=0.6,
            )
            guest.committed = ledger.book(begin, who)
        elif cleaner is not None:
            begin, who = cleaner
            when = "now" if begin <= s.time else f"from {_local(begin, s.time, settings)}"
            chain.add(
                s,
                "schedule_cleaning",
                prio,
                f"Clean {room} first, {when}: {s.detail}.",
                resource_id=who,
                expected_outcome=f"{room} clean by {ready} (simulated).",
                verification_plan=verify,
                confidence=0.7,
            )
            guest.committed = ledger.book(begin, who)
        else:
            chain.add(
                s,
                "request_human_approval",
                prio,
                f"No cleaner or on-call housekeeper is known for {room}; arrange a cleaning: "
                f"{s.detail}.",
                expected_outcome="A manager arranges the cleaning.",
                verification_plan=f"{room} shows cleaning_started; escalate if not.",
                confidence=0.6,
            )
        if d["robot"]:
            chain.add(
                s,
                "dispatch_robot",
                prio,
                f"Robot floor pass in {room} before the human clean; robots do not "
                "certify or release a room.",
                resource_id=d["robot"],
                expected_outcome="Shorter human cleaning (simulated).",
                verification_plan=f"robot_floor_cleaning_completed in {room}.",
                confidence=0.7,
            )
        guest.planned = True
    offer = guest.offered or d["alternative"]
    if offer and not guest.offered:
        chain.add(
            s,
            "reassign_room",
            prio,
            f"{offer} is clean, vacant and not booked during the guest's stay; offer it "
            f"instead of {room}.",
            assets=[room, offer],
            expected_outcome="Guest checks in on time in another room (simulated).",
            verification_plan=f"Guest checks in to {offer}; {room} keeps its cleaning.",
            confidence=0.7,
        )
        guest.offered = offer
    message = f"{offer} is offered pending approval; otherwise {room}" if offer else f"{room}"
    chain.add(
        s,
        "notify_guest",
        prio,
        f"Tell the guest ({d['language']}): {message} expected ready at {ready} "
        "(proposed message; delivery not confirmed).",
        **({"guest_request_ids": [d["request_id"]]} if d["request_id"] else {}),
        expected_outcome="Guest knows a realistic ready time.",
        verification_plan="No waiting_for_room request from the guest; escalate on one.",
        confidence=0.7,
    )


def _turnaround(episodes: list[Episode], settings: Settings) -> list[Decision]:
    """Plan all turnaround episodes together, in signal time order, sharing one ledger."""
    chains = {ep.episode_id: _Chain(ep) for ep in episodes}
    guests: dict[tuple[str, str], _Guest] = {}
    ledger = _Ledger(settings)
    steps = sorted(
        ((s.time, ep.episode_id, i, s) for ep in episodes for i, s in enumerate(ep.signals)),
        key=lambda x: x[:3],
    )
    for _, eid, _, s in steps:
        if s.kind == "day_plan":
            _day_plan(chains[eid], s, settings)
            continue
        guest = guests.setdefault((eid, s.data["booking"]), _Guest())
        _turnaround_step(chains[eid], guest, s, ledger, settings)
    return [d for c in chains.values() for d in c.out]


def _heating(ep: Episode, settings: Settings) -> list[Decision]:
    chain = _Chain(ep)
    s = ep.signals[0]
    d = s.data
    chain.add(
        s,
        "flag_anomaly",
        "high",
        f"Heat pump degradation: {s.detail}.",
        expected_outcome="Operations see the degraded heat pump; no physical effect.",
        verification_plan="24 h COP back above the reference ratio after service.",
        confidence=0.8,
    )
    when = d["technician_from"]
    if when is not None and when - s.time <= pd.Timedelta(hours=24):
        chain.add(
            s,
            "dispatch_technician",
            "high",
            f"Service HP1 (COP {d['ratio']:.2f} of reference); technician on shift "
            f"{'now' if d['technician'] else 'from ' + _local(when, s.time, settings)}.",
            **({"resource_id": d["technician"]} if d["technician"] else {}),
            expected_outcome="Technician finds and fixes the fault (simulated).",
            verification_plan="24 h COP ratio recovers; escalate to a contractor if not.",
            confidence=0.8,
        )
    else:
        chain.add(
            s,
            "order_service",
            "high",
            "Service HP1 by a contractor: no technician on shift within 24 h.",
            expected_outcome="Contractor fixes the fault (simulated).",
            verification_plan="24 h COP ratio recovers after the visit.",
            confidence=0.7,
        )
    lo, hi = d["band"]
    cold = (
        f"coldest forecast hour {d['cold'][0]:.1f} degC at {_local(d['cold'][1], s.time, settings)}"
        if d["cold"]
        else "no forecast known"
    )
    peaks = ", ".join(d["peaks"]) or "unknown"
    chain.add(
        s,
        "optimize_energy",
        "high",
        f"Protect occupied rooms with reduced heat: hold vacant rooms at setback, pre-heat "
        f"occupied rooms within {lo}-{hi} degC before the {cold}; avoid price peaks "
        f"({peaks} local) only where comfort holds. Comfort before cost.",
        assets=["HP1", "BUILDING"],
        expected_outcome="Occupied rooms stay in the comfort band (simulated).",
        verification_plan=f"Occupied rooms at or above {lo} degC; escalate rooms that fall below.",
        confidence=0.6,
    )
    told: set[str] = set()
    last: pd.Timestamp | None = None
    for s in ep.signals:
        new = [r for r in s.data["cold_rooms"] if r not in told]
        if not new or (last is not None and s.time - last < NOTICE_GAP):
            continue
        last = s.time
        chain.add(
            s,
            "notify_guest",
            "high",
            f"Tell guests in {', '.join(new)} that heating is reduced and a repair is "
            "requested (proposed message; delivery not confirmed).",
            assets=new,
            expected_outcome="Guests know the cause and that help is coming.",
            verification_plan="No temperature_cold request from these rooms; escalate on one.",
            confidence=0.7,
        )
        told.update(new)
    return chain.out


def _data_quality(ep: Episode) -> list[Decision]:
    chain = _Chain(ep)
    chain.add(
        ep.signals[0],
        "flag_anomaly",
        "low",
        f"Telemetry outage: {ep.signals[0].detail}.",
        expected_outcome="Rooms on the floor get no leak or comfort verdict while blank.",
        verification_plan="Readings from the floor resume; escalate if blank for 24 h.",
        confidence=0.9,
    )
    return chain.out


def decisions(episodes: list[Episode], states: RoomStates, settings: Settings) -> list[Decision]:
    """Return the decisions of all episodes, without approval flags."""
    out = _turnaround([ep for ep in episodes if ep.issue_type == "room_turnaround"], settings)
    for ep in episodes:
        if ep.issue_type == "water_leak":
            out += _water(ep, states, settings)
        elif ep.issue_type == "heating_comfort":
            out += _heating(ep, settings)
        elif ep.issue_type == "data_quality":
            out += _data_quality(ep)
        elif ep.issue_type != "room_turnaround":
            raise ValueError(f"no plan for issue_type {ep.issue_type!r}")
    return out
