"""Closed loop: check each decision's outcome when it is due, and follow up on a miss.

Every follow-up is a new decision at the time the check becomes possible, using only data
known then, and depends on the decision it checks:

- Leak repair: a corrective work order on the asset completes (known at ``completed_utc``).
  ``repair_verify_h`` later, a leak signal since then means the repair did not hold: ask a
  human. Otherwise a room the episode blocked is released once it is clean.
- Heat pump service: once the COP window after the service has passed, a ``cop_low`` verdict
  in the following hour means the service did not restore it: ask a human.
- Cold room: ``room_heat_verify_h`` after a setpoint boost the occupied room is still below
  the comfort band: request an inspection.
- Repeat complaint: a guest's second cold complaint in a stay asks a human for a staff visit
  or a warmer room; a third does so as critical.
- Ready time: ``promise_grace_min`` after a promised ready time the room is neither clean nor
  occupied, and nothing was said to that room since: tell the guest it is late.
"""

from __future__ import annotations

import math
import re
from collections import Counter

import pandas as pd

from plato40.contract import Decision
from plato40.detect.grid import local_time, stamp
from plato40.episodes import Episode, Signal
from plato40.load.derive import telemetry_wide
from plato40.load.reader import Dataset
from plato40.load.units import BOUNDS
from plato40.settings import Settings
from plato40.state import RoomStates

REPAIRS = ("dispatch_technician", "order_service")
PROMISE = re.compile(
    r"^Tell the guest \((?P<lang>\w\w)\): (?:.*?otherwise )?(?P<room>R\d{3}) expected ready at "
    r"(?:\w{3} (?P<day>\d\d) (?P<mon>\w{3}) )?(?P<time>\d\d:\d\d) local"
)
SUFFIX = " (proposed message; delivery not confirmed)."


class _Out:
    """Follow-up decisions, numbered per episode."""

    def __init__(self) -> None:
        self.decisions: list[Decision] = []
        self.seq: Counter[str] = Counter()

    def add(
        self,
        of: Decision,
        t: pd.Timestamp,
        action: str,
        priority: str,
        assets: list[str],
        reason: str,
        evidence: list[str],
        verify: str,
        **extra: object,
    ) -> None:
        eid = str(of["episode_id"])
        self.seq[eid] += 1
        d: Decision = {
            "event_id": f"{eid}-F{self.seq[eid]:02d}",
            "episode_id": eid,
            "timestamp_utc": stamp(t.ceil("min")),
            "affected_assets": assets,
            "issue_type": of["issue_type"],
            "action": action,
            "priority": priority,
            "reason": reason,
            "requires_human_approval": False,
            "evidence": evidence,
            "dependencies": [of["event_id"]],
            "expected_outcome": "The miss is handled by a person (simulated).",
            "verification_plan": verify,
            "confidence": 0.7,
        }
        d.update(extra)
        self.decisions.append(d)


def _first(decisions: list[Decision], eid: str, actions: tuple[str, ...]) -> Decision | None:
    mine = [d for d in decisions if d["episode_id"] == eid and d["action"] in actions]
    return min(mine, key=lambda d: d["timestamp_utc"]) if mine else None


def _repairs(
    ds: Dataset,
    episodes: list[Episode],
    decisions: list[Decision],
    states: RoomStates,
    settings: Settings,
    out: _Out,
) -> None:
    th = settings.detect
    wo = ds.table("work_orders.csv")
    for ep in episodes:
        if ep.issue_type not in ("water_leak", "heating_comfort"):
            continue
        sent = _first(decisions, ep.episode_id, REPAIRS)
        if sent is None:
            continue
        asset = sent["affected_assets"][0]
        start = ep.signals[0].time
        done = wo.loc[
            (wo["asset_id"] == asset) & (wo["created_utc"] >= start) & wo["completed_utc"].notna()
        ].sort_values("completed_utc", kind="stable")
        if done.empty:
            continue
        order = done.iloc[0]
        fixed = pd.Timestamp(order["completed_utc"])
        ref = f"work_orders.csv:{order['work_order_id']}"
        if ep.issue_type == "water_leak":
            _leak(ep, sent, fixed, ref, states, settings, decisions, out)
            continue
        after = fixed + pd.Timedelta(hours=th.cop_window_h)
        still = [
            s
            for s in ep.signals
            if s.kind == "cop_low" and after < s.time <= after + pd.Timedelta(hours=1)
        ]
        if still:
            out.add(
                sent,
                still[0].time,
                "request_human_approval",
                "high",
                [asset],
                f"{asset} still runs below its reference COP {th.cop_window_h} h after the "
                f"service completed ({stamp(fixed)}): {still[0].detail}. Decide on a second "
                "contractor visit.",
                [ref, *still[0].evidence],
                f"{asset} COP back above {th.cop_ratio_low:.2f} of its reference within "
                f"{th.cop_window_h} h of the next service.",
            )


