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

from plato40.detect.grid import local_time, stamp
from plato40.episodes import Signal
from plato40.load.reader import Dataset
from plato40.settings import Settings
from plato40.staff import Roster
from plato40.state import RoomStates

ISSUE = "room_turnaround"
REQUESTS = ("early_checkin", "waiting_for_room")
Spans = dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]]
CLOCK = re.compile(r"\b(\d{1,2})[:.h](\d{2})\b")


class _Facts:
    """Shared inputs for predicting when a room can be ready."""

    def __init__(self, ds: Dataset, settings: Settings, out_of_order: Spans) -> None:
        self.out_of_order = out_of_order
        self.tz = settings.local_tz
        self.clean = pd.Timedelta(minutes=settings.detect.clean_room_min)
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

    def local(self, t: pd.Timestamp | None, ref: pd.Timestamp) -> str:
        return local_time(t, ref, self.tz)

    def plan_at(self, room: str, start: pd.Timestamp, known: pd.Timestamp) -> dict[str, Any]:
        """Predict cleaning of a vacant ``room`` from ``start`` with what is known at ``known``.

        ``cleaners`` are those on shift at ``begin``, the first moment a cleaner is on shift
        from ``start``; the planner picks the one free first. The on-call housekeeper is an
        option only while no cleaner is on shift and inside the on-call window.
        """
        now = self.roster.on_shift("cleaner", start, known)
        begin = start if now else self.roster.next_start("cleaner", start, known)
        facts: dict[str, Any] = {
            "start": start,
            "begin": begin,
            "cleaner_now": bool(now),
            "cleaners": [] if begin is None else self.roster.on_shift("cleaner", begin, known),
        }
        facts["robots"] = self.roster.on_shift("robot", start, known)
        if begin is None:
            facts["ready_prio"] = facts["ready_now"] = None
        else:
            queue = len(self.states.rooms_in("dirty", known) - {room})
            n = max(len(facts["cleaners"]), 1)
            facts["ready_prio"] = begin + self.clean
            facts["ready_now"] = begin + self.clean * (1 + queue // n)
        facts["oncall"] = None
        if not now and self.oncall and self.oncall_ids:
            wall = start.tz_convert(self.tz).time()
            if self.oncall[0] <= wall < self.oncall[1]:
                facts["oncall"] = self.oncall_ids[0]
        return facts

    def occupant_departure(
        self, room: str, booking_id: str, t: pd.Timestamp
    ) -> pd.Timestamp | None:
        b = self.bookings
        mine = b.loc[
            (b["room_id"] == room)
            & (b["booking_id"] != booking_id)
            & (b["status"] != "cancelled")
            & (b["created_utc"] <= t)
            & (b["arrival_utc"] <= t)
        ]
        if mine.empty:
            return None
        return max(pd.Timestamp(mine.sort_values("arrival_utc")["departure_utc"].iloc[-1]), t)

    def alternatives(
        self, room: str, guests: int, t: pd.Timestamp, until: pd.Timestamp
    ) -> list[str]:
        """Return clean vacant rooms with enough beds and no other booking before ``until``.

        ``until`` is the requesting guest's departure, so the offered room is free for
        the whole stay and no later arrival loses its room.
        """
        b = self.bookings
        booked = set(
            b.loc[
                (b["created_utc"] <= t)
                & (b["arrival_utc"] < until)
                & (b["departure_utc"] > t)
                & ~((b["status"] == "cancelled") & (b["arrival_utc"] <= t)),
                "room_id",
            ].astype(str)
        )
        free = sorted(
            r
            for r in self.states.rooms_in("clean", t)
            if r != room
            and r not in booked
            and self.max_guests.get(r, 0) >= guests
            and not any(a <= t < z for a, z in self.out_of_order.get(r, []))
        )
        return free


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


def signals(ds: Dataset, settings: Settings, out_of_order: Spans | None = None) -> list[Signal]:
    """Return request and checkout signals, plus one day-plan signal per day at risk.

    ``out_of_order`` holds per room the spans with a suspected leak; they are never offered.
    """
    f = _Facts(ds, settings, out_of_order or {})
    arrival = dict(zip(f.bookings["booking_id"], f.bookings["arrival_utc"], strict=True))
    departure = dict(zip(f.bookings["booking_id"], f.bookings["departure_utc"], strict=True))
    guest_count = dict(zip(f.bookings["booking_id"], f.bookings["guest_count"], strict=True))
    follow = pd.Timedelta(hours=settings.detect.checkout_follow_h)
    out: list[Signal] = []
    for r in _triggers(ds):
        t, room, bid, waiting = r["t"], r["room"], r["booking"], r["waiting"]
        state = f.states.at(room, t)
        if bid not in arrival:
            continue
        just = f.states.at(room, t - pd.Timedelta(seconds=1))
        if waiting and state == "clean" and just in ("dirty", "cleaning"):
            out.append(
                Signal(
                    t,
                    ISSUE,
                    "ready",
                    (room,),
                    (r["ref"], f"events.jsonl:{room} cleaning_completed at {stamp(t)}"),
                    f"guest of {bid} is waiting; {room} became clean at {f.local(t, t)}",
                    {
                        "booking": bid,
                        "request_id": r["request_id"],
                        "language": r["language"],
                    },
                )
            )
            continue
        if state not in ("occupied", "dirty", "cleaning"):
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
        guest = {
            "booking": bid,
            "departure": departure[bid],
            "request_id": r["request_id"],
            "language": r["language"],
            "wanted": wanted,
            "waiting": waiting,
        }
        facts |= guest | {
            "vacant": state != "occupied",
            "in_progress": state == "cleaning",
            "alternatives": f.alternatives(room, int(guest_count[bid]), t, departure[bid]),
        }
        detail = (
            f"guest of {bid} wants the room at {f.local(wanted, t)}; room is {state}; "
            f"predicted ready {f.local(ready, t)} without action"
        )
        evidence = (
            r["ref"],
            f"bookings.csv:{bid}",
            f"events.jsonl:{room} {state} at {stamp(t)}",
        )
        out.append(Signal(t, ISSUE, "request", (room,), evidence, detail, facts))
        if state == "occupied":
            actual = f.states.next_entry(room, "dirty", t)
            if actual is not None and actual - t < follow:
                later = f.plan_at(room, actual, actual) | guest
                later |= {"vacant": True, "in_progress": False, "alternatives": []}
                out.append(
                    Signal(
                        actual,
                        ISSUE,
                        "checkout",
                        (room,),
                        (f"events.jsonl:{room} checkout at {stamp(actual)}",),
                        f"{room} checked out at {f.local(actual, actual)}; guest of {bid} "
                        f"wants it at {f.local(wanted, actual)}",
                        later,
                    )
                )
    return out + _day_plans(f, ds, settings)


def _at_local(day: pd.Timestamp, hhmm: time, tz: str) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(day.date(), hhmm)).tz_localize(tz).tz_convert("UTC")


