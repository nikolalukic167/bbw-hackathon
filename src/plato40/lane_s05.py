"""S05 trigger lane on the bus (docs/BUS.md): the building's cleaning capacity per day.

Lane ``s05``, issue ``occupancy_surge``. S01 owns one room's conflict; S05 owns the day. Every
row is timed when its inputs were known (ADR-002). Bookings count from ``created_utc``; a
cancelled booking stays in, since the data has no cancellation time.

Signals (``kind``) and what they propose:

- ``surge_forecast``, at ``day_plan_hour`` local, for the day ``surge_lookahead_d`` days
  ahead: an event day has at least ``surge_occupancy`` rooms booked for the night and at
  least ``surge_early_arrivals`` arrivals booked ``surge_early_days`` or more ahead. Event
  weekends are booked early, not late (dev arrival lead median 26-40 d against 12 d). If the
  day planned with one cleaner fewer than rostered leaves an arrival room after check-in,
  one ``request_human_approval`` per weekend asks for a stand-by cleaner; nobody is planned
  until approved.
- ``day_plan``, at ``day_plan_hour`` local, only when an arrival room is predicted ready
  after its due time (an early check-in time asked for by then, else check-in). The plan
  cleans in key order: arrival rooms first, earliest due first, each room from its release
  (now if dirty, else ``checkout_estimate_local``), on the cleaner shifts known then, at
  ``clean_room_min``. Robots shorten a clean but are not counted: no capacity is invented.
  It proposes a cleaning order and a robot pre-pass for the vacant arrival rooms (planning
  rows: no claims, so no cleaner is held), extra cleaning help when an arrival room is not
  ready by check-in (approval), and one delay notice per late arrival guest who has no
  early check-in or waiting request open with S01 (S01 answers those).

When ``s05`` runs, the planner's ``day_plan`` signals are dropped (pipeline), so the day
plan has one owner. Guest messages go through ``llm_guard``; the language is the guest's
latest request known at ``t``, else English (bookings carry no language).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any

import pandas as pd

from plato40 import llm_guard
from plato40.bus import LANES, LANGUAGES, VERSION
from plato40.detect.grid import stamp, state_ref
from plato40.load.reader import Dataset
from plato40.settings import Settings
from plato40.staff import Roster
from plato40.state import RoomStates

LANE = "s05"
ISSUE = str(LANES[LANE]["issue_type"])
Row = dict[str, Any]
CLOCK = re.compile(r"\b(\d{1,2})[:.h](\d{2})\b")
S01_THREAD = ("early_checkin", "waiting_for_room")

# Day-delay notice: the rules render {room} and {ready}; every text keeps the estimate's hedge.
TEMPLATES: dict[str, str] = {
    "en": "Dear guest, your room {room} is expected to be ready after {ready} local time today. "
    "We will let you know as soon as it is ready.",
    "de": "Liebe Gäste, Ihr Zimmer {room} ist heute voraussichtlich erst nach {ready} Uhr "
    "Ortszeit bereit. Wir melden uns, sobald es fertig ist.",
    "fr": "Chers hôtes, votre chambre {room} devrait être prête aujourd'hui après {ready}, "
    "heure locale. Nous vous informerons dès qu'elle sera prête.",
    "it": "Gentili ospiti, la vostra camera {room} dovrebbe essere pronta oggi dopo le {ready}, "
    "ora locale. Vi avviseremo appena sarà pronta.",
    "nl": "Beste gasten, uw kamer {room} is naar verwachting vandaag na {ready} uur lokale tijd "
    "klaar. We laten het u weten zodra de kamer klaar is.",
    "es": "Estimados huéspedes, se espera que su habitación {room} esté lista hoy después de "
    "las {ready}, hora local. Le avisaremos en cuanto esté lista.",
}


def _clock(hhmm: str) -> time:
    hh, mm = hhmm.split(":")
    return time(int(hh), int(mm))


# Day-delay notice for a late room with no slot in the plan: no time is promised (it needs
# extra help, which is an approval), so the text has no number at all.
UNTIMED: dict[str, str] = {
    "en": "Dear guest, your room {room} may not be ready by check-in time today, as several "
    "rooms are being prepared. We will message you as soon as it is ready.",
    "de": "Liebe Gäste, Ihr Zimmer {room} ist heute möglicherweise zur Check-in-Zeit noch nicht "
    "fertig, da mehrere Zimmer vorbereitet werden. Wir melden uns, sobald es bereit ist.",
    "fr": "Chers hôtes, votre chambre {room} ne sera peut-être pas prête à l'heure d'arrivée "
    "aujourd'hui, plusieurs chambres étant en préparation. Nous vous écrirons dès qu'elle sera "
    "prête.",
    "it": "Gentili ospiti, la vostra camera {room} potrebbe non essere pronta all'orario del "
    "check-in di oggi, perché stiamo preparando diverse camere. Vi scriveremo appena sarà pronta.",
    "nl": "Beste gasten, uw kamer {room} is vandaag mogelijk nog niet klaar bij het inchecken, "
    "omdat meerdere kamers worden voorbereid. We sturen u een bericht zodra de kamer klaar is.",
    "es": "Estimados huéspedes, es posible que su habitación {room} no esté lista a la hora del "
    "check-in de hoy, ya que estamos preparando varias habitaciones. Le escribiremos en cuanto "
    "esté lista.",
}


@dataclass
class _Plan:
    """A day's greedy cleaning plan: predicted ready time per room, None if it does not fit."""

    order: list[str]
    ready: dict[str, pd.Timestamp | None]
    due: dict[str, pd.Timestamp]
    cleaners: list[str]
    minutes: float

    def late(self, rooms: Iterable[str]) -> list[str]:
        """Return the rooms predicted ready after their due time, or not at all."""
        return [r for r in rooms if (x := self.ready[r]) is None or x > self.due[r]]


