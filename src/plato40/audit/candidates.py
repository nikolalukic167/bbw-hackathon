"""Screening rules that list *candidate* scenario episodes for the audit.

These are exploratory screens, deliberately simple and transparent. They are not
detectors: they look at the whole dataset at once (no time-of-availability
discipline) and they exist to find episodes worth plotting, to count instances per
class, and to collect benign look-alikes. Units are stated per function.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from plato40.load import derive
from plato40.load.reader import Dataset
from plato40.settings import Settings


@dataclass(frozen=True)
class Candidate:
    """One candidate episode.

    Attributes:
        scenario: Scenario class label (S01..S05, LOG).
        kind: Sub-type within the class, e.g. ``persistent_room_flow``.
        assets: Affected IDs.
        start: First UTC time of the episode.
        end: Last UTC time of the episode.
        signal: Short description of the evidence with units.

    """

    scenario: str
    kind: str
    assets: tuple[str, ...]
    start: pd.Timestamp
    end: pd.Timestamp
    signal: str


def _local_day(index: pd.Index, tz: str) -> pd.Index:
    return pd.Index(derive.dt_index(index).tz_convert(tz).date)


def _night_mask(index: pd.Index, settings: Settings) -> np.ndarray:
    hour = derive.dt_index(index).tz_convert(settings.local_tz).hour
    return np.asarray((hour >= settings.night.start_hour) & (hour < settings.night.end_hour))


def room_deltas(ds: Dataset, settings: Settings) -> pd.DataFrame:
    """Return room water volumes, litres per 15 min, time x room; resets set missing."""
    return derive.meter_deltas(
        derive.room_water_wide(ds), settings.thresholds.meter_reset_drop_l
    ).deltas


def night_table(ds: Dataset, settings: Settings) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return per local night and room: (minimum 15-min volume L, total volume L).

    A night is labelled by the local date on which the night window starts.
    """
    d = room_deltas(ds, settings)
    night = d.loc[_night_mask(d.index, settings)]
    day = _local_day(night.index, settings.local_tz)
    return night.groupby(day).min(), night.groupby(day).sum(min_count=1)


def gap_series(ds: Dataset, settings: Settings) -> pd.Series:
    """Return main meter volume minus the sum of room volumes, litres per 15 min.

    Missing if the main delta or any room delta is missing in the interval.
    """
    rooms = room_deltas(ds, settings)
    main = derive.meter_deltas(
        derive.main_water_series(ds).to_frame("WM_MAIN"), settings.thresholds.meter_reset_drop_l
    ).deltas["WM_MAIN"]
    return main - rooms.sum(axis=1, min_count=rooms.shape[1])


def night_gap(ds: Dataset, settings: Settings) -> pd.Series:
    """Return the median night gap per local night, litres per hour."""
    gap = gap_series(ds, settings)
    night = gap.loc[_night_mask(gap.index, settings)]
    return night.groupby(_local_day(night.index, settings.local_tz)).median() * 4.0


def _runs_of_days(days: list[object]) -> list[tuple[object, object, int]]:
    """Group sorted dates into runs of consecutive days: (first, last, length)."""
    runs: list[tuple[object, object, int]] = []
    for d in days:
        ts = pd.Timestamp(str(d))
        if runs and pd.Timestamp(str(runs[-1][1])) + pd.Timedelta(days=1) == ts:
            runs[-1] = (runs[-1][0], d, runs[-1][2] + 1)
        else:
            runs.append((d, d, 1))
    return runs


def _local_midnight(day: object, settings: Settings) -> pd.Timestamp:
    return pd.Timestamp(str(day)).tz_localize(settings.local_tz).tz_convert("UTC")


