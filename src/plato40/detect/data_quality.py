"""Data quality: floor outages, dead room sensors, jittering meters and undeclared IDs.

A floor outage (one gateway per floor, audit section 2) is every room of a floor blank in
room telemetry for at least ``outage_min_intervals`` consecutive intervals. It is
signalled once, when the last of those intervals fails to arrive; its end is unknown at
that time and is not cited. A jittering meter shows a negative step in a night window
(after a meter exchange, audit section 6); it is signalled at the end of each such night,
so one episode covers a run of jittering nights.
"""

from __future__ import annotations

import pandas as pd

from plato40.detect.grid import grid, nights, stamp
from plato40.detect.water import room_volumes
from plato40.episodes import Signal
from plato40.load.derive import telemetry_wide
from plato40.load.reader import Dataset
from plato40.settings import Settings

ISSUE = "data_quality"


def signals(ds: Dataset, settings: Settings) -> list[Signal]:
    """Return one signal per floor-wide outage run and per jittering meter night."""
    return _outages(ds, settings) + _dead(ds, settings) + _jitter(ds, settings) + _undeclared(ds)


def _outages(ds: Dataset, settings: Settings) -> list[Signal]:
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
            if run == settings.detect.outage_min_intervals:
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


def _dead(ds: Dataset, settings: Settings) -> list[Signal]:
    """One signal per run of one room's temperature blank while its floor still reports."""
    idx = grid(ds, settings)
    temp = telemetry_wide(ds, "temperature_c").reindex(idx)
    need = int(settings.detect.sensor_dead_h * 60 // settings.interval_minutes)
    out: list[Signal] = []
    rooms = ds.building["building"]["rooms"]
    for gw in ds.building["building"]["gateways"]:
        floor = [str(r["id"]) for r in rooms if r["floor"] == gw["floor"] and r["id"] in temp]
        down = temp[floor].isna().all(axis=1).to_numpy()
        for room in floor:
            blank = temp[room].isna().to_numpy() & ~down
            run = 0
            for i, is_blank in enumerate(blank):
                run = run + 1 if is_blank else 0
                if run == need:
                    start, now = idx[i - run + 1], idx[i]
                    ref = f"room_telemetry.csv:{room} temperature_c:{stamp(start)}..{stamp(now)}"
                    out.append(
                        Signal(
                            now,
                            ISSUE,
                            "sensor_dead",
                            (room,),
                            (ref,),
                            f"{room} temperature blank since {stamp(start)} while the rest of "
                            f"floor {gw['floor']} reports",
                        )
                    )
    return out


def _jitter(ds: Dataset, settings: Settings) -> list[Signal]:
    rooms = room_volumes(ds, settings)
    night = nights(pd.DatetimeIndex(rooms.index), settings)
    inside = night.notna().to_numpy()
    out: list[Signal] = []
    for _, block in rooms.loc[inside].groupby(night[inside].to_numpy(), sort=True):
        first, last = block.index[0], block.index[-1]
        for room in block.columns[(block < 0).any().to_numpy()]:
            out.append(
                Signal(
                    last,
                    ISSUE,
                    "meter_jitter",
                    (str(room),),
                    (f"room_water.csv:{room}:{stamp(first)}..{stamp(last)}",),
                    f"{room} meter steps back {int((block[room] < 0).sum())} times in the "
                    f"night, lowest {block[room].min():.1f} L",
                )
            )
    return out


UNDECLARED = (
    ("room_telemetry.csv", "room_id", "room"),
    ("room_water.csv", "room_id", "room"),
    ("bookings.csv", "room_id", "room"),
    ("equipment.csv", "asset_id", "asset"),
    ("logistics.csv", "asset_id", "asset"),
)


def _undeclared(ds: Dataset) -> list[Signal]:
    """One signal per room or asset ID in the data that building.json does not declare."""
    known = {"room": set(ds.room_ids()), "asset": set(ds.asset_ids())}
    first: dict[tuple[str, str], tuple[pd.Timestamp, str]] = {}
    for file, col, what in UNDECLARED:
        tab = ds.table(file)
        if tab.empty:
            continue
        when = "created_utc" if file == "bookings.csv" else "timestamp_utc"
        for key, t in tab.groupby(col, sort=True)[when].min().items():
            if str(key) in known[what] or pd.isna(t):
                continue
            seen = first.get((what, str(key)))
            if seen is None or pd.Timestamp(t) < seen[0]:
                first[(what, str(key))] = (pd.Timestamp(t), file)
    return [
        Signal(
            t,
            ISSUE,
            "undeclared_id",
            ("BUILDING",),
            (f"{file}:{key} first row {stamp(t)}", "building.json:building"),
            f"{what} {key} appears in {file} but not in building.json; OSAM makes no "
            "decision on it",
        )
        for (what, key), (t, file) in sorted(first.items(), key=lambda x: (x[1][0], x[0]))
    ]
