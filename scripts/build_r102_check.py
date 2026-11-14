"""Build the one-room, one-day check file: each reading next to its past-only median.

usage: python scripts/build_r102_check.py <data_dir> <rolling_medians.json> <out.json>
       [--room R102] [--day 2026-01-05]

One row per parameter and 15-minute timestamp: the reading at that time (hourly series
hold their last reading, marked ``observed = 0``), the past median from
``rolling_medians.json`` and whether the reading counts towards later medians
(``counted``). The output is ``{rows, meta}`` with this script's own text in ``meta``,
as the R102 Median Check dashboard reads it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from rolling_medians import BOUNDS

ROOM_COLUMNS = ("temperature_c", "setpoint_c", "humidity_pct", "co2_ppm")
ENERGY_COLUMNS = (
    "building_load_kw",
    "hvac_electric_kw",
    "pv_kw",
    "battery_discharge_kw_signed",
    "battery_soc_kwh",
    "grid_import_kw",
    "grid_export_kw",
)
Source = tuple[pd.Series, str, str]


def read(data: Path, name: str, *time_cols: str) -> pd.DataFrame:
    """Read a CSV with blank = missing and the given columns as UTC times."""
    df = pd.read_csv(data / name, keep_default_na=False, na_values=[""])
    for c in time_cols:
        df[c] = pd.to_datetime(df[c], utc=True)
    return df


def series(data: Path, room: str) -> dict[str, Source]:
    """Return every parameter's source readings, with its group and source file."""
    out: dict[str, Source] = {}
    en = read(data, "energy.csv", "timestamp_utc").drop_duplicates("timestamp_utc")
    en = en.set_index("timestamp_utc")
    for c in ENERGY_COLUMNS:
        out[c] = (en[c].astype(float), "Energy", "energy.csv")
    for f, grp in (
        ("equipment.csv", "Heat pump and ventilation"),
        ("logistics.csv", "Waste and vending"),
    ):
        d = read(data, f, "timestamp_utc")
        d = d.drop_duplicates(["timestamp_utc", "asset_id", "metric"])
        d = d.loc[d["metric"] != "status"].copy()
        d["value"] = pd.to_numeric(d["value"], errors="coerce")
        for (a, m), g in d.groupby(["asset_id", "metric"]):
            out[f"{a}.{m}"] = (g.set_index("timestamp_utc")["value"], grp, f)
    tar = read(data, "tariff.csv", "timestamp_utc", "published_utc")
    tar = tar.drop_duplicates("timestamp_utc").set_index("timestamp_utc")
    out["price_eur_kwh"] = (tar["price_eur_kwh"], "Price and weather", "tariff.csv")
    we = read(data, "weather.csv", "timestamp_utc").drop_duplicates("timestamp_utc")
    we = we.set_index("timestamp_utc")
    for c in ("outdoor_c", "cloud_cover_pct"):
        out[c] = (we[c].astype(float), "Price and weather", "weather.csv")
    tel = read(data, "room_telemetry.csv", "timestamp_utc")
    tel = tel.drop_duplicates(["timestamp_utc", "room_id"])
    tel = tel.loc[tel["room_id"] == room].set_index("timestamp_utc").sort_index()
    for c in ROOM_COLUMNS:
        out[c] = (tel[c].astype(float), f"Room {room}", "room_telemetry.csv")
    w = read(data, "room_water.csv", "timestamp_utc")
    w = w.drop_duplicates(["timestamp_utc", "room_id"])
    w = w.loc[w["room_id"] == room].set_index("timestamp_utc").sort_index()
    meter = w["meter_total_l"].astype(float)
    out["meter_total_l"] = (meter, f"Room {room}", "room_water.csv")
    step = meter.diff()
    out["water_l_per_15min"] = (step.where(step >= 0), f"Room {room}", "room_water.csv")
    return out


def main() -> None:
    """Write the check file for one room and one UTC day."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("medians", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--room", default="R102")
    ap.add_argument("--day", default="2026-01-05")
    args = ap.parse_args()
    med = json.loads(args.medians.read_text("utf-8"))
    units = med["meta"]["units"]
    day = pd.date_range(f"{args.day}T00:00Z", periods=96, freq="15min")
    rows = []
    for p, (s, grp, src) in series(args.data_dir, args.room).items():
        key = p if p in BOUNDS else p.split(".")[-1]
        for t in day:
            ts = f"{t:%Y-%m-%dT%H:%M}Z"
            observed = t in s.index  # a source reading is stamped exactly at t
            if observed:
                v = s.loc[t]
            else:  # hourly series: the latest reading before t
                prior = s.loc[s.index < t]
                v = prior.iloc[-1] if len(prior) else np.nan
            value = None if pd.isna(v) else float(v)
            in_bounds = key not in BOUNDS or (
                value is not None and BOUNDS[key][0] <= value <= BOUNDS[key][1]
            )
            rows.append(
                {
                    "timestamp_utc": ts,
                    "parameter": p,
                    "unit": units[p],
                    "group": grp,
                    "source": src,
                    "actual": None if value is None else round(value, 4),
                    "past_median": med["rooms"][args.room][ts][p],
                    "observed": int(observed),
                    "counted": int(observed and value is not None and in_bounds),
                }
            )
    me = Path(__file__)
    meta = [
        {"label": me.name, "value": me.read_text("utf-8"), "type": "code", "lang": "python"},
        {
            "label": "Inputs",
            "value": f"{args.data_dir.name} CSVs and {args.medians.name}, "
            f"room {args.room}, {args.day} UTC",
        },
    ]
    args.out.write_text(json.dumps({"rows": rows, "meta": meta}, ensure_ascii=False), "utf-8")
    print(f"{len(rows)} rows for {args.room} on {args.day}")


if __name__ == "__main__":
    main()
