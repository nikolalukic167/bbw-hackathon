"""S03 heat pump degradation, judged every hour on the last 24 h.

The 24 h COP (heat over electricity, intervals where both are present) is divided by the
reference COP at the window's mean outdoor temperature. The reference is fixed, never
refitted on the window judged: a cold snap and a fault can coincide (quickstart, audit
section 8). Each signal carries what the planner needs, as known at its time: occupied
rooms in comfort deficit in the last hour, technician availability, the coldest
forecast hour and the three dearest price hours of the next 24 h.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from plato40.audit.candidates import comfort_deficit
from plato40.audit.derive import equipment_numeric, weather_series
from plato40.detect.grid import stamp
from plato40.episodes import Signal
from plato40.load.reader import Dataset
from plato40.settings import Settings
from plato40.staff import Roster

ISSUE = "heating_comfort"
DAY = pd.Timedelta(hours=24)


def _hourly(series: pd.Series, how: str) -> pd.Series:
    r = series.resample("1h", label="right", closed="right")
    out: pd.Series = r.sum(min_count=1) if how == "sum" else r.mean()
    return out


def _outlook(ds: Dataset, t: pd.Timestamp, tz: str) -> dict[str, Any]:
    """Coldest forecast hour and dearest price hours in (t, t + 24 h], known at ``t``."""
    fc = ds.table("weather_forecast.csv")
    fc = fc.loc[fc["issued_utc"] <= t]
    out: dict[str, Any] = {"cold": None, "peaks": []}
    if not fc.empty:
        latest = fc.loc[fc["issued_utc"] == fc["issued_utc"].max()]
        ahead = latest.loc[(latest["target_utc"] > t) & (latest["target_utc"] <= t + DAY)]
        if not ahead.empty:
            row = ahead.loc[ahead["outdoor_c_forecast"].idxmin()]
            out["cold"] = (float(row["outdoor_c_forecast"]), pd.Timestamp(row["target_utc"]))
    tariff = ds.table("tariff.csv")
    known = tariff.loc[
        (tariff["published_utc"] <= t)
        & (tariff["timestamp_utc"] > t)
        & (tariff["timestamp_utc"] <= t + DAY)
    ]
    top = known.nlargest(3, "price_eur_kwh").sort_values("timestamp_utc")
    out["peaks"] = [f"{pd.Timestamp(ts).tz_convert(tz):%H:%M}" for ts in top["timestamp_utc"]]
    return out


def signals(ds: Dataset, settings: Settings) -> list[Signal]:
    """Return one signal per hour whose 24 h COP ratio is below ``cop_ratio_low``."""
    th = settings.detect
    eq = equipment_numeric(ds)
    heat, elec = eq["HP1/heat_output_kw"], eq["HP1/electric_kw"]
    both = heat.notna() & elec.notna()
    per_day = 24 * 60 // settings.interval_minutes
    h = _hourly(heat.where(both) * settings.interval_minutes / 60, "sum").rolling(24).sum()
    e = _hourly(elec.where(both) * settings.interval_minutes / 60, "sum").rolling(24).sum()
    n = _hourly(both.astype(float), "sum").rolling(24).sum()
    outdoor = _hourly(weather_series(ds), "mean").rolling(24, min_periods=12).mean()
    ref = th.cop_ref_intercept + th.cop_ref_slope * outdoor
    ratio = (h / e) / ref
    ok = (n >= th.cop_min_coverage * per_day) & (e > 0)
    low = ratio.loc[ok & (ratio < th.cop_ratio_low)]
    if low.empty:
        return []
    deficit = comfort_deficit(ds, settings)
    roster = Roster(ds)
    out: list[Signal] = []
    for t, r in zip(pd.DatetimeIndex(low.index), low.to_numpy(dtype=float), strict=True):
        recent = deficit.loc[(deficit.index > t - pd.Timedelta(hours=1)) & (deficit.index <= t)]
        cold_rooms = sorted(str(c) for c in recent.columns[recent.any(axis=0).to_numpy()])
        on = roster.on_shift("technician", t, t)
        data: dict[str, Any] = {
            "ratio": float(r),
            "outdoor": float(outdoor[t]),
            "cold_rooms": cold_rooms,
            "band": ds.building["constraints"]["occupied_comfort_c"],
            "technician": on[0] if on else None,
            "technician_from": t if on else roster.next_start("technician", t, t),
        } | _outlook(ds, t, settings.local_tz)
        out.append(
            Signal(
                t,
                ISSUE,
                "cop_low",
                ("HP1",),
                (
                    f"equipment.csv:HP1 heat_output_kw,electric_kw:{stamp(t - DAY)}..{stamp(t)}",
                    f"weather.csv:outdoor_c:{stamp(t - DAY)}..{stamp(t)}",
                ),
                f"HP1 24 h COP {float(h[t] / e[t]):.2f} is {float(r):.2f} of the reference at "
                f"{float(outdoor[t]):.1f} degC mean outdoor; {len(cold_rooms)} occupied rooms "
                "in comfort deficit",
                data,
            )
        )
    return out
