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

WHAT = {
    "night_flow": "continuous night flow",
    "burst": "burst volume in one night",
    "burst_flow": "sustained high flow",
    "night_gap": "unmetered night flow outside the rooms",
    "floor_outage": "Telemetry outage",
    "sensor_dead": "Room temperature sensor dead",
    "meter_jitter": "Water meter jitter",
}
OUTAGE: dict[str, Any] = {
    "expected_outcome": "Rooms on the floor get no leak or comfort verdict while blank.",
    "verification_plan": "Readings from the floor resume; escalate if blank for 24 h.",
    "confidence": 0.9,
}
DEAD: dict[str, Any] = {
    "expected_outcome": "The room gets no comfort verdict or pre-heat while blank.",
    "verification_plan": "Readings from the room resume; inspect the sensor if not within 24 h.",
    "confidence": 0.8,
}
JITTER: dict[str, Any] = {
    "expected_outcome": "Leak checks on this meter use hourly volumes while it jitters.",
    "verification_plan": "Meter readings rise monotonically again; check the meter if not.",
    "confidence": 0.8,
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


Stay = pd.Timestamp | None


def _consent(
    chain: _Chain, s: Signal, states: RoomStates, priority: str, conf: float, asked: Stay
) -> Stay:
    """Ask the guest for consent to enter unless the room is vacant or this stay was asked.

    Returns the stay (its start) whose guest has been asked. A guest who checked in after
    an earlier request has not been asked, so a new stay gets its own request.
    """
    room = s.assets[0]
    if states.at(room, s.time) != "occupied":
        return asked
    stay = states.since(room, s.time)
    if stay == asked:
        return asked
    chain.add(
        s,
        "notify_guest",
        priority,
        f"Ask the guest in {room} for consent to enter for a leak inspection or repair "
        "(proposed message; delivery not confirmed).",
        expected_outcome="Guest grants or refuses consent.",
        verification_plan="Entry waits for a recorded consent; escalate if none.",
        confidence=conf,
    )
    return stay


def _inspect(
    chain: _Chain, s: Signal, states: RoomStates, priority: str, conf: float, asked: Stay
) -> Stay:
    asked = _consent(chain, s, states, priority, conf, asked)
    room = s.assets[0]
    chain.add(
        s,
        "request_inspection",
        priority,
        f"Inspect {room} for a leak: {s.detail}.",
        expected_outcome="Inspection confirms or rules out a leak (simulated).",
        verification_plan=VERIFY_LEAK,
        confidence=conf,
    )
    return asked


def _repair(
    chain: _Chain,
    s: Signal,
    priority: str,
    reason: str,
    verify: str,
    conf: float,
    settings: Settings,
) -> bool:
    """Send the repair to the in-house technician or an external plumber; True if technician.

    A burst waits for nobody: the technician only if he is on shift now. A slow leak may
    wait for his next shift within ``technician_horizon_h``. Availability is the roster as
    known at the signal (``detect.water``).
    """
    tech, when = s.data.get("technician"), s.data.get("technician_from")
    hours = 0.0 if priority == "critical" else settings.detect.technician_horizon_h
    if when is not None and when - s.time <= pd.Timedelta(hours=hours):
        chain.add(
            s,
            "dispatch_technician",
            priority,
            f"{reason}; technician on shift "
            f"{'now' if tech else 'from ' + _local(when, s.time, settings)}.",
            **({"resource_id": tech} if tech else {}),
            expected_outcome="Technician finds and repairs the leak (simulated).",
            verification_plan=verify,
            confidence=conf,
        )
        return True
    away = "not on shift now" if hours == 0 else f"not on shift within {hours:.0f} h"
    nxt = "" if when is None else f" (next shift {_local(when, s.time, settings)})"
    chain.add(
        s,
        "order_service",
        priority,
        f"{reason}; in-house technician {away}{nxt}, so order an external plumber.",
        expected_outcome="External plumber finds and repairs the leak (simulated).",
        verification_plan=verify,
        confidence=conf,
    )
    return False


def _water(ep: Episode, states: RoomStates, settings: Settings) -> list[Decision]:
    th = settings.detect
    chain = _Chain(ep)
    in_room = ep.assets[0] in states.rooms
    flagged = technician = burst = escalated = False
    sent_night = 0
    asked: Stay = None
    verify = (
        VERIFY_LEAK
        if in_room
        else f"Median night gap main minus rooms below {th.gap_night_l_per_h:.0f} L/h after "
        "repair (the main meter also feeds common areas); escalate if it stays above another night."
    )
    for s in ep.signals:
        if s.kind == "night_blank":
            continue
        what = WHAT[s.kind]
        if s.kind in ("burst", "burst_flow"):
            if burst:
                continue
            if not flagged or technician:
                escalate = " (escalated: a technician is already on the slow leak)" * technician
                chain.add(
                    s,
                    "flag_anomaly",
                    "critical",
                    f"{what}{escalate}: {s.detail}.",
                    expected_outcome="Operations see the suspected burst; no physical effect.",
                    verification_plan=VERIFY_LEAK,
                    confidence=0.8,
                )
            if in_room:
                chain.add(
                    s,
                    "block_room",
                    "critical",
                    f"Take {s.assets[0]} out of sale until the burst is repaired: {s.detail}. "
                    "Room offers skip it meanwhile.",
                    expected_outcome="No guest is moved into or sold the room (simulated).",
                    verification_plan="Room stays blocked until the repair work order completes.",
                    confidence=0.8,
                )
            asked = _inspect(chain, s, states, "critical", 0.8, asked)
            _repair(
                chain,
                s,
                "critical",
                f"Repair the suspected burst: {s.detail}",
                VERIFY_LEAK,
                0.8,
                settings,
            )
            flagged = technician = burst = escalated = True
            continue
        nights = int(s.data["streak"])
        conf = 0.7 if in_room else 0.6
        if nights >= th.leak_persist_nights and not flagged:
            chain.add(
                s,
                "flag_anomaly",
                "medium",
                f"{what}, night {nights} in a row: {s.detail}.",
                expected_outcome="Operations see the suspected leak; no physical effect.",
                verification_plan=verify,
                confidence=conf,
            )
            if in_room:
                asked = _inspect(chain, s, states, "medium", conf, asked)
            flagged = True
        if nights >= th.leak_technician_nights and not technician:
            if in_room:
                asked = _consent(chain, s, states, "high", conf, asked)
            reason = f"{what} persists, night {nights} in a row: {s.detail}"
            escalated = not _repair(chain, s, "high", reason, verify, conf + 0.1, settings)
            technician, sent_night = True, nights
        elif technician and not escalated and nights >= sent_night + th.leak_contractor_nights:
            chain.add(
                s,
                "order_service",
                "high",
                f"{what} still flows {nights - sent_night} nights after the technician was "
                f"requested (night {nights} in a row): {s.detail}; order an external plumber.",
                expected_outcome="External plumber finds and repairs the leak (simulated).",
                verification_plan=verify,
                confidence=conf + 0.1,
            )
            escalated = True
    return chain.out


def _local(t: pd.Timestamp | None, ref: pd.Timestamp, settings: Settings) -> str:
    return local_time(t, ref, settings.local_tz)


def _ready(chain: _Chain, s: Signal) -> None:
    d = s.data
    chain.add(
        s,
        "notify_guest",
        "critical",
        f"Tell the waiting guest ({d['language']}): {s.assets[0]} is ready now; self check-in "
        "works (proposed message; delivery not confirmed).",
        **({"guest_request_ids": [d["request_id"]]} if d["request_id"] else {}),
        expected_outcome="Guest checks in now.",
        verification_plan=f"A check-in to {s.assets[0]} follows; escalate if the guest waits on.",
        confidence=0.9,
    )


def _overstay(chain: _Chain, guest: _Guest, s: Signal, ledger: _Ledger) -> None:
    d = s.data
    room = s.assets[0]
    free = [r for r in d["alternatives"] if ledger.offered.get(r, s.time) <= s.time]
    if free and not guest.offered:
        guest.offered = free[0]
        ledger.offered[free[0]] = d["departure"]
        chain.add(
            s,
            "reassign_room",
            "high",
            f"{free[0]} is clean, vacant and not booked during the guest's stay; offer it, "
            f"since {s.detail}.",
            assets=[room, free[0]],
            expected_outcome="Guest checks in on time in another room (simulated).",
            verification_plan=f"Guest checks in to {free[0]}; {room} keeps its cleaning.",
            confidence=0.7,
        )
    offer = f"{guest.offered} is offered pending approval; " if guest.offered else ""
    chain.add(
        s,
        "notify_guest",
        "high",
        f"Tell the guest ({d['language']}): the previous guest of {room} has not checked out "
        f"yet; {offer}a new ready time follows at checkout (proposed message; delivery not "
        "confirmed).",
        **({"guest_request_ids": [d["request_id"]]} if d["request_id"] else {}),
        expected_outcome="Guest is not left with a promise that has passed.",
        verification_plan=f"Checkout of {room} and an updated ready time follow; escalate if not.",
        confidence=0.7,
    )
    ledger.answered[room] = s.time


def _day_plan(chain: _Chain, s: Signal, settings: Settings, told: set[str]) -> None:
    """Plan a day at risk; guests already given a ready time (``told``) get no blanket notice."""
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
    notify = [r for r in d["late"] if r not in told]
    if notify:
        chain.add(
            s,
            "notify_guest",
            prio,
            f"Tell guests arriving today in {', '.join(notify)}: their room may be ready only "
            f"after check-in at {checkin}"
            + (f", by {latest} at the latest" if d["latest"] else "")
            + "; a ready time follows at check-in (proposed message; delivery not confirmed).",
            assets=notify,
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
    escalated: bool = False


class _Ledger:
    """When each cleaner and the on-call housekeeper is next free, from jobs given so far.

    Requests are planned in time order across rooms, so a promise never assumes a person
    who is already cleaning another room at that time.
    """

    def __init__(self, settings: Settings) -> None:
        self.clean = pd.Timedelta(minutes=settings.detect.clean_room_min)
        self.travel = pd.Timedelta(minutes=settings.detect.oncall_travel_min)
        self.robot_pass = pd.Timedelta(minutes=settings.detect.robot_pass_min)
        self.free: dict[str, pd.Timestamp] = {}
        self.offered: dict[str, pd.Timestamp] = {}
        self.answered: dict[str, pd.Timestamp] = {}

    def cleaner(self, d: Mapping[str, Any]) -> tuple[pd.Timestamp, str] | None:
        """Return the earliest start and the cleaner free first, within one of their shifts."""
        options = [
            (begin, c)
            for c, a, z in d["cleaners"]
            for begin in [max(a, self.free.get(c, a))]
            if begin + self.clean <= z
        ]
        return min(options, default=None)

    def oncall(self, d: Mapping[str, Any]) -> tuple[pd.Timestamp, str] | None:
        """Return when the on-call housekeeper can start, travelling only if not on site."""
        who = d["oncall"]
        if who is None:
            return None
        busy = self.free.get(who)
        return (busy if busy is not None and busy > d["start"] else d["start"] + self.travel, who)

    def robot(self, d: Mapping[str, Any]) -> tuple[pd.Timestamp, str] | None:
        """Return the earliest start and the robot on shift that is free first."""
        start = d["start"]
        return min(((max(start, self.free.get(r, start)), r) for r in d["robots"]), default=None)

    def book(
        self, begin: pd.Timestamp, who: str, length: pd.Timedelta | None = None
    ) -> pd.Timestamp:
        self.free[who] = begin + (self.clean if length is None else length)
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
    queue = d.get("ready_now")
    # The room's own queue beats the first free slot (e.g. the slot is tomorrow): book nothing.
    slow = cleaner is not None and queue is not None and cleaner[0] + ledger.clean > queue
    if slow:
        cleaner = None
    oncall = ledger.oncall(d) if d["waiting"] else None
    if oncall is not None and cleaner is not None and cleaner[0] <= oncall[0]:
        oncall = None
    # A waiting guest overrides a later plan when the on-call housekeeper is sooner.
    recall = (
        oncall is not None
        and d["vacant"]
        and not d["in_progress"]
        and guest.planned
        and guest.committed is not None
        and oncall[0] + ledger.clean < guest.committed
    )
    planned_for = guest.committed
    if d["in_progress"]:
        promise: pd.Timestamp | None = d["ready_prio"]
    elif recall:
        assert oncall is not None
        promise = oncall[0] + ledger.clean
    elif guest.committed is not None:
        promise = guest.committed if guest.committed > s.time else s.time + ledger.clean
    elif oncall is not None:
        promise = oncall[0] + ledger.clean
    elif slow:
        promise = queue
    else:
        promise = None if cleaner is None else cleaner[0] + ledger.clean
    ready = _local(promise, s.time, settings)
    verify = f"{room} shows cleaning_completed by {ready}; if not, update the guest and escalate."
    if d["in_progress"] and not guest.planned:
        guest.planned, guest.committed = True, promise
    if recall:
        assert oncall is not None
        begin, who = oncall
        chain.add(
            s,
            "schedule_cleaning",
            prio,
            f"The guest is waiting and {room} was planned only for "
            f"{_local(planned_for, s.time, settings)}; call the on-call housekeeper now: "
            f"{s.detail}.",
            resource_id=who,
            expected_outcome=f"{room} clean by {ready} (simulated).",
            verification_plan=verify,
            confidence=0.6,
        )
        guest.committed = ledger.book(begin, who)
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
        elif slow:
            guest.committed = promise
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
        robot = None if d["robot_done"] else ledger.robot(d)
        if robot is not None:
            begin, who = robot
            when = "" if begin <= d["start"] else f" from {_local(begin, s.time, settings)}"
            chain.add(
                s,
                "dispatch_robot",
                prio,
                f"Robot floor pass in {room}{when} before the human clean; robots do not "
                "certify or release a room.",
                resource_id=who,
                expected_outcome="Shorter human cleaning (simulated).",
                verification_plan=f"robot_floor_cleaning_completed in {room}.",
                confidence=0.7,
            )
            ledger.book(begin, who, ledger.robot_pass)
        guest.planned = True
    ledger.answered[room] = s.time
    late = promise is None or promise > d["wanted"]
    news = ""
    if guest.offered and not late:
        news = f"; {room} is on time, so the offer of {guest.offered} is withdrawn"
        ledger.offered.pop(guest.offered, None)
        guest.offered = None
    free = [r for r in d["alternatives"] if ledger.offered.get(r, s.time) <= s.time]
    if late and d["vacant"] and free and not guest.offered:
        guest.offered = free[0]
        ledger.offered[free[0]] = d["departure"]
        chain.add(
            s,
            "reassign_room",
            prio,
            f"{free[0]} is clean, vacant and not booked during the guest's stay, and {room} "
            f"misses the wanted time; offer it instead of {room}.",
            assets=[room, free[0]],
            expected_outcome="Guest checks in on time in another room (simulated).",
            verification_plan=f"Guest checks in to {free[0]}; {room} keeps its cleaning.",
            confidence=0.7,
        )
    elif guest.offered and d["waiting"] and not guest.escalated:
        guest.escalated = True
        chain.add(
            s,
            "request_human_approval",
            "critical",
            f"The guest is waiting and {room} is ready only at {ready}; decide the pending "
            f"offer of {guest.offered} now.",
            assets=[room, guest.offered],
            expected_outcome="A manager approves or declines the room move.",
            verification_plan=f"Guest checks in to {guest.offered} or {room}; escalate if not.",
            confidence=0.7,
        )
    tz = settings.local_tz
    overnight = promise is None or promise.tz_convert(tz).date() > s.time.tz_convert(tz).date()
    if d["waiting"] and overnight and not guest.offered and not guest.escalated:
        guest.escalated = True
        chain.add(
            s,
            "request_human_approval",
            "critical",
            f"The guest is waiting and {room} is ready only at {ready}, with no free room to "
            "offer; decide on an on-call cleaner or another arrangement now.",
            expected_outcome="A manager arranges the room tonight or the guest's stay.",
            verification_plan=f"Guest checks in to {room} today; escalate if not.",
            confidence=0.6,
        )
    offer = guest.offered
    message = f"{offer} is offered pending approval; otherwise {room}" if offer else f"{room}"
    chain.add(
        s,
        "notify_guest",
        prio,
        f"Tell the guest ({d['language']}): {message} expected ready at {ready}{news} "
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
            since = s.time - pd.Timedelta(hours=settings.detect.checkout_follow_h)
            told = {r for r, t in ledger.answered.items() if t >= since}
            _day_plan(chains[eid], s, settings, told)
            continue
        if s.kind == "ready":
            _ready(chains[eid], s)
            continue
        if s.kind == "overstay":
            guest = guests.setdefault((eid, s.data["booking"]), _Guest())
            _overstay(chains[eid], guest, s, ledger)
            continue
        guest = guests.setdefault((eid, s.data["booking"]), _Guest())
        if s.kind == "checkin_due" and guest.committed is not None and guest.committed > s.time:
            continue
        _turnaround_step(chains[eid], guest, s, ledger, settings)
    return [d for c in chains.values() for d in c.out]


def _heating(ep: Episode, settings: Settings) -> list[Decision]:
    th = settings.detect
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
    contractor = when is None or when - s.time > pd.Timedelta(hours=th.technician_horizon_h)
    if not contractor:
        chain.add(
            s,
            "dispatch_technician",
            "high",
            f"Service HP1 (COP {d['ratio']:.2f} of reference); technician on shift "
            f"{'now' if d['technician'] else 'from ' + _local(when, s.time, settings)}.",
            **({"resource_id": d["technician"]} if d["technician"] else {}),
            expected_outcome="Technician finds and fixes the fault (simulated).",
            verification_plan=(
                f"COP ratio back above {th.cop_ratio_low:.2f} within {th.contractor_after_h:.0f} h "
                "of the technician's start; a contractor is ordered if not."
            ),
            confidence=0.8,
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
        f"({peaks}) only where comfort holds. Comfort before cost.",
        assets=["HP1", "BUILDING"],
        expected_outcome="Occupied rooms stay in the comfort band (simulated).",
        verification_plan=f"Occupied rooms at or above {lo} degC; escalate rooms that fall below.",
        confidence=0.6,
    )
    deadline = None if contractor else when + pd.Timedelta(hours=th.contractor_after_h)
    told: set[str] = set()
    last: pd.Timestamp | None = None
    for s in ep.signals:
        if contractor or (deadline is not None and s.time >= deadline):
            why = (
                f"no technician on shift within {th.technician_horizon_h:.0f} h"
                if s is ep.signals[0]
                else f"COP still {s.data['ratio']:.2f} of reference "
                f"{th.contractor_after_h:.0f} h after the technician's start"
            )
            chain.add(
                s,
                "order_service",
                "high",
                f"Service HP1 by a contractor: {why}.",
                expected_outcome="Contractor fixes the fault (simulated).",
                verification_plan="24 h COP ratio recovers after the visit.",
                confidence=0.7,
            )
            contractor, deadline = False, None
        new = [
            (r, b)
            for r, b in zip(s.data["cold_rooms"], s.data["cold_stays"], strict=True)
            if b not in told
        ]
        if not new or (last is not None and s.time - last < pd.Timedelta(hours=th.notice_gap_h)):
            continue
        last = s.time
        rooms = [r for r, _ in new]
        chain.add(
            s,
            "notify_guest",
            "high",
            f"Tell guests in {', '.join(rooms)} that heating is reduced and a repair is "
            "requested (proposed message; delivery not confirmed).",
            assets=rooms,
            expected_outcome="Guests know the cause and that help is coming.",
            verification_plan="No temperature_cold request from these rooms; escalate on one.",
            confidence=0.7,
        )
        told.update(b for _, b in new)
    return chain.out


def _logistics(ep: Episode, settings: Settings) -> list[Decision]:
    chain = _Chain(ep)
    s = ep.signals[0]
    d = s.data
    service = "waste pickup" if s.kind == "pickup" else "vending restock"
    arrive = _local(s.time + d["lead"], s.time, settings)
    chain.add(
        s,
        "order_service",
        "medium",
        f"Order an extra {service} (lead {d['lead'].total_seconds() / 3600:.0f} h, by "
        f"{arrive}): {s.detail}.",
        expected_outcome=f"Extra {service} before {_local(d['empty'], s.time, settings)} "
        "(simulated).",
        verification_plan=(
            f"A {'waste_pickup' if s.kind == 'pickup' else 'vending_restocked'} event by "
            f"{arrive} and no {'waste_bin_full' if s.kind == 'pickup' else 'vending_out_of_stock'}"
            "; escalate otherwise."
        ),
        confidence=0.7,
    )
    return chain.out


def _data_quality(ep: Episode) -> list[Decision]:
    chain = _Chain(ep)
    s = ep.signals[0]
    texts = OUTAGE if s.kind == "floor_outage" else DEAD if s.kind == "sensor_dead" else JITTER
    chain.add(s, "flag_anomaly", "low", f"{WHAT[s.kind]}: {s.detail}.", **texts)
    return chain.out


def _one_technician(decisions: list[Decision], settings: Settings) -> None:
    """Send a repair to a contractor while the in-house technician holds another job.

    Scenarios plan independently; this pass walks all technician dispatches in time order
    so S02 and S03 never count on the one technician at the same time.
    """
    hold = pd.Timedelta(hours=settings.detect.technician_job_h)
    busy: tuple[pd.Timestamp, str] | None = None
    jobs = [d for d in decisions if d["action"] == "dispatch_technician"]
    for d in sorted(jobs, key=lambda d: (d["timestamp_utc"], d["event_id"])):
        t, asset = pd.Timestamp(d["timestamp_utc"]), d["affected_assets"][0]
        if busy is None or t >= busy[0] + hold or busy[1] == asset:
            busy = (t, asset)
            continue
        d["action"] = "order_service"
        d.pop("resource_id", None)
        d["reason"] = (
            f"In-house technician is on {busy[1]} since {stamp(busy[0])}; order a contractor "
            f"instead. {d['reason']}"
        )
        d["expected_outcome"] = "Contractor finds and repairs the fault (simulated)."


def decisions(episodes: list[Episode], states: RoomStates, settings: Settings) -> list[Decision]:
    """Return the decisions of all episodes, without approval flags."""
    out = _turnaround([ep for ep in episodes if ep.issue_type == "room_turnaround"], settings)
    for ep in episodes:
        if ep.issue_type == "water_leak":
            out += _water(ep, states, settings)
        elif ep.issue_type == "heating_comfort":
            out += _heating(ep, settings)
        elif ep.issue_type == "logistics":
            out += _logistics(ep, settings)
        elif ep.issue_type == "data_quality":
            out += _data_quality(ep)
        elif ep.issue_type != "room_turnaround":
            raise ValueError(f"no plan for issue_type {ep.issue_type!r}")
    _one_technician(out, settings)
    return out