def _leak(
    ep: Episode,
    sent: Decision,
    fixed: pd.Timestamp,
    ref: str,
    states: RoomStates,
    settings: Settings,
    decisions: list[Decision],
    out: _Out,
) -> None:
    room = ep.assets[0]
    due = fixed + pd.Timedelta(hours=settings.detect.repair_verify_h)
    again: list[Signal] = [
        s for s in ep.signals if fixed < s.time <= due and s.kind != "night_blank"
    ]
    if again:
        out.add(
            sent,
            again[0].time,
            "request_human_approval",
            "critical",
            [room],
            f"Leak on {room} after the repair completed at {stamp(fixed)}: {again[0].detail}. "
            "Decide on a second repair.",
            [ref, *again[0].evidence],
            f"No leak signal on {room} for {settings.detect.repair_verify_h:.0f} h after the "
            "next repair.",
        )
        return
    blocked = _first(decisions, ep.episode_id, ("block_room",))
    if blocked is None or room not in states.rooms:
        return
    t = due if states.at(room, due) == "clean" else states.next_entry(room, "clean", due)
    if t is None:
        return
    out.add(
        blocked,
        t,
        "release_room",
        "medium",
        [room],
        f"Release {room}: the repair completed at {stamp(fixed)} and no leak signal followed "
        f"for {settings.detect.repair_verify_h:.0f} h; the room is clean.",
        [ref, f"events.jsonl:{room} clean at {stamp(t)}"],
        f"{room} can be sold again; reopen the leak episode on any new signal.",
        expected_outcome=f"{room} is back in the sellable stock (simulated).",
    )


def _cold_rooms(
    ds: Dataset, decisions: list[Decision], states: RoomStates, settings: Settings, out: _Out
) -> None:
    th = settings.detect
    lo = float(ds.building["constraints"]["occupied_comfort_c"][0])
    bound = BOUNDS[("room_telemetry.csv", "temperature_c")]
    temp = telemetry_wide(ds, "temperature_c")
    temp = temp.where((temp >= bound.low) & (temp <= bound.high))
    recent = pd.Timedelta(hours=th.room_reading_h)
    for d in decisions:
        if d["action"] != "adjust_room_setpoint" or d["issue_type"] != "heating_comfort":
            continue
        room, t0 = d["affected_assets"][0], pd.Timestamp(d["timestamp_utc"])
        t = t0 + pd.Timedelta(hours=th.room_heat_verify_h)
        if room not in temp.columns or states.since(room, t) != states.since(room, t0):
            continue
        col = temp[room].loc[(temp.index > t - recent) & (temp.index <= t)].dropna()
        if col.empty or col.iloc[-1] >= lo or states.at(room, t) != "occupied":
            continue
        out.add(
            d,
            t,
            "request_inspection",
            "high",
            [room],
            f"{room} is still {math.floor(col.iloc[-1] * 10) / 10:.1f} degC, below {lo:.0f} degC, "
            f"{th.room_heat_verify_h:.0f} h after its setpoint was raised: inspect the room's "
            "heating.",
            [f"room_telemetry.csv:{room} temperature_c:{stamp(t - recent)}..{stamp(t)}"],
            f"{room} at or above {lo:.0f} degC after the inspection; otherwise offer the guest "
            "another room.",
        )


def _repeats(ds: Dataset, decisions: list[Decision], out: _Out) -> None:
    req = ds.table("guest_requests.csv")
    cold = req.loc[req["request_type"] == "temperature_cold"].sort_values(
        "timestamp_utc", kind="stable"
    )
    answered = {g: d for d in decisions for g in d.get("guest_request_ids", [])}
    seen: Counter[str] = Counter()
    before: dict[str, Decision] = {}
    for _, r in cold.iterrows():
        booking, rid = str(r["booking_id"]), str(r["request_id"])
        seen[booking] += 1
        n, earlier = seen[booking], before.get(booking)
        if rid in answered:
            before[booking] = answered[rid]
        if n < 2 or earlier is None:
            continue
        room = str(r["room_id"])
        out.add(
            earlier,
            pd.Timestamp(r["timestamp_utc"]),
            "request_human_approval",
            "critical" if n >= 3 else "high",
            [room],
            f"Complaint {n} about cold from the guest of {booking} in {room}: the earlier "
            "answer did not solve it. Decide on a staff visit or a warmer room.",
            [f"guest_requests.csv:{rid}", f"bookings.csv:{booking}"],
            "A staff visit or room offer is recorded before the request's due time.",
            guest_request_ids=[rid],
        )


