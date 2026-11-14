"""Context snapshot for the supervisor LLM: what the building looks like at ``t``.

``Snapshots(ds, settings).at(t, assets)`` returns a small JSON-able dict:

- ``building``: room states, arrivals and departures in the next ``snapshot_ahead_h``,
  staff on shift, HP1, AHU1, water main, price, forecast, battery and PV;
- ``assets``: the latest readings of each asset asked for (rooms, HP1, AHU1, ...);
- ``moved``: only metrics whose change against 15 min, 1 h or 24 h ago is beyond its
  ``snapshot_moved`` threshold;
- ``refs``: the evidence behind every number, in the ``file.ext:ref`` form decisions use.
  The supervisor may cite only these.

Known-at (ADR-002): every value, and every baseline, comes from rows known at its time:
readings at or before it, tariffs and forecasts by their publication time, bookings by
``created_utc``, roster absences by ``status_known_utc``, work orders by ``created_utc``
(open until ``completed_utc``). A missing reading is reported as missing, never as zero.
Tables are indexed once; each call is a set of binary searches.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time, timedelta
from typing import Any

import numpy as np
import pandas as pd

from plato40.detect.grid import events_ref, local_time, stamp, state_ref
from plato40.load.derive import (
    equipment_numeric,
    hp_status,
    main_water_series,
    meter_deltas,
    room_water_wide,
    telemetry_wide,
    weather_series,
)
from plato40.load.reader import Dataset
from plato40.load.units import BOUNDS
from plato40.settings import Settings
from plato40.state import RoomStates

STATES = ("occupied", "dirty", "cleaning", "clean", "unknown")
BACK = {"15m": pd.Timedelta(minutes=15), "1h": pd.Timedelta(hours=1), "24h": pd.Timedelta(days=1)}
STALE = pd.Timedelta(hours=2)
Value = tuple[float, str]


def _round(x: float) -> float:
    return float(round(x, 2))


class _Series:
    """One time series without missing values, searched by time."""

    def __init__(self, s: pd.Series) -> None:
        s = s.dropna().sort_index()
        self.index = pd.DatetimeIndex(s.index)
        self.values = s.to_numpy()

    def last(self, t: pd.Timestamp, stale: pd.Timedelta = STALE) -> tuple[Any, pd.Timestamp] | None:
        """Return the latest value at or before ``t`` and its time, if newer than ``stale``."""
        i = int(self.index.searchsorted(t, side="right")) - 1
        if i < 0 or t - self.index[i] > stale:
            return None
        return self.values[i], self.index[i]

    def window(self, start: pd.Timestamp, end: pd.Timestamp) -> np.ndarray:
        """Return the values in ``(start, end]``."""
        a = int(self.index.searchsorted(start, side="right"))
        z = int(self.index.searchsorted(end, side="right"))
        return np.asarray(self.values[a:z])


class Snapshots:
    """Context snapshots of one dataset, known-at correct."""

    def __init__(self, ds: Dataset, settings: Settings) -> None:
        """Index every table the snapshot reads, once."""
        self.ds, self.settings, self.th = ds, settings, settings.detect
        self.tz = settings.local_tz
        self.rooms = ds.room_ids()
        self.states = RoomStates(ds)
        bound = BOUNDS[("room_telemetry.csv", "temperature_c")]
        temp = telemetry_wide(ds, "temperature_c")
        temp = temp.where((temp >= bound.low) & (temp <= bound.high))
        self.tel = {
            col: {r: _Series(f[r]) for r in f.columns}
            for col, f in (
                ("temperature_c", temp),
                ("setpoint_c", telemetry_wide(ds, "setpoint_c")),
                ("window_open", telemetry_wide(ds, "window_open")),
            )
        }
        drop = self.th.meter_reset_drop_l
        room = meter_deltas(room_water_wide(ds), drop).deltas
        self.room_water = {str(r): room[r].sort_index() for r in room.columns}
        main = meter_deltas(main_water_series(ds).to_frame("WM_MAIN"), drop).deltas["WM_MAIN"]
        self.main_water = main.sort_index()
        self.main_flow = _Series(main)
        eq = equipment_numeric(ds)
        self.eq = {str(c): _Series(eq[c]) for c in eq.columns}
        self.hp_status = _Series(hp_status(ds))
        self.cop = _Series(self._cop_ratio(eq))
        self.outdoor = _Series(weather_series(ds))
        energy = ds.table("energy.csv").set_index("timestamp_utc").sort_index()
        self.energy = {c: _Series(energy[c]) for c in energy.columns}
        self.tariff = ds.table("tariff.csv").sort_values("timestamp_utc", kind="stable")
        fc = ds.table("weather_forecast.csv")
        self.forecast = fc.sort_values(["issued_utc", "target_utc"], kind="stable")
        self.issues = pd.DatetimeIndex(sorted(fc["issued_utc"].unique()))
        self.bookings = ds.table("bookings.csv")
        self.roster = ds.table("staff_roster.csv")
        self.orders = ds.table("work_orders.csv")
        self.moved = dict(self.th.snapshot_moved)

    def _cop_ratio(self, eq: pd.DataFrame) -> pd.Series:
        """Return the HP1 24 h COP over its reference, per hour (as ``detect.heating``)."""
        th, step = self.th, self.settings.interval_minutes / 60
        heat, elec = eq["HP1/heat_output_kw"], eq["HP1/electric_kw"]
        both = heat.notna() & elec.notna()

        def hourly(s: pd.Series, how: str) -> pd.Series:
            r = s.resample("1h", label="right", closed="right")
            out: pd.Series = r.sum(min_count=1) if how == "sum" else r.mean()
            return out

        w = th.cop_window_h
        h = hourly(heat.where(both) * step, "sum").rolling(w, 1).sum()
        e = hourly(elec.where(both) * step, "sum").rolling(w, 1).sum()
        n = hourly(both.astype(float), "sum").rolling(w, 1).sum()
        outdoor = hourly(weather_series(self.ds), "mean").rolling(w, min_periods=th.outdoor_min_h)
        ref = th.cop_ref_intercept + th.cop_ref_slope * outdoor.mean()
        ok = (n >= th.cop_min_coverage * w * 60 // self.settings.interval_minutes) & (e > 0)
        ratio: pd.Series = ((h / e) / ref).where(ok)
        return ratio

    # ---------------------------------------------------------------- helpers

    def _local(self, day: datetime, hh: int) -> pd.Timestamp:
        stamp_local = pd.Timestamp(datetime.combine(day.date(), time(hh))).tz_localize(self.tz)
        return stamp_local.tz_convert("UTC")

    def _night(self, t: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
        """Return the latest local night window that ended at or before ``t``."""
        night = self.settings.night
        today = t.tz_convert(self.tz)
        end = self._local(today, night.end_hour)
        if end > t:
            end = self._local(today - timedelta(days=1), night.end_hour)
        start = self._local(end.tz_convert(self.tz), night.start_hour)
        return start, end

    def _night_total(self, deltas: pd.Series, t: pd.Timestamp) -> tuple[float | None, str]:
        start, end = self._night(t)
        part = deltas.loc[(deltas.index > start) & (deltas.index <= end)]
        expected = int((end - start) / pd.Timedelta(minutes=self.settings.interval_minutes))
        total = None if len(part) < expected or part.isna().any() else _round(float(part.sum()))
        return total, f"{stamp(start)}..{stamp(end)}"

    def _open_orders(self, t: pd.Timestamp, asset: str | None = None) -> pd.DataFrame:
        wo = self.orders
        done = wo["completed_utc"].notna() & (wo["completed_utc"] <= t)
        mine = wo.loc[(wo["created_utc"] <= t) & ~done]
        return mine if asset is None else mine.loc[mine["asset_id"] == asset]

    def _scalars(self, t: pd.Timestamp, assets: Sequence[str]) -> dict[str, Value]:
        """Return the metrics ``moved`` compares, each with the ref of its row."""
        out: dict[str, Value] = {}

        def put(name: str, hit: tuple[Any, pd.Timestamp] | None, ref: str) -> None:
            if hit is not None:
                out[name] = (_round(float(hit[0])), f"{ref}:{stamp(hit[1])}")

        put("HP1 cop_ratio", self.cop.last(t), "equipment.csv:HP1 24h cop window to")
        for m in ("filter_dp_pa", "airflow_m3h"):
            if f"AHU1/{m}" in self.eq:
                put(f"AHU1 {m}", self.eq[f"AHU1/{m}"].last(t), f"equipment.csv:AHU1 {m}")
        put("WM_MAIN main_flow_l", self.main_flow.last(t), "water_main.csv:WM_MAIN")
        for name, col in (
            ("BAT1 soc_kwh", "battery_soc_kwh"),
            ("PV1 pv_kw", "pv_kw"),
            ("BUILDING load_kw", "building_load_kw"),
        ):
            if col in self.energy:
                put(name, self.energy[col].last(t), f"energy.csv:{col}")
        put("BUILDING outdoor_c", self.outdoor.last(t), "weather.csv:outdoor_c")
        price = self._price(t)
        if price is not None:
            out["BUILDING price_eur_kwh"] = price
        counts = _state_counts(self.states, self.rooms, t)
        for s in ("occupied", "dirty"):
            out[f"BUILDING {s}"] = (
                float(counts.get(s, 0)),
                events_ref(self.ds, t),
            )
        for a in assets:
            if a not in self.tel["temperature_c"]:
                continue
            for col in ("temperature_c", "setpoint_c"):
                put(f"{a} {col}", self.tel[col][a].last(t), f"room_telemetry.csv:{a} {col}")
            if a in self.room_water:
                total, span = self._night_total(self.room_water[a], t)
                if total is not None:
                    out[f"{a} night_water_l"] = (total, f"room_water.csv:WM_{a} {span}")
        return out

    def _price(self, t: pd.Timestamp) -> Value | None:
        tf = self.tariff
        hour = t.floor("h")
        row = tf.loc[(tf["timestamp_utc"] == hour) & (tf["published_utc"] <= t)]
        if row.empty:
            return None
        return _round(float(row["price_eur_kwh"].iloc[0])), f"tariff.csv:{stamp(hour)}"

    # ---------------------------------------------------------------- snapshot

    def at(self, t: pd.Timestamp, assets: Sequence[str] = ()) -> dict[str, Any]:
        """Return the snapshot at ``t`` for the ``assets`` asked for."""
        refs: list[str] = []
        building = self._building(t, refs)
        detail = {a: self._asset(a, t, refs) for a in assets}
        now = self._scalars(t, assets)
        then = {k: self._scalars(t - d, assets) for k, d in BACK.items()}
        moved = []
        for name, (value, ref) in now.items():
            limit = self.moved.get(name.split(" ", 1)[1])
            if limit is None:
                continue
            row: dict[str, Any] = {"metric": name, "now": value}
            beyond = False
            for k in BACK:
                old = then[k].get(name)
                row[k] = None if old is None else _round(value - old[0])
                beyond |= old is not None and abs(value - old[0]) > limit
            if beyond:
                moved.append(row)
                refs.append(ref)
                refs += [then[k][name][1] for k in BACK if name in then[k]]
        return {
            "t": stamp(t),
            "local": local_time(t, t, self.tz),
            "building": building,
            "assets": detail,
            "moved": moved,
            "refs": sorted(set(refs)),
        }

    def _building(self, t: pd.Timestamp, refs: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        counts = _state_counts(self.states, self.rooms, t)
        out["rooms"] = {s: counts.get(s, 0) for s in STATES if counts.get(s, 0)}
        refs.append(events_ref(self.ds, t))
        temps = [float(hit[0]) for r in self.rooms if (hit := self.tel["temperature_c"][r].last(t))]
        if temps:
            # One room is judged against the whole building: a heat wave or a cold night shows
            # in every room, a local fault in one.
            out["room_temps_c"] = {
                "rooms": len(temps),
                "median": _round(float(np.median(temps))),
                "min": _round(min(temps)),
                "max": _round(max(temps)),
                "below_20": sum(x < 20 for x in temps),
                "above_26": sum(x > 26 for x in temps),
            }
            refs.append(f"room_telemetry.csv:all rooms temperature_c at or before {stamp(t)}")
        ahead = t + pd.Timedelta(hours=self.th.snapshot_ahead_h)
        b = self.bookings.loc[self.bookings["created_utc"] <= t]
        for key, col in (("arrivals", "arrival_utc"), ("departures", "departure_utc")):
            due = b.loc[(b[col] > t) & (b[col] <= ahead)].sort_values([col, "room_id"])
            out[f"{key}_next_{self.th.snapshot_ahead_h:.0f}h"] = sorted(set(due["room_id"]))
            refs += [f"bookings.csv:{k}" for k in due["booking_id"]]
        r = self.roster
        believed = (r["status"] == "scheduled") | (r["status_known_utc"] > t)
        on = r.loc[believed & (r["start_utc"] <= t) & (r["end_utc"] > t)]
        out["on_shift"] = {
            role: sorted(map(str, g["resource_id"])) for role, g in on.groupby("role", sort=True)
        }
        refs += [f"staff_roster.csv:{s}" for s in on["shift_id"]]
        hp: dict[str, Any] = {}
        cop = self.cop.last(t)
        if cop is not None:
            hp["cop_ratio"] = _round(float(cop[0]))
            refs.append(f"equipment.csv:HP1 24h cop window to:{stamp(cop[1])}")
        status = self.hp_status.last(t)
        if status is not None:
            hp["status"] = str(status[0])
            refs.append(f"equipment.csv:HP1 status:{stamp(status[1])}")
        out["HP1"] = hp
        ahu: dict[str, Any] = {}
        for m in ("filter_dp_pa", "airflow_m3h", "fan_kw"):
            hit = self.eq[f"AHU1/{m}"].last(t) if f"AHU1/{m}" in self.eq else None
            if hit is not None:
                ahu[m] = _round(float(hit[0]))
                refs.append(f"equipment.csv:AHU1 {m}:{stamp(hit[1])}")
        out["AHU1"] = ahu or "no reading in the last 2 h"
        water: dict[str, Any] = {}
        flow = self.main_flow.last(t)
        if flow is not None:
            water["flow_l_15min"] = _round(float(flow[0]))
            refs.append(f"water_main.csv:WM_MAIN:{stamp(flow[1])}")
        total, span = self._night_total(self.main_water, t)
        water["last_night_l"] = total
        if total is not None:
            refs.append(f"water_main.csv:WM_MAIN {span}")
        out["WM_MAIN"] = water
        out["energy"] = self._energy(t, refs)
        out["forecast"] = self._forecast(t, refs)
        out["open_work_orders"] = len(self._open_orders(t))
        return out

    def _energy(self, t: pd.Timestamp, refs: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, col in (
            ("soc_kwh", "battery_soc_kwh"),
            ("pv_kw", "pv_kw"),
            ("load_kw", "building_load_kw"),
            ("grid_import_kw", "grid_import_kw"),
        ):
            hit = self.energy[col].last(t) if col in self.energy else None
            if hit is not None:
                out[key] = _round(float(hit[0]))
                refs.append(f"energy.csv:{col}:{stamp(hit[1])}")
        price = self._price(t)
        if price is not None:
            out["price_now"] = price[0]
            refs.append(price[1])
            tf = self.tariff
            day = t.tz_convert(self.tz).date()
            today = tf.loc[
                (tf["timestamp_utc"].dt.tz_convert(self.tz).dt.date == day)
                & (tf["published_utc"] <= t)
            ]
            out["price_today_mean"] = _round(float(today["price_eur_kwh"].mean()))
            refs.append(f"tariff.csv:{day} published by {stamp(t)}")
        return out

    def _forecast(self, t: pd.Timestamp, refs: list[str]) -> dict[str, Any]:
        i = int(self.issues.searchsorted(t, side="right")) - 1
        if i < 0:
            return {}
        fc, latest = self.forecast, self.issues[i]
        nxt = fc.loc[
            (fc["issued_utc"] == latest)
            & (fc["target_utc"] > t)
            & (fc["target_utc"] <= t + pd.Timedelta(hours=24))
        ]
        if nxt.empty:
            return {"issued": stamp(latest)}
        temps = nxt["outdoor_c_forecast"].to_numpy(dtype=float)
        k = int(np.argmin(temps))
        out: dict[str, Any] = {
            "issued": stamp(latest),
            "next24h_mean_c": _round(float(temps.mean())),
            "next24h_min_c": _round(float(temps[k])),
            "coldest_at": local_time(pd.Timestamp(nxt["target_utc"].iloc[k]), t, self.tz),
        }
        refs.append(f"weather_forecast.csv:issued {stamp(latest)}")
        if i > 0:
            prev = fc.loc[fc["issued_utc"] == self.issues[i - 1]].set_index("target_utc")
            both = nxt.set_index("target_utc")["outdoor_c_forecast"].align(
                prev["outdoor_c_forecast"], join="inner"
            )
            if len(both[0]):
                out["vs_previous_issue_k"] = _round(float((both[0] - both[1]).mean()))
                refs.append(f"weather_forecast.csv:issued {stamp(self.issues[i - 1])}")
        return out

    def _asset(self, a: str, t: pd.Timestamp, refs: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if a in self.tel["temperature_c"]:
            for col, key in (
                ("temperature_c", "temperature_c"),
                ("setpoint_c", "setpoint_c"),
                ("window_open", "window_open"),
            ):
                hit = self.tel[col][a].last(t)
                out[key] = None if hit is None else _round(float(hit[0]))
                if hit is not None:
                    refs.append(f"room_telemetry.csv:{a} {col}:{stamp(hit[1])}")
            out["state"] = self.states.at(a, t)
            since = self.states.since(a, t)
            if since is not None:
                out["state_since"] = local_time(since, t, self.tz)
                refs.append(state_ref(self.ds, a, t))
            if a in self.room_water:
                total, span = self._night_total(self.room_water[a], t)
                out["night_water_l"] = total
                if total is not None:
                    refs.append(f"room_water.csv:WM_{a} {span}")
        elif a == "HP1":
            for m in ("heat_output_kw", "electric_kw", "supply_temp_c"):
                hit = self.eq[f"HP1/{m}"].last(t) if f"HP1/{m}" in self.eq else None
                out[m] = None if hit is None else _round(float(hit[0]))
                if hit is not None:
                    refs.append(f"equipment.csv:HP1 {m}:{stamp(hit[1])}")
        elif a == "AHU1":
            for m in ("filter_dp_pa", "airflow_m3h", "fan_kw", "supply_air_c"):
                hit = self.eq[f"AHU1/{m}"].last(t) if f"AHU1/{m}" in self.eq else None
                out[m] = None if hit is None else _round(float(hit[0]))
                if hit is not None:
                    refs.append(f"equipment.csv:AHU1 {m}:{stamp(hit[1])}")
        orders = self._open_orders(t, a)
        out["open_work_orders"] = [
            {
                "id": str(w["work_order_id"]),
                "type": str(w["type"]),
                "scheduled": local_time(pd.Timestamp(w["scheduled_utc"]), t, self.tz)
                if pd.notna(w["scheduled_utc"])
                else None,
                "assigned_to": str(w["assigned_to"]),
            }
            for _, w in orders.iterrows()
        ]
        refs += [f"work_orders.csv:{w}" for w in orders["work_order_id"]]
        return out


def _state_counts(states: RoomStates, rooms: Sequence[str], t: pd.Timestamp) -> dict[str, int]:
    """Return the number of rooms per state at ``t``."""
    counts: dict[str, int] = {}
    for r in rooms:
        s = states.at(r, t)
        counts[s] = counts.get(s, 0) + 1
    return counts
