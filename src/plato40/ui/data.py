"""Data behind the demo page (``ui.py``): lanes, the year strip, hourly series, day extracts.

Everything is computed from one pipeline run of the dataset, so the page and ``insights.md``
read the same numbers. ``plato40.kpi`` (tiles, misses, decision trail) is used when present;
without it those keys are null and the page hides them. Values are recorded data: our
decisions never change the replay, so a room we cleaned still reads dirty here.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from plato40 import arbiter, bus, contract, pipeline
from plato40.context import Snapshots
from plato40.contract import Decision
from plato40.load.reader import Dataset
from plato40.settings import Settings
from plato40.staff import Roster
from plato40.state import RoomStates

PLANNER_LANES = {"logistics": "S07", "data_quality": "DQ", "ventilation_maintenance": "S04"}
STATES = ("occupied", "dirty", "cleaning", "clean", "unknown")
FOLLOW_UP = re.compile(r"-F\d\d$")
STEP = pd.Timedelta(minutes=15)


def lanes(issues: Iterable[str]) -> list[dict[str, str]]:
    """Return the issue types with their scenario codes, in arbiter rank order."""
    code = {str(v["issue_type"]): k.upper() for k, v in bus.LANES.items()} | PLANNER_LANES
    rank = {str(v["issue_type"]): arbiter.LANE_RANK.get(k, 8.5) for k, v in bus.LANES.items()}
    seen = set(issues)
    out = []
    for n, issue in enumerate(sorted(seen, key=lambda i: (rank.get(i, 9.0), i))):
        out.append(
            {
                "key": issue,
                "short": code.get(issue, issue[:3].upper()),
                "label": issue.replace("_", " "),
                "color": f"var(--ln-{issue}, var(--l{n % 10}))",
            }
        )
    return out


def _kpi_module() -> Any | None:
    try:
        return importlib.import_module("plato40.kpi")
    except ImportError:
        return None


def _call(mod: Any | None, name: str, *args: Any) -> Any | None:
    fn = getattr(mod, name, None) if mod is not None else None
    return fn(*args) if callable(fn) else None


def routine_episodes(run: pipeline.Run, mod: Any | None) -> set[str]:
    """Return the routine episodes (guest replies, too-hot replies, pre-heat batches)."""
    found = _call(mod, "routine", run)
    if found is not None:
        return set(found)
    by_ep: dict[str, set[str]] = defaultdict(set)
    for d in run.decisions:
        by_ep[d["episode_id"]].add(d["action"])
    return {
        e.episode_id
        for e in run.episodes
        if e.issue_type == "guest_service"
        or (
            e.issue_type == "energy_optimization"
            and by_ep[e.episode_id] <= {"adjust_room_setpoint"}
        )
        or (
            e.issue_type == "heating_comfort" and all(s.kind == "comfort_excess" for s in e.signals)
        )
    }


def _local_day(t: str | pd.Timestamp, tz: str) -> str:
    return f"{pd.Timestamp(t).tz_convert(tz):%Y-%m-%d}"


def year(run: pipeline.Run, routine: set[str], miss_days: Iterable[str], tz: str) -> dict[str, Any]:
    """Return per local day: non-routine decisions per lane, routine count, approval and miss."""
    times = [pd.Timestamp(d["timestamp_utc"]) for d in run.decisions]
    if not times:
        return {"days": [], "lanes": {}, "routine": [], "approval": [], "miss": []}
    first, last = min(times).tz_convert(tz).normalize(), max(times).tz_convert(tz).normalize()
    days = [f"{d:%Y-%m-%d}" for d in pd.date_range(first, last, freq="D")]
    at = {d: n for n, d in enumerate(days)}
    issues = sorted({d["issue_type"] for d in run.decisions})
    per_lane = {i: [0] * len(days) for i in issues}
    routine_n, approval = [0] * len(days), [False] * len(days)
    for d in run.decisions:
        n = at[_local_day(d["timestamp_utc"], tz)]
        if d["episode_id"] in routine:
            routine_n[n] += 1
        else:
            per_lane[d["issue_type"]][n] += 1
        if d.get("requires_human_approval") and d["issue_type"] != "guest_service":
            approval[n] = True
    miss = [False] * len(days)
    for day in miss_days:
        if day in at:
            miss[at[day]] = True
    return {
        "days": days,
        "lanes": per_lane,
        "routine": routine_n,
        "approval": approval,
        "miss": miss,
    }


def _num(x: Any) -> float | None:
    if x is None:
        return None
    v = float(x[0] if isinstance(x, tuple) else x)
    return None if v != v else round(v, 3)


def series(snap: Snapshots, run: pipeline.Run) -> dict[str, Any]:
    """Return hourly building-level series over the run: COP ratio, outdoor, flow, SOC, price."""
    times = [pd.Timestamp(d["timestamp_utc"]) for d in run.decisions]
    if not times:
        return {}
    hours = pd.date_range(min(times).floor("h"), max(times).ceil("h"), freq="h")
    stale = pd.Timedelta(hours=2)
    occupied = [len(snap.states.rooms_in("occupied", t)) for t in hours]
    tariff = snap.tariff.drop_duplicates("timestamp_utc", keep="last").set_index("timestamp_utc")
    price = tariff["price_eur_kwh"].reindex(hours)
    return {
        "t": [int(t.value // 10**6) for t in hours],
        "hp1_cop_ratio": [_num(snap.cop.last(t, stale)) for t in hours],
        "outdoor_c": [_num(snap.outdoor.last(t, stale)) for t in hours],
        "main_flow_l_h": [_num(snap.main_flow.last(t, stale)) for t in hours],
        "soc_kwh": [_num(snap.energy["battery_soc_kwh"].last(t, stale)) for t in hours],
        "occupied": occupied,
        "price_eur_kwh": [None if v != v else round(float(v), 4) for v in price.to_numpy()],
    }


def _bookmarks(run: pipeline.Run, routine: set[str], day: str, tz: str) -> list[dict[str, Any]]:
    out = []
    for d in run.decisions:
        if _local_day(d["timestamp_utc"], tz) != day or d["episode_id"] in routine:
            continue
        kind = "decision"
        if FOLLOW_UP.search(d["event_id"]):
            kind = "followup"
        elif d["event_id"].startswith("SV-"):
            kind = "supervisor"
        label = f"{d['action'].replace('_', ' ')}: {', '.join(d['affected_assets'][:3])}"
        out.append(
            {"t": d["timestamp_utc"], "label": label, "event_id": d["event_id"], "kind": kind}
        )
    for e in run.episodes:
        if e.episode_id in routine or not e.signals:
            continue
        s = e.signals[0]
        if _local_day(s.time, tz) == day:
            out.append(
                {
                    "t": f"{s.time.tz_convert('UTC'):%Y-%m-%dT%H:%M}Z",
                    "label": f"{s.kind.replace('_', ' ')}: {', '.join(s.assets[:3])}",
                    "event_id": None,
                    "kind": "signal",
                    "episode_id": e.episode_id,
                }
            )
    order = {"signal": 0, "decision": 1, "followup": 2, "supervisor": 3}
    return sorted(out, key=lambda b: (b["t"], order[b["kind"]], str(b["event_id"])))


def day_extract(
    snap: Snapshots, run: pipeline.Run, routine: set[str], day: str, tz: str
) -> dict[str, Any]:
    """Return the 15-minute extract of one local day: rooms, plant, shifts, bookmarks."""
    start = pd.Timestamp(day, tz=tz)
    steps = pd.date_range(start, start + pd.Timedelta(days=1) - STEP, freq=STEP).tz_convert("UTC")
    stale = pd.Timedelta(hours=1)
    state_code = {s: n for n, s in enumerate(STATES)}
    rooms: dict[str, Any] = {}
    for room in snap.rooms:
        tel = {
            col: snap.tel[col].get(room) for col in ("temperature_c", "setpoint_c", "window_open")
        }
        rooms[room] = {
            "state": [
                state_code.get(snap.states.at(room, t), state_code["unknown"]) for t in steps
            ],
            "temp": [
                _num(tel["temperature_c"].last(t, stale)) if tel["temperature_c"] else None
                for t in steps
            ],
            "sp": [
                _num(tel["setpoint_c"].last(t, stale)) if tel["setpoint_c"] else None for t in steps
            ],
            "win": [
                _num(tel["window_open"].last(t, stale)) if tel["window_open"] else None
                for t in steps
            ],
        }
    ahu = snap.eq.get("AHU1/filter_dp_pa")
    roster = Roster(snap.ds)
    soc_plan: list[float | None] = [None] * len(steps)
    for s in run.signals:  # the battery plan published that day (lane_s06 facts), on the grid
        if s.kind != "tariff_published" or "soc_path_kwh" not in s.data:
            continue
        t0 = pd.Timestamp(str(s.data["soc_path_from"]))
        for n, v in enumerate(s.data["soc_path_kwh"]):
            i = int((t0 + n * STEP - steps[0]) / STEP)
            if 0 <= i < len(steps):
                soc_plan[i] = round(float(v), 2)
    return {
        "t": [int(t.value // 10**6) for t in steps],
        "states": list(STATES),
        "rooms": rooms,
        "plant": {
            "hp1_status": [(v[0] if (v := snap.hp_status.last(t, stale)) else None) for t in steps],
            "cop": [_num(snap.cop.last(t, pd.Timedelta(hours=2))) for t in steps],
            "ahu_dp": [_num(ahu.last(t, stale)) if ahu else None for t in steps],
            "soc": [_num(snap.energy["battery_soc_kwh"].last(t, stale)) for t in steps],
            "soc_plan": soc_plan,
            "flow": [_num(snap.main_flow.last(t, stale)) for t in steps],
        },
        "shift": {
            role: [roster.on_shift(role, t, t) for t in steps]
            for role in ("cleaner", "robot", "technician")
        },
        "bookmarks": _bookmarks(run, routine, day, tz),
    }


def rerun_status(path: Path | None, decisions_path: Path) -> dict[str, Any] | None:
    """Return the recorded rerun check, only if it is about this decisions file."""
    if path is None or not path.exists():
        return None
    rec: dict[str, Any] = dict(json.loads(path.read_text("utf-8")))
    sha = hashlib.sha256(decisions_path.read_bytes()).hexdigest()
    rec["about_this_file"] = rec.get("decisions_sha256") == sha
    return rec


def building(ds: Dataset) -> dict[str, Any]:
    """Return the rooms with floor and orientation, and the plant asset IDs."""
    b = ds.building["building"]
    return {
        "rooms": [
            {"id": r["id"], "floor": r["floor"], "orientation": r.get("orientation", "")}
            for r in b["rooms"]
        ],
        "assets": [a["id"] for a in b["assets"]],
    }


def demo_data(
    ds: Dataset,
    settings: Settings,
    run: pipeline.Run,
    decisions_path: Path,
    days: Sequence[str],
    supervisor_log: list[dict[str, Any]] | None,
    rerun_path: Path | None,
) -> dict[str, Any]:
    """Return the page keys that need the dataset: kpi, misses, outcomes, year, series, days."""
    mod = _kpi_module()
    tz = settings.local_tz
    routine = routine_episodes(run, mod)
    log = supervisor_log if supervisor_log is not None else run.reviews
    kpi = _call(mod, "kpi", ds, run, settings, log)
    misses = _call(mod, "misses", ds, run, settings, log)
    outcomes = _call(mod, "outcomes", ds, run, settings)
    if kpi is not None:
        kpi["rerun"] = rerun_status(rerun_path, decisions_path)
        ids = contract.known_asset_ids(ds.building)
        kpi["violations"] = {
            "n": len(pipeline.check(ds, RoomStates(ds), run.decisions)),
            "checked": True,
        }
        kpi["violations"]["n"] += len(contract.check_file(run.decisions, ids))
    miss_days = list(misses.get("days", [])) if isinstance(misses, dict) else []
    snap = Snapshots(ds, settings)
    lo, hi = (float(x) for x in ds.building["constraints"]["occupied_comfort_c"])
    return {
        "thresholds": {"cop_ratio_low": settings.detect.cop_ratio_low, "comfort_c": [lo, hi]},
        "building": building(ds),
        "routine": sorted(routine),
        "kpi": kpi,
        "misses": misses,
        "outcomes": outcomes,
        "year": year(run, routine, miss_days, tz),
        "series": series(snap, run),
        "days": {day: day_extract(snap, run, routine, day, tz) for day in days},
    }


def decisions_match(run: pipeline.Run, file_decisions: list[Decision]) -> list[str]:
    """Return what differs between the run and the committed file, so a stale page is visible."""
    a = Counter(d["event_id"] for d in run.decisions)
    b = Counter(d["event_id"] for d in file_decisions)
    out = []
    if a != b:
        only_run, only_file = sum((a - b).values()), sum((b - a).values())
        out.append(f"{only_run} decisions only in the run, {only_file} only in the file")
    return out
