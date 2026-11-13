"""S02 water leak signals, judged per local night (01:00-05:00, audit section 8).

- ``night_flow``: every night interval of a room has at least ``leak_floor_l_per_15min``.
  Normal rooms reach zero at least once on 100 % of nights (audit section 6). Needs the
  whole night present; a night with a blank interval gives no verdict.
- ``burst``: a room's night volume reaches ``burst_night_volume_l``. Known from the
  interval where the running total crosses it, so it fires during the night.
- ``night_gap``: median of main meter minus room meters reaches ``gap_night_l_per_h``;
  points at pipes outside the rooms. Needs every meter present all night.
"""

from __future__ import annotations

from plato40.audit.candidates import gap_series, room_deltas
from plato40.detect.grid import grid, nights, stamp
from plato40.episodes import Signal
from plato40.load.reader import Dataset
from plato40.settings import Settings

ISSUE = "water_leak"


def signals(ds: Dataset, settings: Settings) -> list[Signal]:
    """Return water leak signals for the whole dataset, each timed when it became known."""
    th = settings.detect
    idx = grid(ds, settings)
    rooms = room_deltas(ds, settings).reindex(idx)
    gap = gap_series(ds, settings).reindex(idx)
    night = nights(idx, settings)
    inside = night.notna().to_numpy()
    out: list[Signal] = []
    for _, block in rooms.loc[inside].groupby(night[inside].to_numpy(), sort=True):
        first, last = block.index[0], block.index[-1]
        for room in block.columns:
            series = block[room]
            ref = (f"room_water.csv:{room}:{stamp(first)}..{stamp(last)}",)
            if series.notna().all() and series.min() >= th.leak_floor_l_per_15min:
                out.append(
                    Signal(
                        last,
                        ISSUE,
                        "night_flow",
                        (room,),
                        ref,
                        f"flow in all {len(series)} night intervals, "
                        f"minimum {series.min():.1f} L per 15 min",
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
        if g.notna().all() and g.median() * 4.0 >= th.gap_night_l_per_h:
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
                    f"median night gap main minus rooms {g.median() * 4.0:.1f} L/h",
                )
            )
    return out
