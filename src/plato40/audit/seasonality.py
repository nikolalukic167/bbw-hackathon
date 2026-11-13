"""Section 7: seasonality across the dev year and instance counts per scenario class."""

from __future__ import annotations

import pandas as pd

from plato40.audit import candidates as cand
from plato40.audit import derive
from plato40.audit.render import fmt, image, new_figure, save_figure, table
from plato40.load.reader import Dataset
from plato40.settings import Settings


def monthly_table(ds: Dataset, settings: Settings) -> pd.DataFrame:
    """Return per local month the main operating quantities (units in column names)."""
    tz = settings.local_tz

    def local_month(index: pd.Index) -> pd.Index:
        return pd.Index(derive.dt_index(index).tz_convert(tz).month)

    cop = derive.daily_cop(ds, tz)
    cop_month = pd.Index(derive.dt_index(cop.index).month)
    ops = cand.daily_operations(ds, settings)
    ops_month = pd.Index(pd.to_datetime([str(d) for d in ops.index]).month)
    energy = ds.table("energy.csv").set_index("timestamp_utc")
    energy_month = local_month(energy.index)
    main = derive.main_water_series(ds).diff()
    main_month = local_month(main.index)
    temp = derive.telemetry_wide(ds, "temperature_c")
    occ = derive.occupancy_matrix(ds, pd.DatetimeIndex(temp.index))
    hot = ((temp > settings.thresholds.overheat_c) & occ).sum(axis=1) * 0.25
    deficit = cand.comfort_deficit(ds, settings).sum(axis=1) * 0.25
    gr = ds.table("guest_requests.csv")
    gr_month = derive.local(gr["timestamp_utc"], tz).dt.month
    out = pd.DataFrame(
        {
            "outdoor_c": cop["outdoor_c"].groupby(cop_month).mean(),
            "hp_heat_mwh": cop["heat_kwh"].groupby(cop_month).sum() / 1000.0,
            "cop": (
                cop["heat_kwh"].groupby(cop_month).sum() / cop["elec_kwh"].groupby(cop_month).sum()
            ),
            "pv_mwh": energy["pv_kw"].groupby(energy_month).sum() * 0.25 / 1000.0,
            "load_mwh": energy["building_load_kw"].groupby(energy_month).sum() * 0.25 / 1000.0,
            "occupied_rooms": ops["occupied_22h"].groupby(ops_month).mean(),
            "water_m3_per_day": main.groupby(main_month).sum()
            / 1000.0
            / main.groupby(main_month).count()
            * 96.0,
            "occupied_hot_room_h": hot.groupby(local_month(hot.index)).sum(),
            "comfort_deficit_room_h": deficit.groupby(local_month(deficit.index)).sum(),
            "requests_hot": (gr["request_type"] == "temperature_hot").groupby(gr_month).sum(),
            "requests_cold": (gr["request_type"] == "temperature_cold").groupby(gr_month).sum(),
        }
    )
    return out.fillna(0.0)


def class_counts(ds: Dataset, settings: Settings) -> pd.DataFrame:
    """Return candidate counts per (class, kind) and local month."""
    found = cand.all_candidates(ds, settings)
    frame = pd.DataFrame(
        [(f"{c.scenario} {c.kind}", c.start.tz_convert(settings.local_tz).month) for c in found],
        columns=["class", "month"],
    )
    return frame.groupby(["class", "month"]).size().unstack(fill_value=0)


def deficit_by_hour(ds: Dataset, settings: Settings) -> pd.DataFrame:
    """Return comfort-deficit room-hours by local hour, May-Sep vs the rest of the year."""
    deficit = cand.comfort_deficit(ds, settings).sum(axis=1) * 0.25
    idx = derive.dt_index(deficit.index).tz_convert(settings.local_tz)
    warm = idx.month.isin([5, 6, 7, 8, 9])
    return pd.DataFrame(
        {
            "May-Sep": deficit[warm].groupby(idx[warm].hour).sum(),
            "Oct-Apr": deficit[~warm].groupby(idx[~warm].hour).sum(),
        }
    ).fillna(0.0)


