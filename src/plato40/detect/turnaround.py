"""S01 turnaround conflicts, judged at each guest request and waiting event.

Triggers are early check-in and waiting-for-room requests and ``arrival_waiting_room``
events.
The requested time is the first ``HH:MM`` in the request text (present in every early
check-in request of quickstart and dev), on the booking's local arrival day; without
one, the guest wants the room now. A room is ready ``clean_room_min`` after a cleaner
can start on it: after checkout and once a cleaner is on shift. Without action the room
waits behind the other dirty rooms, shared over the cleaners on shift. A conflict is a
predicted ready time later than the requested time.

If the room is still occupied at request time, a second signal fires at the actual
checkout, when cleaning can be scheduled. Only facts known at each signal time are used:
bookings created before it, roster absences known before it.
"""

from __future__ import annotations

import re
from datetime import datetime, time
from typing import Any

import pandas as pd

from plato40.detect.grid import stamp
from plato40.episodes import Signal
from plato40.load.reader import Dataset
from plato40.settings import Settings
from plato40.staff import Roster
from plato40.state import RoomStates

ISSUE = "room_turnaround"
REQUESTS = ("early_checkin", "waiting_for_room")
CLOCK = re.compile(r"\b(\d{1,2})[:.h](\d{2})\b")


class _Facts:
    """Shared inputs for predicting when a room can be ready."""

    def __init__(self, ds: Dataset, settings: Settings) -> None:
        self.ds = ds
        self.tz = settings.local_tz
        self.clean = pd.Timedelta(minutes=settings.detect.clean_room_min)
        self.travel = pd.Timedelta(minutes=settings.detect.oncall_travel_min)
        self.states = RoomStates(ds)
        self.roster = Roster(ds)
        self.bookings = ds.table("bookings.csv")
        self.max_guests = {
            str(r["id"]): int(r["max_guests"]) for r in ds.building["building"]["rooms"]
        }
        staff = ds.building["staff"]
        self.oncall_ids = [
            str(r["resource_id"]) for r in staff["resources"] if r["role"] == "on_call_housekeeping"
        ]
        m = re.search(r"(\d\d):(\d\d)-(\d\d):(\d\d)", staff["notes"]["on_call_housekeeping"])
        self.oncall = (time(int(m[1]), int(m[2])), time(int(m[3]), int(m[4]))) if m else None

    def local(self, t: pd.Timestamp) -> str:
        return f"{t.tz_convert(self.tz):%H:%M}"

    def plan_at(self, room: str, start: pd.Timestamp, known: pd.Timestamp) -> dict[str, Any]:
        """Predict cleaning of a vacant ``room`` from ``start`` with what is known at ``known``."""
        cleaners = self.roster.on_shift("cleaner", start, known)
        begin = start if cleaners else self.roster.next_start("cleaner", start, known)
        facts: dict[str, Any] = {"cleaner": cleaners[0] if cleaners else None}
        robots = self.roster.on_shift("robot", start, known)
        facts["robot"] = robots[0] if robots else None
        if begin is None:
            facts["ready_prio"] = facts["ready_now"] = None
        else:
            queue = len(self.states.rooms_in("dirty", known) - {room})
            n = max(len(self.roster.on_shift("cleaner", begin, known)), 1)
            facts["ready_prio"] = begin + self.clean
            facts["ready_now"] = begin + self.clean * (1 + queue // n)
        facts["oncall"] = None
        if not cleaners and self.oncall and self.oncall_ids:
            wall = start.tz_convert(self.tz).time()
            if self.oncall[0] <= wall < self.oncall[1]:
                facts["oncall"] = self.oncall_ids[0]
                facts["ready_oncall"] = start + self.travel + self.clean
        return facts

    def occupant_departure(
        self, room: str, booking_id: str, t: pd.Timestamp
    ) -> pd.Timestamp | None:
        b = self.bookings
        mine = b.loc[
            (b["room_id"] == room)
            & (b["booking_id"] != booking_id)
            & (b["created_utc"] <= t)
            & (b["arrival_utc"] <= t)
        ]
        if mine.empty:
            return None
        return max(pd.Timestamp(mine.sort_values("arrival_utc")["departure_utc"].iloc[-1]), t)

    def alternative(
        self, room: str, guests: int, t: pd.Timestamp, until: pd.Timestamp
    ) -> str | None:
        """Return a clean vacant room with enough beds and no arrival booked up to ``until``."""
        b = self.bookings
        booked = set(
            b.loc[
                (b["created_utc"] <= t) & (b["arrival_utc"] <= until) & (b["departure_utc"] > t),
                "room_id",
            ].astype(str)
        )
        free = sorted(
            r
            for r in self.states.rooms_in("clean", t)
            if r != room and r not in booked and self.max_guests.get(r, 0) >= guests
        )
        return free[0] if free else None


def requested_time(text: str, arrival: pd.Timestamp, t: pd.Timestamp, tz: str) -> pd.Timestamp:
    """Return the requested local time on the arrival day (UTC), never before ``t``."""
    m = CLOCK.search(text)
    if m is None or int(m[1]) > 23 or int(m[2]) > 59:
        return t
    day = arrival.tz_convert(tz).date()
    wanted = pd.Timestamp(datetime.combine(day, time(int(m[1]), int(m[2])))).tz_localize(tz)
    return max(wanted.tz_convert("UTC"), t)


def _triggers(ds: Dataset) -> list[dict[str, Any]]:
    """Return S01 triggers sorted by time: requests and ``arrival_waiting_room`` events.

    A waiting event has no request; the guest's language is taken from their latest
    earlier request, else English.
    """
    req = ds.table("guest_requests.csv").sort_values("timestamp_utc", kind="stable")
    rows: list[dict[str, Any]] = []
    for r in req.to_dict("records"):
        if r["request_type"] in REQUESTS:
            rows.append(
                {
                    "t": pd.Timestamp(r["timestamp_utc"]),
                    "room": str(r["room_id"]),
                    "booking": str(r["booking_id"]),
                    "waiting": r["request_type"] == "waiting_for_room",
                    "text": str(r["text"]),
                    "request_id": str(r["request_id"]),
                    "language": str(r["language"]),
                    "ref": f"guest_requests.csv:{r['request_id']}",
                }
            )
    ev = ds.events
    for e in ev.loc[ev["type"] == "arrival_waiting_room"].to_dict("records"):
        t, bid = pd.Timestamp(e["timestamp_utc"]), str(e["booking_id"])
        earlier = req.loc[(req["booking_id"] == bid) & (req["timestamp_utc"] <= t), "language"]
        rows.append(
            {
                "t": t,
                "room": str(e["room_id"]),
                "booking": bid,
                "waiting": True,
                "text": "",
                "request_id": None,
                "language": str(earlier.iloc[-1]) if len(earlier) else "en",
                "ref": f"events.jsonl:{e['event_id']}",
            }
        )
    return sorted(rows, key=lambda x: (x["t"], x["ref"]))


def signals(ds: Dataset, settings: Settings) -> list[Signal]:
    """Return a signal per conflicting request, plus one at checkout if still occupied."""
    f = _Facts(ds, settings)
    arrival = dict(zip(f.bookings["booking_id"], f.bookings["arrival_utc"], strict=True))
    guest_count = dict(zip(f.bookings["booking_id"], f.bookings["guest_count"], strict=True))
    out: list[Signal] = []
    for r in _triggers(ds):
        t, room, bid, waiting = r["t"], r["room"], r["booking"], r["waiting"]
        state = f.states.at(room, t)
        if state not in ("occupied", "dirty", "cleaning") or bid not in arrival:
            continue
        wanted = t if waiting else requested_time(r["text"], arrival[bid], t, f.tz)
        if state == "occupied":
            checkout = f.occupant_departure(room, bid, t)
            if checkout is None:
                continue
        else:
            checkout = t
        facts = f.plan_at(room, checkout, t)
        if state == "cleaning":
            facts["ready_prio"] = facts["ready_now"] = t + f.clean
        ready = facts["ready_now"]
        if ready is not None and ready <= wanted:
            continue
        guests = int(guest_count[bid])
        facts |= {
            "request_id": r["request_id"],
            "language": r["language"],
            "wanted": wanted,
            "waiting": waiting,
            "vacant": state != "occupied",
            "alternative": f.alternative(room, guests, t, wanted),
        }
        promise = facts["ready_prio"]
        detail = (
            f"guest of {bid} wants the room at {f.local(wanted)} local; room is {state}; "
            f"predicted ready {f.local(ready) if ready is not None else 'not today'} without "
            f"action, {f.local(promise) if promise is not None else 'not today'} if prioritised"
        )
        evidence = (
            r["ref"],
            f"bookings.csv:{bid}",
            f"events.jsonl:{room} {state} at {stamp(t)}",
        )
        out.append(Signal(t, ISSUE, "request", (room,), evidence, detail, facts))
        if state == "occupied":
            actual = f.states.next_entry(room, "dirty", t)
            if actual is not None and actual - t < pd.Timedelta(hours=24):
                later = f.plan_at(room, actual, actual) | {
                    k: facts[k] for k in ("request_id", "language", "wanted", "waiting")
                }
                later["vacant"] = True
                later["alternative"] = None
                out.append(
                    Signal(
                        actual,
                        ISSUE,
                        "checkout",
                        (room,),
                        (f"events.jsonl:{room} checkout at {stamp(actual)}",),
                        f"{room} checked out at {f.local(actual)} local; guest of {bid} "
                        f"wants it at {f.local(wanted)} local",
                        later,
                    )
                )
    return out
