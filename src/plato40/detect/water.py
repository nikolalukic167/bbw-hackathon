"""S02 water leak signals: per local night (01:00-05:00, audit section 8), and bursts at any hour.

- ``night_flow``: every night interval of a room has at least ``leak_floor_l_per_15min``.
  Normal rooms reach zero at least once on 100 % of nights (audit section 6). Needs the
  whole night present; a lone blank reading is bridged (one bridged reading can hide at
  most one of the 10 or more dry intervals every normal dev night has), a longer blank
  gives no verdict. On a night where the meter jitters (a negative step), hourly volumes
  are judged instead, since the jitter cancels within an hour.
- ``burst``: a room's night volume reaches ``burst_night_volume_l`` with flow in every
  night interval so far. A bath fills in one interval (up to 250 L in dev bathtub
  rooms) between dry ones, so it cannot trip it; a burst flows in all of them. Known
  from the interval where the running total crosses it, so it fires during the night.
- ``burst_flow``: a room draws at least ``burst_flow_l_per_15min`` in each of
  ``burst_flow_intervals`` consecutive intervals, at any hour.
- Night runs: each ``night_flow`` and ``night_gap`` signal carries ``streak``, the run of
  leak nights so far. A night without a verdict (blank intervals, low gap coverage) neither
  counts nor breaks the run; while a run is open it emits ``night_blank`` so the episode
  stays one incident.
- ``night_gap``: median of main meter minus room meters reaches ``gap_night_l_per_h`` above
  the run's own baseline, or ``gap_absolute_l_per_h`` whatever the baseline; points at pipes
  outside the rooms. Needs the gap on ``gap_min_coverage`` of the night. The baseline is a
  low quantile of the past normal nights (``gap_limit``), so another run of the building
  with higher normal losses outside the rooms learns its own normal instead of alarming
  every night. A leak running from the first night is learnt as normal; only the absolute
  net catches it.

Every signal carries the in-house technician's availability as known at its time
(``technician`` on shift now, ``technician_from`` his next shift), so the planner can
route the repair to him or to an external plumber.
"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd

from plato40.detect.grid import grid, nights, stamp
from plato40.episodes import Signal, group
from plato40.load import derive
from plato40.load.reader import Dataset
from plato40.settings import Settings
from plato40.staff import Roster

ISSUE = "water_leak"


def out_of_order(
    signals: list[Signal], ds: Dataset, settings: Settings
) -> dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]]:
    """Return per room the spans with a suspected leak, for room offers to avoid.

    A span starts at the room's first leak signal and ends when the next repair work order
    on the room completes (known at ``completed_utc``), or ``episode_quiet_h`` after the
    last signal if none does.
    """
    quiet = settings.detect.episode_quiet_h
    rooms = set(ds.room_ids())
    wo = ds.table("work_orders.csv")
    spans: dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
    for ep in group([s for s in signals if s.assets[0] in rooms], quiet):
        room, start, last = ep.assets[0], ep.signals[0].time, ep.signals[-1].time
        done = wo.loc[(wo["asset_id"] == room) & (wo["created_utc"] >= start), "completed_utc"]
        end = done.min() if done.notna().any() else last + pd.Timedelta(hours=quiet)
        spans.setdefault(room, []).append((start, pd.Timestamp(end)))
    return spans


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


def gap_limit(normal: list[float], settings: Settings) -> tuple[float, float | None]:
    """Return the night gap (L/h) that is a leak, and the baseline it came from, if any.

    ``normal`` holds the median gaps of the past nights without a leak verdict, oldest first.
    """
    th = settings.detect
    recent = normal[-th.gap_baseline_nights :]
    if len(recent) < th.gap_baseline_min_nights:
        return th.gap_absolute_l_per_h, None
    base = float(pd.Series(recent).quantile(th.gap_baseline_quantile))
    return min(th.gap_absolute_l_per_h, base + th.gap_night_l_per_h), base


def signals(ds: Dataset, settings: Settings) -> list[Signal]:
    """Return water leak signals for the whole dataset, each timed when it became known."""
    th = settings.detect
    rooms = room_volumes(ds, settings)
    gap = _gap(ds, settings, rooms)
    night = nights(pd.DatetimeIndex(rooms.index), settings)
    inside = night.notna().to_numpy()
    per_hour = 60 / settings.interval_minutes
    out: list[Signal] = []
    streak: dict[str, int] = {}
    normal: list[float] = []

    def verdict(asset: str, blank: bool, leaking: bool) -> int | None:
        """Update the asset's run of leak nights; a night without a verdict keeps it."""
        if blank:
            return None
        streak[asset] = streak.get(asset, 0) + 1 if leaking else 0
        return streak[asset]

    def held(asset: str, t: pd.Timestamp, ref: tuple[str, ...], why: str) -> None:
        if streak.get(asset, 0):
            out.append(
                Signal(
                    t,
                    ISSUE,
                    "night_blank",
                    (asset,),
                    ref,
                    f"no verdict ({why}); run of {streak[asset]} leak nights kept",
                    {"streak": streak[asset]},
                )
            )

    for _, block in rooms.loc[inside].groupby(night[inside].to_numpy(), sort=True):
        first, last = block.index[0], block.index[-1]
        for room in block.columns:
            series = block[room]
            ref = (f"room_water.csv:{room}:{stamp(first)}..{stamp(last)}",)
            blank = bool(series.isna().any())
            flowing, how = _continuous(series, settings)
            n = verdict(room, blank, flowing)
            if blank:
                held(room, last, ref, f"{int(series.isna().sum())} blank intervals")
            elif flowing:
                out.append(
                    Signal(
                        last,
                        ISSUE,
                        "night_flow",
                        (room,),
                        ref,
                        f"flow in all {len(series)} night intervals, {how}",
                        {"streak": n},
                    )
                )
            running = series.cumsum()
            wet = (series >= th.leak_floor_l_per_15min).cumprod().astype(bool)
            crossed = running.index[((running >= th.burst_night_volume_l) & wet).to_numpy()]
            if len(crossed):
                t = crossed[0]
                out.append(
                    Signal(
                        t,
                        ISSUE,
                        "burst",
                        (room,),
                        (f"room_water.csv:{room}:{stamp(first)}..{stamp(t)}",),
                        f"{running[t]:.0f} L since {stamp(first)}, flow in every interval",
                    )
                )
        g = gap.loc[block.index]
        refs = (
            f"water_main.csv:WM_MAIN:{stamp(first)}..{stamp(last)}",
            f"room_water.csv:all rooms:{stamp(first)}..{stamp(last)}",
        )
        blank = bool(g.notna().mean() < th.gap_min_coverage)
        rate = float(g.median() * per_hour)
        limit, base = gap_limit(normal, settings)
        high = bool(rate >= limit)
        n = verdict("WM_MAIN", blank, high)
        if blank:
            held("WM_MAIN", last, refs, f"gap on {int(g.notna().sum())} of {len(g)} intervals")
        elif high:
            against = (
                f"baseline {base:.1f} L/h from past nights"
                if base is not None
                else "no baseline yet"
            )
            out.append(
                Signal(
                    last,
                    ISSUE,
                    "night_gap",
                    ("WM_MAIN",),
                    refs,
                    f"median night gap main minus rooms {rate:.1f} L/h over "
                    f"{int(g.notna().sum())} of {len(g)} intervals ({against}, "
                    f"leak from {limit:.1f} L/h)",
                    {"streak": n, "baseline_l_per_h": base, "limit_l_per_h": limit},
                )
            )
        else:
            normal.append(rate)
    return _with_technician(out + _burst_flow(rooms, settings), ds)


def _with_technician(signals: list[Signal], ds: Dataset) -> list[Signal]:
    """Return the signals with the technician's availability as known at each signal time."""
    roster = Roster(ds)
    out = []
    for s in signals:
        on = roster.on_shift("technician", s.time, s.time)
        when = s.time if on else roster.next_start("technician", s.time, s.time)
        out.append(
            replace(
                s, data={**s.data, "technician": on[0] if on else None, "technician_from": when}
            )
        )
    return out


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
