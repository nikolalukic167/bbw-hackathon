"""Run figures shared by ``scripts/insights.py`` and the console (``plato40 ui``).

Numbers only: ints, floats, lists and dicts, never formatted text. The report and the page
read the same functions, so they can never disagree. Each function takes the dataset, the
run and the settings, plus the supervisor log or the rerun record where it needs them.

"Routine" work is a guest-desk reply, a reply to a "too hot" request, or an arrival
pre-heat batch; every other episode Steward acted on is an incident.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from plato40.detect.grid import stamp
from plato40.detect.logistics import _schedule
from plato40.episodes import Episode
from plato40.load import derive
from plato40.load.reader import Dataset
from plato40.pipeline import Run
from plato40.settings import Settings

Log = list[dict[str, Any]]
CHECKINS = ("checkin", "checkin_early", "checkin_after_wait")
PLANT = frozenset({"BAT1", "PV1", "BUILDING"})
FOLLOW_UP = re.compile(r"-F\d\d$")
FOLLOW_UP_KINDS = (
    ("Leak on ", "repair_not_held"),
    ("Release ", "room_released"),
    ("reference COP", "service_not_restored"),
    ("after its setpoint was raised", "room_still_cold"),
    ("Complaint ", "repeat_complaint"),
    ("is not ready at the promised", "ready_time_missed"),
)
REASON_CLASSES = (
    ("not in the input", "number_or_id_not_in_input"),
    ("not copied from refs", "evidence_not_from_refs"),
    ("not shown", "asset_or_decision_not_shown"),
    ("answer is not", "malformed"),
    ("call failed", "call_failed"),
    ("no cached answer", "no_cached_answer"),
)


def _hours(a: pd.Timestamp, b: pd.Timestamp) -> float:
    return float((b - a).total_seconds() / 3600.0)


def _local_date(t: pd.Timestamp, tz: str) -> date:
    out: date = t.tz_convert(tz).date()
    return out


# ---------------------------------------------------------------- episodes


def acted(run: Run) -> list[Episode]:
    """Return the episodes Steward made at least one decision on."""
    decided = {d["episode_id"] for d in run.decisions}
    return [e for e in run.episodes if e.episode_id in decided]


def too_hot(run: Run) -> set[str]:
    """Return heating episodes made only of replies to ``temperature_hot`` requests."""
    return {
        e.episode_id
        for e in run.episodes
        if e.issue_type == "heating_comfort"
        and e.signals
        and all(s.kind == "comfort_excess" for s in e.signals)
    }


def routine(run: Run) -> set[str]:
    """Return the episodes that are routine work, not incidents.

    Routine: guest-desk replies, too-hot replies, arrival pre-heat batches and the daily
    battery plan (energy episodes with only setpoint and energy-plan decisions).
    """
    by_ep: dict[str, set[str]] = defaultdict(set)
    for d in run.decisions:
        by_ep[d["episode_id"]].add(d["action"])
    out = set(too_hot(run))
    for e in run.episodes:
        if e.issue_type == "guest_service" or (
            e.issue_type == "energy_optimization"
            and by_ep.get(e.episode_id, set()) <= {"adjust_room_setpoint", "optimize_energy"}
            and ("optimize_energy" not in by_ep[e.episode_id] or set(e.assets) <= PLANT)
        ):
            out.add(e.episode_id)
    return out


def incidents_by_lane(run: Run) -> dict[str, Any]:
    """Return incidents and routine episodes per issue type, too-hot replies apart."""
    hot, rout = too_hot(run), routine(run)
    incidents: Counter[str] = Counter()
    routine_n: Counter[str] = Counter()
    for e in acted(run):
        lane = "comfort_hot" if e.episode_id in hot else e.issue_type
        (routine_n if e.episode_id in rout else incidents)[lane] += 1
    return {
        "incidents": dict(sorted(incidents.items())),
        "routine": dict(sorted(routine_n.items())),
        "incidents_total": sum(incidents.values()),
        "routine_total": sum(routine_n.values()),
    }


# ---------------------------------------------------------------- decisions


def decisions(run: Run) -> dict[str, Any]:
    """Return the decision count and the split by autonomy level."""
    level = Counter(str(d.get("autonomy")) for d in run.decisions)
    total = len(run.decisions)
    return {
        "total": total,
        "by_autonomy": dict(sorted(level.items())),
        "autonomous_share": level["autonomous"] / total if total else float("nan"),
    }


def approvals(ds: Dataset, run: Run) -> dict[str, Any]:
    """Return approvals: all, the guest desk's consent requests, the manager's own, per week."""
    asked = [d for d in run.decisions if d["requires_human_approval"]]
    desk = sum(d["issue_type"] == "guest_service" for d in asked)
    weeks = int(ds.manifest["days"]) / 7.0
    manager = len(asked) - desk
    return {
        "total": len(asked),
        "guest_desk": desk,
        "manager": manager,
        "consent": sum(bool(d.get("requires_guest_consent")) for d in run.decisions),
        "weeks": weeks,
        "manager_per_week": manager / weeks if weeks else float("nan"),
    }


def followups_by_kind(run: Run) -> dict[str, int]:
    """Return the closed-loop follow-ups (``verify.py``) per kind."""
    out: Counter[str] = Counter()
    for d in run.decisions:
        if not FOLLOW_UP.search(d["event_id"]):
            continue
        kind = next((k for key, k in FOLLOW_UP_KINDS if key in d["reason"]), "other")
        out[kind] += 1
    return dict(sorted(out.items()))


# ---------------------------------------------------------------- guests


def requests(ds: Dataset, run: Run) -> dict[str, Any]:
    """Return guest requests per type: count, answered (cited), answered by the due time."""
    req = ds.table("guest_requests.csv")
    first: dict[str, pd.Timestamp] = {}
    for d in run.decisions:
        t = pd.Timestamp(d["timestamp_utc"])
        for g in d.get("guest_request_ids", []):
            first[g] = min(first.get(g, t), t)
    by_type = []
    for kind, g in sorted(req.groupby("request_type"), key=lambda x: (-len(x[1]), str(x[0]))):
        ids = g["request_id"].astype(str).tolist()
        due = dict(zip(ids, g["response_due_utc"], strict=True))
        hit = [i for i in ids if i in first]
        ok = [i for i in hit if pd.notna(due[i]) and first[i] <= pd.Timestamp(due[i])]
        by_type.append(
            {"type": str(kind), "total": len(ids), "answered": len(hit), "on_time": len(ok)}
        )
    return {
        "total": sum(r["total"] for r in by_type),
        "answered": sum(r["answered"] for r in by_type),
        "on_time": sum(r["on_time"] for r in by_type),
        "by_type": by_type,
    }


def waits(ds: Dataset, run: Run, settings: Settings) -> list[dict[str, Any]]:
    """Return each waiting guest and whether a decision on the room came earlier that day."""
    tz = settings.local_tz
    ev = ds.events
    rows = ev.loc[ev["type"] == "arrival_waiting_room"]
    entered = (
        ev.loc[ev["type"] == "checkin_after_wait"]
        .drop_duplicates("booking_id")
        .set_index("booking_id")["timestamp_utc"]
    )
    # The day plan is the planner's (room_turnaround) or, with the s05 lane, occupancy_surge.
    planned = {
        pd.Timestamp(d["timestamp_utc"])
        for d in run.decisions
        if d["issue_type"] in ("room_turnaround", "occupancy_surge")
    }
    plans = {
        _local_date(s.time, tz): s
        for s in run.signals
        if s.kind == "day_plan" and s.time in planned
    }
    by_room: dict[str, list[pd.Timestamp]] = defaultdict(list)
    for d in run.decisions:
        for asset in d["affected_assets"]:
            by_room[str(asset)].append(pd.Timestamp(d["timestamp_utc"]))
    req = ds.table("guest_requests.csv")
    early = req.loc[req["request_type"] == "early_checkin"]
    out = []
    for t_raw, room, booking in zip(
        rows["timestamp_utc"], rows["room_id"], rows["booking_id"], strict=True
    ):
        t, day = pd.Timestamp(t_raw), _local_date(pd.Timestamp(t_raw), tz)
        earlier = [x for x in by_room.get(str(room), []) if x < t and _local_date(x, tz) == day]
        plan = plans.get(day)
        rooms = plan.data.get("arrival_rooms") or list(plan.data.get("keys", {})) if plan else []
        if plan is not None and plan.time < t and room in rooms:
            earlier.append(plan.time)
        waited = _hours(t, entered[booking]) if booking in entered.index else float("nan")
        asked = early.loc[(early["booking_id"] == booking) & (early["timestamp_utc"] <= t)]
        out.append(
            {
                "t": t,
                "day": day,
                "room": str(room),
                "booking": str(booking),
                "anticipated": bool(earlier),
                "lead_h": _hours(min(earlier), t) if earlier else None,
                "waited_h": waited,
                "requested": not asked.empty,
                "plan_day": day in plans,
            }
        )
    return out


def plan_days(run: Run, settings: Settings) -> set[date]:
    """Return the local days with a day plan that led to a decision."""
    planned = {
        pd.Timestamp(d["timestamp_utc"])
        for d in run.decisions
        if d["issue_type"] in ("room_turnaround", "occupancy_surge")
    }
    return {
        _local_date(s.time, settings.local_tz)
        for s in run.signals
        if s.kind == "day_plan" and s.time in planned
    }


# ---------------------------------------------------------------- energy and logistics


def preheat(ds: Dataset, run: Run, tz: str = "UTC") -> dict[str, Any]:
    """Return pre-heated rooms with a check-in within 12 h and those at the band by then.

    ``below_days`` counts the rooms below the band per local check-in day.
    """
    lo = float(ds.building["constraints"]["occupied_comfort_c"][0])
    temp = derive.telemetry_wide(ds, "temperature_c")
    ev = ds.events
    ins = ev.loc[ev["type"].isin(list(CHECKINS))]
    entry = {
        str(r): pd.DatetimeIndex(g["timestamp_utc"]).sort_values()
        for r, g in ins.groupby("room_id")
    }
    rooms = at_band = 0
    below: Counter[str] = Counter()
    for d in run.decisions:
        if d["issue_type"] != "energy_optimization" or d["action"] != "adjust_room_setpoint":
            continue
        t = pd.Timestamp(d["timestamp_utc"])
        for room in (a for a in d["affected_assets"] if a in temp.columns):
            when = entry.get(room, pd.DatetimeIndex([]))
            nxt = when[(when > t) & (when <= t + pd.Timedelta(hours=12))]
            if not len(nxt):
                continue
            col = temp[room].loc[: nxt[0]].dropna()
            if col.empty:
                continue
            rooms += 1
            ok = col.iloc[-1] >= lo
            at_band += int(ok)
            if not ok:
                below[str(_local_date(pd.Timestamp(nxt[0]), tz))] += 1
    return {"rooms": rooms, "at_band": at_band, "band_low_c": lo, "below_days": dict(below)}


def orders(ds: Dataset, run: Run) -> dict[str, Any]:
    """Return each extra logistics order against the shortage it was meant to prevent."""
    ev = ds.events
    out = ev.loc[ev["type"] == "vending_out_of_stock"]
    full = ev.loc[ev["type"] == "waste_bin_full"]
    shortage = {
        a["id"]: (out if a["type"] == "vending_machine" else full.loc[full["asset_id"] == a["id"]])
        for a in ds.building["building"]["assets"]
        if a["type"] in ("vending_machine", "waste_container")
    }
    ordered = {d["episode_id"] for d in run.decisions if d["action"] == "order_service"}
    eps = [e for e in acted(run) if e.issue_type == "logistics" and e.episode_id in ordered]
    services = {
        a["id"]: a.get("restock") or a.get("pickup") or ""
        for a in ds.building["building"]["assets"]
    }
    rows = []
    covered: set[Any] = set()
    for ep in eps:
        first = ep.signals[0].time
        # The planner's signals carry the lead; the s07 lane's take it from building.json.
        regular = max(pd.Timestamp(s.data["regular"]) for s in ep.signals)
        lead = ep.signals[0].data.get("lead") or _schedule(services[ep.assets[0]])[2]
        need = _hours(first, first + pd.Timedelta(lead))
        found = shortage.get(ep.assets[0], out.iloc[:0])
        hit = found.loc[(found["timestamp_utc"] >= first) & (found["timestamp_utc"] <= regular)]
        rows.append(
            {
                "asset": ep.assets[0],
                "hit": not hit.empty,
                "need_h": need,
                "lead_h": _hours(first, pd.Timestamp(hit["timestamp_utc"].min()))
                if not hit.empty
                else None,
            }
        )
        covered |= set(hit.index)
    return {
        "orders": rows,
        "shortages": sum(len(v) for v in shortage.values()),
        "covered": len(covered),
    }


# ---------------------------------------------------------------- supervisor and limits


def reason_class(text: str) -> str:
    """Return the class of a supervisor rejection or no-answer reason."""
    return next((label for key, label in REASON_CLASSES if key in text), "other_check")


def supervisor_summary(run: Run, log: Log | None) -> dict[str, Any] | None:
    """Return the supervisor's reviews, answers, rejections and additions; None if no log."""
    if not log:
        return None
    added = [i for r in log for i in r.get("added", [])]
    mine = {d["event_id"]: d for d in run.decisions if d["event_id"] in set(added)}
    none = [r for r in log if r["source"] == "none"]
    rejected = [r for r in log if r["source"] != "none" and r.get("why")]
    return {
        "reviews": len(log),
        "by_kind": dict(sorted(Counter(r["kind"] for r in log).items())),
        "answered": len(log) - len(none),
        "none": len(none),
        "none_why": dict(Counter(reason_class(r.get("why", "")) for r in none).most_common()),
        "rejected": len(rejected),
        "rejected_why": dict(Counter(reason_class(r["why"]) for r in rejected).most_common()),
        "added": added,
        "added_in_run": sorted(mine),
        "added_by_autonomy": dict(
            Counter(str(d.get("autonomy")) for d in mine.values()).most_common()
        ),
    }


