"""Write the golden bus examples from a real run: S01 in full, one S02 and one S03 shape.

usage: python scripts/export_examples.py <quickstart_dir> <out_dir>
Every row is taken from the pipeline's own signals and decisions; nothing is typed by hand.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from plato40 import contract, pipeline
from plato40.detect.grid import stamp
from plato40.load.reader import load_dataset
from plato40.settings import Settings

S01_EPISODES = (
    "EP-room_turnaround-BUILDING-20260124T0600Z",
    "EP-room_turnaround-R108-20260124T1300Z",
    "EP-room_turnaround-R308-20260107T2258Z",
)
LANE_OF = {"room_turnaround": "s01", "water_leak": "s02", "heating_comfort": "s03"}


def plain(x: Any) -> Any:
    """Return ``x`` as JSON: UTC times as ``...Z``, sets sorted, numpy scalars as Python."""
    if isinstance(x, pd.Timestamp):
        return stamp(x)
    if isinstance(x, dict):
        return {str(k): plain(v) for k, v in x.items()}
    if isinstance(x, set | frozenset):
        return sorted(plain(v) for v in x)
    if isinstance(x, list | tuple):
        return [plain(v) for v in x]
    if isinstance(x, np.generic):
        return x.item()
    if isinstance(x, float):
        return round(x, 4)
    return x


def intent(d: dict[str, Any]) -> str:
    """Return the S01 intent of a planner decision, read from its action and reason."""
    a, r = d["action"], d["reason"]
    if a == "dispatch_robot":
        return "robot_prepass"
    if a == "reassign_room":
        return "offer_alternative_room"
    if a == "request_human_approval":
        return "escalate_waiting_guest" if "pending offer" in r else "request_extra_cleaning"
    if a == "schedule_cleaning":
        return "clean_first"
    if "withdrawn" in r:
        return "withdraw_offer"
    if "offered pending approval" in r:
        return "offer_alternative_room"
    if "is ready now" in r:
        return "notify_ready_now"
    if r.startswith("Tell guests arriving today"):
        return "notify_day_delay"
    return "promise_ready_time"


def intent_s02(d: dict[str, Any]) -> str:
    """Return the S02 intent of a planner decision."""
    if d["action"] == "notify_guest":
        return "ask_entry_consent"
    if d["action"] == "request_inspection":
        return "inspect_pipes" if "WM_MAIN" in d["affected_assets"] else "inspect_room"
    if d["action"] == "order_service" and "after the technician" in d["reason"]:
        return "escalate_plumber"
    return {
        "flag_anomaly": "flag_suspected_leak",
        "block_room": "block_room",
        "dispatch_technician": "dispatch_plumber",
        "order_service": "dispatch_plumber",
    }.get(d["action"], "watch_next_night")


def cause_s02(assets: list[str]) -> str:
    """Return the S02 cause: a leak in a room, or in pipes only the main meter sees."""
    return "pipe_leak" if assets == ["WM_MAIN"] else "room_leak"


def intent_s03(d: dict[str, Any]) -> str:
    """Return the S03 intent of a planner decision."""
    return {
        "flag_anomaly": "flag_hp_degradation",
        "dispatch_technician": "service_heat_pump",
        "order_service": "escalate_contractor",
        "optimize_energy": "protect_occupied_rooms",
        "notify_guest": "notify_guest_comfort",
    }.get(d["action"], "flag_sensor_gap")


INTENT = {"s01": intent, "s02": intent_s02, "s03": intent_s03}


def message(d: dict[str, Any], lang_default: str) -> dict[str, str]:
    """Return the guest message of a notify decision: its language tag and quoted text."""
    m = re.match(
        r"Tell (?:the |the waiting )?guests?(?: arriving today in [^:]+)? ?\((\w\w)\): (.*?)"
        r"(?: \(proposed message; delivery not confirmed\))?\.?$",
        d["reason"],
    )
    if m:
        return {"language": m[1], "text": m[2]}
    text = d["reason"].removesuffix(" (proposed message; delivery not confirmed).")
    return {"language": lang_default, "text": text.removesuffix(".") + "."}


def main(data: Path, out: Path) -> None:
    """Run the pipeline on data and write one bus file and one decisions file per lane."""
    ds = load_dataset(data)
    settings = Settings(data_root=data, out_dir=out)
    run = pipeline.run(ds, settings)
    minutes = {"robot": settings.detect.robot_pass_min, "cleaner": settings.detect.clean_room_min}
    staff = {str(r["resource_id"]) for r in ds.building["staff"]["resources"]}
    by_ep = {e.episode_id: e for e in run.episodes}
    picks = list(S01_EPISODES)
    for issue in ("water_leak", "heating_comfort"):
        picks.append(
            next(
                e.episode_id
                for e in run.episodes
                if e.issue_type == issue
                and any(d["episode_id"] == e.episode_id for d in run.decisions)
            )
        )
    out.mkdir(parents=True, exist_ok=True)
    for lane in ("s01", "s02", "s03"):
        rows, decisions, sig_ids = [], [], {}
        for eid in (p for p in picks if LANE_OF[by_ep[p].issue_type] == lane):
            for n, s in enumerate(by_ep[eid].signals, start=1):
                sid = f"{lane}-{eid[3:]}-sig{n:02d}"
                sig_ids[(eid, stamp(s.time))] = sid
                rows.append(
                    {
                        "v": 1,
                        "row": "signal",
                        "lane": lane,
                        "id": sid,
                        "t_known": stamp(s.time),
                        "issue_type": s.issue_type,
                        "kind": s.kind,
                        "assets": list(s.assets),
                        "evidence": list(s.evidence),
                        "detail": s.detail,
                        "facts": plain(dict(s.data)),
                    }
                )
                if lane == "s02" and s.kind != "night_blank":
                    rows[-1]["cause"] = cause_s02(list(s.assets))
            for n, d in enumerate((d for d in run.decisions if d["episode_id"] == eid), start=1):
                answered = [
                    v for (e, t), v in sig_ids.items() if e == eid and t <= d["timestamp_utc"]
                ]
                p = {
                    "v": 1,
                    "row": "proposal",
                    "lane": lane,
                    "id": f"{lane}-{eid[3:]}-prop{n:02d}",
                    "t": d["timestamp_utc"],
                    "source": "rules",
                    "episode_id": eid,
                    "signal_ids": answered[-1:],
                    "action": d["action"],
                    "assets": d["affected_assets"],
                    "intent": INTENT[lane](d),
                    "priority": d["priority"],
                    "evidence": d["evidence"],
                    "reason": d["reason"],
                }
                for k in (
                    "resource_id",
                    "guest_request_ids",
                    "confidence",
                    "expected_outcome",
                    "verification_plan",
                ):
                    if k in d:
                        p[k] = d[k]
                if d["action"] == "notify_guest":
                    p["message"] = message(d, "en")
                if lane == "s02":
                    p["cause"] = cause_s02(d["affected_assets"])
                    if d["action"] == "dispatch_technician" and "resource_id" in d:
                        p["claims"] = [
                            {
                                "res": "technician",
                                "id": d["resource_id"],
                                "from": d["timestamp_utc"],
                                "min": int(settings.detect.technician_job_h * 60),
                            }
                        ]
                who = d.get("resource_id") or next(
                    (a for a in d["affected_assets"] if a in staff), None
                )
                if lane == "s01" and who and d["action"] in ("schedule_cleaning", "dispatch_robot"):
                    res = (
                        "robot"
                        if d["action"] == "dispatch_robot"
                        else ("on_call" if who.startswith("ONCALL") else "cleaner")
                    )
                    p["claims"] = [
                        {
                            "res": res,
                            "id": who,
                            "from": d["timestamp_utc"],
                            "min": int(minutes.get(res, minutes["cleaner"])),
                        }
                    ]
                rows.append(p)
                rows.append(
                    {
                        "v": 1,
                        "row": "verdict",
                        "lane": lane,
                        "id": f"{p['id']}-v",
                        "t": d["timestamp_utc"],
                        "proposal_id": p["id"],
                        "accepted": True,
                        "why": run.rules[d["event_id"]],
                        "event_id": d["event_id"],
                    }
                )
                decisions.append(d)
        if lane != "s01":
            cited = {i for r in rows if r["row"] == "proposal" for i in r["signal_ids"]}
            rows = [r for r in rows if r["row"] != "signal" or r["id"] in cited]
        rows.sort(key=lambda r: (r.get("t_known") or r["t"], ROW_ORDER[r["row"]], r["id"]))
        (out / f"{lane}.bus.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows), "utf-8"
        )
        contract.write_decisions(decisions, out / f"{lane}.decisions.jsonl")
        print(
            f"{lane}: {sum(r['row'] == 'signal' for r in rows)} signals, "
            f"{sum(r['row'] == 'proposal' for r in rows)} proposals, {len(decisions)} decisions"
        )


ROW_ORDER = {"signal": 0, "proposal": 1, "verdict": 2}

if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
