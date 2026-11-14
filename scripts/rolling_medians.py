"""Past-only medians ("ground truth so far") for every room and every 15-minute timestamp.

For each timestamp t of the 15-minute grid, every parameter gets the median of all its
values with timestamp strictly before t (t itself is excluded). The first timestamp has
no past, so its medians are null. A trigger at t can compare the reading at t with this
median without looking at t or the future.

Usage:
    python rolling_medians.py <data_dir> [out.json]

Parameters and sources:
    energy.csv        building_load_kw, hvac_electric_kw, pv_kw, battery_discharge_kw_signed,
                      battery_soc_kwh, grid_import_kw, grid_export_kw        (same for all rooms)
    equipment.csv     one parameter per asset and numeric metric, e.g. HP1.electric_kw
                      (status is text, so it has no median and is left out)  (same for all rooms)
    logistics.csv     one parameter per asset and metric, e.g. VEND1.stock_drinks
                                                                             (same for all rooms)
    room_telemetry    temperature_c, setpoint_c, humidity_pct, co2_ppm      (per room)
    room_water.csv    meter_total_l (cumulative counter, as asked) and
                      water_l_per_15min (consumption per interval)           (per room)
    tariff.csv        price_eur_kwh                                          (same for all rooms)
    weather.csv       outdoor_c, cloud_cover_pct                             (same for all rooms)

Data rules:
    - A blank cell is missing and is skipped; it never counts as zero.
    - Exact duplicate rows (same timestamp and key) keep the first row.
    - Values outside physical plausibility bounds (e.g. a room at 85 degC) are skipped.
    - Hourly files (tariff, logistics) use every hourly value stamped before t.
    - water_l_per_15min: difference of consecutive meter readings of one room; a drop of
      more than 1000 L is a meter exchange and any other negative step is meter jitter;
      both intervals are missing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

UNITS = {
    "building_load_kw": "kW",
    "hvac_electric_kw": "kW",
    "pv_kw": "kW",
    "battery_discharge_kw_signed": "kW (positive = discharging)",
    "battery_soc_kwh": "kWh",
    "grid_import_kw": "kW",
    "grid_export_kw": "kW",
    "temperature_c": "°C",
    "setpoint_c": "°C",
    "humidity_pct": "%",
    "co2_ppm": "ppm",
    "meter_total_l": "L (cumulative meter reading)",
    "water_l_per_15min": "L per 15 min",
    "price_eur_kwh": "EUR/kWh",
    "outdoor_c": "°C",
    "cloud_cover_pct": "%",
}
METRIC_UNITS = {  # equipment and logistics metrics, by metric name
    "electric_kw": "kW",
    "heat_output_kw": "kW",
    "supply_temp_c": "°C",
    "airflow_m3h": "m³/h",
    "filter_dp_pa": "Pa",
    "fan_kw": "kW",
    "supply_air_c": "°C",
    "fill_pct": "%",
    "stock_drinks": "units",
    "stock_snacks": "units",
}
BOUNDS = {  # plausible closed ranges; values outside are skipped
    "temperature_c": (5.0, 40.0),
    "setpoint_c": (15.0, 26.0),
    "humidity_pct": (5.0, 95.0),
    "co2_ppm": (350.0, 5000.0),
    "water_l_per_15min": (0.0, 300.0),
    "building_load_kw": (0.0, 200.0),
    "hvac_electric_kw": (0.0, 70.0),
    "pv_kw": (0.0, 60.0),
    "battery_discharge_kw_signed": (-40.0, 40.0),
    "battery_soc_kwh": (0.0, 100.0),
    "grid_import_kw": (0.0, 250.0),
    "grid_export_kw": (0.0, 100.0),
    "outdoor_c": (-30.0, 42.0),
    "cloud_cover_pct": (0.0, 100.0),
    "price_eur_kwh": (-0.5, 2.0),
    "HP1.heat_output_kw": (0.0, 120.0),
    "HP1.electric_kw": (0.0, 70.0),
    "HP1.supply_temp_c": (15.0, 75.0),
    "AHU1.airflow_m3h": (0.0, 5500.0),
    "AHU1.filter_dp_pa": (0.0, 600.0),
    "AHU1.fan_kw": (0.0, 10.0),
    "AHU1.supply_air_c": (5.0, 40.0),
    "fill_pct": (0.0, 100.0),
}
RESET_DROP_L = 1000.0


def read(path: Path, *time_cols: str) -> pd.DataFrame:
    """Read a CSV with blank = missing and the given columns as UTC times."""
    df = pd.read_csv(path, keep_default_na=False, na_values=[""])
    for c in time_cols:
        df[c] = pd.to_datetime(df[c], utc=True)
    return df


def bounded(s: pd.Series, name: str) -> pd.Series:
    """Return ``s`` with values outside the plausibility bounds of ``name`` set missing."""
    key = name if name in BOUNDS else name.split(".")[-1]
    if key not in BOUNDS:
        return s
    lo, hi = BOUNDS[key]
    return s.where((s >= lo) & (s <= hi))


def past_median(s: pd.Series, grid: pd.DatetimeIndex) -> np.ndarray:
    """Median of the values of ``s`` stamped strictly before each grid time; NaN if none."""
    s = s.dropna().sort_index()
    out = np.full(len(grid), np.nan)
    if s.empty:
        return out
    running = s.expanding().median().to_numpy()  # median of values up to and including i
    # last value strictly before t: index position searchsorted(t, 'left') - 1
    pos = s.index.searchsorted(grid, side="left") - 1
    ok = pos >= 0
    out[ok] = running[pos[ok]]
    return out


def main(data: Path, out_path: Path) -> None:
    """Compute every past-only median and write the JSON file."""
    manifest = json.loads((data / "manifest.json").read_text("utf-8"))
    grid = pd.date_range(manifest["start_utc"], manifest["end_utc"], freq="15min", inclusive="left")
    rooms = [
        r["id"]
        for r in json.loads((data / "building.json").read_text("utf-8"))["building"]["rooms"]
    ]

    shared: dict[str, np.ndarray] = {}  # parameters that are the same for every room
    units = dict(UNITS)

    energy = read(data / "energy.csv", "timestamp_utc").drop_duplicates("timestamp_utc")
    energy = energy.set_index("timestamp_utc")
    for c in (
        "building_load_kw",
        "hvac_electric_kw",
        "pv_kw",
        "battery_discharge_kw_signed",
        "battery_soc_kwh",
        "grid_import_kw",
        "grid_export_kw",
    ):
        shared[c] = past_median(bounded(energy[c].astype(float), c), grid)

    for file in ("equipment.csv", "logistics.csv"):
        df = read(data / file, "timestamp_utc")
        df = df.drop_duplicates(["timestamp_utc", "asset_id", "metric"])
        df = df.loc[df["metric"] != "status"]  # text state, no median
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        for (asset, metric), g in df.groupby(["asset_id", "metric"], sort=True):
            name = f"{asset}.{metric}"
            shared[name] = past_median(bounded(g.set_index("timestamp_utc")["value"], name), grid)
            units[name] = METRIC_UNITS.get(metric, "unknown")

    tariff = read(data / "tariff.csv", "timestamp_utc", "published_utc")
    tariff = tariff.drop_duplicates("timestamp_utc").set_index("timestamp_utc")
    shared["price_eur_kwh"] = past_median(bounded(tariff["price_eur_kwh"], "price_eur_kwh"), grid)

    weather = read(data / "weather.csv", "timestamp_utc").drop_duplicates("timestamp_utc")
    weather = weather.set_index("timestamp_utc")
    for c in ("outdoor_c", "cloud_cover_pct"):
        shared[c] = past_median(bounded(weather[c].astype(float), c), grid)

    tel = read(data / "room_telemetry.csv", "timestamp_utc").drop_duplicates(
        ["timestamp_utc", "room_id"]
    )
    water = read(data / "room_water.csv", "timestamp_utc").drop_duplicates(
        ["timestamp_utc", "room_id"]
    )
    per_room: dict[str, dict[str, np.ndarray]] = {}
    for room in rooms:
        t = tel.loc[tel["room_id"] == room].set_index("timestamp_utc").sort_index()
        w = (
            water.loc[water["room_id"] == room]
            .set_index("timestamp_utc")
            .sort_index()["meter_total_l"]
        )
        cols = {
            c: past_median(bounded(t[c].astype(float), c), grid)
            for c in ("temperature_c", "setpoint_c", "humidity_pct", "co2_ppm")
        }
        cols["meter_total_l"] = past_median(w.astype(float), grid)
        step = w.astype(float).diff()
        step = step.where(step >= 0)  # meter exchange (< -1000 L) and jitter (< 0) are missing
        cols["water_l_per_15min"] = past_median(bounded(step, "water_l_per_15min"), grid)
        per_room[room] = cols

    order = (
        list(UNITS)[:7]
        + sorted(k for k in shared if "." in k)
        + [
            "price_eur_kwh",
            "outdoor_c",
            "cloud_cover_pct",
            "temperature_c",
            "setpoint_c",
            "humidity_pct",
            "co2_ppm",
            "meter_total_l",
            "water_l_per_15min",
        ]
    )
    stamps = [f"{t:%Y-%m-%dT%H:%M}Z" for t in grid]

    def val(x: float) -> float | None:
        return None if np.isnan(x) else round(float(x), 4)

    result = {
        "meta": {
            "dataset": manifest.get("profile"),
            "start_utc": stamps[0],
            "end_utc_exclusive": manifest["end_utc"],
            "interval_minutes": 15,
            "definition": "median of all valid values stamped strictly before the timestamp; "
            "null when there is none",
            "same_for_all_rooms": [k for k in order if k in shared],
            "per_room": [
                "temperature_c",
                "setpoint_c",
                "humidity_pct",
                "co2_ppm",
                "meter_total_l",
                "water_l_per_15min",
            ],
            "units": {k: units[k] for k in order},
            "notes": [
                "Blank cells are missing and skipped; implausible values are skipped.",
                "equipment status (HP1.status) is text and has no median.",
                "meter_total_l is a cumulative counter, so its median follows the counter, not "
                "the use; water_l_per_15min is the consumption and is the one to compare for "
                "leaks.",
                "Hourly series (tariff, logistics) use every hourly value stamped before t.",
            ],
        },
        "rooms": {
            room: {
                stamps[i]: {
                    k: val((shared[k] if k in shared else per_room[room][k])[i]) for k in order
                }
                for i in range(len(grid))
            }
            for room in rooms
        },
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(
        f"wrote {out_path}: {len(rooms)} rooms x {len(grid)} timestamps x {len(order)} parameters"
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    data_dir = Path(sys.argv[1])
    main(data_dir, Path(sys.argv[2]) if len(sys.argv) > 2 else data_dir / "rolling_medians.json")
