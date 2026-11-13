"""Section 5: exploratory scenario signals, candidate episodes and benign look-alikes.

Everything here is labelled *candidate*. Screens are defined in ``candidates``; this
module renders them, plots raw signals around them and counts look-alikes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from plato40.audit import candidates as cand
from plato40.audit import derive
from plato40.audit.render import fmt, image, iso, new_figure, save_figure, table
from plato40.audit.timing import AHU_DAY_MODE_FAN_KW
from plato40.load.reader import Dataset
from plato40.settings import Settings

MAX_ROWS = 25


def candidate_table(found: list[cand.Candidate]) -> str:
    """Render candidates as a table; long lists are folded into a details block."""
    rows = [[c.kind, ", ".join(c.assets), iso(c.start), iso(c.end), c.signal] for c in found]
    if not rows:
        return "No candidates."
    body = table(["kind", "assets", "start (UTC)", "end (UTC)", "signal"], rows)
    if len(rows) <= MAX_ROWS:
        return body
    return f"<details><summary>{len(rows)} candidates</summary>\n\n{body}\n\n</details>"


def _shade(
    ax: object, spans: list[tuple[pd.Timestamp, pd.Timestamp]], color: str, label: str
) -> None:
    for i, (a, b) in enumerate(spans):
        ax.axvspan(a, b, color=color, alpha=0.15, lw=0, label=label if i == 0 else None)  # type: ignore[attr-defined]


def _night_spans(
    start: pd.Timestamp, end: pd.Timestamp, settings: Settings
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    tz = settings.local_tz
    days = pd.date_range(
        start.tz_convert(tz).normalize() - pd.Timedelta(days=1),
        end.tz_convert(tz).normalize(),
        freq="D",
    )
    return [
        (
            d + pd.Timedelta(hours=settings.night.start_hour),
            d + pd.Timedelta(hours=settings.night.end_hour),
        )
        for d in days
    ]


def s01_section(ds: Dataset, settings: Settings, found: list[cand.Candidate]) -> list[str]:
    """Render S01 (turnaround) signals and look-alikes."""
    ops = cand.daily_operations(ds, settings)
    ev = ds.events
    waits = ev.loc[ev["type"] == "arrival_waiting_room"]
    wait_day = derive.local(waits["timestamp_utc"], settings.local_tz).dt.date.value_counts()
    ops["waits"] = wait_day.reindex(ops.index).fillna(0).astype(int)
    fig, axes = new_figure(2, 2.6)
    x = pd.to_datetime(ops.index)
    axes[0].plot(x, ops["occupied_22h"], lw=1, label="rooms occupied at 22:00 local")
    axes[0].bar(x, ops["checkouts"], alpha=0.4, label="check-outs per day")
    axes[0].legend(loc="upper left")
    axes[1].step(x, ops["cleaners_present"], where="mid", label="cleaner shifts present")
    axes[1].bar(x, ops["cleaners_absent"], color="tab:red", alpha=0.6, label="cleaner absent")
    axes[1].bar(
        x,
        ops["waits"],
        color="tab:purple",
        alpha=0.6,
        bottom=ops["cleaners_absent"],
        label="guests waiting for room",
    )
    axes[1].legend(loc="upper left")
    axes[0].set_title(f"{ds.profile}: turnaround load, staffing and guest waits per local day")
    fig_path = save_figure(
        fig, settings.figures_dir, f"{ds.profile}_s01_operations", settings.figure_dpi
    )
    early_ok = int((ev["type"] == "checkin_early").sum())
    robot_done = ev.loc[ev["type"] == "robot_floor_cleaning_completed"]
    human_done = ev.loc[ev["type"] == "cleaning_completed"]
    robot_first = 0
    for room, t in zip(human_done["room_id"], human_done["timestamp_utc"], strict=True):
        r = robot_done.loc[robot_done["room_id"] == room, "timestamp_utc"]
        if bool(((r < t) & (r >= t - pd.Timedelta(hours=6))).any()):
            robot_first += 1
    days_with_waits = int((ops["waits"] > 0).sum())
    wait_absent = int(((ops["waits"] > 0) & (ops["cleaners_absent"] > 0)).sum())
    return [
        "#### S01 Turnaround",
        "",
        "Hypothesis: guests wait for a room on days when check-outs are many, a cleaner is "
        "absent, or an early check-in was requested for a room whose previous guest left "
        "late; robots finishing a floor does not make a room ready.",
        "",
        f"Screen: every `arrival_waiting_room` event. {len(found)} candidates on "
        f"{days_with_waits} local days; {wait_absent} of those days had a cleaner absent.",
        "",
        candidate_table(found),
        "",
        image(fig_path, f"{ds.profile}: S01 operations per day"),
        "",
        "Benign look-alikes seen:",
        "",
        f"- `checkin_early` without any wait: {early_ok} events. An early arrival is not a "
        "conflict when the room was ready.",
        f"- Human room cleanings preceded by a robot floor cleaning of the same room within "
        f"6 h: {robot_first} of {len(human_done)}. A robot `completed` event is not readiness; "
        "only the human `cleaning_completed` (and inspection when required) is.",
        "- Room door access by the booking credential after check-out is not logged as a "
        "return; exits are not logged at all, so presence must come from motion, not doors.",
        "",
    ]


def s02_section(ds: Dataset, settings: Settings, found: list[cand.Candidate]) -> list[str]:
    """Render S02 (water leak) signals and look-alikes."""
    th = settings.thresholds
    deltas = cand.room_deltas(ds, settings)
    floor, total = cand.night_table(ds, settings)
    spiky = int(((total >= 20.0) & (floor < th.night_floor_l_per_15min)).sum().sum())
    figs: list[str] = []
    for kind in (
        "persistent_room_night_flow",
        "loud_room_night_volume",
        "night_gap_main_minus_rooms",
    ):
        first = next((c for c in found if c.kind == kind), None)
        if first is None:
            continue
        a = first.start - pd.Timedelta(days=2)
        b = min(first.end, first.start + pd.Timedelta(days=4)) + pd.Timedelta(days=1)
        fig, axes = new_figure(2, 2.6)
        if kind == "night_gap_main_minus_rooms":
            series = cand.gap_series(ds, settings).loc[a:b] * 4.0
            axes[0].plot(series.index, series.to_numpy(), lw=0.8, label="main minus rooms (L/h)")
            main = derive.main_water_series(ds).diff().loc[a:b] * 4.0
            axes[1].plot(main.index, main.to_numpy(), lw=0.8, label="main meter (L/h)")
        else:
            room = first.assets[0]
            series = deltas[room].loc[a:b] * 4.0
            axes[0].plot(series.index, series.to_numpy(), lw=0.8, label=f"{room} flow (L/h)")
            pres = derive.telemetry_wide(ds, "presence")[room].loc[a:b]
            occ = derive.occupancy_matrix(ds, pd.DatetimeIndex(pres.index))[room]
            axes[1].step(pres.index, pres.to_numpy(), where="post", lw=0.6, label="presence")
            axes[1].step(
                occ.index,
                occ.astype(float).to_numpy() * 0.8,
                where="post",
                lw=1.2,
                label="reconstructed stay (0.8 = occupied)",
            )
        for ax in axes:
            _shade(ax, _night_spans(a, b, settings), "tab:blue", "night window (local)")
            ax.legend(loc="upper left")
        axes[0].set_title(
            f"{ds.profile}: {kind} candidate {', '.join(first.assets)} from {iso(first.start)}"
        )
        figs.append(
            save_figure(fig, settings.figures_dir, f"{ds.profile}_s02_{kind}", settings.figure_dpi)
        )
    wo = ds.table("work_orders.csv")
    flush = wo.loc[wo["summary"].str.contains("Legionella", na=False)]
    gap = cand.gap_series(ds, settings)
    day = pd.Index(derive.dt_index(gap.index).tz_convert(settings.local_tz).date)
    daily_gap = gap.groupby(day).sum(min_count=1)
    parity = pd.Series(
        [pd.Timestamp(str(d)).toordinal() % 2 for d in daily_gap.index], index=daily_gap.index
    )
    gap_even = daily_gap[parity == 0].median()
    gap_odd = daily_gap[parity == 1].median()
    cleaning = derive.local(
        ds.events.loc[ds.events["type"] == "cleaning_started", "timestamp_utc"], settings.local_tz
    ).dt.hour
    ev = ds.events
    nf = ev.loc[(ev["type"] == "bms_alarm") & (ev["alarm_code"] == "WATER_NIGHT_FLOW")]
    lines = [
        "#### S02 Hidden water leak",
        "",
        "Hypothesis: a leak shows as flow that never stops at night in one room meter "
        "(running toilet: small and steady; burst hose: large), or as a night-time gap "
        "between the main meter and the sum of room meters (leak outside the rooms). "
        "Guests, cleaning and meter artefacts produce short spikes, daytime use or steps, "
        "not a steady night floor.",
        "",
        f"Screens: room night floor >= {fmt(th.night_floor_l_per_15min)} L per 15 min in "
        f"every night interval ({settings.night.start_hour:02d}:00-"
        f"{settings.night.end_hour:02d}:00 local) for >= {th.night_floor_min_nights} nights; "
        f"room night volume >= {fmt(th.burst_night_volume_l, 0)} L; median night gap "
        f">= {fmt(th.gap_night_l_per_h)} L/h. {len(found)} candidates.",
        "",
        candidate_table(found),
        "",
    ]
    lines += [image(f, f"{ds.profile}: S02 candidate signals") + "\n" for f in figs]
    lines += [
        "Benign look-alikes seen:",
        "",
        f"- Night use that is not continuous (room night volume >= 20 L but some night "
        f"interval below the floor threshold): {fmt(spiky, 0)} room-nights. These are "
        "showers, baths and toilet flushes; they fail the persistence test.",
        f"- Legionella flushing work orders: {len(flush)}. Flushing draws water in "
        "long-vacant rooms by design; vacant-room flow on those days is expected.",
        f"- Cleaning starts between {int(cleaning.min()) if len(cleaning) else 0}:00 and "
        f"{int(cleaning.max()) if len(cleaning) else 0}:00 local: cleaning water is "
        "daytime and outside the night window.",
        f"- Daily main-minus-rooms gap alternates by day parity: median "
        f"{fmt(gap_even, 0)} L on even and {fmt(gap_odd, 0)} L on odd ordinal days. An "
        "every-other-day common-area draw (consistent with roof-garden irrigation) raises "
        "the daily gap without any leak; the night gap is the cleaner signal.",
        "- Meter artefacts: resets at exchange and post-exchange jitter (section 2) create "
        "negative and positive steps that must not read as flow.",
        f"- `WATER_NIGHT_FLOW` BMS alarms: {len(nf) // 2 if len(nf) else 0} raise/clear "
        "pairs, on the main meter only; they cannot localise a room leak and miss small "
        "steady leaks.",
        "",
    ]
    return lines


def s03_section(ds: Dataset, settings: Settings, found: list[cand.Candidate]) -> list[str]:
    """Render S03 (energy vs comfort) signals and look-alikes."""
    tz = settings.local_tz
    cop = derive.daily_cop(ds, tz)
    expected = cand.cop_expected(cop)
    cold = cand.occupied_cold_hours(ds, settings)
    tariff = ds.table("tariff.csv").set_index("timestamp_utc")["price_eur_kwh"]
    price_day = tariff.groupby(pd.Index(derive.dt_index(tariff.index).tz_convert(tz).date)).max()
    status = derive.hp_status(ds)
    lockouts = status.loc[status.str.startswith("lockout", na=False)]
    lock_day = lockouts.groupby(
        pd.Index(derive.dt_index(lockouts.index).tz_convert(tz).date)
    ).size()
    fig, axes = new_figure(3, 2.3)
    x = cop.index
    axes[0].plot(x, cop["cop"], marker=".", lw=1, label="daily COP")
    axes[0].plot(x, expected, lw=1, ls="--", label="Theil-Sen fit on outdoor degC")
    axes[0].legend(loc="upper left")
    axes[1].plot(x, cop["outdoor_c"], lw=1, color="tab:green", label="outdoor mean (degC)")
    axes[1].legend(loc="upper left")
    xc = pd.to_datetime(cold.index)
    axes[2].bar(
        xc, cold.to_numpy(), color="tab:red", alpha=0.6, label="occupied comfort-deficit room-h"
    )
    if len(lock_day):
        axes[2].bar(
            pd.to_datetime(lock_day.index),
            lock_day.to_numpy() * 10,
            color="black",
            alpha=0.6,
            label="HP1 lockout intervals x10",
        )
    ax2 = axes[2].twinx()
    ax2.plot(
        pd.to_datetime(price_day.index),
        price_day.to_numpy(),
        color="tab:orange",
        lw=1,
        label="daily max price (EUR/kWh)",
    )
    axes[2].legend(loc="upper left")
    ax2.legend(loc="upper right")
    axes[0].set_title(f"{ds.profile}: heat pump efficiency, weather, comfort and price")
    fig_path = save_figure(
        fig, settings.figures_dir, f"{ds.profile}_s03_energy_comfort", settings.figure_dpi
    )
    temp = derive.telemetry_wide(ds, "temperature_c")
    occ = derive.occupancy_matrix(ds, pd.DatetimeIndex(temp.index))
    window = derive.telemetry_wide(ds, "window_open").reindex_like(temp)
    vacant_cold = float(((temp < settings.thresholds.comfort_low_c) & ~occ).sum().sum()) * 0.25
    window_cold = (
        float(((temp < settings.thresholds.comfort_low_c) & occ & (window == 1.0)).sum().sum())
        * 0.25
    )
    floor_cold = cand.comfort_deficit(ds, settings).sum()
    by_floor = floor_cold.groupby(floor_cold.index.str[1]).sum() * 0.25
    gr = ds.table("guest_requests.csv")
    complaints = int((gr["request_type"] == "temperature_cold").sum())
    return [
        "#### S03 Energy versus comfort",
        "",
        "Hypothesis: a degraded heat pump shows as daily COP well below what the outdoor "
        "temperature predicts; when it meets a cold spell, occupied rooms fall below the "
        "comfort band and below their setpoint. Price peaks matter for when to pre-heat, "
        "not whether to protect occupants.",
        "",
        f"Screens: daily COP < {fmt(1 - settings.thresholds.cop_drop_fraction, 2)} x fit; "
        f">= {fmt(settings.thresholds.occupied_cold_room_hours, 0)} occupied comfort-deficit "
        f"room-h per day. {len(found)} candidates.",
        "",
        candidate_table(found),
        "",
        image(fig_path, f"{ds.profile}: S03 signals"),
        "",
        f"HP1 lockout intervals: {len(lockouts)}; guest `temperature_cold` requests: "
        f"{complaints}. Occupied comfort-deficit room-hours by floor: "
        + ", ".join(f"F{k} {fmt(v, 0)} h" for k, v in by_floor.items())
        + ".",
        "",
        "Benign look-alikes seen:",
        "",
        f"- Vacant rooms below {fmt(settings.thresholds.comfort_low_c, 0)} degC: "
        f"{fmt(vacant_cold, 0)} room-h. This is the 18 degC setback, not a fault.",
        f"- Occupied rooms below the band with the window open: {fmt(window_cold, 0)} "
        "room-h. Cause is the window, not the plant.",
        "- Guests who set 19.5 or 20 degC: below-band temperature at the guest's own "
        "setpoint is a choice; the deficit screen requires a 1.5 K shortfall to the setpoint.",
        "- A frozen temperature sensor reads a constant value below the band (section 2) "
        "and looks like a cold room.",
        "- Cold days alone lower COP. When a cold spell and a degradation coincide in a "
        "short window, a COP fit on that window absorbs part of the degradation, so daily "
        "COP alone under-reports it.",
        "",
    ]


def s04_section(ds: Dataset, settings: Settings, found: list[cand.Candidate]) -> list[str]:
    """Render S04 (maintenance cascade) signals and look-alikes."""
    eq = derive.equipment_numeric(ds)
    roster = ds.table("staff_roster.csv")
    tech_off = roster.loc[(roster["role"] == "technician") & (roster["status"] != "scheduled")]
    wo = ds.table("work_orders.csv")
    postponed = wo.loc[wo["status"] == "postponed"]
    first = next((c for c in found if c.kind == "filter_dp_high"), None)
    lines = [
        "#### S04 Maintenance cascade",
        "",
        "Hypothesis: filter loading raises AHU1 filter dp and fan power and lowers airflow "
        "over days; a dp sensor gap can hide the crossing, but fan power keeps reporting; "
        "the technician being absent or a postponed filter change turns it into a cascade.",
        "",
        f"{len(found)} candidates.",
        "",
        candidate_table(found),
        "",
    ]
    if first is not None:
        a = first.start - pd.Timedelta(days=10)
        b = first.end + pd.Timedelta(days=2)
        fig, axes = new_figure(3, 2.2)
        for ax, col, label in (
            (axes[0], "AHU1/filter_dp_pa", "filter dp (Pa)"),
            (axes[1], "AHU1/fan_kw", "fan power (kW)"),
            (axes[2], "AHU1/airflow_m3h", "airflow (m3/h)"),
        ):
            s = eq[col].loc[a:b]
            ax.plot(s.index, s.to_numpy(), lw=0.7, label=label)
            missing = s.isna()
            idx = derive.dt_index(s.index)
            spans = [
                (idx[int(i)], idx[min(int(i) + 1, len(s) - 1)])
                for i in np.flatnonzero(missing.to_numpy())
            ]
            _shade(ax, spans, "tab:grey", "reading missing")
            off = tech_off.loc[(tech_off["end_utc"] >= a) & (tech_off["start_utc"] <= b)]
            _shade(
                ax,
                list(zip(off["start_utc"], off["end_utc"], strict=True)),
                "tab:red",
                "technician absent",
            )
            ax.legend(loc="upper left")
        axes[0].axhline(settings.thresholds.filter_dp_alarm_pa, color="black", lw=0.6, ls=":")
        axes[0].set_title(f"{ds.profile}: AHU1 around the first filter candidate")
        lines += [
            image(
                save_figure(
                    fig, settings.figures_dir, f"{ds.profile}_s04_ahu", settings.figure_dpi
                ),
                f"{ds.profile}: S04 AHU1 signals",
            ),
            "",
        ]
    dp = eq["AHU1/filter_dp_pa"]
    fan = eq["AHU1/fan_kw"]
    night_mode = fan < AHU_DAY_MODE_FAN_KW
    night_fan = fan[night_mode].median()
    night_air = eq["AHU1/airflow_m3h"][night_mode].median()
    fan_max = fan.max()
    hour = derive.dt_index(dp.index).tz_convert(settings.local_tz).hour
    night_ratio = dp[(hour >= 1) & (hour < 5)].median() / dp[(hour >= 10) & (hour < 16)].median()
    lines += [
        f"Technician absences: {len(tech_off)} shifts; postponed work orders: "
        f"{len(postponed)}"
        + (
            " ("
            + "; ".join(f"{iso(r.created_utc)} {r.summary}" for r in postponed.itertuples())
            + ")"
            if len(postponed)
            else ""
        )
        + ".",
        "",
        "Benign look-alikes seen:",
        "",
        f"- Daily dp cycle: the night median dp is {fmt(night_ratio, 2)} times the "
        "midday median, so dp alarms clear at night without any repair (see section 4, "
        "alarms cleared while active).",
        "- A dp data gap is not degradation and not recovery; fan power continuing to rise "
        "across the gap is the second signal that separates the two.",
        f"- AHU1 night setback: median fan {fmt(night_fan, 2)} kW and airflow "
        f"{fmt(night_air, 0)} m3/h while fan < {fmt(AHU_DAY_MODE_FAN_KW)} kW. Low airflow is "
        "a fault signal only in day mode.",
        f"- Fan power has a ceiling (maximum observed {fmt(fan_max, 2)} kW). Once it is "
        "reached, further filter loading lowers airflow instead of raising fan power, so "
        "the second signal changes channel.",
        "",
    ]
    return lines


def s05_section(ds: Dataset, settings: Settings, found: list[cand.Candidate]) -> list[str]:
    """Render S05 (event weekend) signals."""
    ops = cand.daily_operations(ds, settings)
    n = len(ds.room_ids())
    weekend = ops.loc[ops["weekday"].isin(["Fri", "Sat"])]
    return [
        "#### S05 Unexpected weekend",
        "",
        "Hypothesis: an event weekend shows as near-full occupancy Friday to Sunday with "
        "many same-day turnarounds, and a cleaner absence in the same window cuts capacity "
        "below demand.",
        "",
        f"Screen: Fri-Sun with peak occupancy >= "
        f"{fmt(settings.thresholds.occupancy_surge_fraction * 100, 0)} % of {n} rooms, or "
        f"any cleaner absence on those days. {len(found)} candidates.",
        "",
        candidate_table(found),
        "",
        f"Normal weekends for comparison: Friday and Saturday nights have median "
        f"{fmt(weekend['occupied_22h'].median(), 0)} occupied rooms (max "
        f"{fmt(weekend['occupied_22h'].max(), 0)}). Benign look-alikes: a full weekend with "
        "all cleaners present, and a weekday absence; neither is S05 on its own. The "
        "on-call housekeeper has no roster rows, so on-call capacity can only be inferred "
        "from `oncall_housekeeping_called` events "
        f"({int((ds.events['type'] == 'oncall_housekeeping_called').sum())} here).",
        "",
    ]


def log_section(ds: Dataset, settings: Settings, found: list[cand.Candidate]) -> list[str]:
    """Render logistics fill levels and stock-outs."""
    lg = derive.logistics_wide(ds)
    fig, axes = new_figure(2, 2.5)
    axes[0].plot(lg.index, lg["WASTE_MAIN/fill_pct"], lw=0.8, label="WASTE_MAIN fill (%)")
    for f in ("WASTE_F1", "WASTE_F2", "WASTE_F3", "WASTE_F4"):
        axes[0].plot(lg.index, lg[f"{f}/fill_pct"], lw=0.4, alpha=0.6, label=f)
    axes[0].legend(loc="upper left", ncol=5)
    axes[1].plot(lg.index, lg["VEND1/stock_drinks"], lw=0.8, label="drinks (units)")
    axes[1].plot(lg.index, lg["VEND1/stock_snacks"], lw=0.8, label="snacks (units)")
    axes[1].legend(loc="upper left")
    axes[0].set_title(f"{ds.profile}: waste fill levels and vending stock (hourly)")
    fig_path = save_figure(
        fig, settings.figures_dir, f"{ds.profile}_logistics", settings.figure_dpi
    )
    peak = lg["WASTE_MAIN/fill_pct"].max()
    return [
        "#### Logistics",
        "",
        "Hypothesis: the central container fills between the Tuesday and Friday pickups "
        "and reaches its cap after busy weekends; vending drinks run out before the "
        "Monday/Thursday restock in high season.",
        "",
        f"{len(found)} candidates (fill at the 100 % cap, `waste_bin_full`, "
        f"`vending_out_of_stock`). Peak WASTE_MAIN fill {fmt(peak)} %.",
        "",
        candidate_table(found),
        "",
        image(fig_path, f"{ds.profile}: logistics"),
        "",
        "Benign look-alike: a value of 100 % is a cap, so the time at 100 % understates "
        "overflow, and a low weekend fill when the hotel is empty is not a sensor fault.",
        "",
    ]


def render(datasets: list[Dataset], settings: Settings) -> str:
    """Return the markdown for section 5."""
    parts = [
        "## 5. Scenario signals (exploratory)",
        "",
        "Every episode below is a *candidate* from a transparent screening rule "
        "(`src/plato40/audit/candidates.py`), run on the whole dataset with hindsight. "
        "These are not detections and the screens are not detectors: a detector must use "
        "only information known at decision time. Work orders written after the fact are "
        "used here only to judge whether a candidate is plausible (last subsection).",
        "",
    ]
    for i, ds in enumerate(datasets, start=1):
        found = cand.all_candidates(ds, settings)
        by = {
            k: [c for c in found if c.scenario == k]
            for k in ("S01", "S02", "S03", "S04", "S05", "LOG")
        }
        parts += [f"### 5.{i} `{ds.profile}`", ""]
        parts += s01_section(ds, settings, by["S01"])
        parts += s02_section(ds, settings, by["S02"])
        parts += s03_section(ds, settings, by["S03"])
        parts += s04_section(ds, settings, by["S04"])
        parts += s05_section(ds, settings, by["S05"])
        parts += log_section(ds, settings, by["LOG"])
    parts += [
        f"### 5.{len(datasets) + 1} Candidates against corrective work orders",
        "",
        "Work orders are written by people after the fact, so they are not ground truth "
        "and a detector may not use them before `created_utc`. They are still the best "
        "available plausibility check: a corrective order on the same asset shortly after a "
        "candidate supports it.",
        "",
    ]
    for ds in datasets:
        found = cand.all_candidates(ds, settings)
        orders = corrective_orders(ds)
        covered = sum(
            1
            for asset, created in zip(orders["asset_id"], orders["created_utc"], strict=True)
            if related_candidates(found, str(asset), pd.Timestamp(created))
        )
        metering = int((orders["type"] == "metering").sum())
        parts += [
            f"`{ds.profile}`: {covered} of {len(orders) - metering} corrective orders "
            f"(excluding guest-reported defects) have a candidate on the same asset in the "
            f"14 d before; the {metering} metering order(s) are meter exchanges, not faults.",
            "",
            workorder_table(ds, settings),
            "",
        ]
    return "\n".join(parts)


def related_candidates(
    found: list[cand.Candidate], asset: str, created: pd.Timestamp
) -> list[cand.Candidate]:
    """Return candidates on ``asset`` active from 14 d before to 1 d after ``created``."""
    return [
        c
        for c in found
        if (
            asset in c.assets
            or (asset.startswith("WM_R") and asset[3:] in c.assets)
            or (asset == "HP1" and c.scenario == "S03")
        )
        and c.start <= created + pd.Timedelta(days=1)
        and c.end >= created - pd.Timedelta(days=14)
    ]


def corrective_orders(ds: Dataset) -> pd.DataFrame:
    """Return corrective and metering work orders, excluding generic guest reports."""
    wo = ds.table("work_orders.csv")
    return wo.loc[
        wo["type"].isin(["corrective", "metering"])
        & ~wo["summary"].str.startswith("Guest-reported", na=False)
    ]


def followed_by_order(ds: Dataset, settings: Settings, kind: str) -> tuple[int, int, list[str]]:
    """Return (candidates of ``kind`` with a related order, all of ``kind``, summaries)."""
    found = cand.all_candidates(ds, settings)
    of_kind = [c for c in found if c.kind == kind]
    hit: set[int] = set()
    summaries: list[str] = []
    orders = corrective_orders(ds)
    for asset, created, summary in zip(
        orders["asset_id"], orders["created_utc"], orders["summary"], strict=True
    ):
        rel = [
            c
            for c in related_candidates(found, str(asset), pd.Timestamp(created))
            if c.kind == kind
        ]
        for c in rel:
            hit.add(of_kind.index(c))
        if rel:
            summaries.append(str(summary))
    return len(hit), len(of_kind), sorted(set(summaries))


def workorder_table(ds: Dataset, settings: Settings) -> str:
    """Return corrective/metering work orders with the nearest preceding candidate."""
    found = cand.all_candidates(ds, settings)
    rows = []
    orders = corrective_orders(ds)
    for order_id, asset_raw, created_raw, summary in zip(
        orders["work_order_id"],
        orders["asset_id"],
        orders["created_utc"],
        orders["summary"],
        strict=True,
    ):
        asset = str(asset_raw)
        created = pd.Timestamp(created_raw)
        related = related_candidates(found, asset, created)
        lead = (
            fmt((created - min(c.start for c in related)).total_seconds() / 3600.0)
            if related
            else "-"
        )
        rows.append(
            [
                str(order_id),
                iso(created),
                asset,
                str(summary)[:70],
                ", ".join(sorted({c.kind for c in related})) or "none",
                lead,
            ]
        )
    return table(
        [
            "work order",
            "created (UTC)",
            "asset",
            "summary",
            "candidate kinds",
            "candidate start to work order (h)",
        ],
        rows,
    )
