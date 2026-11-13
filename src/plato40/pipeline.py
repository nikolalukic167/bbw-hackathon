"""The decision pipeline: data -> signals -> episodes -> decisions -> policy -> checks."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from plato40 import contract, llm_guard, messages, plan, policy, verify
from plato40.contract import Decision
from plato40.detect import data_quality, heating, logistics, turnaround, water
from plato40.episodes import Episode, Signal, group
from plato40.load.reader import Dataset
from plato40.settings import Settings
from plato40.state import RoomStates


def context_at(ds: Dataset, states: RoomStates, t: pd.Timestamp) -> policy.Context:
    """Return the policy context at UTC time ``t``."""
    return policy.Context.from_building(
        ds.building, states.rooms_in("occupied", t), states.rooms_in("clean", t)
    )


def _known_at(ds: Dataset, decisions: list[Decision]) -> list[str]:
    """Return decisions citing a guest request or an event_id from after their own time."""
    req = ds.table("guest_requests.csv")
    asked = dict(zip(req["request_id"].astype(str), req["timestamp_utc"], strict=True))
    when = {d["event_id"]: pd.Timestamp(d["timestamp_utc"]) for d in decisions}
    errors = []
    for n, d in enumerate(decisions, start=1):
        t = pd.Timestamp(d["timestamp_utc"])
        late = [g for g in d.get("guest_request_ids", []) if g in asked and asked[g] > t]
        late += [e for e in d.get("dependencies", []) if e in when and when[e] > t]
        if late:
            errors.append(f"line {n}: cites {late} from after {d['timestamp_utc']}")
    return errors


def check(ds: Dataset, states: RoomStates, decisions: list[Decision]) -> list[str]:
    """Return contract, known-at and policy violations, each prefixed with its line number."""
    errors = contract.check_file(decisions, contract.known_asset_ids(ds.building))
    if errors:
        return errors
    errors = _known_at(ds, decisions)
    for n, d in enumerate(decisions, start=1):
        ctx = context_at(ds, states, pd.Timestamp(d["timestamp_utc"]))
        errors += [f"line {n}: {e}" for e in policy.violations(d, ctx)]
    return errors


@dataclass
class Run:
    """Everything one run produced: all signals and episodes, the decisions, their rules."""

    signals: list[Signal]
    episodes: list[Episode]
    decisions: list[Decision]
    rules: dict[str, str] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    messages: dict[str, llm_guard.Result] = field(default_factory=dict)
    verdicts: list[dict[str, Any]] = field(default_factory=list)


def run(ds: Dataset, settings: Settings) -> Run:
    """Run all detectors and planners on one dataset; raise on any violation."""
    states = RoomStates(ds)
    leaks = water.signals(ds, settings)
    signals = (
        leaks
        + turnaround.signals(ds, settings, water.out_of_order(leaks, ds, settings))
        + heating.signals(ds, settings)
        + logistics.signals(ds, settings)
        + data_quality.signals(ds, settings)
    )
    episodes = group(signals, settings.detect.episode_quiet_h)
    decisions = plan.decisions(episodes, states, settings)
    decisions.sort(key=lambda d: (d["timestamp_utc"], d["event_id"]))
    result = Run(signals, episodes, decisions)
    if settings.llm_cache is not None:
        result.messages = messages.localise(decisions, settings.llm_cache)
    for d in decisions:
        ctx = context_at(ds, states, pd.Timestamp(d["timestamp_utc"]))
        result.rules[d["event_id"]] = policy.apply(d, ctx)
    if settings.lanes:
        _lanes(ds, settings, result)
    if settings.follow_ups:
        more = verify.follow_ups(ds, result.episodes, result.decisions, states, settings)
        if settings.llm_cache is not None:
            result.messages |= messages.localise(more, settings.llm_cache)
        for d in more:
            ctx = context_at(ds, states, pd.Timestamp(d["timestamp_utc"]))
            result.rules[d["event_id"]] = policy.apply(d, ctx)
        result.decisions = sorted(
            result.decisions + more, key=lambda d: (d["timestamp_utc"], d["event_id"])
        )
    result.violations = check(ds, states, result.decisions)
    if result.violations:
        raise RuntimeError("decisions violate contract or policy:\n" + "\n".join(result.violations))
    return result


def _lanes(ds: Dataset, settings: Settings, result: Run) -> None:
    """Replace the planner's decisions of each lane in ``settings.lanes`` by that lane's.

    All lanes go to the arbiter together, so they share one ledger and the lane rank. The
    lanes' signals and episodes replace the planner's of the same issue type in ``result``,
    so the trace, explain and the console show what the decisions came from.
    """
    from plato40 import arbiter, bus, lanes  # the arbiter imports this module

    call = None
    if settings.llm_cache is not None and os.environ.get("PLATO40_LLM") == "on":
        model = os.environ.get("PLATO40_LLM_MODEL", llm_guard.DEFAULT_MODEL)
        call = llm_guard.anthropic_call(model)
    cache = settings.llm_cache or settings.out_dir / "llm_cache"
    known = lanes.registry()
    rows: list[dict[str, Any]] = []
    for lane in settings.lanes:
        rows += known[lane](ds, settings, cache, call)[0]
    decisions, verdicts = arbiter.arbitrate(ds, settings, result.decisions, rows, settings.lanes)
    result.verdicts = verdicts
    issues = {str(bus.LANES[lane]["issue_type"]) for lane in settings.lanes}
    episodes: dict[str, Episode] = {}
    for r in rows:
        if r["row"] != "signal":
            continue
        issue = str(r["issue_type"])
        eid = "EP-" + str(r["id"]).removeprefix(f"{r['lane']}-").rsplit("-sig", 1)[0]
        ep = episodes.setdefault(eid, Episode(eid, issue, tuple(r["assets"])))
        ep.signals.append(
            Signal(
                pd.Timestamp(r["t_known"]),
                issue,
                r["kind"],
                tuple(r["assets"]),
                tuple(r["evidence"]),
                r["detail"],
                r["facts"],
            )
        )
    result.signals = [s for s in result.signals if s.issue_type not in issues] + [
        s for ep in episodes.values() for s in ep.signals
    ]
    result.episodes = [e for e in result.episodes if e.issue_type not in issues] + list(
        episodes.values()
    )
    result.decisions = decisions
    kept = {d["event_id"] for d in decisions}
    result.rules = {k: v for k, v in result.rules.items() if k in kept}
    result.rules |= {v["event_id"]: v["why"] for v in verdicts if v["accepted"]}