# ---------------------------------------------------------------- trail


def _index(ev: pd.DataFrame, types: Iterable[str], key: str) -> dict[str, pd.DataFrame]:
    rows = ev.loc[ev["type"].isin(list(types)) & ev[key].notna()]
    return {str(k): g.sort_values("timestamp_utc", kind="stable") for k, g in rows.groupby(key)}


def _first_after(frame: pd.DataFrame | None, t: pd.Timestamp, within: pd.Timedelta) -> Any:
    if frame is None:
        return None
    hit = frame.loc[(frame["timestamp_utc"] > t) & (frame["timestamp_utc"] <= t + within)]
    return None if hit.empty else hit.iloc[0]


def outcomes(ds: Dataset, run: Run, settings: Settings) -> dict[str, dict[str, Any]]:
    """Return per decision what came next: the decisions that depend on it, and the outcome.

    ``next`` are the decisions whose ``dependencies`` name it (its own ``dependencies``
    are on the decision itself). ``outcome`` is the first event in the data that shows the
    result of its action, as ``{t, kind, detail}``, or None ("no outcome in the data").
    Outcomes are hindsight: read after the fact, never by the rules.
    """
    del settings
    ev = ds.events.copy()
    ev["timestamp_utc"] = pd.to_datetime(ev["timestamp_utc"], utc=True)
    cleaned = _index(ev, ["cleaning_completed"], "room_id")
    robot = _index(ev, ["robot_floor_cleaning_completed"], "room_id")
    arrive = _index(ev, [*CHECKINS, "arrival_waiting_room"], "room_id")
    entered = _index(ev, list(CHECKINS), "room_id")
    service = {
        "VEND1": ev.loc[ev["type"] == "vending_restocked"].sort_values("timestamp_utc"),
        "WASTE_MAIN": ev.loc[ev["type"] == "waste_pickup"].sort_values("timestamp_utc"),
    }
    wo = ds.table("work_orders.csv")
    done = wo.loc[wo["completed_utc"].notna()].sort_values("completed_utc", kind="stable")
    temp = derive.telemetry_wide(ds, "temperature_c")
    lo = float(ds.building["constraints"]["occupied_comfort_c"][0])
    rooms = set(ds.room_ids())
    after: dict[str, list[str]] = defaultdict(list)
    for d in run.decisions:
        for x in d.get("dependencies", []):
            after[x].append(d["event_id"])
    day, half = pd.Timedelta(hours=36), pd.Timedelta(hours=12)

    def event(row: Any, note: str = "") -> dict[str, Any]:
        ref = f"events.jsonl:{row['event_id']}"
        return {
            "t": stamp(pd.Timestamp(row["timestamp_utc"])),
            "kind": str(row["type"]),
            "detail": f"{ref} {note}".strip(),
        }

    out: dict[str, dict[str, Any]] = {}
    for d in run.decisions:
        t, action = pd.Timestamp(d["timestamp_utc"]), d["action"]
        room = next((a for a in d["affected_assets"] if a in rooms), None)
        found: dict[str, Any] | None = None
        if action == "schedule_cleaning" and room:
            row = _first_after(cleaned.get(room), t, day)
            found = event(row) if row is not None else None
        elif action == "dispatch_robot" and room:
            row = _first_after(robot.get(room), t, day)
            found = event(row) if row is not None else None
        elif action == "notify_guest" and room:
            row = _first_after(arrive.get(room), t, day)
            found = event(row) if row is not None else None
        elif action == "order_service" and d["affected_assets"][0] in service:
            row = _first_after(service[d["affected_assets"][0]], t, pd.Timedelta(days=7))
            found = event(row) if row is not None else None
        elif action in ("dispatch_technician", "order_service"):
            asset = d["affected_assets"][0]
            hit = done.loc[(done["asset_id"] == asset) & (done["completed_utc"] > t)]
            if not hit.empty:
                w = hit.iloc[0]
                found = {
                    "t": stamp(pd.Timestamp(w["completed_utc"])),
                    "kind": "work_order_completed",
                    "detail": f"work_orders.csv:{w['work_order_id']} {w['assigned_to']}",
                }
        elif action == "adjust_room_setpoint" and d["issue_type"] == "energy_optimization" and room:
            row = _first_after(entered.get(room), t, half)
            if row is not None and room in temp.columns:
                col = temp[room].loc[: pd.Timestamp(row["timestamp_utc"])].dropna()
                if not col.empty:
                    c = float(col.iloc[-1])
                    side = "at or above" if c >= lo else "below"
                    found = event(row, f"room at {c:.1f} degC, {side} {lo:.0f} degC at check-in")
        out[d["event_id"]] = {"next": after.get(d["event_id"], []), "outcome": found}
    return out