def _day_plans(f: _Facts, ds: Dataset, settings: Settings) -> list[Signal]:
    """Return one ``day_plan`` signal per local day whose turnover load is at risk.

    At ``day_plan_hour`` local: workload is the rooms dirty then plus the rooms whose
    guest departs that day (bookings known then); capacity is cleaner minutes from then
    to check-in, roster as known then. Load = workload x ``day_clean_min`` / capacity.
    Predicted ready times assume arrival rooms are cleaned first, at ``clean_room_min``.
    """
    th = settings.detect
    hh, mm = (int(x) for x in ds.building["operations"]["check_in_from_local"].split(":"))
    days = pd.date_range(
        pd.Timestamp(ds.manifest["start_utc"]).tz_convert(f.tz).normalize(),
        pd.Timestamp(ds.manifest["end_utc"]).tz_convert(f.tz).normalize(),
        freq="1D",
        inclusive="left",
    )
    b = f.bookings
    out: list[Signal] = []
    for day in days:
        t = _at_local(day, time(th.day_plan_hour), f.tz)
        checkin = _at_local(day, time(hh, mm), f.tz)
        known = b.loc[b["created_utc"] <= t]
        on_day = known["departure_utc"].dt.tz_convert(f.tz).dt.date == day.date()
        departing = {
            str(r): pd.Timestamp(d)
            for r, d in zip(
                known.loc[on_day, "room_id"], known.loc[on_day, "departure_utc"], strict=True
            )
            if f.states.at(str(r), t) == "occupied"
        }
        dirty = sorted(f.states.rooms_in("dirty", t))
        shifts = f.roster.shifts("cleaner", t, checkin, t)
        minutes = sum((b2 - a).total_seconds() / 60 for _, a, b2 in shifts)
        workload = len(dirty) + len(departing)
        load = workload * th.day_clean_min / minutes if minutes > 0 else float(workload > 0)
        if workload == 0 or load < th.day_load_high:
            continue
        arriving = known["arrival_utc"].dt.tz_convert(f.tz).dt.date == day.date()
        arrival_rooms = sorted(
            {str(r) for r in known.loc[arriving, "room_id"]} & (set(dirty) | set(departing))
        )
        jobs = sorted(
            (r not in arrival_rooms, departing.get(r, t), r) for r in [*dirty, *departing]
        )
        free = {rid: a for rid, a, _ in shifts}
        span = t + pd.Timedelta(hours=th.day_plan_span_h)
        until = {rid: z for rid, _, z in f.roster.shifts("cleaner", t, span, t)}
        ready: dict[str, pd.Timestamp] = {}
        for _, avail, room in jobs:
            options = [
                (max(free[c], avail), c) for c in free if max(free[c], avail) + f.clean <= until[c]
            ]
            if options:
                begin, c = min(options)
                free[c] = ready[room] = begin + f.clean
        done = [ready[r] for r in arrival_rooms if r in ready]
        latest = max(done) if done and len(done) == len(arrival_rooms) else None
        cleaners = sorted({rid for rid, _, _ in shifts})
        data: dict[str, Any] = {
            "load": load,
            "help": load >= th.day_load_help,
            "cleaners": cleaners,
            "robots": f.roster.on_shift("robot", t, t),
            "arrival_rooms": arrival_rooms,
            "vacant_arrival": sorted(set(arrival_rooms) & set(dirty)),
            "latest": latest,
            "checkin": checkin,
        }
        detail = (
            f"{workload} rooms to turn, {len(cleaners)} cleaners with {minutes:.0f} min before "
            f"{f.local(checkin, t)}: load {load:.2f}; {len(arrival_rooms)} rooms with "
            "arrivals today"
        )
        out.append(
            Signal(
                t,
                ISSUE,
                "day_plan",
                ("BUILDING",),
                (
                    f"staff_roster.csv:cleaner shifts {day.date()} as known at {stamp(t)}",
                    f"bookings.csv:departures and arrivals {day.date()} created by {stamp(t)}",
                    f"events.jsonl:room states at {stamp(t)}",
                ),
                detail,
                data,
            )
        )
    return out
