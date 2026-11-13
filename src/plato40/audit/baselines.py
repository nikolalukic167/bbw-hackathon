"""Section 6: normal-operation baselines that later detectors can use.

Baselines exclude the candidate episodes of section 5 where that matters (room night
water), so a leak does not inflate its own reference. All numbers are per dataset.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from plato40.audit import candidates as cand
from plato40.audit import derive
from plato40.audit.render import fmt, image, new_figure, save_figure, table
from plato40.load.reader import Dataset
from plato40.settings import Settings


def night_water_baseline(ds: Dataset, settings: Settings) -> pd.DataFrame:
    """Return per room: nights, share of nights with zero floor, night volume p50/p95 (L).

    Nights inside an S02 candidate for that room are excluded.
    """
    floor, total = cand.night_table(ds, settings)
    excluded = 0
    for c in cand.all_candidates(ds, settings):
        if c.scenario != "S02":
            continue
        days = pd.date_range(
            c.start.tz_convert(settings.local_tz).normalize(),
            c.end.tz_convert(settings.local_tz),
            freq="D",
        ).date
        for room in c.assets:
            if room in floor.columns:
                mask = floor.index.isin(days)
                excluded += int(mask.sum())
                floor.loc[mask, room] = np.nan
                total.loc[mask, room] = np.nan
    occupied = _occupied_nights(ds, settings, floor.index)
    rows = []
    for room in floor.columns:
        f = floor[room].dropna()
        t = total[room].dropna()
        occ = occupied[room].reindex(t.index).fillna(False).astype(bool)
        rows.append(
            {
                "room": room,
                "nights": len(t),
                "zero_floor_pct": 100.0 * float((f <= 0.0).mean()) if len(f) else np.nan,
                "p50_l": t.median(),
                "p95_l": t.quantile(0.95),
                "p95_occupied_l": t[occ].quantile(0.95) if occ.any() else np.nan,
                "p95_vacant_l": t[~occ].quantile(0.95) if (~occ).any() else np.nan,
            }
        )
    out = pd.DataFrame(rows).set_index("room")
    out.attrs["excluded_room_nights"] = excluded
    return out


def _occupied_nights(ds: Dataset, settings: Settings, days: pd.Index) -> pd.DataFrame:
    """Return day x room: True if the room was occupied at the night window start."""
    probe = pd.DatetimeIndex(
        [pd.Timestamp(str(d)).tz_localize(settings.local_tz) for d in days]
    ) + pd.Timedelta(days=1, hours=settings.night.start_hour)
    occ = derive.occupancy_matrix(ds, probe.tz_convert("UTC"))
    occ.index = days
    return occ


def cleaning_durations(ds: Dataset) -> pd.DataFrame:
    """Return matched cleaning tasks: room, resource, kind, start, minutes.

    A start is matched to the next completion of the same room and resource. Kinds:
    ``room`` (human cleaning) and ``robot_floor``.
    """
    ev = ds.events.sort_values(["timestamp_utc", "event_id"])
    rows = []
    for kind, start_type, end_type in (
        ("room", "cleaning_started", "cleaning_completed"),
        ("robot_floor", "robot_floor_cleaning_started", "robot_floor_cleaning_completed"),
    ):
        starts = ev.loc[ev["type"] == start_type]
        ends = ev.loc[ev["type"] == end_type]
        for key, s_grp in starts.groupby(["room_id", "resource_id"]):
            e_grp = ends.loc[(ends["room_id"] == key[0]) & (ends["resource_id"] == key[1])]
            e_times = pd.DatetimeIndex(e_grp["timestamp_utc"]).sort_values()
            for t in s_grp["timestamp_utc"]:
                pos = int(e_times.searchsorted(t))
                if pos < len(e_times):
                    rows.append(
                        {
                            "room": key[0],
                            "resource": key[1],
                            "kind": kind,
                            "start": t,
                            "minutes": (e_times[pos] - t).total_seconds() / 60.0,
                        }
                    )
    return pd.DataFrame(rows, columns=["room", "resource", "kind", "start", "minutes"])


def turnaround_lag(ds: Dataset) -> pd.Series:
    """Return hours from each check-out to the next human cleaning completion in that room."""
    ev = ds.events
    co = ev.loc[ev["type"].isin(derive.STAY_END_TYPES)]
    done = ev.loc[ev["type"] == "cleaning_completed"]
    lags = []
    for r in co.itertuples(index=False):
        d = done.loc[
            (done["room_id"] == r.room_id) & (done["timestamp_utc"] >= r.timestamp_utc),
            "timestamp_utc",
        ]
        if len(d):
            lags.append((d.min() - r.timestamp_utc).total_seconds() / 3600.0)
    return pd.Series(lags, dtype=float)


def energy_model(ds: Dataset, settings: Settings) -> tuple[pd.DataFrame, np.ndarray, float]:
    """Fit daily HP1 heat (kWh) = a + b * HDD + c * occupied rooms, least squares.

    HDD is heating degree-days on base 18 degC from the daily mean outdoor temperature.
    Returns (daily frame, coefficients [a, b, c], R^2).
    """
    cop = derive.daily_cop(ds, settings.local_tz)
    ops = cand.daily_operations(ds, settings)
    ops.index = pd.to_datetime([str(d) for d in ops.index])
    frame = cop.join(ops["occupied_22h"], how="inner")
    frame["hdd"] = (18.0 - frame["outdoor_c"]).clip(lower=0.0)
    x = np.column_stack([np.ones(len(frame)), frame["hdd"], frame["occupied_22h"]])
    y = frame["heat_kwh"].to_numpy()
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    pred = x @ coef
    ss_res = float(((y - pred) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    return frame, coef, 1.0 - ss_res / ss_tot if ss_tot else float("nan")


def render(datasets: list[Dataset], settings: Settings) -> str:
    """Return the markdown for section 6."""
    parts = [
        "## 6. Normal-operation baselines",
        "",
        "Hypotheses: (a) most room meters show zero flow in at least one night interval "
        "on almost every night, so a non-zero floor is rare; (b) daily heat demand is "
        "explained mostly by heating degree-days, with a smaller occupancy term; (c) a "
        "human room cleaning takes a stable time per resource, so readiness can be "
        "predicted from the queue.",
        "",
    ]
    for i, ds in enumerate(datasets, start=1):
        nb = night_water_baseline(ds, settings)
        cl = cleaning_durations(ds)
        lag = turnaround_lag(ds)
        frame, coef, r2 = energy_model(ds, settings)
        fig, axes = new_figure(1, 3.2)
        sc = axes[0].scatter(
            frame["outdoor_c"], frame["heat_kwh"], c=frame["occupied_22h"], s=10, cmap="viridis"
        )
        fig.colorbar(sc, ax=axes[0], label="rooms occupied at 22:00")
        axes[0].set_xlabel("daily mean outdoor temperature (degC)")
        axes[0].set_ylabel("HP1 heat output (kWh/day)")
        axes[0].set_title(f"{ds.profile}: daily heat demand vs outdoor temperature")
        fig_path = save_figure(
            fig, settings.figures_dir, f"{ds.profile}_baseline_heat", settings.figure_dpi
        )
        by_kind = cl.groupby(["kind", "resource"])["minutes"].describe()
        parts += [
            f"### 6.{i} `{ds.profile}`",
            "",
            f"Night water per room ({settings.night.start_hour:02d}:00-"
            f"{settings.night.end_hour:02d}:00 local), excluding "
            f"{nb.attrs['excluded_room_nights']} room-nights inside S02 candidates. Across "
            f"rooms the median share of nights with a zero 15-minute floor is "
            f"{fmt(nb['zero_floor_pct'].median())} % (lowest room "
            f"{fmt(nb['zero_floor_pct'].min())} %). Night volume p95 is "
            f"{fmt(nb['p95_occupied_l'].median())} L for occupied and "
            f"{fmt(nb['p95_vacant_l'].median())} L for vacant room-nights (median over rooms).",
            "",
            "<details><summary>Night water baseline per room</summary>",
            "",
            table(
                [
                    "room",
                    "nights",
                    "zero floor (%)",
                    "p50 night volume (L)",
                    "p95 night volume (L)",
                    "p95 occupied (L)",
                    "p95 vacant (L)",
                ],
                [
                    [
                        r,
                        fmt(v.nights, 0),
                        fmt(v.zero_floor_pct),
                        fmt(v.p50_l),
                        fmt(v.p95_l),
                        fmt(v.p95_occupied_l),
                        fmt(v.p95_vacant_l),
                    ]
                    for r, v in nb.iterrows()
                ],
            ),
            "",
            "</details>",
            "",
            f"Daily heat model: heat_kWh = {fmt(coef[0])} + {fmt(coef[1], 2)} x HDD(18 degC) "
            f"+ {fmt(coef[2], 2)} x occupied rooms, R^2 = {fmt(r2, 3)} over {len(frame)} days.",
            "",
            image(fig_path, f"{ds.profile}: heat demand baseline"),
            "",
            "Task durations (minutes, matched start to completion):",
            "",
            table(
                ["kind", "resource", "tasks", "median", "p90", "max"],
                [
                    [
                        str(k),
                        str(r),
                        fmt(n, 0),
                        fmt(med),
                        fmt(
                            cl.loc[(cl["kind"] == k) & (cl["resource"] == r), "minutes"].quantile(
                                0.9
                            )
                        ),
                        fmt(mx),
                    ]
                    for (k, r), n, med, mx in zip(
                        by_kind.index, by_kind["count"], by_kind["50%"], by_kind["max"], strict=True
                    )
                ],
            ),
            "",
            f"Check-out to next completed room cleaning: median {fmt(lag.median(), 2)} h, "
            f"p90 {fmt(lag.quantile(0.9), 2)} h, over {len(lag)} check-outs.",
            "",
        ]
    return "\n".join(parts)
