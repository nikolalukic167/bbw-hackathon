"""Supervisor LLM (ADR-003): reviews the rules' decisions and may add a few checked ones.

The rules decide first. At chosen review points the supervisor sees what was known at that
time: the decisions of the last 24 h and, when given, a context snapshot. It may add up to
``MAX_ADDS`` decisions from ``ADD``, each following up decisions it was shown. It never
changes or removes a decision. Review points:

- ``daily``: every day at ``REVIEW_LOCAL_H`` local, over the last 24 h of the building.
- ``hotspot``: an asset that got decisions of two or more issue types within 24 h, at the
  later decision's time, once per asset and local day (cross-domain conflicts).

An answer is used only if it passes every check: schema, action and priority enums, assets
it was shown, ``about`` naming decisions it was shown, every evidence ref copied from the
refs it was shown, and every number and ID in its reason present in its input. A failing
answer adds nothing and goes to ``rejected/``. Answers are cached like guest messages, so a
rerun with ``PLATO40_LLM=off`` reproduces them, and a cache miss with the LLM off adds nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pandas as pd

from plato40 import contract, llm_guard, policy
from plato40.contract import Decision
from plato40.detect.grid import stamp
from plato40.load.reader import Dataset
from plato40.settings import Settings
from plato40.state import RoomStates

PROMPT_VERSION = "supervisor/1"
DEFAULT_MODEL = "claude-sonnet-5-5"
ADD = ("flag_anomaly", "request_human_approval", "request_inspection", "defer_action")
MAX_ADDS = 2
REVIEW_LOCAL_H = 7
WINDOW = pd.Timedelta(hours=24)
MAX_SHOWN = 30
WORKERS = 16
SYSTEM = (
    "You supervise operations for PLATO-40, a 40-room aparthotel run by rule-based "
    "triggers for water leaks, room turnaround, heating comfort, maintenance, logistics, "
    "occupancy and energy. You get the decisions the rules took in the last 24 hours and a "
    "snapshot of the building, all as known at time t; you know nothing after t. Your job is "
    "to catch what the rules miss: conflicts between domains on one asset or one person, a "
    "problem that keeps coming back, a pattern no single trigger sees, and data glitches that "
    "look like faults. If the rules handled it, add nothing: most reviews should add nothing, "
    "and an alert without a new reason is spam. Safety comes before guest comfort, comfort "
    "before energy. You may add at most 2 decisions, only flag_anomaly, "
    "request_human_approval, request_inspection or defer_action. Each must name in 'about' "
    "the event_ids it follows up, cite evidence copied exactly from 'refs', and use in its "
    "reason only numbers, times and IDs that appear in the input. Never invent a cause, a "
    "capacity or a person. Reading the snapshot: cop_ratio is HP1's COP divided by its own "
    "reference COP, so 1.0 is normal and 0.97 is fine; night water is a room's total over the "
    "night, and an occupied room uses water at night (a shower is 50 to 80 l), so a leak shows "
    "as flow in every night interval, not as one large total; an open work order has no "
    "completion time yet, which does not prove the work was not done. Always answer with the "
    "review tool, with an empty add list when you add nothing."
)
SKIP_HOTSPOT = ("energy_optimization",)
"""Issue types that never make a hotspot: arrival pre-heat touches most rooms every day."""
TOOL: Any = {
    "name": "review",
    "description": "Your review of the rules' decisions at time t.",
    "input_schema": {
        "type": "object",
        "properties": {
            "assessment": {
                "type": "string",
                "description": "One or two sentences: what the rules handled, what they missed.",
            },
            "add": {
                "type": "array",
                "maxItems": MAX_ADDS,
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"enum": list(ADD)},
                        "priority": {"enum": list(contract.PRIORITIES)},
                        "assets": {"type": "array", "items": {"type": "string"}},
                        "about": {"type": "array", "items": {"type": "string"}},
                        "reason": {"type": "string"},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                        "check": {"type": "string", "description": "How to tell it worked."},
                    },
                    "required": [
                        "action",
                        "priority",
                        "assets",
                        "about",
                        "reason",
                        "evidence",
                        "check",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["assessment", "add"],
        "additionalProperties": False,
    },
}

Snapshot = Callable[[pd.Timestamp, Sequence[str]], dict[str, Any]]


def _points(decisions: list[Decision], settings: Settings) -> list[tuple[str, pd.Timestamp, str]]:
    """Return the review points as (kind, t, asset), in time order."""
    if not decisions:
        return []
    tz = settings.local_tz
    times = [pd.Timestamp(d["timestamp_utc"]) for d in decisions]
    first, last = min(times).tz_convert(tz), max(times).tz_convert(tz)
    out = []
    for day in pd.date_range(first.normalize(), last.normalize(), freq="D"):
        t = (day + pd.Timedelta(hours=REVIEW_LOCAL_H)).tz_convert("UTC")
        if first <= t <= last:
            out.append(("daily", t, "BUILDING"))
    seen: dict[str, list[tuple[pd.Timestamp, str]]] = {}
    done: set[tuple[str, object]] = set()
    for d, t in sorted(zip(decisions, times, strict=True), key=lambda x: (x[1], x[0]["event_id"])):
        for a in d["affected_assets"]:
            if a == "BUILDING" or d["issue_type"] in SKIP_HOTSPOT:
                continue
            recent = [(s, i) for s, i in seen.get(a, []) if t - s < WINDOW]
            recent.append((t, d["issue_type"]))
            seen[a] = recent
            key = (a, t.tz_convert(tz).date())
            if len({i for _, i in recent}) >= 2 and key not in done:
                done.add(key)
                out.append(("hotspot", t, a))
    return sorted(out, key=lambda x: (x[1], x[0], x[2]))


def _shown(decisions: list[Decision], kind: str, t: pd.Timestamp, asset: str) -> list[Decision]:
    """Return the decisions a review sees: known by t, in the window, most urgent first."""
    mine = [
        d
        for d in decisions
        if t - WINDOW < pd.Timestamp(d["timestamp_utc"]) <= t
        and (kind == "daily" or asset in d["affected_assets"])
    ]
    rank = {p: n for n, p in enumerate(contract.PRIORITIES)}
    mine.sort(key=lambda d: (rank[d["priority"]], d["timestamp_utc"], d["event_id"]))
    return sorted(mine[:MAX_SHOWN], key=lambda d: (d["timestamp_utc"], d["event_id"]))


def _user(
    kind: str, t: pd.Timestamp, asset: str, shown: list[Decision], all_in: int, snap: dict[str, Any]
) -> tuple[str, set[str], set[str], set[str]]:
    """Return the user turn and the refs, event_ids and assets it shows."""
    refs = {e for d in shown for e in d["evidence"]} | set(snap.get("refs", []))
    assets = {a for d in shown for a in d["affected_assets"]} | set(snap.get("assets", {}))
    assets |= {asset}
    counts: Counter[str] = Counter(f"{d['issue_type']}/{d['action']}" for d in shown)
    body = {
        "review": kind,
        "t": stamp(t),
        "asset": asset,
        "decisions_in_window": all_in,
        "shown": len(shown),
        "counts": dict(sorted(counts.items())),
        "decisions": [
            {
                "event_id": d["event_id"],
                "t": d["timestamp_utc"],
                "issue": d["issue_type"],
                "action": d["action"],
                "priority": d["priority"],
                "autonomy": d.get("autonomy", ""),
                "assets": d["affected_assets"],
                "reason": d["reason"][:160],
            }
            for d in shown
        ],
        "snapshot": {k: v for k, v in snap.items() if k != "refs"},
        "refs": sorted(refs),
    }
    user = json.dumps(body, ensure_ascii=False, sort_keys=True)
    return user, refs, {d["event_id"] for d in shown}, assets


def _problems(
    raw: str, user: str, refs: set[str], ids: set[str], assets: set[str], known: frozenset[str]
) -> list[str]:
    """Return why an answer cannot be used; empty if every add passes."""
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        return ["answer is not JSON"]
    if isinstance(out, dict) and set(out) == {"assessment"}:
        out["add"] = []
    if not isinstance(out, dict) or set(out) != {"assessment", "add"}:
        return ["answer is not {assessment, add}"]
    adds = out["add"]
    if not isinstance(adds, list) or len(adds) > MAX_ADDS:
        return [f"add is not a list of at most {MAX_ADDS}"]
    why = []
    for n, a in enumerate(adds, start=1):
        if not isinstance(a, dict) or set(a) != set(
            TOOL["input_schema"]["properties"]["add"]["items"]["required"]
        ):
            why.append(f"add {n}: wrong fields")
            continue
        if a["action"] not in ADD:
            why.append(f"add {n}: action {a['action']!r} not in {ADD}")
        if a["priority"] not in contract.PRIORITIES:
            why.append(f"add {n}: priority {a['priority']!r}")
        bad = [x for x in a["assets"] if x not in known or (x not in assets and x not in user)]
        if not a["assets"] or bad:
            why.append(f"add {n}: assets empty or not shown: {bad}")
        bad = [x for x in a["about"] if x not in ids]
        if not a["about"] or bad:
            why.append(f"add {n}: about empty or not shown: {bad}")
        bad = [x for x in a["evidence"] if x not in refs]
        if not a["evidence"] or bad:
            why.append(f"add {n}: evidence empty or not copied from refs: {bad}")
        text = f"{a['reason']} {a['check']}"
        bad = [i for i in llm_guard.ID.findall(text) if i not in user]
        bad += [x for x in llm_guard.NUMBER.findall(llm_guard.ID.sub(" ", text)) if x not in user]
        if bad:
            why.append(f"add {n}: numbers or IDs not in the input: {bad}")
    return why


def review(
    ds: Dataset,
    settings: Settings,
    decisions: list[Decision],
    cache_dir: Path,
    call: llm_guard.Call | None = None,
    snapshot: Snapshot | None = None,
) -> tuple[list[Decision], dict[str, str], list[dict[str, Any]]]:
    """Return the added decisions (flags set by the policy), their rules, and a review log."""
    from plato40.pipeline import context_at  # the pipeline imports this module

    model = os.environ.get("PLATO40_SUPERVISOR_MODEL", DEFAULT_MODEL)
    root = cache_dir / "supervisor"
    known = contract.known_asset_ids(ds.building)
    jobs = []
    for kind, t, asset in _points(decisions, settings):
        shown = _shown(decisions, kind, t, asset)
        window = sum(
            1
            for d in decisions
            if t - WINDOW < pd.Timestamp(d["timestamp_utc"]) <= t
            and (kind == "daily" or asset in d["affected_assets"])
        )
        snap = (
            snapshot(t, sorted({a for d in shown for a in d["affected_assets"]} - {"BUILDING"})[:8])
            if snapshot
            else {}
        )
        user, refs, ids, assets = _user(kind, t, asset, shown, window, snap)
        blob = json.dumps(
            {
                "model": model,
                "prompt": PROMPT_VERSION,
                "system": SYSTEM,
                "tool": TOOL,
                "user": user,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        jobs.append(
            (kind, t, asset, user, refs, ids, assets, hashlib.sha256(blob.encode()).hexdigest())
        )

    live = call is not None and os.environ.get("PLATO40_LLM", "off") == "on"
    missing = [j for j in jobs if not (root / f"{j[-1]}.json").exists()]
    answers: dict[str, tuple[str, str]] = {}
    if live and missing:
        assert call is not None

        def ask(job: tuple[Any, ...]) -> tuple[str, tuple[str, str]]:
            for attempt in (1, 2):  # a model may answer in prose once instead of the tool
                try:
                    return job[-1], (call(SYSTEM, job[3]), "llm")
                except StopIteration:
                    if attempt == 2:
                        return job[-1], ("", "call failed: no tool answer twice")
                except Exception as exc:  # network, quota, timeout: the rules' decisions stand
                    return job[-1], ("", f"call failed: {exc!r}")
            return job[-1], ("", "unreachable")

        with ThreadPoolExecutor(WORKERS) as pool:
            answers = dict(pool.map(ask, missing))

    states = RoomStates(ds)
    by_id = {d["event_id"]: d for d in decisions}
    added: list[Decision] = []
    rules: dict[str, str] = {}
    log: list[dict[str, Any]] = []
    for kind, t, asset, user, refs, ids, assets, key in jobs:
        path = root / f"{key}.json"
        if path.exists():
            raw, source = json.loads(path.read_text("utf-8"))["answer"], "cache"
        elif key in answers and answers[key][1] == "llm":
            raw, source = answers[key][0], "llm"
        else:
            reason = answers[key][1] if key in answers else "no cached answer; LLM off"
            log.append(
                {"kind": kind, "t": stamp(t), "asset": asset, "source": "none", "why": reason}
            )
            continue
        why = _problems(raw, user, refs, ids, assets, known)
        if source == "llm":
            where = root / "rejected" if why else root
            where.mkdir(parents=True, exist_ok=True)
            record = {
                "model": model,
                "prompt": PROMPT_VERSION,
                "user": user,
                "answer": raw,
                "violations": why,
            }
            (where / path.name).write_text(
                json.dumps(record, ensure_ascii=False, sort_keys=True, indent=1), "utf-8"
            )
        row: dict[str, Any] = {"kind": kind, "t": stamp(t), "asset": asset, "source": source}
        if why:
            log.append(row | {"why": "; ".join(why)})
            continue
        out = json.loads(raw)
        row["assessment"] = out["assessment"]
        mine = []
        for n, a in enumerate(out.get("add", []), start=1):
            first = by_id[a["about"][0]]
            d: Decision = {
                "event_id": f"SV-{kind[0].upper()}-{asset}-{t:%Y%m%dT%H%M}Z-{n:02d}",
                "timestamp_utc": stamp(t),
                "affected_assets": list(a["assets"]),
                "issue_type": first["issue_type"],
                "action": a["action"],
                "priority": a["priority"],
                "reason": f"Supervisor review ({model}): {a['reason']}",
                "requires_human_approval": False,
                "episode_id": first["episode_id"],
                "evidence": list(a["evidence"]),
                "dependencies": list(a["about"]),
                "expected_outcome": "A person looks at what the supervisor flagged (simulated).",
                "verification_plan": a["check"],
                "confidence": 0.6,
            }
            try:
                rules[d["event_id"]] = policy.apply(d, context_at(ds, states, t))
            except ValueError as exc:
                row.setdefault("dropped", []).append(str(exc))
                continue
            errors = contract.check_decision(d, known)
            if errors:
                row.setdefault("dropped", []).append("; ".join(errors))
                continue
            mine.append(d)
        row["added"] = [d["event_id"] for d in mine]
        added += mine
        log.append(row)
    return added, rules, log
