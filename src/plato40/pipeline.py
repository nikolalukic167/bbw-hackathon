"""The decision pipeline: data -> signals -> episodes -> decisions -> policy -> checks."""

from __future__ import annotations

import pandas as pd

from plato40 import contract, plan, policy
from plato40.contract import Decision
from plato40.detect import data_quality, turnaround, water
from plato40.episodes import group
from plato40.load.reader import Dataset
from plato40.settings import Settings
from plato40.state import RoomStates


def context_at(ds: Dataset, states: RoomStates, t: pd.Timestamp) -> policy.Context:
    """Return the policy context at UTC time ``t``."""
    return policy.Context.from_building(
        ds.building, states.rooms_in("occupied", t), states.rooms_in("clean", t)
    )


def check(ds: Dataset, states: RoomStates, decisions: list[Decision]) -> list[str]:
    """Return contract and policy violations, each prefixed with its line number."""
    errors = contract.check_file(decisions, contract.known_asset_ids(ds.building))
    if errors:
        return errors
    for n, d in enumerate(decisions, start=1):
        ctx = context_at(ds, states, pd.Timestamp(d["timestamp_utc"]))
        errors += [f"line {n}: {e}" for e in policy.violations(d, ctx)]
    return errors


def run(ds: Dataset, settings: Settings) -> list[Decision]:
    """Return all decisions for one dataset, sorted by time; raise on any violation."""
    states = RoomStates(ds)
    signals = (
        water.signals(ds, settings)
        + turnaround.signals(ds, settings)
        + data_quality.signals(ds, settings)
    )
    episodes = group(signals, settings.detect.episode_quiet_h)
    decisions = [d for ep in episodes for d in plan.decisions(ep, states, settings)]
    decisions.sort(key=lambda d: (d["timestamp_utc"], d["event_id"]))
    for d in decisions:
        policy.apply(d, context_at(ds, states, pd.Timestamp(d["timestamp_utc"])))
    errors = check(ds, states, decisions)
    if errors:
        raise RuntimeError("decisions violate contract or policy:\n" + "\n".join(errors))
    return decisions