def year(ds: Dataset, run: Run, settings: Settings) -> dict[str, dict[str, Any]]:
    """Return per local day the incident decisions per lane, approvals and misses.

    Routine work counts under the lane "routine"; approvals are the manager's (guest-desk
    consent requests left out); misses are counted by kind.
    """
    tz = settings.local_tz
    rout = routine(run)
    lanes: dict[str, Counter[str]] = defaultdict(Counter)
    asked: Counter[str] = Counter()
    missed: dict[str, Counter[str]] = defaultdict(Counter)
    for d in run.decisions:
        day = str(_local_date(pd.Timestamp(d["timestamp_utc"]), tz))
        lanes[day]["routine" if d["episode_id"] in rout else d["issue_type"]] += 1
        if d["requires_human_approval"] and d["issue_type"] != "guest_service":
            asked[day] += 1
    for w in waits(ds, run, settings):
        if not w["anticipated"]:
            missed[str(w["day"])]["wait_not_anticipated"] += 1
    for day, n in preheat(ds, run, tz)["below_days"].items():
        missed[day]["cold_checkin"] += n
    ev = ds.events
    for kind in ("vending_out_of_stock", "waste_bin_full"):
        for t in ev.loc[ev["type"] == kind, "timestamp_utc"]:
            missed[str(_local_date(pd.Timestamp(t), tz))][kind] += 1
    days = sorted(set(lanes) | set(missed))
    return {
        day: {
            "lanes": dict(sorted(lanes[day].items())),
            "approvals": asked[day],
            "misses": dict(sorted(missed[day].items())),
        }
        for day in days
    }


