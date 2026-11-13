"""Section 2: data quality (missingness, plausibility, duplicates, order, meters, time zones)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from plato40.audit import derive
from plato40.audit.render import fmt, image, iso, new_figure, save_figure, table
from plato40.load.reader import Dataset
from plato40.load.schema import CSV_SCHEMAS, Kind
from plato40.load.units import BOUNDS, MAIN_WATER_DELTA, ROOM_WATER_DELTA
from plato40.settings import Settings

TELEMETRY_MEASURES = (
    "temperature_c",
    "setpoint_c",
    "humidity_pct",
    "co2_ppm",
    "presence",
    "window_open",
    "housekeeping_state",
)


def missingness_rows(ds: Dataset) -> list[list[str]]:
    """Return file, column, missing count and share for every column with gaps."""
    rows: list[list[str]] = []
    for schema in CSV_SCHEMAS:
        frame = ds.table(schema.name)
        for col in frame.columns:
            n = int(frame[col].isna().sum())
            if n:
                rows.append(
                    [f"`{schema.name}`", f"`{col}`", fmt(n, 0), fmt(100.0 * n / len(frame), 2)]
                )
    eq = ds.table("equipment.csv")
    by_metric = eq.loc[eq["value"].isna()].groupby(["asset_id", "metric"]).size()
    for (asset, metric), n_missing in zip(by_metric.index, by_metric.to_numpy(), strict=True):
        total = int(((eq["asset_id"] == asset) & (eq["metric"] == metric)).sum())
        rows.append(
            [
                "`equipment.csv`",
                f"`value` ({asset}/{metric})",
                fmt(n_missing, 0),
                fmt(100.0 * n_missing / total, 2),
            ]
        )
    return rows


@dataclass(frozen=True)
class Episode:
    """A contiguous run of missing data for one group of series."""

    group: str
    start: pd.Timestamp
    end: pd.Timestamp
    width: int

    @property
    def hours(self) -> float:
        """Duration in hours, counting the final 15-minute interval."""
        return (self.end - self.start).total_seconds() / 3600.0 + 0.25


def _runs(mask: pd.Series, step: pd.Timedelta) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Return (start, end) of contiguous True runs in a time-indexed boolean series."""
    idx = mask.index[mask.to_numpy()]
    if len(idx) == 0:
        return []
    steps = idx.to_series().diff().to_numpy()
    breaks = np.flatnonzero(steps[1:] != np.timedelta64(step))
    starts = [idx[0], *idx[breaks + 1]]
    ends = [*idx[breaks], idx[-1]]
    return list(zip(starts, ends, strict=True))


def telemetry_outages(ds: Dataset) -> list[Episode]:
    """Return episodes where whole telemetry rows are missing, grouped by floor.

    A row is an outage row when every measured column is blank. Width is the maximum
    number of rooms simultaneously out during the episode.
    """
    tel = derive.dedupe_keep_first(ds.table("room_telemetry.csv"), ["timestamp_utc", "room_id"])
    out_rows = tel.loc[tel[list(TELEMETRY_MEASURES)].isna().all(axis=1)].copy()
    out_rows["floor"] = "F" + out_rows["room_id"].str[1]
    episodes: list[Episode] = []
    step = pd.Timedelta(minutes=15)
    for floor, grp in out_rows.groupby("floor"):
        counts = grp.groupby("timestamp_utc").size()
        full = pd.Series(True, index=counts.index)
        for start, end in _runs(full, step):
            episodes.append(Episode(str(floor), start, end, int(counts.loc[start:end].max())))
    return sorted(episodes, key=lambda e: (e.start, e.group))


