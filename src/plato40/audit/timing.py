"""Section 4: when information becomes known, and how BMS alarms relate to telemetry."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from plato40.audit import derive
from plato40.audit.render import fmt, image, iso, new_figure, save_figure, table
from plato40.load.known_at import lead_time_hours
from plato40.load.reader import Dataset
from plato40.load.schema import schema_by_name
from plato40.settings import Settings


@dataclass(frozen=True)
class AlarmRule:
    """Our reconstruction of a BMS alarm rule.

    Attributes:
        code: ``alarm_code`` in events.jsonl.
        series: Which derived series the rule watches (see ``_series``).
        op: ``<`` or ``>``; the alarm condition is ``series op threshold``.
        threshold: Value in the series' unit, inferred from raised alarm values.
        unit: Unit of the series.

    """

    code: str
    series: str
    op: str
    threshold: float
    unit: str


AHU_DAY_MODE_FAN_KW = 2.0
"""Fan power (kW) above which AHU1 is in day mode; night setback runs near 1 kW."""

ALARM_RULES = (
    AlarmRule("ROOM_TEMP_LOW", "room_temperature", "<", 17.5, "degC"),
    AlarmRule("ROOM_TEMP_HIGH", "room_temperature", ">", 28.0, "degC"),
    AlarmRule("FILTER_DP_HIGH", "AHU1/filter_dp_pa", ">", 250.0, "Pa"),
    AlarmRule("AIRFLOW_LOW", "AHU1/airflow_day_m3h", "<", 3500.0, "m3/h"),
    AlarmRule("WATER_NIGHT_FLOW", "main_night_delta", ">", 25.0, "L/15min"),
    AlarmRule("SENSOR_COMM_LOSS", "room_row_missing", ">", 0.5, "flag"),
)


def lead_summary(ds: Dataset) -> list[list[str]]:
    """Return lead time (effective minus known, hours) quantiles per availability source."""
    schemas = schema_by_name()
    rows: list[list[str]] = []
    sources = [
        ("tariff: hour start - published_utc", "tariff.csv", None),
        ("forecast: target_utc - issued_utc", "weather_forecast.csv", None),
        ("roster scheduled: start - status_known", "staff_roster.csv", "scheduled"),
        ("roster sick: start - status_known", "staff_roster.csv", "sick"),
        ("roster vacation: start - status_known", "staff_roster.csv", "vacation"),
        ("roster out_of_service: start - status_known", "staff_roster.csv", "out_of_service"),
        ("booking: arrival - created_utc", "bookings.csv", None),
        ("work order: scheduled - created_utc", "work_orders.csv", None),
    ]
    for label, file, status in sources:
        frame = ds.table(file)
        if status is not None:
            frame = frame.loc[frame["status"] == status]
        if frame.empty:
            rows.append([label, "0", "-", "-", "-", "-", "-"])
            continue
        lead = lead_time_hours(frame, schemas[file])
        rows.append(
            [
                label,
                fmt(len(lead), 0),
                fmt(lead.min()),
                fmt(lead.quantile(0.1)),
                fmt(lead.median()),
                fmt(lead.max()),
                fmt(int((lead < 0).sum()), 0),
            ]
        )
    gr = ds.table("guest_requests.csv")
    due = (gr["response_due_utc"] - gr["timestamp_utc"]).dt.total_seconds() / 3600.0
    rows.append(
        [
            "guest request: response_due - timestamp",
            fmt(len(due), 0),
            fmt(due.min()),
            fmt(due.quantile(0.1)),
            fmt(due.median()),
            fmt(due.max()),
            fmt(int((due < 0).sum()), 0),
        ]
    )
    return rows


def roster_changes(ds: Dataset, tz: str) -> list[list[str]]:
    """Return every non-scheduled roster row with its notice in hours."""
    r = ds.table("staff_roster.csv")
    r = r.loc[r["status"] != "scheduled"].sort_values("start_utc")
    notice = (r["start_utc"] - r["status_known_utc"]).dt.total_seconds() / 3600.0
    start_local = derive.local(r["start_utc"], tz)
    return [
        [
            str(shift),
            str(resource),
            str(status),
            pd.Timestamp(start_local_ts).strftime("%a %Y-%m-%d %H:%M"),
            fmt(n),
        ]
        for shift, resource, status, start_local_ts, n in zip(
            r["shift_id"], r["resource_id"], r["status"], start_local, notice, strict=True
        )
    ]


def forecast_error(ds: Dataset) -> list[list[str]]:
    """Return forecast mean absolute error (degC) and bias by lead-time bucket."""
    fc = ds.table("weather_forecast.csv").copy()
    obs = derive.weather_series(ds)
    fc["observed"] = fc["target_utc"].map(obs)
    fc["lead_h"] = (fc["target_utc"] - fc["issued_utc"]).dt.total_seconds() / 3600.0
    fc["err"] = fc["outdoor_c_forecast"] - fc["observed"]
    fc["bucket"] = pd.cut(fc["lead_h"], [0, 6, 12, 24, 36, 48], right=True)
    g = fc.groupby("bucket", observed=True)["err"]
    return [
        [str(b), fmt(int(s.count()), 0), fmt(s.abs().mean(), 2), fmt(s.mean(), 2)] for b, s in g
    ]


def _series(ds: Dataset, name: str, settings: Settings) -> pd.DataFrame:
    """Return a time x asset frame for an alarm rule's watched series.

    ``main_night_delta`` is the main meter volume (L/15min) inside the local night
    window and 0 outside it, so daytime use never satisfies the night-flow rule.
    ``AHU1/airflow_day_m3h`` is airflow in day mode only; the night setback airflow
    (about 3,000 m3/h) is a schedule, not a fault.
    """
    night_tz = settings.local_tz
    night_hours = (settings.night.start_hour, settings.night.end_hour)
    if name == "room_temperature":
        return derive.telemetry_wide(ds, "temperature_c")
    if name == "room_row_missing":
        tel = derive.dedupe_keep_first(ds.table("room_telemetry.csv"), ["timestamp_utc", "room_id"])
        measures = ["temperature_c", "humidity_pct", "co2_ppm", "presence", "window_open"]
        tel = tel.assign(miss=tel[measures].isna().all(axis=1).astype(float))
        return tel.pivot(index="timestamp_utc", columns="room_id", values="miss")
    if name == "main_night_delta":
        main = derive.main_water_series(ds).diff()
        hour = derive.dt_index(main.index).tz_convert(night_tz).hour
        night = (hour >= night_hours[0]) & (hour < night_hours[1])
        return main.where(night, 0.0).to_frame("WM_MAIN")
    eq = derive.equipment_numeric(ds)
    if name == "AHU1/airflow_day_m3h":
        day_mode = eq["AHU1/fan_kw"] >= AHU_DAY_MODE_FAN_KW
        return eq["AHU1/airflow_m3h"].where(day_mode, np.inf).to_frame("AHU1")
    asset = name.split("/")[0]
    return eq[[name]].rename(columns={name: asset})


@dataclass(frozen=True)
class AlarmMatch:
    """An alarm matched against the telemetry it claims to describe."""

    code: str
    asset: str
    raised: pd.Timestamp
    onset: pd.Timestamp | None
    reported: float | None
    observed: float | None

    @property
    def delay_h(self) -> float | None:
        """Alarm time minus telemetry onset, hours; None if no supporting telemetry."""
        if self.onset is None:
            return None
        return (self.raised - self.onset).total_seconds() / 3600.0


def _condition(values: pd.Series, rule: AlarmRule) -> pd.Series:
    """Return True where the rule condition holds, False where not, NaN where unknown."""
    cond = values < rule.threshold if rule.op == "<" else values > rule.threshold
    return cond.astype(float).where(values.notna())


def match_alarms(ds: Dataset, settings: Settings) -> list[AlarmMatch]:
    """Match each raised alarm to the onset of its condition in the preceding window.

    The onset is the first interval of the contiguous run (missing values do not break a
    run) that contains the last condition-true interval at or before the alarm. If the
    condition never holds in the window, the alarm has no supporting telemetry.
    """
    ev = ds.events
    alarms = ev.loc[(ev["type"] == "bms_alarm") & (ev["state"] == "raised")]
    window = pd.Timedelta(hours=settings.thresholds.alarm_match_window_h)
    out: list[AlarmMatch] = []
    for rule in ALARM_RULES:
        frame = _series(ds, rule.series, settings)
        of_code = alarms.loc[alarms["alarm_code"] == rule.code]
        values = pd.to_numeric(of_code["value"]).to_numpy(dtype=float)
        for asset_raw, t_raw, value in zip(
            of_code["asset_id"], of_code["timestamp_utc"], values, strict=True
        ):
            asset = str(asset_raw)
            raised = pd.Timestamp(t_raw)
            col = asset if asset in frame.columns else None
            reported = None if np.isnan(value) else float(value)
            if col is None:
                out.append(AlarmMatch(rule.code, asset, raised, None, reported, None))
                continue
            s = frame[col].loc[raised - window : raised]
            cond = _condition(s, rule).ffill()
            observed = float(s.iloc[-1]) if len(s) else np.nan
            true_idx = cond.index[(cond == 1.0).to_numpy()]
            onset: pd.Timestamp | None
            if len(true_idx) == 0:
                onset = None
            else:
                last = true_idx[-1]
                before = cond.loc[:last]
                false_idx = before.index[(before == 0.0).to_numpy()]
                onset = pd.Timestamp(
                    before.index[0] if len(false_idx) == 0 else before.loc[false_idx[-1] :].index[1]
                )
            out.append(
                AlarmMatch(
                    rule.code,
                    asset,
                    raised,
                    onset,
                    reported,
                    None if np.isnan(observed) else observed,
                )
            )
    return out


def unalarmed_episodes(ds: Dataset, min_hours: float, settings: Settings) -> list[list[str]]:
    """Count condition episodes of at least ``min_hours`` with no raised alarm nearby."""
    ev = ds.events
    alarms = ev.loc[(ev["type"] == "bms_alarm") & (ev["state"] == "raised")]
    rows: list[list[str]] = []
    window = pd.Timedelta(hours=settings.thresholds.alarm_match_window_h)
    for rule in ALARM_RULES:
        frame = _series(ds, rule.series, settings)
        total = 0
        missed = 0
        for col in frame.columns:
            cond = _condition(frame[col], rule).ffill().fillna(0.0) == 1.0
            run_id = (~cond).cumsum()
            runs = cond.groupby(run_id).agg(["sum"])
            for rid, n in runs["sum"].items():
                if n * 0.25 < min_hours:
                    continue
                members = cond.index[((run_id == rid) & cond).to_numpy()]
                start, end = members[0], members[-1]
                total += 1
                hit = alarms.loc[
                    (alarms["alarm_code"] == rule.code)
                    & (alarms["asset_id"].astype(str) == str(col))
                    & (alarms["timestamp_utc"] >= start)
                    & (alarms["timestamp_utc"] <= end + window)
                ]
                missed += int(hit.empty)
        rows.append([rule.code, fmt(total, 0), fmt(missed, 0)])
    return rows


def clears_while_active(ds: Dataset) -> int:
    """Count 'cleared' alarms whose reported value still satisfies the raise condition."""
    ev = ds.events
    cleared = ev.loc[(ev["type"] == "bms_alarm") & (ev["state"] == "cleared")]
    n = 0
    for rule in ALARM_RULES:
        vals = pd.to_numeric(cleared.loc[cleared["alarm_code"] == rule.code, "value"])
        n += int((_condition(vals, rule) == 1.0).sum())
    return n


def alarm_rows(matches: list[AlarmMatch], ds: Dataset, window_h: float) -> list[list[str]]:
    """Summarise matches per alarm code.

    Delays equal to the lookback window are censored: the condition already held when
    the window opened, so the true delay is at least that long.
    """
    ev = ds.events
    alarms = ev.loc[ev["type"] == "bms_alarm"]
    rows: list[list[str]] = []
    for rule in ALARM_RULES:
        ms = [m for m in matches if m.code == rule.code]
        raised_vals = pd.to_numeric(
            alarms.loc[(alarms["alarm_code"] == rule.code) & (alarms["state"] == "raised"), "value"]
        )
        delays = pd.Series([m.delay_h for m in ms if m.delay_h is not None], dtype=float)
        unsupported = sum(1 for m in ms if m.delay_h is None)
        rows.append(
            [
                rule.code,
                f"{rule.series} {rule.op} {fmt(rule.threshold)} {rule.unit}",
                f"{fmt(raised_vals.min(), 2)}..{fmt(raised_vals.max(), 2)}"
                if raised_vals.notna().any()
                else "-",
                fmt(len(ms), 0),
                fmt(delays.median(), 2) if len(delays) else "-",
                fmt(delays.quantile(0.9), 2) if len(delays) else "-",
                fmt(delays.max(), 2) if len(delays) else "-",
                fmt(int((delays >= window_h - 0.25).sum()), 0),
                fmt(unsupported, 0),
            ]
        )
    return rows


def delay_figure(matches: list[AlarmMatch], ds: Dataset, settings: Settings) -> str:
    """Plot alarm delay distributions per code; return the figure path."""
    fig, axes = new_figure(1, 3.0)
    ax = axes[0]
    codes = [r.code for r in ALARM_RULES]
    data = [[m.delay_h for m in matches if m.code == c and m.delay_h is not None] for c in codes]
    keep = [(c, d) for c, d in zip(codes, data, strict=True) if d]
    if keep:
        ax.boxplot([d for _, d in keep], tick_labels=[c for c, _ in keep], orientation="horizontal")
    ax.set_xlabel("alarm time minus telemetry onset (h)")
    ax.set_title(f"{ds.profile}: BMS alarm delay vs our reconstruction of the rule")
    return save_figure(fig, settings.figures_dir, f"{ds.profile}_alarm_delay", settings.figure_dpi)


def unsupported_examples(matches: list[AlarmMatch], limit: int = 10) -> list[list[str]]:
    """Return alarms with no supporting telemetry in the lookback window."""
    rows = [
        [m.code, m.asset, iso(m.raised), fmt(m.reported, 2), fmt(m.observed, 2)]
        for m in matches
        if m.delay_h is None
    ]
    return rows[:limit]


def render(datasets: list[Dataset], settings: Settings) -> str:
    """Return the markdown for section 4."""
    w = settings.thresholds.alarm_match_window_h
    parts = [
        "## 4. Information timing",
        "",
        "Hypotheses: (a) day-ahead prices are published once a day at 13:00 local for the "
        "next local day, so they are known 11 to 35 h ahead; (b) forecasts are issued daily "
        "at 06:00 local for 48 h; (c) sick notes arrive hours before the shift, planned "
        "absences weeks before; (d) BMS alarms lag the telemetry they describe and some "
        "have no telemetry support at all.",
        "",
        "Lead time is effective time minus the time the row became known (positive = known in "
        "advance). A decision at time t may use a row only if its known time is at or before "
        "t (ADR-002).",
        "",
    ]
    for i, ds in enumerate(datasets, start=1):
        matches = match_alarms(ds, settings)
        parts += [
            f"### 4.{i} `{ds.profile}`",
            "",
            table(
                [
                    "source",
                    "rows",
                    "min (h)",
                    "p10 (h)",
                    "median (h)",
                    "max (h)",
                    "known late (<0)",
                ],
                lead_summary(ds),
            ),
            "",
            "Roster changes (status other than scheduled), local shift start:",
            "",
            table(
                ["shift", "resource", "status", "shift start (local)", "notice (h)"],
                roster_changes(ds, settings.local_tz),
            ),
            "",
            "`bookings.csv` has no cancellation timestamp: a `cancelled` booking does not say "
            "when the cancellation became known, so cancellations cannot be replayed "
            "faithfully (open question 2).",
            "",
            "Forecast error by lead time (forecast minus measured outdoor temperature):",
            "",
            table(["lead (h)", "pairs", "MAE (degC)", "bias (degC)"], forecast_error(ds)),
            "",
            f"BMS alarms. Each raised alarm is matched to the onset of its condition in the "
            f"preceding {fmt(w, 0)} h. Thresholds are our reconstruction from the raised "
            "values; *unsupported* means the condition never held in the window, i.e. the "
            "alarm looks false or refers to data we do not have.",
            "",
            table(
                [
                    "code",
                    "reconstructed rule",
                    "raised values",
                    "raised",
                    "median delay (h)",
                    "p90 delay (h)",
                    "max delay (h)",
                    "censored at window",
                    "unsupported",
                ],
                alarm_rows(matches, ds, w),
            ),
            "",
            image(delay_figure(matches, ds, settings), f"{ds.profile}: BMS alarm delay"),
            "",
        ]
        ex = unsupported_examples(matches)
        if ex:
            parts += [
                "Unsupported alarms (first rows):",
                "",
                table(["code", "asset", "raised (UTC)", "reported value", "series at alarm"], ex),
                "",
            ]
        parts += [
            f"Alarms cleared while the reported value still meets the raise condition: "
            f"{clears_while_active(ds)}.",
            "",
            "Condition episodes of 1 h or longer and whether any alarm followed (alarms are "
            "incomplete if many are missed):",
            "",
            table(["code", "episodes", "without alarm"], unalarmed_episodes(ds, 1.0, settings)),
            "",
        ]
    return "\n".join(parts)