def finite(x: float) -> float | None:
    """Return ``x`` or None when it is not a finite number (JSON has no NaN)."""
    return float(x) if np.isfinite(x) else None


# ---------------------------------------------------------------- entry points for the console


def kpi(ds: Dataset, run: Run, settings: Settings, log: Log | None = None) -> dict[str, Any]:
    """Return the Year view's figures; ``supervisor`` is None without a review log."""
    del settings  # the figures need no setting today; kept for one call shape
    found = requests(ds, run)
    dec = decisions(run)
    return {
        "requests": {k: found[k] for k in ("total", "answered", "on_time", "by_type")},
        "decisions": dec | {"autonomous_share": finite(dec["autonomous_share"])},
        "approvals": approvals(ds, run),
        "incidents": incidents_by_lane(run),
        "followups": followups_by_kind(run),
        "supervisor": supervisor_summary(run, log),
    }


def _battery(run: Run) -> dict[str, Any] | None:
    """Return the battery plan's days and median simulated saving (EUR), if it planned any."""
    plans = [
        float(s.data["saving_eur"])
        for s in run.signals
        if s.issue_type == "energy_optimization"
        and s.kind == "tariff_published"
        and "saving_eur" in s.data
    ]
    if not plans:
        return None
    return {"days": len(plans), "saving_eur_median": float(np.median(plans))}