def series_gaps(frame: pd.DataFrame, min_intervals: int) -> list[Episode]:
    """Return missing runs of at least ``min_intervals`` 15-min steps per column."""
    episodes: list[Episode] = []
    step = pd.Timedelta(minutes=15)
    full_index = pd.date_range(frame.index.min(), frame.index.max(), freq=step)
    frame = frame.reindex(full_index)
    for col in frame.columns:
        for start, end in _runs(frame[col].isna(), step):
            n = int((end - start) / step) + 1
            if n >= min_intervals:
                episodes.append(Episode(str(col), start, end, 1))
    return sorted(episodes, key=lambda e: (e.start, e.group))


def implausible_rows(ds: Dataset) -> list[list[str]]:
    """Return series, unit, bounds, counts below/above and observed extremes."""
    rows: list[list[str]] = []
    eq = derive.equipment_numeric(ds)
    lg = derive.logistics_wide(ds)
    for (file, col), bound in BOUNDS.items():
        values: pd.Series
        if file == "equipment.csv":
            if col not in eq.columns:
                continue
            values = eq[col]
        elif file == "logistics.csv":
            cols = [c for c in lg.columns if c.endswith("/" + col)]
            if not cols:
                continue
            values = pd.Series(lg[cols].to_numpy().ravel())
        else:
            values = ds.table(file)[col]
        values = values.dropna()
        lo = int((values < bound.low).sum())
        hi = int((values > bound.high).sum())
        rows.append(
            [
                f"`{file}` {col}",
                bound.unit,
                f"{fmt(bound.low)}..{fmt(bound.high)}",
                fmt(lo, 0),
                fmt(hi, 0),
                f"{fmt(values.min(), 2)}..{fmt(values.max(), 2)}",
            ]
        )
    return rows


def out_of_bounds_values(ds: Dataset) -> list[float]:
    """Return the sorted distinct out-of-bounds values over room telemetry measures."""
    tel = ds.table("room_telemetry.csv")
    values: set[float] = set()
    for col in ("temperature_c", "humidity_pct", "co2_ppm"):
        b = BOUNDS[("room_telemetry.csv", col)]
        s = tel[col].dropna()
        values |= {float(v) for v in s[(s < b.low) | (s > b.high)]}
    return sorted(values)


def implausible_examples(ds: Dataset, limit: int = 8) -> list[list[str]]:
    """Return individual out-of-bounds room temperature readings with context."""
    tel = ds.table("room_telemetry.csv")
    b = BOUNDS[("room_telemetry.csv", "temperature_c")]
    bad = tel.loc[(tel["temperature_c"] < b.low) | (tel["temperature_c"] > b.high)]
    bad = bad.head(limit)
    return [
        [iso(t), str(room), fmt(temp, 2), fmt(sp, 1)]
        for t, room, temp, sp in zip(
            bad["timestamp_utc"],
            bad["room_id"],
            bad["temperature_c"],
            bad["setpoint_c"],
            strict=True,
        )
    ]


def frozen_runs(wide: pd.DataFrame, min_intervals: int) -> pd.DataFrame:
    """Return runs where a numeric series repeats the exact same value.

    Output columns: series, start, end, intervals, value.
    """
    rows = []
    for col in wide.columns:
        s = wide[col]
        valid = s.notna()
        same = valid & (s == s.shift(1))
        run_id = (~same).cumsum()
        lengths = same.groupby(run_id).sum() + 1
        for rid, length in lengths.items():
            if length < min_intervals:
                continue
            members = s.index[(run_id == rid).to_numpy()]
            rows.append(
                {
                    "series": col,
                    "start": members[0],
                    "end": members[-1],
                    "intervals": int(length),
                    "value": float(s.loc[members[0]]),
                }
            )
    return pd.DataFrame(rows, columns=["series", "start", "end", "intervals", "value"])


def duplicate_rows(ds: Dataset) -> list[list[str]]:
    """Return per file: duplicate key rows and how many carry conflicting values."""
    rows: list[list[str]] = []
    for schema in CSV_SCHEMAS:
        frame = ds.table(schema.name)
        key = list(schema.primary_key)
        dup_mask = frame.duplicated(key, keep=False)
        n_dup = int(frame.duplicated(key).sum())
        if not n_dup:
            continue
        dups = frame.loc[dup_mask]
        conflicting = int((dups.drop_duplicates().groupby(key, dropna=False).size() > 1).sum())
        first = dups[schema.time_column].min()
        rows.append([f"`{schema.name}`", fmt(n_dup, 0), fmt(conflicting, 0), iso(first)])
    return rows


