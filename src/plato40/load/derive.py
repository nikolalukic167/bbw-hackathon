"""Derived series shared by the audit and the detectors.

Every function here is a pure transformation of loaded frames. Units are stated per
function. Missing inputs stay missing in the outputs.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from plato40.load.reader import Dataset

STAY_START_TYPES = ("checkin", "checkin_early", "checkin_after_wait", "stay_in_progress")
STAY_END_TYPES = ("checkout", "checkout_early")


def local(ts: pd.Series, tz: str) -> pd.Series:
    """Convert a UTC timestamp series to local wall-clock time in ``tz``."""
    out: pd.Series = ts.dt.tz_convert(tz)
    return out


def dt_index(index: pd.Index) -> pd.DatetimeIndex:
    """Return ``index`` typed as a DatetimeIndex (it must hold timestamps)."""
    return pd.DatetimeIndex(index)


def dedupe_keep_first(frame: pd.DataFrame, key: list[str]) -> pd.DataFrame:
    """Drop exact key duplicates, keeping the first row in file order."""
    return frame.loc[~frame.duplicated(key, keep="first")]


@dataclass(frozen=True)
class MeterDeltas:
    """Interval consumption from cumulative meters.

    Attributes:
        deltas: Wide frame, index = interval end (UTC), columns = meter; litres per
            15 min. An interval is missing if either bounding reading is missing or
            the meter was reset inside it.
        resets: One row per detected reset: meter, time, drop in litres.

    """

    deltas: pd.DataFrame
    resets: pd.DataFrame


def meter_deltas(wide: pd.DataFrame, reset_drop_l: float) -> MeterDeltas:
    """Difference a wide cumulative-reading frame (litres) into 15-min volumes.

    A negative step larger than ``reset_drop_l`` is a meter reset or exchange; that
    interval is set missing. Smaller negative steps are kept as reported so the audit
    can count them.
    """
    deltas = wide.sort_index().diff()
    is_reset = deltas < -reset_drop_l
    rows = []
    for col in deltas.columns:
        values = deltas[col].to_numpy(dtype=float)
        for i in np.flatnonzero(is_reset[col].to_numpy()):
            rows.append({"meter": col, "timestamp_utc": deltas.index[int(i)], "drop_l": -values[i]})
    resets = pd.DataFrame(rows, columns=["meter", "timestamp_utc", "drop_l"])
    return MeterDeltas(deltas.mask(is_reset), resets)


def room_water_wide(ds: Dataset) -> pd.DataFrame:
    """Return room meter readings (L) as a time x room frame, duplicates dropped."""
    water = dedupe_keep_first(ds.table("room_water.csv"), ["timestamp_utc", "room_id"])
    return water.pivot(index="timestamp_utc", columns="room_id", values="meter_total_l")


def main_water_series(ds: Dataset) -> pd.Series:
    """Return the main meter reading (L) indexed by UTC time, duplicates dropped."""
    main = dedupe_keep_first(ds.table("water_main.csv"), ["timestamp_utc"])
    return main.set_index("timestamp_utc")["meter_total_l"].sort_index()


def stays(ds: Dataset) -> pd.DataFrame:
    """Reconstruct actual stays from lifecycle events.

    Returns one row per booking with ``room_id``, ``start`` (first check-in or
    in-progress marker) and ``end`` (checkout, or the data end if none). A
    ``room_reassigned`` event moves the stay to ``to_room`` from its timestamp on; the
    audit only counts these, it does not split the stay.
    """
    ev = ds.events
    starts = (
        ev.loc[ev["type"].isin(STAY_START_TYPES)]
        .groupby("booking_id")
        .agg(room_id=("room_id", "first"), start=("timestamp_utc", "min"))
    )
    ends = (
        ev.loc[ev["type"].isin(STAY_END_TYPES)]
        .groupby("booking_id")
        .agg(end=("timestamp_utc", "max"))
    )
    out = starts.join(ends, how="left")
    data_end = pd.Timestamp(ds.manifest["end_utc"])
    out["end"] = out["end"].fillna(data_end)
    return out.reset_index()


def occupancy_matrix(ds: Dataset, index: pd.DatetimeIndex) -> pd.DataFrame:
    """Return a boolean time x room frame: True while a reconstructed stay is active."""
    rooms = ds.room_ids()
    occ = pd.DataFrame(False, index=index, columns=rooms)
    st = stays(ds)
    for room, start, end in zip(st["room_id"], st["start"], st["end"], strict=True):
        if room in occ.columns:
            occ.loc[(index >= start) & (index < end), str(room)] = True
    return occ


def comfort_deficit(ds: Dataset, low_c: float, deficit_k: float) -> pd.DataFrame:
    """Return a boolean time x room frame of occupied comfort deficits.

    A deficit interval is occupied (reconstructed stay), window closed or unknown,
    temperature below ``low_c`` and at least ``deficit_k`` below the active setpoint.
    The setpoint condition excludes guests who chose a cool room.
    """
    temp = telemetry_wide(ds, "temperature_c")
    setpoint = telemetry_wide(ds, "setpoint_c").reindex_like(temp)
    window = telemetry_wide(ds, "window_open").reindex_like(temp)
    occ = occupancy_matrix(ds, pd.DatetimeIndex(temp.index))
    return occ & (window != 1.0) & (temp < low_c) & (setpoint - temp >= deficit_k)


def telemetry_wide(ds: Dataset, column: str) -> pd.DataFrame:
    """Return one room telemetry column as a time x room frame, duplicates dropped."""
    tel = dedupe_keep_first(ds.table("room_telemetry.csv"), ["timestamp_utc", "room_id"])
    return tel.pivot(index="timestamp_utc", columns="room_id", values=column)


def equipment_numeric(ds: Dataset) -> pd.DataFrame:
    """Return numeric equipment metrics as time x "ASSET/metric" (units per metric)."""
    eq = ds.table("equipment.csv")
    num = eq.loc[eq["metric"] != "status"].copy()
    num["value"] = pd.to_numeric(num["value"], errors="coerce")
    num["series"] = num["asset_id"] + "/" + num["metric"]
    num = dedupe_keep_first(num, ["timestamp_utc", "series"])
    return num.pivot(index="timestamp_utc", columns="series", values="value").sort_index()


def hp_status(ds: Dataset) -> pd.Series:
    """Return the HP1 status string per timestamp (missing where absent)."""
    eq = ds.table("equipment.csv")
    st = eq.loc[(eq["asset_id"] == "HP1") & (eq["metric"] == "status")]
    st = dedupe_keep_first(st, ["timestamp_utc"])
    return st.set_index("timestamp_utc")["value"].sort_index()


def logistics_wide(ds: Dataset) -> pd.DataFrame:
    """Return logistics as time x "ASSET/metric" (percent or units)."""
    lg = ds.table("logistics.csv").copy()
    lg["series"] = lg["asset_id"] + "/" + lg["metric"]
    lg = dedupe_keep_first(lg, ["timestamp_utc", "series"])
    return lg.pivot(index="timestamp_utc", columns="series", values="value").sort_index()


def weather_series(ds: Dataset) -> pd.Series:
    """Return measured outdoor temperature (degC) indexed by UTC time."""
    w = dedupe_keep_first(ds.table("weather.csv"), ["timestamp_utc"])
    return w.set_index("timestamp_utc")["outdoor_c"].sort_index()


def daily_cop(ds: Dataset, tz: str) -> pd.DataFrame:
    """Return per local day: HP1 heat (kWh), electricity (kWh), COP (-), mean outdoor degC.

    COP is the ratio of daily heat output to daily electric input over the intervals
    where both are present. Days with less than half the intervals present are dropped.
    """
    eq = equipment_numeric(ds)
    heat = eq["HP1/heat_output_kw"]
    elec = eq["HP1/electric_kw"]
    both = heat.notna() & elec.notna()
    frame = pd.DataFrame(
        {
            "heat_kwh": heat.where(both) * 0.25,
            "elec_kwh": elec.where(both) * 0.25,
            "n": both.astype(int),
        }
    )
    frame_idx = dt_index(frame.index).tz_convert(tz)
    day = frame.groupby(frame_idx.date).sum(min_count=1)
    outdoor = weather_series(ds)
    outdoor_idx = dt_index(outdoor.index).tz_convert(tz)
    day["outdoor_c"] = outdoor.groupby(outdoor_idx.date).mean()
    day = day.loc[day["n"] >= 48]
    day["cop"] = day["heat_kwh"] / day["elec_kwh"].replace(0.0, np.nan)
    day.index = pd.to_datetime(day.index)
    return day
