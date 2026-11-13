"""Signals from detectors, grouped into episodes (one episode = one incident)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class Signal:
    """One detector observation.

    Attributes:
        time: UTC time the observation became known; all cited data is at or before it.
        issue_type: Contract ``issue_type``, e.g. ``water_leak``.
        kind: Detector sub-type, e.g. ``night_flow``.
        assets: Affected IDs; with ``issue_type`` the key that groups signals.
        evidence: Contract evidence references, each ending at or before ``time``.
        detail: Measured values with units, for the decision reason.
        data: Structured facts the planner needs, e.g. a predicted ready time.

    """

    time: pd.Timestamp
    issue_type: str
    kind: str
    assets: tuple[str, ...]
    evidence: tuple[str, ...]
    detail: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class Episode:
    """Signals on one key with no quiet gap longer than the episode limit."""

    episode_id: str
    issue_type: str
    assets: tuple[str, ...]
    signals: list[Signal] = field(default_factory=list)


def _episode_id(issue_type: str, assets: tuple[str, ...], first: pd.Timestamp) -> str:
    where = assets[0] if len(assets) == 1 else f"{assets[0]}+{len(assets) - 1}"
    return f"EP-{issue_type}-{where}-{first:%Y%m%dT%H%M}Z"


def group(signals: Iterable[Signal], quiet_h: float) -> list[Episode]:
    """Group signals per (issue_type, assets); a gap above ``quiet_h`` hours starts a new one."""
    by_key: dict[tuple[str, tuple[str, ...]], list[Signal]] = defaultdict(list)
    for s in signals:
        by_key[(s.issue_type, s.assets)].append(s)
    quiet = pd.Timedelta(hours=quiet_h)
    episodes: list[Episode] = []
    for (issue, assets), items in sorted(by_key.items()):
        current: Episode | None = None
        for s in sorted(items, key=lambda x: (x.time, x.kind)):
            if current is None or s.time - current.signals[-1].time > quiet:
                current = Episode(_episode_id(issue, assets, s.time), issue, assets)
                episodes.append(current)
            current.signals.append(s)
    return episodes