def order_violations(ds: Dataset) -> list[list[str]]:
    """Return per file the number of rows whose time is earlier than the previous row.

    Checked in file order within each key series (e.g. per room).
    """
    rows: list[list[str]] = []
    for schema in CSV_SCHEMAS:
        frame = ds.table(schema.name)
        group_cols = [c for c in schema.primary_key if c != schema.time_column]
        if schema.cadence_minutes is None:
            continue
        t = frame[schema.time_column]
        prev = frame.groupby(group_cols)[schema.time_column].shift(1) if group_cols else t.shift(1)
        rows.append([f"`{schema.name}`", fmt(int((t < prev).sum()), 0)])
    ev = ds.events["timestamp_utc"]
    rows.append(["`events.jsonl`", fmt(int((ev < ev.shift(1)).sum()), 0)])
    return rows


def raw_time_suffixes(ds: Dataset) -> list[list[str]]:
    """Return per timestamp column the share of raw strings ending in 'Z'."""
    rows: list[list[str]] = []
    for schema in CSV_SCHEMAS:
        cols = [c for c, k in schema.columns.items() if k is Kind.TIMESTAMP]
        raw = pd.read_csv(ds.path / schema.name, usecols=cols, dtype=str, keep_default_na=False)
        for col in cols:
            s = raw[col]
            s = s.loc[s != ""]
            rows.append([f"`{schema.name}` {col}", fmt(100.0 * s.str.endswith("Z").mean(), 2)])
    return rows


