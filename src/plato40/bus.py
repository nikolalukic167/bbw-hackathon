"""The trigger bus (docs/BUS.md): an append-only JSONL board between triggers and the arbiter.

Three row types, one object per line:

- ``signal``: a trigger observed something. Same fields as ``episodes.Signal``.
- ``proposal``: a trigger (or the LLM advisor) proposes one contract action.
- ``verdict``: the arbiter accepted or rejected a proposal; only the arbiter writes these.

Only the arbiter writes ``decisions.jsonl``. A proposal never carries ``autonomy`` or the
approval flags: the policy sets those. Each lane (s01, s02, s03) has its own enums.
Every check here is a contract: a row that fails it is rejected, never repaired.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from plato40 import contract

VERSION = 1
ROWS = ("signal", "proposal", "verdict")
SOURCES = ("rules", "llm")

LANES: dict[str, dict[str, Any]] = {
    "s01": {
        "issue_type": "room_turnaround",
        "kinds": ("request", "checkin_due", "overstay", "checkout", "ready", "day_plan"),
        "intents": (
            "clean_first",
            "robot_prepass",
            "promise_ready_time",
            "notify_ready_now",
            "notify_day_delay",
            "offer_alternative_room",
            "withdraw_offer",
            "escalate_waiting_guest",
            "request_extra_cleaning",
        ),
        "actions": (
            "schedule_cleaning",
            "dispatch_robot",
            "notify_guest",
            "reassign_room",
            "request_human_approval",
            "release_room",
            "defer_action",
        ),
    },
    "s02": {
        "issue_type": "water_leak",
        "kinds": ("night_flow", "burst", "burst_flow", "night_gap", "night_blank"),
        "intents": (
            "flag_suspected_leak",
            "inspect_room",
            "inspect_pipes",
            "ask_entry_consent",
            "block_room",
            "dispatch_plumber",
            "escalate_plumber",
            "flag_meter_artefact",
            "watch_next_night",
        ),
        "actions": (
            "request_inspection",
            "dispatch_technician",
            "order_service",
            "block_room",
            "notify_guest",
            "flag_anomaly",
            "request_human_approval",
            "defer_action",
        ),
        "causes": (
            "room_leak",
            "pipe_leak",
            "guest_use",
            "cleaning_use",
            "meter_artefact",
            "meter_exchange",
            "unknown",
        ),
    },
    "s03": {
        "issue_type": "heating_comfort",
        "kinds": ("cop_low", "comfort_deficit", "cold_forecast", "price_peak"),
        "intents": (
            "flag_hp_degradation",
            "protect_occupied_rooms",
            "preheat_before_cold",
            "shift_load_off_peak",
            "service_heat_pump",
            "escalate_contractor",
            "notify_guest_comfort",
            "flag_sensor_gap",
        ),
        "actions": (
            "adjust_room_setpoint",
            "optimize_energy",
            "dispatch_technician",
            "order_service",
            "notify_guest",
            "flag_anomaly",
            "request_human_approval",
            "defer_action",
        ),
        "causes": ("hp_degradation", "cold_snap", "sensor_gap", "unknown"),
    },
}
LANGUAGES = ("de", "en", "fr", "it", "nl", "es")
RESOURCES = ("cleaner", "robot", "technician", "on_call", "room")

COMMON = {"v": int, "row": str, "lane": str, "id": str}
FIELDS: dict[str, dict[str, type]] = {
    "signal": COMMON
    | {
        "t_known": str,
        "issue_type": str,
        "kind": str,
        "assets": list,
        "evidence": list,
        "detail": str,
        "facts": dict,
    },
    "proposal": COMMON
    | {
        "t": str,
        "source": str,
        "episode_id": str,
        "signal_ids": list,
        "intent": str,
        "action": str,
        "assets": list,
        "priority": str,
        "evidence": list,
        "reason": str,
    },
    "verdict": COMMON | {"t": str, "proposal_id": str, "accepted": bool, "why": str},
}
OPTIONAL: dict[str, dict[str, type]] = {
    "signal": {"cause": str},
    "proposal": {
        "resource_id": str,
        "guest_request_ids": list,
        "message": dict,
        "claims": list,
        "cause": str,
        "confidence": float,
        "expected_outcome": str,
        "verification_plan": str,
        "needs": list,
    },
    "verdict": {"event_id": str},
}
POLICY_OWNED = ("autonomy", "requires_human_approval", "requires_guest_consent")
REF = re.compile(r"^[a-z_]+\.(csv|jsonl|json):\S")


def _ts(value: str) -> pd.Timestamp | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return pd.Timestamp(value)


def _utc(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts


def _shape(row: Any) -> list[str]:
    """Return the field and type violations that make a row unsafe to read further."""
    if not isinstance(row, Mapping):
        return ["row is not a JSON object"]
    kind = row.get("row")
    if kind not in ROWS:
        return [f"row {kind!r} not in {ROWS}"]
    need, extra = FIELDS[kind], OPTIONAL[kind]
    errors = [f"missing {k}" for k in need if k not in row]
    errors += [f"unknown field {k}" for k in row if k not in need and k not in extra]
    errors += [f"{k} is set by the policy, not by a proposal" for k in POLICY_OWNED if k in row]
    for key, expected in (need | extra).items():
        if key not in row:
            continue
        if not contract._type_ok(row[key], expected):
            errors.append(f"{key} is not {expected.__name__}")
        elif expected is list and key != "claims" and not all(isinstance(x, str) for x in row[key]):
            errors.append(f"{key} is not a list of strings")
    if "v" in row and type(row["v"]) is not int:
        errors.append("v is not int")
    return errors


def check_row(row: Mapping[str, Any], asset_ids: frozenset[str]) -> list[str]:
    """Return contract violations of one bus row; empty if it conforms."""
    errors = _shape(row)
    if errors:
        return errors
    kind = row["row"]
    if row["v"] != VERSION:
        errors.append(f"v {row['v']} is not {VERSION}")
    lane = LANES.get(row["lane"])
    if lane is None:
        return [*errors, f"lane {row['lane']!r} not in {tuple(LANES)}"]
    for key in ("t_known", "t"):
        if key in row and _ts(row[key]) is None:
            errors.append(f"{key} {row[key]!r} is not ISO 8601 UTC ending in Z")
    if kind == "verdict":
        return errors
    if "cause" in row and row["cause"] not in lane.get("causes", ()):
        errors.append(f"cause {row['cause']!r} not in lane {row['lane']} causes")
    unknown = sorted(set(map(str, row["assets"])) - asset_ids)
    if not row["assets"] or unknown:
        errors.append(f"assets empty or unknown: {unknown}")
    bad = [e for e in row["evidence"] if not isinstance(e, str) or not REF.match(e)]
    if not row["evidence"] or bad:
        errors.append(f"evidence empty or not 'file.ext:ref': {bad}")
    if kind == "signal":
        if row["issue_type"] != lane["issue_type"]:
            errors.append(f"issue_type {row['issue_type']!r} is not {lane['issue_type']!r}")
        if row["kind"] not in lane["kinds"]:
            errors.append(f"kind {row['kind']!r} not in lane {row['lane']} kinds")
        return errors
    if row["source"] not in SOURCES:
        errors.append(f"source {row['source']!r} not in {SOURCES}")
    if row["intent"] not in lane["intents"]:
        errors.append(f"intent {row['intent']!r} not in lane {row['lane']} intents")
    if row["action"] not in lane["actions"]:
        errors.append(f"action {row['action']!r} not in lane {row['lane']} actions")
    if row["priority"] not in contract.PRIORITIES:
        errors.append(f"priority {row['priority']!r} not in {contract.PRIORITIES}")
    if not row["signal_ids"]:
        errors.append("signal_ids is empty: a proposal answers at least one signal")
    if "resource_id" in row and row["resource_id"] not in asset_ids:
        errors.append(f"unknown resource_id {row['resource_id']!r}")
    if "confidence" in row and not 0 <= row["confidence"] <= 1:
        errors.append(f"confidence {row['confidence']!r} is not in [0, 1]")
    msg = row.get("message")
    if msg is not None:
        text = msg.get("text") if set(msg) == {"language", "text"} else None
        if msg.get("language") not in LANGUAGES or not isinstance(text, str) or not text.strip():
            errors.append(f"message must be {{language in {LANGUAGES}, non-empty text}}")
        if row["action"] != "notify_guest":
            errors.append("message is only for notify_guest")
    elif row["action"] == "notify_guest":
        errors.append("notify_guest needs a message")
    for c in row.get("claims", []):
        if not isinstance(c, dict) or c.get("res") not in RESOURCES:
            errors.append(f"claim {c!r}: res not in {RESOURCES}")
        elif _ts(c.get("from", "")) is None or type(c.get("min")) is not int or c["min"] <= 0:
            errors.append(f"claim {c!r}: needs from (UTC Z) and min (positive int)")
        elif (start := _ts(c["from"])) is not None and (t := _ts(row["t"])) and start < t:
            errors.append(f"claim {c!r}: from is before the proposal time {row['t']}")
    return errors


def check_known_at(
    rows: list[Mapping[str, Any]], requests: Mapping[str, pd.Timestamp]
) -> list[str]:
    """Return proposals citing a signal or guest request that was not known at their time.

    Rows with a broken shape are skipped here; ``check_file`` reports them.
    """
    ok = [(n, r) for n, r in enumerate(rows, start=1) if not _shape(r)]
    known = {r["id"]: _ts(r["t_known"]) for _, r in ok if r["row"] == "signal"}
    asked = {g: _utc(t) for g, t in requests.items()}
    errors = []
    for n, r in ok:
        t = _ts(r.get("t", ""))
        if r["row"] != "proposal" or t is None:  # a bad time is reported by check_row
            continue
        late = [s for s in r["signal_ids"] if (k := known.get(s)) is None or k > t]
        late += [g for g in r.get("guest_request_ids", []) if g not in asked or asked[g] > t]
        if late:
            errors.append(f"line {n}: {r['id']} cites {late} unknown or from after {r['t']}")
    return errors


def check_file(rows: list[Mapping[str, Any]], asset_ids: frozenset[str]) -> list[str]:
    """Return row contract violations and duplicate ids, each prefixed with its line number."""
    errors, seen = [], set()
    for n, r in enumerate(rows, start=1):
        errors += [f"line {n}: {e}" for e in check_row(r, asset_ids)]
        rid = r.get("id") if isinstance(r, Mapping) else None
        if not isinstance(rid, str):
            continue
        if rid in seen:
            errors.append(f"line {n}: duplicate id {rid}")
        seen.add(rid)
    return errors


def read(path: Path, until: pd.Timestamp | None = None) -> list[dict[str, Any]]:
    """Read bus rows; with ``until``, only rows known at or before it (the known-at reader)."""
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    if until is None:
        return rows
    out, until = [], _utc(until)
    for n, r in enumerate(rows, start=1):
        t = _ts(r.get("t_known") or r.get("t", "")) if isinstance(r, dict) else None
        if t is None:
            raise ValueError(f"{path}:{n}: not an object with a valid t_known or t")
        if t <= until:
            out.append(r)
    return out


def append(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    """Append rows; never rewrites a line already written."""
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