def s02_candidates(ds: Dataset, settings: Settings) -> list[Candidate]:
    """S02 screens: persistent room night flow, loud room night volume, night gap.

    - persistent: every night-window interval >= ``night_floor_l_per_15min`` for at
      least ``night_floor_min_nights`` consecutive nights.
    - loud: night-window total >= ``burst_night_volume_l`` in one room.
    - gap: median night gap (main minus rooms) >= ``gap_night_l_per_h``.
    """
    th = settings.thresholds
    floor, total = night_table(ds, settings)
    out: list[Candidate] = []
    for room in floor.columns:
        floored = floor.index[(floor[room] >= th.night_floor_l_per_15min).to_numpy()]
        for first, last, n in _runs_of_days(list(floored)):
            if n < th.night_floor_min_nights:
                continue
            rate = floor.loc[first:last, room].median() * 4.0
            out.append(
                Candidate(
                    "S02",
                    "persistent_room_night_flow",
                    (room,),
                    _local_midnight(first, settings),
                    _local_midnight(last, settings) + pd.Timedelta(days=1),
                    f"{n} nights with flow in every night interval, median floor {rate:.1f} L/h",
                )
            )
        volumes = total[room].to_numpy(dtype=float)
        for i in np.flatnonzero(volumes >= th.burst_night_volume_l):
            day = total.index[int(i)]
            out.append(
                Candidate(
                    "S02",
                    "loud_room_night_volume",
                    (room,),
                    _local_midnight(day, settings),
                    _local_midnight(day, settings) + pd.Timedelta(days=1),
                    f"{volumes[i]:.0f} L in the night window",
                )
            )
    gap = night_gap(ds, settings)
    high = gap.index[(gap >= th.gap_night_l_per_h).to_numpy()]
    for first, last, n in _runs_of_days(list(high)):
        out.append(
            Candidate(
                "S02",
                "night_gap_main_minus_rooms",
                ("WM_MAIN",),
                _local_midnight(first, settings),
                _local_midnight(last, settings) + pd.Timedelta(days=1),
                f"{n} nights, median night gap {gap.loc[first:last].median():.1f} L/h",
            )
        )
    return sorted(out, key=lambda c: (c.start, c.kind, c.assets))


def comfort_deficit(ds: Dataset, settings: Settings) -> pd.DataFrame:
    """Return the occupied comfort deficits with the audit thresholds (see ``derive``)."""
    th = settings.thresholds
    return derive.comfort_deficit(ds, th.comfort_low_c, th.comfort_deficit_k)


def occupied_cold_hours(ds: Dataset, settings: Settings) -> pd.Series:
    """Return per local day the occupied comfort-deficit room-hours (h)."""
    per_interval = comfort_deficit(ds, settings).sum(axis=1) * 0.25
    return per_interval.groupby(
        _local_day(pd.DatetimeIndex(per_interval.index), settings.local_tz)
    ).sum()


