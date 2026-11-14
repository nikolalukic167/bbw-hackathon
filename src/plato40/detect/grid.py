"""The 15-minute time grid and the local night window shared by detectors."""

from __future__ import annotations

import pandas as pd

from plato40.load.reader import Dataset
from plato40.settings import Settings


def grid(ds: Dataset, settings: Settings) -> pd.DatetimeIndex:
    """Return every expected interval timestamp (UTC) of the dataset."""
    return pd.date_range(
        pd.Timestamp(ds.manifest["start_utc"]),
        pd.Timestamp(ds.manifest["end_utc"]),
        freq=f"{settings.interval_minutes}min",
        inclusive="left",
    )


def nights(index: pd.DatetimeIndex, settings: Settings) -> pd.Series:
    """Map each timestamp in the local night window to its night label, else missing.

    A night is labelled by the local date it starts on. Timestamps are interval ends,
    as in the meter deltas.
    """
    local = index.tz_convert(settings.local_tz)
    inside = (local.hour >= settings.night.start_hour) & (local.hour < settings.night.end_hour)
    label = pd.Series(local.date, index=index, dtype=object)
    return label.where(inside)


def stamp(t: pd.Timestamp) -> str:
    """Format a UTC time the way the contract writes it, e.g. ``2026-04-09T02:00Z``."""
    return f"{t.tz_convert('UTC'):%Y-%m-%dT%H:%M}Z"


def local_time(t: pd.Timestamp | None, ref: pd.Timestamp, tz: str) -> str:
    """Format ``t`` as local wall time, with the date if it is not on ``ref``'s local day."""
    if t is None:
        return "unknown"
    lt = t.tz_convert(tz)
    same_day = lt.date() == ref.tz_convert(tz).date()
    return f"{lt:%H:%M} local" if same_day else f"{lt:%a %d %b %H:%M} local"


STATE_EVENTS = (
    "stay_in_progress",
    "checkin",
    "checkin_early",
    "checkin_after_wait",
    "checkout",
    "checkout_early",
    "no_show",
    "cleaning_started",
    "cleaning_completed",
)
_INDEX: list[tuple[Dataset, dict[str, tuple[pd.DatetimeIndex, list[str]]], pd.DataFrame]] = []


def _events(ds: Dataset) -> tuple[dict[str, tuple[pd.DatetimeIndex, list[str]]], pd.DataFrame]:
    for seen, rooms, ev in _INDEX:
        if seen is ds:
            return rooms, ev
    ev = ds.events.sort_values(["timestamp_utc", "event_id"], kind="stable")
    st = ev.loc[ev["type"].isin(STATE_EVENTS) & ev["room_id"].notna()]
    rooms = {
        str(room): (pd.DatetimeIndex(g["timestamp_utc"]), [str(x) for x in g["event_id"]])
        for room, g in st.groupby("room_id", sort=True)
    }
    _INDEX[:] = [(ds, rooms, ev)]
    return rooms, ev


def state_ref(ds: Dataset, room: str, t: pd.Timestamp) -> str:
    """Return the evidence ref of the last event at or before ``t`` that set ``room``'s state."""
    times, ids = _events(ds)[0].get(room, (pd.DatetimeIndex([]), []))
    k = int(times.searchsorted(t, side="right"))
    return f"events.jsonl:{ids[k - 1]}" if k else f"events.jsonl:{room} no event by {stamp(t)}"


def events_ref(ds: Dataset, t: pd.Timestamp) -> str:
    """Return the evidence ref of every event known at ``t``, as a range of event IDs."""
    ev = _events(ds)[1]
    known = ev.loc[ev["timestamp_utc"] <= t, "event_id"]
    if known.empty:
        return f"events.jsonl:no event by {stamp(t)}"
    return f"events.jsonl:{known.iloc[0]}..{known.iloc[-1]}"
