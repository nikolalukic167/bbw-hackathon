"""Floor-wide telemetry outages (one gateway per floor, audit section 2).

A floor outage is every room of a floor blank in room telemetry for at least
``MIN_INTERVALS`` consecutive intervals. It is signalled once, when the last of those
intervals fails to arrive; its end is unknown at that time and is not cited.
"""

from __future__ import annotations

from plato40.audit.derive import telemetry_wide
from plato40.detect.grid import grid, stamp
from plato40.episodes import Signal
from plato40.load.reader import Dataset
from plato40.settings import Settings

ISSUE = "data_quality"
MIN_INTERVALS = 2


def signals(ds: Dataset, settings: Settings) -> list[Signal]:
    """Return one signal per floor-wide outage run."""
    idx = grid(ds, settings)
    temp = telemetry_wide(ds, "temperature_c").reindex(idx)
    out: list[Signal] = []
    for gw in ds.building["building"]["gateways"]:
        rooms = tuple(
            str(r["id"]) for r in ds.building["building"]["rooms"] if r["floor"] == gw["floor"]
        )
        blank = temp[list(rooms)].isna().all(axis=1).to_numpy()
        run = 0
        for i, is_blank in enumerate(blank):
            run = run + 1 if is_blank else 0
            if run == MIN_INTERVALS:
                start, now = idx[i - run + 1], idx[i]
                out.append(
                    Signal(
                        now,
                        ISSUE,
                        "floor_outage",
                        rooms,
                        (f"room_telemetry.csv:{gw['id']} rooms:{stamp(start)}..{stamp(now)}",),
                        f"all {len(rooms)} rooms on floor {gw['floor']} ({gw['id']}) blank "
                        f"since {stamp(start)}",
                    )
                )
    return out