def meter_artefacts(ds: Dataset, settings: Settings) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (resets, negative-step summary) for room meters and the main meter."""
    drop = settings.thresholds.meter_reset_drop_l
    rooms = derive.meter_deltas(derive.room_water_wide(ds), drop)
    main = derive.meter_deltas(derive.main_water_series(ds).to_frame("WM_MAIN"), drop)
    resets = pd.concat([rooms.resets, main.resets], ignore_index=True)
    allsteps = pd.concat([rooms.deltas, main.deltas], axis=1)
    neg = allsteps.where(allsteps < 0)
    summary = pd.DataFrame(
        {
            "negative_steps": neg.notna().sum(),
            "negative_sum_l": neg.sum(),
            "first": neg.apply(lambda s: s.first_valid_index()),
        }
    )
    summary = summary.loc[summary["negative_steps"] > 0]
    return resets, summary


def above_delta_bounds(ds: Dataset, settings: Settings) -> tuple[int, int]:
    """Return counts of 15-min volumes above the room and main plausibility bounds."""
    drop = settings.thresholds.meter_reset_drop_l
    rooms = derive.meter_deltas(derive.room_water_wide(ds), drop).deltas
    main = derive.meter_deltas(derive.main_water_series(ds).to_frame("WM_MAIN"), drop).deltas
    return (
        int((rooms > ROOM_WATER_DELTA.high).sum().sum()),
        int((main > MAIN_WATER_DELTA.high).sum().sum()),
    )


def energy_balance(ds: Dataset) -> pd.Series:
    """Return the README energy balance residual per interval in kW.

    residual = grid_import + pv + discharge - load - charge - grid_export, where
    discharge = max(battery_signed, 0) and charge = max(-battery_signed, 0).
    """
    e = ds.table("energy.csv")
    bat = e["battery_discharge_kw_signed"]
    return (
        e["grid_import_kw"]
        + e["pv_kw"]
        + bat.clip(lower=0)
        - e["building_load_kw"]
        - (-bat).clip(lower=0)
        - e["grid_export_kw"]
    )


def dst_checks(ds: Dataset, tz: str) -> list[list[str]]:
    """Return local-clock regularities that only hold if local time is handled correctly.

    For each check, report the distinct local wall-clock times and the distinct UTC
    times; a single local value with two UTC values means the source follows
    Europe/Vienna across DST.
    """
    rows: list[list[str]] = []

    def add(label: str, ts: pd.Series) -> None:
        ts = ts.dropna()
        loc = derive.local(ts, tz).dt.strftime("%H:%M").value_counts()
        utc = ts.dt.strftime("%H:%M").value_counts()
        rows.append(
            [
                label,
                ", ".join(f"{k} ({v})" for k, v in loc.head(3).items()),
                ", ".join(f"{k} ({v})" for k, v in utc.head(3).items()),
            ]
        )

    b = ds.table("bookings.csv")
    add("booking arrival_utc", b["arrival_utc"])
    add("booking departure_utc", b["departure_utc"])
    add("tariff published_utc", ds.table("tariff.csv")["published_utc"])
    add("forecast issued_utc", ds.table("weather_forecast.csv")["issued_utc"])
    r = ds.table("staff_roster.csv")
    add("cleaner shift start_utc", r.loc[r["role"] == "cleaner", "start_utc"])
    add("technician shift start_utc", r.loc[r["role"] == "technician", "start_utc"])
    wp = ds.events.loc[ds.events["type"] == "waste_pickup", "timestamp_utc"]
    add("waste_pickup events", wp)
    return rows


CATEGORICALS = (
    ("room_telemetry.csv", "housekeeping_state"),
    ("bookings.csv", "status"),
    ("bookings.csv", "channel"),
    ("staff_roster.csv", "status"),
    ("access_log.csv", "result"),
    ("work_orders.csv", "type"),
    ("work_orders.csv", "status"),
    ("guest_requests.csv", "requires_guest_consent"),
)


def categorical_rows(ds: Dataset) -> list[list[str]]:
    """Return observed values and counts of the categorical columns, missing included."""
    rows: list[list[str]] = []
    for file, col in CATEGORICALS:
        counts = ds.table(file)[col].value_counts(dropna=False)
        rows.append(
            [
                f"`{file}` {col}",
                ", ".join(
                    f"{'(missing)' if pd.isna(k) else k} {fmt(v, 0)}"
                    for k, v in zip(counts.index, counts.to_numpy(), strict=True)
                ),
            ]
        )
    eq = ds.table("equipment.csv")
    hp = eq.loc[(eq["asset_id"] == "HP1") & (eq["metric"] == "status"), "value"]
    rows.append(
        [
            "`equipment.csv` HP1 status",
            ", ".join(f"{k} {fmt(v, 0)}" for k, v in hp.value_counts(dropna=False).items()),
        ]
    )
    return rows


def missing_over_time_figure(ds: Dataset, settings: Settings) -> str:
    """Plot the daily share of missing cells for key series; return the figure path."""
    tel = ds.table("room_telemetry.csv")
    water = ds.table("room_water.csv")
    eq = ds.table("equipment.csv")
    tz = settings.local_tz

    def daily_share(frame: pd.DataFrame, cols: list[str]) -> pd.Series:
        miss = frame[cols].isna().mean(axis=1)
        day = derive.local(frame["timestamp_utc"], tz).dt.date
        return miss.groupby(day).mean() * 100.0

    fig, axes = new_figure(1, 3.2)
    ax = axes[0]
    for label, series in (
        ("room telemetry (all measures)", daily_share(tel, list(TELEMETRY_MEASURES))),
        ("room water meters", daily_share(water, ["meter_total_l"])),
        ("equipment values", daily_share(eq, ["value"])),
    ):
        ax.plot(pd.to_datetime(series.index), series.to_numpy(), label=label, lw=1)
    ax.set_ylabel("missing cells per local day (%)")
    ax.legend(loc="upper left")
    ax.set_title(f"{ds.profile}: share of missing values per day")
    return save_figure(
        fig, settings.figures_dir, f"{ds.profile}_missing_per_day", settings.figure_dpi
    )


def render_dataset(ds: Dataset, settings: Settings, number: str) -> str:
    """Return the markdown for one dataset's data-quality subsection."""
    parts = [f"### {number} `{ds.profile}`", ""]
    parts += [
        "Missing values per column (blank field = missing):",
        "",
        table(["file", "column", "missing", "share (%)"], missingness_rows(ds)),
        "",
        image(missing_over_time_figure(ds, settings), f"{ds.profile}: missing values per day"),
        "",
    ]
    outages = telemetry_outages(ds)
    floor_wide = sum(1 for e in outages if e.width == 10)
    parts += [
        f"Whole-row telemetry outages (all measures blank): {len(outages)} episodes, "
        f"total {fmt(sum(e.hours for e in outages))} h. In {floor_wide} of them all 10 rooms "
        "of one floor are blank at the same time, the signature of a floor gateway "
        "(`GW_F1`..`GW_F4`) rather than individual sensors. Remaining single-column gaps "
        "are sensor dropouts."
        if outages
        else "Whole-row telemetry outages: none.",
        "",
    ]
    if outages:
        parts += [
            "<details><summary>Outage episodes</summary>",
            "",
            table(
                ["floor", "start (UTC)", "end (UTC)", "hours", "rooms out"],
                [[e.group, iso(e.start), iso(e.end), fmt(e.hours, 2), e.width] for e in outages],
            ),
            "",
            "</details>",
            "",
        ]
    gaps = series_gaps(derive.equipment_numeric(ds), min_intervals=8)
    parts += [f"Equipment gaps of 2 h or longer: {len(gaps)}.", ""]
    if gaps:
        parts += [
            table(
                ["series", "start (UTC)", "end (UTC)", "hours"],
                [[g.group, iso(g.start), iso(g.end), fmt(g.hours, 2)] for g in gaps[:20]],
            ),
            "",
        ]
    parts += [
        "Plausibility (bounds are our assumptions, see `src/plato40/load/units.py`):",
        "",
        table(
            ["series", "unit", "bounds", "below", "above", "observed range"],
            implausible_rows(ds),
        ),
        "",
    ]
    examples = implausible_examples(ds)
    if examples:
        parts += [
            "Out-of-bounds room temperatures (first rows). Distinct out-of-bounds values: "
            + ", ".join(fmt(v, 1) for v in out_of_bounds_values(ds))
            + ". Exact round values repeated across rooms look like sensor error codes, "
            "not physics:",
            "",
            table(["time (UTC)", "room", "temperature_c", "setpoint_c"], examples),
            "",
        ]
    room_hi, main_hi = above_delta_bounds(ds, settings)
    parts += [
        f"15-minute water volumes above bounds: rooms {fmt(room_hi, 0)} "
        f"(> {fmt(ROOM_WATER_DELTA.high, 0)} L), main {fmt(main_hi, 0)} "
        f"(> {fmt(MAIN_WATER_DELTA.high, 0)} L).",
        "",
    ]
    temp = derive.telemetry_wide(ds, "temperature_c")
    frozen = frozen_runs(temp, min_intervals=12)
    eq_frozen = frozen_runs(
        derive.equipment_numeric(ds).drop(columns=["HP1/supply_temp_c"], errors="ignore"), 12
    )
    parts += [
        f"Frozen values (identical reading for 3 h or more): room temperature "
        f"{len(frozen)} runs in {frozen['series'].nunique() if len(frozen) else 0} rooms, "
        f"longest {fmt(frozen['intervals'].max() / 4 if len(frozen) else 0)} h; "
        f"equipment {len(eq_frozen)} runs.",
        "",
    ]
    if len(frozen):
        top = frozen.sort_values("intervals", ascending=False).head(10)
        parts += [
            table(
                ["room", "start (UTC)", "end (UTC)", "hours", "value (degC)"],
                [
                    [str(name), iso(a), iso(b), fmt(int(n) / 4, 2), fmt(v, 2)]
                    for name, a, b, n, v in zip(
                        top["series"],
                        top["start"],
                        top["end"],
                        top["intervals"],
                        top["value"],
                        strict=True,
                    )
                ],
            ),
            "",
        ]
    dups = duplicate_rows(ds)
    parts += [
        "Duplicate primary keys:",
        "",
        table(["file", "duplicate rows", "with conflicting values", "first at"], dups)
        if dups
        else "none.",
        "",
        "Timestamps earlier than the previous row of the same series, in file order:",
        "",
        table(["file", "violations"], order_violations(ds)),
        "",
    ]
    resets, neg = meter_artefacts(ds, settings)
    wo = ds.table("work_orders.csv")
    metering = wo.loc[wo["type"] == "metering"]
    parts += [
        f"Cumulative meter resets (drop > {fmt(settings.thresholds.meter_reset_drop_l, 0)} L): "
        f"{len(resets)}. Metering work orders: {len(metering)}.",
        "",
    ]
    if len(resets):
        parts += [
            table(
                ["meter", "time (UTC)", "drop (L)"],
                [[r.meter, iso(r.timestamp_utc), fmt(r.drop_l)] for r in resets.itertuples()],
            ),
            "",
        ]
    if len(metering):
        parts += [
            table(
                ["work order", "asset", "scheduled (UTC)", "completed (UTC)"],
                [
                    [r.work_order_id, r.asset_id, iso(r.scheduled_utc), iso(r.completed_utc)]
                    for r in metering.itertuples()
                ],
            ),
            "",
        ]
    parts += [
        "Smaller negative steps (meter jitter), by meter:",
        "",
        table(
            ["meter", "negative steps", "sum (L)", "first (UTC)"],
            [
                [m, fmt(r.negative_steps, 0), fmt(r.negative_sum_l), iso(r.first)]
                for m, r in neg.iterrows()
            ],
        )
        if len(neg)
        else "none.",
        "",
    ]
    res = energy_balance(ds)
    parts += [
        f"Energy balance residual (README identity): median {fmt(res.median(), 3)} kW, "
        f"|residual| > 0.5 kW in {fmt(int((res.abs() > 0.5).sum()), 0)} of "
        f"{fmt(int(res.notna().sum()), 0)} intervals, max |residual| {fmt(res.abs().max(), 2)} kW.",
        "",
        "Categorical values as observed (a documented value that never occurs is absent "
        "here, e.g. `inspection_required`):",
        "",
        table(["column", "values (count)"], categorical_rows(ds)),
        "",
        "Time zones. Share of raw timestamp strings that end in `Z` (UTC):",
        "",
        table(["column", "ends in Z (%)"], raw_time_suffixes(ds)),
        "",
        "Local-clock regularities (top three values, count in brackets). One local value "
        "with two UTC values means the source follows Europe/Vienna across daylight saving "
        "time; consumers must convert, not assume a fixed offset:",
        "",
        table(["series", "local (Europe/Vienna)", "UTC"], dst_checks(ds, settings.local_tz)),
        "",
    ]
    return "\n".join(parts)


def render(datasets: list[Dataset], settings: Settings) -> str:
    """Return the markdown for section 2."""
    parts = [
        "## 2. Data quality",
        "",
        "Hypotheses: (a) missing data comes in two forms, single-sensor dropouts and "
        "gateway outages that blank a whole floor; (b) a few implausible spikes exist and "
        "are isolated; (c) duplicates are exact copies; (d) meter resets coincide with "
        "metering work orders; (e) all timestamps are UTC and local operations follow "
        "Europe/Vienna including DST.",
        "",
    ]
    for i, ds in enumerate(datasets, start=1):
        parts.append(render_dataset(ds, settings, f"2.{i}"))
    return "\n".join(parts)
