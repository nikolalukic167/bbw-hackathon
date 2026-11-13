"""The decision pipeline: data -> signals -> episodes -> decisions -> policy -> checks."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from plato40 import contract, plan, policy
from plato40.contract import Decision
from plato40.detect import data_quality, heating, turnaround, water
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


def run(ds: Dataset, settings: Settings) -> Run:
    """Run all detectors and planners on one dataset; raise on any violation."""
    states = RoomStates(ds)
    signals = (
        water.signals(ds, settings)
        + turnaround.signals(ds, settings)
        + heating.signals(ds, settings)
        + data_quality.signals(ds, settings)
    )
    episodes = group(signals, settings.detect.episode_quiet_h)
    decisions = [d for ep in episodes for d in plan.decisions(ep, states, settings)]
    decisions.sort(key=lambda d: (d["timestamp_utc"], d["event_id"]))
    result = Run(signals, episodes, decisions)
    for d in decisions:
        ctx = context_at(ds, states, pd.Timestamp(d["timestamp_utc"]))
        result.rules[d["event_id"]] = policy.apply(d, ctx)
    result.violations = check(ds, states, decisions)
    if result.violations:
        raise RuntimeError("decisions violate contract or policy:\n" + "\n".join(result.violations))
    return result
