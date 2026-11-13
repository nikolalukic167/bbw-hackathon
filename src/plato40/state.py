"""Building state at a moment in time, reconstructed from what was known then."""

from __future__ import annotations

import pandas as pd


def occupied_rooms(stays: pd.DataFrame, t: pd.Timestamp) -> frozenset[str]:
    """Return rooms with a stay in progress at ``t`` (UTC).

    ``stays`` has ``room_id``, ``start`` and ``end`` (UTC), as built by
    ``plato40.audit.derive.stays``. A stay starts at check-in and ends at checkout, both
    events that are known when they happen, so this uses no future information.
    """
    active = stays.loc[(stays["start"] <= t) & (stays["end"] > t), "room_id"]
    return frozenset(map(str, active))