def promised_at(d: Decision, tz: str) -> tuple[str, pd.Timestamp, str] | None:
    """Return the room, the UTC ready time and the language of a room-turnaround promise."""
    m = PROMISE.match(d["reason"]) if d["issue_type"] == "room_turnaround" else None
    if m is None:
        return None
    t0 = pd.Timestamp(d["timestamp_utc"])
    local = t0.tz_convert(tz)
    day = f"{local.year}-{m['mon']}-{m['day']}" if m["day"] else f"{local:%Y-%m-%d}"
    promised = pd.Timestamp(f"{day} {m['time']}", tz=tz).tz_convert("UTC")
    if promised < t0:
        promised += pd.DateOffset(years=1)
    return str(m["room"]), promised, str(m["lang"])


def _promises(decisions: list[Decision], states: RoomStates, settings: Settings, out: _Out) -> None:
    tz = settings.local_tz
    grace = pd.Timedelta(minutes=settings.detect.promise_grace_min)
    said: dict[str, list[pd.Timestamp]] = {}
    for d in decisions:
        if d["action"] == "notify_guest":
            for a in d["affected_assets"]:
                said.setdefault(a, []).append(pd.Timestamp(d["timestamp_utc"]))
    for d in decisions:
        found = promised_at(d, tz)
        if found is None:
            continue
        t0 = pd.Timestamp(d["timestamp_utc"])
        room, promised, lang = found
        t = promised + grace
        if states.at(room, t) in ("clean", "occupied"):
            continue
        if any(t0 < s <= t for s in said.get(room, [])):
            continue
        out.add(
            d,
            t,
            "notify_guest",
            "high",
            [room],
            f"Tell the guest ({lang}): {room} is not ready at the promised "
            f"{local_time(promised, t, tz)}; staff are on it and a new ready time follows" + SUFFIX,
            [f"events.jsonl:{room} {states.at(room, t)} at {stamp(t)}"],
            "Guest gets the new ready time; escalate if the guest waits.",
            expected_outcome="Guest is not left with a promise that has passed.",
            **({"guest_request_ids": d["guest_request_ids"]} if "guest_request_ids" in d else {}),
        )


def _early_ready(ds: Dataset, decisions: list[Decision], settings: Settings, out: _Out) -> None:
    """Tell a guest holding a ready time, and not checked in, that the room is clean now."""
    tz = settings.local_tz
    grace = pd.Timedelta(minutes=settings.detect.promise_grace_min)
    ev = ds.events.sort_values(["timestamp_utc", "event_id"], kind="stable")
    done = ev.loc[ev["type"] == "cleaning_completed"]
    entered = ev.loc[ev["type"].isin(["checkin", "checkin_after_wait", "checkin_early"])]
    said: dict[str, list[pd.Timestamp]] = {}
    last: dict[str, tuple[Decision, pd.Timestamp, pd.Timestamp, str]] = {}
    for d in decisions:
        if d["action"] == "notify_guest":
            for a in d["affected_assets"]:
                said.setdefault(a, []).append(pd.Timestamp(d["timestamp_utc"]))
    for d in sorted(decisions, key=lambda d: d["timestamp_utc"]):
        found = promised_at(d, tz)
        if found is not None:
            room, promised, lang = found
            last[room] = (d, pd.Timestamp(d["timestamp_utc"]), promised, lang)
    for room, (d, t0, promised, lang) in last.items():
        mine = done.loc[(done["room_id"] == room) & (done["timestamp_utc"] > t0)]
        if mine.empty:
            continue
        tc = pd.Timestamp(mine["timestamp_utc"].iloc[0])
        came = entered.loc[
            (entered["room_id"] == room) & (entered["timestamp_utc"].between(t0, tc))
        ]
        if not came.empty or any(t0 < s <= tc for s in said.get(room, [])):
            continue
        out.add(
            d,
            tc,
            "notify_guest",
            "high",
            [room],
            f"Tell the guest ({lang}): {room} is ready now"
            + (
                f", before the promised {local_time(promised, tc, tz)}"
                if tc + grace <= promised
                else ""
            )
            + "; self check-in works"
            + SUFFIX,
            [f"events.jsonl:{mine['event_id'].iloc[0]}"],
            "A check-in to the room follows; escalate if the guest waits on.",
            expected_outcome="Guest checks in as soon as the room is clean (simulated).",
            **({"guest_request_ids": d["guest_request_ids"]} if "guest_request_ids" in d else {}),
        )


def follow_ups(
    ds: Dataset,
    episodes: list[Episode],
    decisions: list[Decision],
    states: RoomStates,
    settings: Settings,
) -> list[Decision]:
    """Return the follow-up decisions for ``decisions``, before the policy sets their flags."""
    out = _Out()
    _repairs(ds, episodes, decisions, states, settings, out)
    _cold_rooms(ds, decisions, states, settings, out)
    _repeats(ds, decisions, out)
    _promises(decisions, states, settings, out)
    _early_ready(ds, decisions, settings, out)
    return out.decisions