def render(datasets: list[Dataset], settings: Settings) -> str:
    """Return the markdown for section 7 (dev profile only)."""
    dev = [ds for ds in datasets if ds.profile == "dev"]
    parts = [
        "## 7. Seasonality (dev only)",
        "",
        "Hypotheses: heating demand and HP efficiency follow outdoor temperature; summer "
        "brings overheating because the plant is heating-only; water use follows "
        "occupancy; each scenario class occurs several times with varying severity.",
        "",
    ]
    if not dev:
        parts.append("No dev dataset was given.")
        return "\n".join(parts)
    ds = dev[0]
    m = monthly_table(ds, settings)
    counts = class_counts(ds, settings)
    dh = deficit_by_hour(ds, settings)
    fig, axes = new_figure(3, 2.3)
    x = m.index
    axes[0].bar(x, m["hp_heat_mwh"], alpha=0.6, label="HP1 heat (MWh)")
    axes[0].plot(x, m["pv_mwh"], marker="o", color="tab:orange", label="PV (MWh)")
    axes[0].legend(loc="upper center")
    axes[1].plot(
        x,
        m["occupied_hot_room_h"],
        marker="o",
        color="tab:red",
        label=f"occupied room-h > {settings.thresholds.overheat_c:.0f} degC",
    )
    axes[1].plot(
        x,
        m["comfort_deficit_room_h"],
        marker="o",
        color="tab:blue",
        label="occupied comfort-deficit room-h",
    )
    axes[1].legend(loc="upper left")
    axes[2].plot(x, m["occupied_rooms"], marker="o", label="mean rooms occupied at 22:00")
    axes[2].plot(x, m["water_m3_per_day"], marker="o", label="main water (m3/day)")
    axes[2].legend(loc="upper left")
    axes[2].set_xticks(list(x))
    axes[2].set_xlabel("local month (2026)")
    axes[0].set_title("dev: monthly energy, comfort and occupancy")
    fig_path = save_figure(fig, settings.figures_dir, "dev_seasonality", settings.figure_dpi)
    peak_hot = int(m.index[int(m["occupied_hot_room_h"].to_numpy().argmax())])
    warm_night = float(dh.loc[[0, 1, 2, 3, 4, 5], "May-Sep"].sum())
    warm_total = float(dh["May-Sep"].sum())
    parts += [
        table(
            [
                "month",
                "outdoor (degC)",
                "HP heat (MWh)",
                "COP (-)",
                "PV (MWh)",
                "load (MWh)",
                "rooms occupied",
                "water (m3/day)",
                f"occupied >{fmt(settings.thresholds.overheat_c, 0)} degC (room-h)",
                "comfort deficit (room-h)",
                "hot requests",
                "cold requests",
            ],
            [
                [
                    str(k),
                    fmt(r.outdoor_c),
                    fmt(r.hp_heat_mwh, 2),
                    fmt(r.cop, 2),
                    fmt(r.pv_mwh, 2),
                    fmt(r.load_mwh, 2),
                    fmt(r.occupied_rooms),
                    fmt(r.water_m3_per_day),
                    fmt(r.occupied_hot_room_h, 0),
                    fmt(r.comfort_deficit_room_h, 0),
                    fmt(r.requests_hot, 0),
                    fmt(r.requests_cold, 0),
                ]
                for k, r in m.iterrows()
            ],
        ),
        "",
        image(fig_path, "dev: seasonality"),
        "",
        f"Overheating peaks in month {peak_hot} with "
        f"{fmt(m['occupied_hot_room_h'].max(), 0)} occupied room-hours above "
        f"{fmt(settings.thresholds.overheat_c, 0)} degC; the "
        "plant has no cooling (building.json `operations.cooling`).",
        "",
        f"Comfort deficits in May-Sep: {fmt(warm_total, 0)} room-h, of which "
        f"{fmt(warm_night, 0)} room-h fall between 00:00 and 06:00 local "
        f"({fmt(100.0 * warm_night / warm_total if warm_total else 0.0)} %, against "
        f"{fmt(100.0 * 6 / 24)} % if spread evenly over the day). The data does not show the "
        "cause (open question 6).",
        "",
        "Candidate counts per class and local month (section 5 screens):",
        "",
        table(
            ["class", *[str(c) for c in counts.columns], "total"],
            [
                [k, *[fmt(v, 0) for v in r.to_numpy()], fmt(int(r.sum()), 0)]
                for k, r in counts.iterrows()
            ],
        ),
        "",
    ]
    return "\n".join(parts)