@dataclass
class _Board:
    """The rows this lane writes, with per-episode sequence numbers for their IDs."""

    rows: list[Row] = field(default_factory=list)
    seq: Counter[str] = field(default_factory=Counter)
    messages: Counter[str] = field(default_factory=Counter)

    def _id(self, eid: str, what: str) -> str:
        self.seq[f"{eid}/{what}"] += 1
        return f"{LANE}-{eid.removeprefix('EP-')}-{what}{self.seq[f'{eid}/{what}']:02d}"

    def signal(
        self,
        eid: str,
        t: pd.Timestamp,
        kind: str,
        evidence: list[str],
        detail: str,
        facts: Mapping[str, Any],
        cause: str,
    ) -> Row:
        row: Row = {
            "v": VERSION,
            "row": "signal",
            "lane": LANE,
            "id": self._id(eid, "sig"),
            "t_known": stamp(t),
            "issue_type": ISSUE,
            "kind": kind,
            "assets": ["BUILDING"],
            "evidence": evidence,
            "detail": detail,
            "facts": dict(facts),
            "cause": cause,
        }
        self.rows.append(row)
        return row

    def propose(
        self,
        eid: str,
        t: pd.Timestamp,
        sig: Row,
        intent: str,
        action: str,
        assets: list[str],
        priority: str,
        reason: str,
        **extra: Any,
    ) -> Row:
        row: Row = {
            "v": VERSION,
            "row": "proposal",
            "lane": LANE,
            "id": self._id(eid, "prop"),
            "t": stamp(t),
            "source": "rules",
            "episode_id": eid,
            "signal_ids": [sig["id"]],
            "intent": intent,
            "action": action,
            "assets": assets,
            "priority": priority,
            "evidence": list(sig["evidence"]),
            "reason": reason,
            "cause": sig["cause"],
        }
        row.update({k: v for k, v in extra.items() if v is not None})
        self.rows.append(row)
        return row


