"""Staff and robot availability as known at decision time.

``staff_roster.csv`` has one row per shift with its final status and the time that
status became known. Before ``status_known_utc`` an absence is not yet known, so the
shift counts as worked.
"""

from __future__ import annotations

import pandas as pd

from plato40.load.reader import Dataset


class Roster:
    """Shift queries that respect when absences became known."""

    def __init__(self, ds: Dataset) -> None:
        """Index the roster of one dataset."""
        self._r = ds.table("staff_roster.csv")

    def _working(self, role: str, known_at: pd.Timestamp) -> pd.DataFrame:
        r = self._r
        believed = (r["status"] == "scheduled") | (r["status_known_utc"] > known_at)
        return r.loc[(r["role"] == role) & believed]

    def on_shift(self, role: str, at: pd.Timestamp, known_at: pd.Timestamp) -> list[str]:
        """Return resource IDs of ``role`` believed on shift at ``at``, sorted."""
        r = self._working(role, known_at)
        on = r.loc[(r["start_utc"] <= at) & (r["end_utc"] > at), "resource_id"]
        return sorted(map(str, on))

    def next_start(
        self, role: str, after: pd.Timestamp, known_at: pd.Timestamp
    ) -> pd.Timestamp | None:
        """Return the earliest time at or after ``after`` that ``role`` is believed on shift."""
        r = self._working(role, known_at)
        r = r.loc[r["end_utc"] > after]
        if r.empty:
            return None
        return max(pd.Timestamp(r["start_utc"].min()), after)

    def shifts(
        self, role: str, start: pd.Timestamp, end: pd.Timestamp, known_at: pd.Timestamp
    ) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
        """Return (resource, from, to) of ``role`` shifts clipped to ``[start, end)``."""
        r = self._working(role, known_at)
        r = r.loc[(r["start_utc"] < end) & (r["end_utc"] > start)].sort_values("resource_id")
        return [
            (str(rid), max(pd.Timestamp(a), start), min(pd.Timestamp(b), end))
            for rid, a, b in zip(r["resource_id"], r["start_utc"], r["end_utc"], strict=True)
        ]