def theil_sen(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Return (slope, intercept) of the Theil-Sen line: robust to ~29 % outliers."""
    i, j = np.triu_indices(len(x), 1)
    dx = x[j] - x[i]
    keep = dx != 0
    slope = float(np.median((y[j] - y[i])[keep] / dx[keep]))
    return slope, float(np.median(y - slope * x))


def cop_expected(cop: pd.DataFrame) -> pd.Series:
    """Return the expected daily COP (-) from a Theil-Sen fit of COP on outdoor degC.

    The fit uses all days of the dataset. It is robust to a minority of degraded days,
    but not when degraded days dominate a short window (see the audit text).
    """
    slope, intercept = theil_sen(cop["outdoor_c"].to_numpy(), cop["cop"].to_numpy())
    return intercept + slope * cop["outdoor_c"]


def s03_candidates(ds: Dataset, settings: Settings) -> list[Candidate]:
    """S03 screens, reported separately because they can disagree.

    - ``cop_below_outdoor_fit``: daily COP below (1 - ``cop_drop_fraction``) times the
      Theil-Sen expectation for that day's mean outdoor temperature.
    - ``occupied_comfort_deficit``: at least ``occupied_cold_room_hours`` occupied
      comfort-deficit room-hours (see ``comfort_deficit``) in one local day.
    """
    th = settings.thresholds
    cop = derive.daily_cop(ds, settings.local_tz)
    expected = cop_expected(cop)
    ratio = cop["cop"] / expected
    low = [d.date() for d in cop.index[(ratio < 1.0 - th.cop_drop_fraction).to_numpy()]]
    cold = occupied_cold_hours(ds, settings)
    cold_days = list(cold.index[(cold >= th.occupied_cold_room_hours).to_numpy()])
    out: list[Candidate] = []
    for first, last, n in _runs_of_days(low):
        window = cop.loc[str(first) : str(last)]
        out.append(
            Candidate(
                "S03",
                "cop_below_outdoor_fit",
                ("HP1",),
                _local_midnight(first, settings),
                _local_midnight(last, settings) + pd.Timedelta(days=1),
                f"{n} d, COP {window['cop'].min():.2f}..{window['cop'].max():.2f}, "
                f"ratio to fit {ratio.loc[window.index].min():.2f}, outdoor "
                f"{window['outdoor_c'].min():.1f}..{window['outdoor_c'].max():.1f} degC",
            )
        )
    for first, last, n in _runs_of_days(cold_days):
        out.append(
            Candidate(
                "S03",
                "occupied_comfort_deficit",
                ("BUILDING",),
                _local_midnight(first, settings),
                _local_midnight(last, settings) + pd.Timedelta(days=1),
                f"{n} d, {cold.loc[first:last].sum():.0f} occupied room-h below "
                f"{th.comfort_low_c:.0f} degC and >= {th.comfort_deficit_k:.1f} K under setpoint",
            )
        )
    return out


def s04_candidates(ds: Dataset, settings: Settings) -> list[Candidate]:
    """S04 screens on AHU1, on local days.

    - ``filter_dp_high``: days whose maximum filter dp exceeds ``filter_dp_alarm_pa``;
      runs may be bridged by days without any dp reading.
    - ``filter_dp_data_gap``: dp missing for 24 h or more, with the fan power change
      across the gap (a second signal that keeps reporting while dp is silent).
    """
    tz = settings.local_tz
    eq = derive.equipment_numeric(ds)
    dp = eq["AHU1/filter_dp_pa"]
    fan = eq["AHU1/fan_kw"]
    day = _local_day(pd.DatetimeIndex(dp.index), tz)
    dp_max = dp.groupby(day).max()
    fan_mean = fan.groupby(day).mean()
    state = (dp_max > settings.thresholds.filter_dp_alarm_pa).astype(float).where(dp_max.notna())
    bridged = state.ffill().fillna(0.0) == 1.0
    bridged &= state.bfill().fillna(0.0) == 1.0
    out: list[Candidate] = []
    for first, last, n in _runs_of_days(list(bridged.index[bridged.to_numpy()])):
        span = dp_max.loc[first:last]
        out.append(
            Candidate(
                "S04",
                "filter_dp_high",
                ("AHU1",),
                _local_midnight(first, settings),
                _local_midnight(last, settings) + pd.Timedelta(days=1),
                f"{n} d, daily max dp up to {span.max():.0f} Pa "
                f"({int(span.isna().sum())} d without dp), fan "
                f"{fan_mean.loc[first:last].max():.2f} kW daily mean",
            )
        )
    missing = dp.isna()
    run_id = (~missing).cumsum()
    for rid in sorted(set(run_id[missing])):
        members = dp.index[((run_id == rid) & missing).to_numpy()]
        hours = len(members) * 0.25
        if hours < 24:
            continue
        before = fan.loc[: members[0]].dropna()
        after = fan.loc[members[-1] :].dropna()
        f0 = before.iloc[-96:].mean() if len(before) else float("nan")
        f1 = after.iloc[:96].mean() if len(after) else float("nan")
        dp0 = dp.loc[: members[0]].dropna()
        dp1 = dp.loc[members[-1] :].dropna()
        out.append(
            Candidate(
                "S04",
                "filter_dp_data_gap",
                ("AHU1",),
                members[0],
                members[-1],
                f"dp missing {hours:.0f} h; dp {dp0.iloc[-1] if len(dp0) else float('nan'):.0f}"
                f"->{dp1.iloc[0] if len(dp1) else float('nan'):.0f} Pa across the gap, "
                f"fan 24 h mean {f0:.2f}->{f1:.2f} kW",
            )
        )
    return sorted(out, key=lambda c: (c.start, c.kind))


def daily_operations(ds: Dataset, settings: Settings) -> pd.DataFrame:
    """Return per local day: occupied rooms at 22:00 local, check-outs, cleaners present.

    Cleaners present counts rostered cleaner shifts with status ``scheduled``.
    """
    tz = settings.local_tz
    start = pd.Timestamp(ds.manifest["start_utc"])
    end = pd.Timestamp(ds.manifest["end_utc"])
    days = pd.date_range(start.tz_convert(tz).normalize(), end.tz_convert(tz), freq="D")
    probe = (days + pd.Timedelta(hours=22)).tz_convert("UTC")
    occ = derive.occupancy_matrix(ds, pd.DatetimeIndex(probe))
    ev = ds.events
    co = ev.loc[ev["type"].isin(derive.STAY_END_TYPES), "timestamp_utc"]
    co_day = pd.Series(1, index=co.to_numpy()).groupby(_local_day(pd.DatetimeIndex(co), tz)).sum()
    r = ds.table("staff_roster.csv")
    cl = r.loc[r["role"] == "cleaner"]
    cl_day = cl.assign(day=_local_day(pd.DatetimeIndex(cl["start_utc"]), tz))
    present = cl_day.loc[cl_day["status"] == "scheduled"].groupby("day").size()
    absent = cl_day.loc[cl_day["status"] != "scheduled"].groupby("day").size()
    out = pd.DataFrame(index=pd.Index(days.date, name="day"))
    out["occupied_22h"] = occ.sum(axis=1).to_numpy()
    out["checkouts"] = co_day.reindex(out.index).fillna(0).astype(int)
    out["cleaners_present"] = present.reindex(out.index).fillna(0).astype(int)
    out["cleaners_absent"] = absent.reindex(out.index).fillna(0).astype(int)
    out["weekday"] = [pd.Timestamp(str(d)).day_name()[:3] for d in out.index]
    return out


def s05_candidates(ds: Dataset, settings: Settings) -> list[Candidate]:
    """S05 screen: Fri-Sun with high occupancy, or a weekend day with a cleaner absent.

    Weekends are grouped Fri..Sun by the local date of the Friday.
    """
    ops = daily_operations(ds, settings)
    n_rooms = len(ds.room_ids())
    out: list[Candidate] = []
    weekend = ops.loc[ops["weekday"].isin(["Fri", "Sat", "Sun"])]
    groups: dict[object, list[object]] = {}
    for day in weekend.index:
        ts = pd.Timestamp(str(day))
        friday = (ts - pd.Timedelta(days=(ts.weekday() - 4) % 7)).date()
        groups.setdefault(friday, []).append(day)
    for weekend_of, days in sorted(groups.items(), key=lambda kv: str(kv[0])):
        w = ops.loc[days]
        peak = int(w["occupied_22h"].max())
        absent = int(w["cleaners_absent"].sum())
        if peak < settings.thresholds.occupancy_surge_fraction * n_rooms and absent == 0:
            continue
        kind = (
            "surge_and_absence"
            if absent and peak >= 0.9 * n_rooms
            else ("surge" if peak >= 0.9 * n_rooms else "absence_on_weekend")
        )
        out.append(
            Candidate(
                "S05",
                kind,
                ("BUILDING",),
                _local_midnight(days[0], settings),
                _local_midnight(days[-1], settings) + pd.Timedelta(days=1),
                f"weekend of {weekend_of}: peak {peak}/{n_rooms} rooms occupied at 22:00, "
                f"{int(w['checkouts'].sum())} check-outs, {absent} cleaner shift(s) absent",
            )
        )
    return out


def s01_candidates(ds: Dataset, settings: Settings) -> list[Candidate]:
    """S01 screen: a guest waited for a room (``arrival_waiting_room``).

    The signal adds the wait (h) until ``checkin_after_wait``, whether an early
    check-in was requested for that booking, and the cleaning timeline of the room.
    """
    ev = ds.events
    waits = ev.loc[ev["type"] == "arrival_waiting_room"]
    after = ev.loc[ev["type"] == "checkin_after_wait"].set_index("booking_id")["timestamp_utc"]
    gr = ds.table("guest_requests.csv")
    early = set(gr.loc[gr["request_type"] == "early_checkin", "booking_id"].dropna())
    clean_done = ev.loc[ev["type"] == "cleaning_completed"]
    co = ev.loc[ev["type"].isin(derive.STAY_END_TYPES)]
    out: list[Candidate] = []
    for room, booking, t_wait in zip(
        waits["room_id"], waits["booking_id"], waits["timestamp_utc"], strict=True
    ):
        start = pd.Timestamp(t_wait)
        end = pd.Timestamp(after.get(booking, start))
        wait_h = (end - start).total_seconds() / 3600.0
        day_start = start - pd.Timedelta(hours=12)
        prev_co = co.loc[(co["room_id"] == room) & co["timestamp_utc"].between(day_start, start)]
        done = clean_done.loc[
            (clean_done["room_id"] == room) & clean_done["timestamp_utc"].between(day_start, end)
        ]
        early_flag = "early check-in requested" if booking in early else "no request"
        co_txt = (
            f"previous {prev_co['type'].iloc[-1]} {prev_co['timestamp_utc'].iloc[-1]:%H:%M}Z"
            if len(prev_co)
            else "no check-out in prior 12 h"
        )
        done_txt = (
            f"cleaning completed {done['timestamp_utc'].iloc[-1]:%H:%M}Z"
            if len(done)
            else "no cleaning completed"
        )
        out.append(
            Candidate(
                "S01",
                "guest_waited_for_room",
                (str(room),),
                start,
                end,
                f"waited {wait_h:.2f} h; {early_flag}; {co_txt}; {done_txt}",
            )
        )
    return out


def logistics_candidates(ds: Dataset, settings: Settings) -> list[Candidate]:
    """Logistics screen: container at the 100 % cap, out-of-stock and bin-full events."""
    lg = derive.logistics_wide(ds)
    out: list[Candidate] = []
    for col in [c for c in lg.columns if c.endswith("/fill_pct")]:
        full = (lg[col] >= settings.thresholds.waste_full_pct).fillna(False)
        run_id = (~full).cumsum()
        for rid in sorted(set(run_id[full])):
            members = full.index[((run_id == rid) & full).to_numpy()]
            out.append(
                Candidate(
                    "LOG",
                    "fill_at_cap",
                    (col.split("/")[0],),
                    pd.Timestamp(members[0]),
                    pd.Timestamp(members[-1]),
                    f"{col} at {settings.thresholds.waste_full_pct:.0f} % for "
                    f"{len(members)} h (capped value, may overflow)",
                )
            )
    ev = ds.events
    stock = ev.loc[ev["type"].isin(["vending_out_of_stock", "waste_bin_full"])]
    products = (
        stock["product"] if "product" in stock.columns else pd.Series(None, index=stock.index)
    )
    for etype, asset, t, product in zip(
        stock["type"], stock["asset_id"], stock["timestamp_utc"], products, strict=True
    ):
        out.append(
            Candidate(
                "LOG",
                str(etype),
                (str(asset),),
                pd.Timestamp(t),
                pd.Timestamp(t),
                f"{etype}" + (f" ({product})" if isinstance(product, str) else ""),
            )
        )
    return sorted(out, key=lambda c: (c.start, c.kind))


_CACHE: dict[tuple[int, int], list[Candidate]] = {}


def all_candidates(ds: Dataset, settings: Settings) -> list[Candidate]:
    """Run every screen and return the candidates sorted by class and time.

    Results are memoised per (dataset, settings) object pair within one process.
    """
    key = (id(ds), id(settings))
    if key in _CACHE:
        return _CACHE[key]
    found = (
        s01_candidates(ds, settings)
        + s02_candidates(ds, settings)
        + s03_candidates(ds, settings)
        + s04_candidates(ds, settings)
        + s05_candidates(ds, settings)
        + logistics_candidates(ds, settings)
    )
    _CACHE[key] = sorted(found, key=lambda c: (c.scenario, c.start, c.kind))
    return _CACHE[key]
