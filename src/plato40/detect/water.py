"""S02 water leak signals: per local night (01:00-05:00, audit section 8), and bursts at any hour.

- ``night_flow``: every night interval of a room has at least ``leak_floor_l_per_15min``.
  Normal rooms reach zero at least once on 100 % of nights (audit section 6). Needs the
  whole night present; a lone blank reading is bridged (one bridged reading can hide at
  most one of the 10 or more dry intervals every normal dev night has), a longer blank
  gives no verdict. On a night where
  the meter jitters (a negative step), hourly volumes are judged instead, since the
  jitter cancels within an hour.
- ``burst``: a room's night volume reaches ``burst_night_volume_l``. Known from the
  interval where the running total crosses it, so it fires during the night.
- ``burst_flow``: a room draws at least ``burst_flow_l_per_15min`` in each of
  ``burst_flow_intervals`` consecutive intervals, at any hour.
- ``night_gap``: median of main meter minus room meters reaches ``gap_night_l_per_h``;
  points at pipes outside the rooms. Needs the gap on ``gap_min_coverage`` of the night.
"""

from __future__ import annotations

import pandas as pd

from plato40.detect.grid import grid, nights, stamp
from plato40.episodes import Signal
from plato40.load import derive
from plato40.load.reader import Dataset
from plato40.settings import Settings

ISSUE = "water_leak"


def _volumes(wide: pd.DataFrame, ds: Dataset, settings: Settings) -> pd.DataFrame:
    """Return litres per interval on the full grid from cumulative readings (L).

    Readings go on the grid before differencing, so an absent row is missing rather than
    folded into the next interval. A lone blank reading between two present ones is
    bridged linearly: the volume over its two intervals is measured, only the split is
    not. Longer blanks (gateway outages) stay missing, and so does a meter reset.
    """
    on_grid = wide.reindex(grid(ds, settings))
    lone = on_grid.isna() & on_grid.shift(1).notna() & on_grid.shift(-1).notna()
    bridged = on_grid.mask(lone, (on_grid.shift(1) + on_grid.shift(-1)) / 2)
    return derive.meter_deltas(bridged, settings.detect.meter_reset_drop_l).deltas


def room_volumes(ds: Dataset, settings: Settings) -> pd.DataFrame:
    """Return room water volumes (L per interval) on the full grid; resets are missing."""
    return _volumes(derive.room_water_wide(ds), ds, settings)


def _gap(ds: Dataset, settings: Settings, rooms: pd.DataFrame) -> pd.Series:
    """Return main meter volume minus all room volumes; missing if any meter is missing."""
    main = _volumes(derive.main_water_series(ds).to_frame("WM_MAIN"), ds, settings)
    return main["WM_MAIN"] - rooms.sum(axis=1, min_count=rooms.shape[1])


def _continuous(series: pd.Series, settings: Settings) -> tuple[bool, str]:
    """Return whether a room flowed all night, and how that was measured."""
    floor = settings.detect.leak_floor_l_per_15min
    if series.isna().any():
        return False, ""
    if (series >= 0).all():
        return bool(series.min() >= floor), f"minimum {series.min():.1f} L per 15 min"
    per_hour = 60 // settings.interval_minutes
    hourly = series.groupby(pd.DatetimeIndex(series.index).floor("1h")).sum(min_count=per_hour)
    ok = hourly.notna().all() and hourly.min() >= floor * per_hour
    return bool(ok), f"jittering meter, minimum {hourly.min():.1f} L per hour"


def signals(ds: Dataset, settings: Settings) -> list[Signal]:
    """Return water leak signals for the whole dataset, each timed when it became known."""
    th = settings.detect
    rooms = room_volumes(ds, settings)
    gap = _gap(ds, settings, rooms)
    night = nights(pd.DatetimeIndex(rooms.index), settings)
    inside = night.notna().to_numpy()
    per_hour = 60 / settings.interval_minutes
    out: list[Signal] = []
    for _, block in rooms.loc[inside].groupby(night[inside].to_numpy(), sort=True):
        first, last = block.index[0], block.index[-1]
        for room in block.columns:
            series = block[room]
            ref = (f"room_water.csv:{room}:{stamp(first)}..{stamp(last)}",)
            flowing, how = _continuous(series, settings)
            if flowing:
                out.append(
                    Signal(
                        last,
                        ISSUE,
                        "night_flow",
                        (room,),
                        ref,
                        f"flow in all {len(series)} night intervals, {how}",
                    )
                )
            running = series.fillna(0.0).cumsum()
            crossed = running.index[(running >= th.burst_night_volume_l).to_numpy()]
            if len(crossed):
                t = crossed[0]
                out.append(
                    Signal(
                        t,
                        ISSUE,
                        "burst",
                        (room,),
                        (f"room_water.csv:{room}:{stamp(first)}..{stamp(t)}",),
                        f"{running[t]:.0f} L since {stamp(first)}",
                    )
                )
        g = gap.loc[block.index]
        if (
            g.notna().mean() >= th.gap_min_coverage
            and g.median() * per_hour >= th.gap_night_l_per_h
        ):
            out.append(
                Signal(
                    last,
                    ISSUE,
                    "night_gap",
                    ("WM_MAIN",),
                    (
                        f"water_main.csv:WM_MAIN:{stamp(first)}..{stamp(last)}",
                        f"room_water.csv:all rooms:{stamp(first)}..{stamp(last)}",
                    ),
                    f"median night gap main minus rooms {g.median() * per_hour:.1f} L/h over "
                    f"{int(g.notna().sum())} of {len(g)} intervals",
                )
            )
    return out + _burst_flow(rooms, settings)


def _burst_flow(rooms: pd.DataFrame, settings: Settings) -> list[Signal]:
    """Return one signal per run of sustained high room flow, at the run's first full window."""
    th = settings.detect
    n = th.burst_flow_intervals
    held = (rooms >= th.burst_flow_l_per_15min).astype(float).rolling(n).sum() >= n
    out: list[Signal] = []
    for room in rooms.columns:
        starts = held[room] & ~held[room].shift(1, fill_value=False)
        for t in rooms.index[starts.to_numpy()]:
            begin = t - pd.Timedelta(minutes=settings.interval_minutes * (n - 1))
            volume = rooms.loc[begin:t, room].sum()
            out.append(
                Signal(
                    t,
                    ISSUE,
                    "burst_flow",
                    (room,),
                    (f"room_water.csv:{room}:{stamp(begin)}..{stamp(t)}",),
                    f"{volume:.0f} L in {n} intervals since {stamp(begin)}, at least "
                    f"{th.burst_flow_l_per_15min:.0f} L in each",
                )
            )
    return out
