"""Action chains per episode. Approval flags are left to the policy.

Water leak (room): a slow leak waits ``leak_persist_nights`` consecutive nights, then
flags and inspects; at ``leak_technician_nights`` it requests a technician. A burst
flags, inspects and requests a technician at once, all critical. Inspecting an occupied
room first asks the guest for consent. Water is never shut off by the system.
Water leak (main-minus-rooms gap): flag, then technician, on the same night counts.
Turnaround: prioritise the cleaning (and a robot pass) once the room is vacant, call the
on-call housekeeper only when no cleaner is on shift, offer a ready room (approval), and
tell the guest a realistic time.
Heating: flag, technician (or contractor if none within 24 h), a comfort-first energy plan,
and at most one notice per 12 h to occupied rooms newly in comfort deficit.
Data quality: one flag per outage episode.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from plato40.contract import Decision
from plato40.detect.grid import stamp
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
            "timestamp_utc": stamp(s.time),
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


def _local(t: pd.Timestamp | None, settings: Settings) -> str:
    return "not today" if t is None else f"{t.tz_convert(settings.local_tz):%H:%M} local"


def _turnaround(ep: Episode, settings: Settings) -> list[Decision]:
    chain = _Chain(ep)
    cleaning_planned = False
    offered: str | None = None
    for s in ep.signals:
        d = s.data
        room = s.assets[0]
        lead = d["wanted"] - s.time
        prio = "critical" if d["waiting"] else "high" if lead <= pd.Timedelta(hours=3) else "medium"
        promise = d["ready_prio"]
        verify = (
            f"{room} shows cleaning_completed by {_local(promise, settings)}; "
            "if not, update the guest and escalate."
        )
        if d["vacant"] and not cleaning_planned:
            if d["cleaner"]:
                chain.add(
                    s,
                    "schedule_cleaning",
                    prio,
                    f"Clean {room} first: {s.detail}.",
                    resource_id=d["cleaner"],
                    expected_outcome=f"{room} clean by {_local(promise, settings)} (simulated).",
                    verification_plan=verify,
                    confidence=0.7,
                )
                cleaning_planned = True
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
            if d["oncall"] and not cleaning_planned:
                promise = d["ready_oncall"]
                chain.add(
                    s,
                    "schedule_cleaning",
                    prio,
                    f"No cleaner on shift; call the on-call housekeeper for {room}: {s.detail}.",
                    resource_id=d["oncall"],
                    expected_outcome=f"{room} clean by {_local(promise, settings)} (simulated).",
                    verification_plan=verify,
                    confidence=0.6,
                )
                cleaning_planned = True
        offer = offered or d["alternative"]
        if offer and not offered:
            chain.add(
                s,
                "reassign_room",
                prio,
                f"{offer} is clean, vacant and not booked before the guest arrives; offer it "
                f"instead of {room}.",
                assets=[room, offer],
                expected_outcome="Guest checks in on time in another room (simulated).",
                verification_plan=f"Guest checks in to {offer}; {room} keeps its cleaning.",
                confidence=0.7,
            )
            offered = offer
        message = f"{offer} is offered pending approval; otherwise {room}" if offer else f"{room}"
        chain.add(
            s,
            "notify_guest",
            prio,
            f"Tell the guest ({d['language']}): {message} expected ready at "
            f"{_local(promise, settings)} (proposed message; delivery not confirmed).",
            **({"guest_request_ids": [d["request_id"]]} if d["request_id"] else {}),
            expected_outcome="Guest knows a realistic ready time.",
            verification_plan="No waiting_for_room request from the guest; escalate on one.",
            confidence=0.7,
        )
    return chain.out


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
            f"{'now' if d['technician'] else 'from ' + _local(when, settings)}.",
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
        f"coldest forecast hour {d['cold'][0]:.1f} degC at {_local(d['cold'][1], settings)}"
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


def decisions(ep: Episode, states: RoomStates, settings: Settings) -> list[Decision]:
    """Return the decisions of one episode, without approval flags."""
    if ep.issue_type == "water_leak":
        return _water(ep, states, settings)
    if ep.issue_type == "room_turnaround":
        return _turnaround(ep, settings)
    if ep.issue_type == "heating_comfort":
        return _heating(ep, settings)
    if ep.issue_type == "data_quality":
        return _data_quality(ep)
    raise ValueError(f"no plan for issue_type {ep.issue_type!r}")
