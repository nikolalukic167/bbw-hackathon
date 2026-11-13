"""The decision output contract (DECISION_CONTRACT.md v2): writing and validating.

A decision is a plain JSON object. This module checks shape only: required fields, types,
enumerations, known asset IDs and unique ``event_id``. Whether a decision is allowed is
the policy's job (``plato40.policy``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

Decision = dict[str, Any]

REQUIRED: dict[str, type] = {
    "event_id": str,
    "timestamp_utc": str,
    "affected_assets": list,
    "issue_type": str,
    "action": str,
    "priority": str,
    "reason": str,
    "requires_human_approval": bool,
}
OPTIONAL: dict[str, type] = {
    "autonomy": str,
    "episode_id": str,
    "evidence": list,
    "expected_outcome": str,
    "verification_plan": str,
    "dependencies": list,
    "guest_request_ids": list,
    "requires_guest_consent": bool,
    "resource_id": str,
    "confidence": float,
}
PRIORITIES = ("critical", "high", "medium", "low")
AUTONOMY_LEVELS = ("autonomous", "human_approval", "approval_and_consent")
ACTIONS = (
    "schedule_cleaning",
    "dispatch_robot",
    "adjust_room_setpoint",
    "optimize_energy",
    "block_room",
    "release_room",
    "reassign_room",
    "request_inspection",
    "dispatch_technician",
    "order_service",
    "notify_guest",
    "flag_anomaly",
    "defer_action",
    "request_human_approval",
)
EXTRA_ASSET_IDS = ("MAIN_ENTRANCE", "BUILDING")


def known_asset_ids(building: Mapping[str, Any]) -> frozenset[str]:
    """Return every ID a decision may name: rooms, assets, staff, robots, extras."""
    b = building["building"]
    ids = [r["id"] for r in b["rooms"]] + [a["id"] for a in b["assets"]]
    ids += [r["resource_id"] for r in building["staff"]["resources"]]
    return frozenset(map(str, ids)) | frozenset(EXTRA_ASSET_IDS)


def _type_ok(value: Any, expected: type) -> bool:
    if expected is float:
        return isinstance(value, int | float) and not isinstance(value, bool)
    return isinstance(value, expected)


def check_decision(d: Mapping[str, Any], asset_ids: frozenset[str]) -> list[str]:
    """Return contract violations of one decision; empty if it conforms."""
    errors = [f"missing {k}" for k in REQUIRED if k not in d]
    for key, expected in (REQUIRED | OPTIONAL).items():
        if key in d and not _type_ok(d[key], expected):
            errors.append(f"{key} is not {expected.__name__}")
    if errors:
        return errors
    ts = d["timestamp_utc"]
    if not ts.endswith("Z"):
        errors.append("timestamp_utc does not end in Z")
    else:
        try:
            datetime.fromisoformat(ts)
        except ValueError:
            errors.append(f"timestamp_utc unparsable: {ts}")
    if d["priority"] not in PRIORITIES:
        errors.append(f"priority {d['priority']!r} not in {PRIORITIES}")
    if d["action"] not in ACTIONS:
        errors.append(f"action {d['action']!r} not in the vocabulary")
    if "autonomy" in d and d["autonomy"] not in AUTONOMY_LEVELS:
        errors.append(f"autonomy {d['autonomy']!r} not in {AUTONOMY_LEVELS}")
    if not d["affected_assets"]:
        errors.append("affected_assets is empty")
    unknown = sorted(set(map(str, d["affected_assets"])) - asset_ids)
    if unknown:
        errors.append(f"unknown affected_assets {unknown}")
    if "resource_id" in d and d["resource_id"] not in asset_ids:
        errors.append(f"unknown resource_id {d['resource_id']!r}")
    if "confidence" in d and not 0.0 <= d["confidence"] <= 1.0:
        errors.append("confidence outside 0..1")
    return errors


def read_decisions(path: Path) -> list[Decision]:
    """Read a decisions.jsonl file; raise ValueError naming the line on bad JSON."""
    decisions = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{n}: invalid JSON: {exc.msg}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"{path}:{n}: not a JSON object")
        decisions.append(obj)
    return decisions


def check_file(decisions: list[Decision], asset_ids: frozenset[str]) -> list[str]:
    """Return contract violations of a whole file, each prefixed with its line number."""
    errors = []
    seen: set[str] = set()
    for n, d in enumerate(decisions, start=1):
        errors += [f"line {n}: {e}" for e in check_decision(d, asset_ids)]
        event_id = d.get("event_id")
        if isinstance(event_id, str):
            if event_id in seen:
                errors.append(f"line {n}: duplicate event_id {event_id}")
            seen.add(event_id)
    return errors


def write_decisions(decisions: Iterable[Decision], path: Path) -> int:
    """Write decisions as UTF-8 JSON lines; return how many were written."""
    lines = [json.dumps(d, ensure_ascii=False) for d in decisions]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return len(lines)