def misses(ds: Dataset, run: Run, settings: Settings, log: Log | None = None) -> dict[str, Any]:
    """Return the Limits panel's misses as [missed, total] pairs, and the days they fell on."""
    w = waits(ds, run, settings)
    heat = preheat(ds, run, settings.local_tz)
    o = orders(ds, run)
    sv = supervisor_summary(run, log)
    hp = [
        e
        for e in acted(run)
        if e.issue_type == "heating_comfort" and any(s.kind == "cop_low" for s in e.signals)
    ]
    # A guest-facing miss (a wait not anticipated, a stock-out, a full container) marks its
    # day; a pre-heated room below the band falls on most winter days, so it is counted
    # apart and left off the default markers.
    by_kind = {
        "wait_not_anticipated": {str(x["day"]) for x in w if not x["anticipated"]},
        "cold_checkin": set(heat["below_days"]),
    }
    ev = ds.events
    for kind in ("vending_out_of_stock", "waste_bin_full"):
        by_kind[kind] = {
            str(_local_date(pd.Timestamp(t), settings.local_tz))
            for t in ev.loc[ev["type"] == kind, "timestamp_utc"]
        }
    days = set().union(*(v for k, v in by_kind.items() if k != "cold_checkin"))
    return {
        "waits": [sum(not x["anticipated"] for x in w), len(w)],
        "preheat": [heat["rooms"] - heat["at_band"], heat["rooms"]],
        "orders": [sum(not x["hit"] for x in o["orders"]), len(o["orders"])],
        "supervisor_rejected": [sv["rejected"], sv["answered"]] if sv else None,
        "hp_incidents": len(hp),
        "leak_incidents": sum(e.issue_type == "water_leak" for e in acted(run)),
        "battery": _battery(run),
        "days": sorted(days),
        "days_by_kind": {k: sorted(v) for k, v in sorted(by_kind.items())},
    }
