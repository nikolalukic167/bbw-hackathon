"""Room state over time, driven by lifecycle events.

States: ``occupied`` (check-in to checkout), ``dirty`` (after checkout or a move),
``cleaning`` (human cleaning started), ``clean`` (human cleaning completed), and
``unknown`` before a room's first event. Robot floor cleaning does not change state:
robots cannot certify a room. Each event is known when it happens, so the state at
``t`` uses no future information.
"""

from __future__ import annotations

import pandas as pd

from plato40.load.reader import Dataset

STATE_OF_EVENT = {
    "checkin": "occupied",
    "checkin_early": "occupied",
    "checkin_after_wait": "occupied",
    "stay_in_progress": "occupied",
    "checkout": "dirty",
    "checkout_early": "dirty",
    "cleaning_started": "cleaning",
    "cleaning_completed": "clean",
}
UNKNOWN = "unknown"


class RoomStates:
    """State timeline per room, queried at any UTC time."""

    def __init__(self, ds: Dataset) -> None:
        """Build the timelines from ``events.jsonl``; ties keep file order."""
        ev = ds.events
        rows = ev.loc[ev["type"].isin(STATE_OF_EVENT), ["timestamp_utc", "room_id", "type"]]
        frame = pd.DataFrame(
            {
                "t": rows["timestamp_utc"],
                "room": rows["room_id"].astype(str),
                "state": rows["type"].map(STATE_OF_EVENT),
            }
        )
        if "from_room" in ev.columns:
            moves = ev.loc[ev["type"] == "room_reassigned"]
            frame = pd.concat(
                [
                    frame,
                    pd.DataFrame(
                        {"t": moves["timestamp_utc"], "room": moves["from_room"], "state": "dirty"}
                    ),
                    pd.DataFrame(
                        {"t": moves["timestamp_utc"], "room": moves["to_room"], "state": "occupied"}
                    ),
                ]
            )
        frame = frame.sort_values("t", kind="stable")
        self._times: dict[str, pd.DatetimeIndex] = {}
        self._states: dict[str, list[str]] = {}
        for room, g in frame.groupby("room", sort=True):
            self._times[str(room)] = pd.DatetimeIndex(g["t"])
            self._states[str(room)] = [str(s) for s in g["state"]]
        self.rooms = ds.room_ids()

    def at(self, room: str, t: pd.Timestamp) -> str:
        """Return the state of ``room`` at ``t`` (events at exactly ``t`` included)."""
        times = self._times.get(room)
        if times is None:
            return UNKNOWN
        i = int(times.searchsorted(t, side="right")) - 1
        return self._states[room][i] if i >= 0 else UNKNOWN

    def since(self, room: str, t: pd.Timestamp) -> pd.Timestamp | None:
        """Return when the event that set the state of ``room`` at ``t`` happened, if any.

        For an occupied room this identifies the guest's stay: a new check-in or move-in
        gives a new time.
        """
        times = self._times.get(room)
        if times is None:
            return None
        i = int(times.searchsorted(t, side="right")) - 1
        return pd.Timestamp(times[i]) if i >= 0 else None

    def next_entry(self, room: str, state: str, t: pd.Timestamp) -> pd.Timestamp | None:
        """Return the first time after ``t`` that ``room`` enters ``state``, if any."""
        times = self._times.get(room)
        if times is None:
            return None
        for i in range(int(times.searchsorted(t, side="right")), len(times)):
            if self._states[room][i] == state:
                return pd.Timestamp(times[i])
        return None

    def rooms_in(self, state: str, t: pd.Timestamp) -> frozenset[str]:
        """Return the rooms in ``state`` at ``t``."""
        return frozenset(r for r in self.rooms if self.at(r, t) == state)
