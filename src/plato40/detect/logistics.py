"""Logistics: an extra waste pickup or vending restock before the regular one comes too late.

Each hour, the use rate is the mean hourly change over the last ``logistics_rate_h`` hours,
leaving out the steps of a pickup or restock. From it the time the central container reaches
its cap (or a vending product runs out) is projected. A signal fires when that time comes
before the next regular service, an extra service ordered now (lead time from
``building.json``) still arrives before the regular one, and the projected time is at most
``lead + logistics_buffer_h`` hours away. Floor bins are emptied daily and are not watched.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta

import pandas as pd

from plato40.detect.grid import local_time, stamp
from plato40.episodes import Signal
from plato40.load.derive import logistics_wide
from plato40.load.reader import Dataset
from plato40.settings import Settings

ISSUE = "logistics"
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WATCH = {
    "WASTE_MAIN/fill_pct": ("pickup", 100.0, 1.0, "container full"),
    "VEND1/stock_drinks": ("restock", 0.0, -1.0, "drinks out of stock"),
    "VEND1/stock_snacks": ("restock", 0.0, -1.0, "snacks out of stock"),
}


def _schedule(text: str) -> tuple[set[int], time, pd.Timedelta]:
    """Parse e.g. ``Tue + Fri 07:00 local; ... (~24 h lead time)`` from building.json."""
    days = {DAYS.index(d) for d in re.findall(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", text)}
    hh, mm = re.search(r"(\d\d):(\d\d)", text).groups()  # type: ignore[union-attr]
    lead = re.search(r"~(\d+) h lead", text)
    return days, time(int(hh), int(mm)), pd.Timedelta(hours=int(lead[1]) if lead else 24)


def _next(t: pd.Timestamp, days: set[int], at: time, tz: str) -> pd.Timestamp:
    """Return the first regular service after ``t``."""
    day = t.tz_convert(tz).date()
    for k in range(8):
        d = day + timedelta(days=k)
        when = pd.Timestamp(datetime.combine(d, at)).tz_localize(tz).tz_convert("UTC")
        if d.weekday() in days and when > t:
            return when
    raise ValueError(f"no service day in {days}")


def signals(ds: Dataset, settings: Settings) -> list[Signal]:
    """Return one signal per hour in which an extra service is needed now."""
    th = settings.detect
    lg = logistics_wide(ds)
    assets = {a["id"]: a for a in ds.building["building"]["assets"]}
    buffer = pd.Timedelta(hours=th.logistics_buffer_h)
    out: list[Signal] = []
    for col, (service, cap, sign, what) in WATCH.items():
        if col not in lg.columns:
            continue
        asset = col.split("/")[0]
        days, at, lead = _schedule(assets[asset][service])
        v = lg[col]
        step = v.diff() * sign
        rate = step.where(step >= 0).rolling(th.logistics_rate_h, min_periods=1).mean()
        left = (cap - v) * sign
        for t, value, r, room in zip(lg.index, v, rate, left, strict=True):
            if pd.isna(value) or pd.isna(r) or r <= 0:
                continue
            t = pd.Timestamp(t)
            empty = t + pd.Timedelta(hours=max(float(room), 0.0) / float(r))
            regular = _next(t, days, at, settings.local_tz)
            if not (empty < regular and t + lead < regular and empty - t <= lead + buffer):
                continue
            window = t - pd.Timedelta(hours=th.logistics_rate_h)
            out.append(
                Signal(
                    t,
                    ISSUE,
                    service,
                    (asset,),
                    (f"logistics.csv:{col}:{stamp(window)}..{stamp(t)}",),
                    f"{col} {value:.0f} now, {r:.1f} per hour over {th.logistics_rate_h} h: "
                    f"{what} at about {local_time(empty, t, settings.local_tz)}, before the "
                    f"regular {service} {local_time(regular, t, settings.local_tz)}",
                    {"lead": lead, "regular": regular, "empty": empty},
                )
            )
    return out