class _Lane:
    """Builds the s05 rows of one dataset in time order."""

    def __init__(
        self, ds: Dataset, settings: Settings, cache: Path, call: llm_guard.Call | None
    ) -> None:
        self.ds, self.settings, self.cache, self.call = ds, settings, cache, call
        self.th = settings.detect
        self.tz = settings.local_tz
        self.board = _Board()
        self.roster = Roster(ds)
        self.states = RoomStates(ds)
        self.bookings = ds.table("bookings.csv")
        self.requests = ds.table("guest_requests.csv")
        self.clean = pd.Timedelta(minutes=self.th.clean_room_min)
        ops = ds.building["operations"]
        self.checkin = _clock(ops["check_in_from_local"])
        self.checkout = _clock(self.th.checkout_estimate_local)
        self.night = time(22, 0)

    # ---------------------------------------------------------------- facts known at t

    def _at(self, day: pd.Timestamp, hhmm: time) -> pd.Timestamp:
        local = pd.Timestamp(datetime.combine(day.date(), hhmm)).tz_localize(self.tz)
        return local.tz_convert("UTC")

    def _hhmm(self, t: pd.Timestamp | None) -> str:
        return "unknown" if t is None else f"{t.tz_convert(self.tz):%H:%M}"

    def _known(self, t: pd.Timestamp) -> pd.DataFrame:
        return self.bookings.loc[self.bookings["created_utc"] <= t]

    def _local_day(self, col: pd.Series) -> pd.Series:
        out: pd.Series = col.dt.tz_convert(self.tz).dt.date
        return out

    def _event_day(self, day: pd.Timestamp, t: pd.Timestamp) -> dict[str, int] | None:
        """Return the event-day counts of ``day`` as known at ``t``, or None if not one."""
        b = self._known(t)
        night = self._at(day, self.night)
        booked = int(((b["arrival_utc"] <= night) & (b["departure_utc"] > night)).sum())
        lead = b["arrival_utc"] - b["created_utc"]
        early = int(
            (
                (self._local_day(b["arrival_utc"]) == day.date())
                & (lead >= pd.Timedelta(days=self.th.surge_early_days))
            ).sum()
        )
        if booked >= self.th.surge_occupancy and early >= self.th.surge_early_arrivals:
            return {"night_bookings": booked, "early_arrivals": early}
        return None

    def _due(self, booking: str, day: pd.Timestamp, t: pd.Timestamp) -> pd.Timestamp:
        """Return the time the arriving guest wants the room: an asked time, else check-in."""
        r = self.requests
        asked = r.loc[
            (r["booking_id"] == booking)
            & (r["request_type"] == "early_checkin")
            & (r["timestamp_utc"] <= t)
        ].sort_values("timestamp_utc", kind="stable")
        checkin = self._at(day, self.checkin)
        for text in reversed(asked["text"].astype(str).tolist()):
            m = CLOCK.search(text)
            if m and int(m[1]) < 24:
                return min(checkin, self._at(day, time(int(m[1]), int(m[2]))))
        return checkin

    def _language(self, booking: str, t: pd.Timestamp) -> str:
        r = self.requests
        mine = r.loc[(r["booking_id"] == booking) & (r["timestamp_utc"] <= t)]
        earlier = mine.sort_values("timestamp_utc", kind="stable")["language"]
        lang = str(earlier.iloc[-1]) if len(earlier) else "en"
        return lang if lang in LANGUAGES else "en"

    def _s01_thread(self, booking: str, t: pd.Timestamp) -> bool:
        r = self.requests
        return bool(
            (
                (r["booking_id"] == booking)
                & r["request_type"].isin(S01_THREAD)
                & (r["timestamp_utc"] <= t)
            ).any()
        )

    def _plan(
        self,
        jobs: Mapping[str, pd.Timestamp],
        arrivals: Mapping[str, pd.Timestamp],
        shifts: list[tuple[str, pd.Timestamp, pd.Timestamp]],
    ) -> _Plan:
        """Greedy list schedule: arrival rooms first, earliest due first, then release."""
        far = pd.Timestamp.max.tz_localize("UTC")
        order = sorted(jobs, key=lambda r: (r not in arrivals, arrivals.get(r, far), jobs[r], r))
        free = {c: a for c, a, _ in shifts}
        until = {c: z for c, _, z in shifts}
        ready: dict[str, pd.Timestamp | None] = {}
        for room in order:
            options = [
                (max(free[c], jobs[room]), c)
                for c in sorted(free)
                if max(free[c], jobs[room]) + self.clean <= until[c]
            ]
            if options:
                begin, c = min(options)
                free[c] = begin + self.clean
                ready[room] = free[c]
            else:
                ready[room] = None
        minutes = sum((z - a).total_seconds() / 60 for _, a, z in shifts)
        return _Plan(order, ready, dict(arrivals), sorted(free), minutes)

    def _day(
        self, day: pd.Timestamp, t: pd.Timestamp
    ) -> tuple[dict[str, pd.Timestamp], dict[str, pd.Timestamp], dict[str, str]]:
        """Return rooms to turn on ``day`` (with release), arrival rooms (with due), bookings."""
        b = self._known(t)
        estimate = max(t, self._at(day, self.checkout))
        jobs: dict[str, pd.Timestamp] = {}
        if t >= self._at(day, time(0, 0)):
            for room in sorted(self.states.rooms_in("dirty", t)):
                jobs[room] = t
        leaving = b.loc[self._local_day(b["departure_utc"]) == day.date()]
        for room in sorted(set(leaving["room_id"].astype(str))):
            if room not in jobs and (
                t < self._at(day, time(0, 0)) or self.states.at(room, t) == "occupied"
            ):
                jobs[room] = estimate
        arriving = b.loc[self._local_day(b["arrival_utc"]) == day.date()]
        guests = {
            str(r): str(k)
            for r, k in zip(arriving["room_id"], arriving["booking_id"], strict=True)
            if str(r) in jobs
        }
        arrivals = {room: self._due(k, day, t) for room, k in sorted(guests.items())}
        return jobs, arrivals, guests

    # ---------------------------------------------------------------- UC1 outlook

    def _outlook(self, t: pd.Timestamp, day: pd.Timestamp, open_eid: str | None) -> str | None:
        counts = self._event_day(day, t)
        if counts is None:
            return None
        eid = open_eid or f"EP-{ISSUE}-BUILDING-{t:%Y%m%dT%H%M}Z"
        jobs, arrivals, _ = self._day(day, t)
        end = self._at(day, time(23, 59))
        shifts = self.roster.shifts("cleaner", self._at(day, time(0, 0)), end, t)
        short = self._plan(jobs, arrivals, shifts[:-1]) if shifts else None
        late = short.late(arrivals) if short else sorted(arrivals)
        checkin = self._at(day, self.checkin)
        after = [r for r in late if short is None or (x := short.ready[r]) is None or x > checkin]
        label = f"{day:%a %d %b}"
        sig = self.board.signal(
            eid,
            t,
            "surge_forecast",
            [
                f"bookings.csv:arrivals and stays {day.date()} created by {stamp(t)}",
                f"staff_roster.csv:cleaner shifts {day.date()} as known at {stamp(t)}",
            ],
            f"event day {label}: {counts['night_bookings']} bookings for the night (cancelled "
            "kept), "
            f"{counts['early_arrivals']} arrivals booked at least "
            f"{self.th.surge_early_days:.0f} days ahead; {len(jobs)} departures, "
            f"{len(arrivals)} arrival rooms, {len(shifts)} cleaner shifts rostered",
            {
                "day": str(day.date()),
                **counts,
                "departures": len(jobs),
                "arrival_rooms": sorted(arrivals),
                "cleaners": [c for c, _, _ in shifts],
                "late_with_one_fewer": after,
            },
            "event_weekend",
        )
        asked = any(
            r["row"] == "proposal"
            and r["episode_id"] == eid
            and r["intent"] == "request_standby_cleaner"
            for r in self.board.rows
        )
        if after and not asked:
            self.board.propose(
                eid,
                t,
                sig,
                "request_standby_cleaner",
                "request_human_approval",
                ["BUILDING"],
                "high",
                f"Event day {label}: with one cleaner fewer than the {len(shifts)} rostered "
                f"(one sick note is enough), {len(after)} of {len(arrivals)} arrival rooms "
                f"are not ready by check-in. Approve a stand-by cleaner for {label}; none "
                "is planned until approved.",
                confidence=0.6,
                expected_outcome="A manager decides on a stand-by cleaner a week ahead.",
                verification_plan=(
                    f"Approval answered before {label}; on the day, every arrival room "
                    "cleaning_completed before check-in."
                ),
            )
        return eid

    # ---------------------------------------------------------------- UC2-UC4 day plan

    def _day_plan(self, t: pd.Timestamp, day: pd.Timestamp) -> None:
        jobs, arrivals, guests = self._day(day, t)
        span = t + pd.Timedelta(hours=self.th.day_plan_span_h)
        shifts = self.roster.shifts("cleaner", t, span, t)
        plan = self._plan(jobs, arrivals, shifts)
        late = plan.late(arrivals)
        if not late:
            return
        checkin = self._at(day, self.checkin)
        after = [r for r in late if (x := plan.ready[r]) is None or x > checkin]
        rostered = self.roster.shifts("cleaner", t, span, pd.Timestamp.min.tz_localize("UTC"))
        sick = [c for c in rostered if c not in shifts]
        event = self._event_day(day, t)
        cause = "event_weekend" if event else "sick_cleaner" if sick else "turnover_peak"
        eid = f"EP-{ISSUE}-BUILDING-{t:%Y%m%dT%H%M}Z"
        vacant = [r for r in plan.order if r in arrivals and jobs[r] <= t]
        keys = [f"{r} due {self._hhmm(plan.due[r])}" for r in plan.order if r in arrivals]
        sig = self.board.signal(
            eid,
            t,
            "day_plan",
            [
                f"staff_roster.csv:cleaner shifts {day.date()} as known at {stamp(t)}",
                *(f"bookings.csv:{k}" for k in sorted(set(guests.values()))),
                *(state_ref(self.ds, r, t) for r in sorted(jobs)),
            ],
            f"{len(jobs)} rooms to turn, {len(plan.cleaners)} cleaners "
            f"({plan.minutes:.0f} min); {len(late)} of {len(arrivals)} arrival rooms "
            "predicted after their due time",
            {
                "keys": {r: stamp(plan.due[r]) for r in plan.order if r in arrivals},
                "pred_ready": {r: (stamp(x) if (x := plan.ready[r]) else None) for r in plan.order},
                "late_rooms": late,
                "cleaners": plan.cleaners,
                "sick_cleaners": sorted({c for c, _, _ in sick}),
                "cleaner_min": round(plan.minutes),
                "demand_min": round(len(jobs) * self.clean.total_seconds() / 60),
                **({"event": event} if event else {}),
            },
            cause,
        )
        prio = "high" if after else "medium"
        verify = "cleaning_completed in each arrival room by its predicted ready time + 30 min."
        order = self.board.propose(
            eid,
            t,
            sig,
            "clean_first",
            "schedule_cleaning",
            [*plan.cleaners, *vacant] if plan.cleaners else ["BUILDING"],
            prio,
            "Clean in this order: arrival rooms first, earliest due first ("
            + ", ".join(keys)
            + "), then the rest. A planning row: S01 assigns each cleaner at checkout.",
            confidence=0.6,
            expected_outcome=(
                f"{len(arrivals) - len(late)} of {len(arrivals)} arrival rooms ready by their "
                "due time (simulated)."
            ),
            verification_plan=verify,
        )
        robots = self.roster.on_shift("robot", t, t)
        if robots and vacant:
            self.board.propose(
                eid,
                t,
                sig,
                "robot_prepass",
                "dispatch_robot",
                [*robots, *vacant],
                prio,
                f"Robot floor passes first in the vacant arrival rooms {', '.join(vacant)}; "
                "robots do not certify or release a room.",
                confidence=0.6,
                expected_outcome="Shorter human cleaning in arrival rooms (simulated).",
                verification_plan="robot_floor_cleaning_completed before each human clean.",
            )
        n = len(plan.cleaners)
        approval: Row | None = None
        if after:
            # The late rooms are assets of the approval too, so a review of one room sees it.
            approval = self.board.propose(
                eid,
                t,
                sig,
                "request_extra_cleaning",
                "request_human_approval",
                ["BUILDING", *after],
                "high",
                f"{len(after)} arrival rooms ({', '.join(after)}) are not ready by check-in at "
                f"{self._hhmm(checkin)} with the {n} cleaner{'' if n == 1 else 's'} on shift. "
                "Approve extra cleaning help (on-call from 15:30 or a stand-by cleaner); none "
                "is assumed until approved.",
                confidence=0.6,
                expected_outcome="A manager decides on extra help.",
                verification_plan=verify,
            )
        for room in late:
            booking = guests[room]
            if self._s01_thread(booking, t):
                continue
            ready = plan.ready[room]
            at = self._hhmm(ready) if ready is not None else None
            language = self._language(booking, t)
            task = llm_guard.Task(
                lane=LANE,
                t=stamp(t),
                known_by=sig["t_known"],
                language=language,
                intent="notify_day_delay",
                facts={"room": room, "booking": booking},
                say={"room": room, "ready": at} if at else {"room": room},
                must_say=("room", "ready") if at else ("room",),
                template=TEMPLATES[language] if at else UNTIMED[language],
                hedged=at is not None,
            )
            said = llm_guard.phrase(task, self.cache, self.call)
            self.board.messages[said.source] += 1
            what = (
                f"{room} expected ready after {at} local today"
                if at
                else f"{room} may not be ready by check-in today; no time is promised yet, as "
                "the plan has no slot for it without the extra cleaning help asked for, and a "
                "ready time follows once the room is scheduled"
            )
            self.board.propose(
                eid,
                t,
                sig,
                "notify_day_delay",
                "notify_guest",
                [room],
                prio,
                f"Tell the guest of {booking} in {room} ({language}): {what}; arrival rooms are "
                "cleaned first (proposed message; delivery not confirmed).",
                message={"language": language, "text": said.text},
                needs=[order["id"]] + ([approval["id"]] if approval and not at else []),
                confidence=0.6,
                expected_outcome="The guest does not arrive early and wait.",
                verification_plan=(
                    f"No arrival_waiting_room for {room} before "
                    + (f"{at} local" if at else "check-in")
                    + "; escalate on one."
                ),
            )

    def run(self) -> list[Row]:
        start = pd.Timestamp(self.ds.manifest["start_utc"]).tz_convert(self.tz).normalize()
        end = pd.Timestamp(self.ds.manifest["end_utc"]).tz_convert(self.tz).normalize()
        ahead = pd.Timedelta(days=self.th.surge_lookahead_d)
        outlook: str | None = None
        for day in pd.date_range(start, end, freq="1D", inclusive="left"):
            t = self._at(day, time(self.th.day_plan_hour))
            outlook = self._outlook(t, day + ahead, outlook)
            self._day_plan(t, day)
        return self.board.rows


def rows(
    ds: Dataset, settings: Settings, cache: Path, call: llm_guard.Call | None = None
) -> tuple[list[Row], Counter[str]]:
    """Return the s05 signal and proposal rows of ``ds`` in time order, and message sources."""
    lane = _Lane(ds, settings, cache, call)
    out = lane.run()
    return out, lane.board.messages
