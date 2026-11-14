"""One typed settings object for the whole package.

Every tunable number used by the audit lives here, with its unit. Paths are never
defaults: the data directory and the output directory always come from the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class NightWindow:
    """Local-time window used for minimum-night-flow analysis.

    Hours are Europe/Vienna wall-clock hours, start inclusive, end exclusive.
    """

    start_hour: int = 1
    end_hour: int = 5


@dataclass(frozen=True)
class AuditThresholds:
    """Thresholds that turn raw signals into audit candidates.

    These are exploratory screening values for the audit report. Detectors never read
    them; their thresholds are in ``DetectorSettings``.
    """

    night_floor_l_per_15min: float = 0.5
    """Minimum 15-minute room meter delta (L) in every night interval to call a night "floored"."""

    night_floor_min_nights: int = 2
    """Consecutive floored nights (count) before a room becomes a persistent-flow candidate."""

    burst_night_volume_l: float = 300.0
    """Night-window volume (L) in one room in one night that marks a loud-leak candidate."""

    gap_night_l_per_h: float = 10.0
    """Median night gap main minus rooms (L/h) that marks a gap-leak candidate night."""

    meter_reset_drop_l: float = 1000.0
    """A cumulative meter drop larger than this (L) is a reset, smaller drops are jitter."""

    cop_drop_fraction: float = 0.15
    """Daily heat pump COP below (1 - this) times the outdoor-temperature fit marks a candidate."""

    occupied_cold_room_hours: float = 10.0
    """Occupied room-hours (h) below the comfort band in one local day that mark a candidate."""

    filter_dp_alarm_pa: float = 250.0
    """AHU1 filter differential pressure (Pa) treated as high; below the lowest raised BMS value."""

    comfort_low_c: float = 20.0
    """Lower bound of the occupied comfort band (degC), from building.json constraints."""

    overheat_c: float = 26.0
    """Occupied room temperature (degC) above which a room-hour counts as overheated."""

    comfort_deficit_k: float = 1.5
    """Shortfall below the active setpoint (K) required for an occupied comfort deficit."""

    waste_full_pct: float = 100.0
    """Fill level (%) at which the logged value is capped, so the container may overflow."""

    occupancy_surge_fraction: float = 0.9
    """Share of rooms occupied (fraction) that marks a high-occupancy night."""

    alarm_match_window_h: float = 24.0
    """Window (h) before a BMS alarm in which a matching telemetry change is searched."""


@dataclass(frozen=True)
class DetectorSettings:
    """Detector thresholds. Starting values come from the audit (sections 5, 6, 8)."""

    leak_floor_l_per_15min: float = 0.2
    """A night whose every 15-min room volume (L) is at least this has continuous flow. Running
    toilets draw 0.9-1.1 L (quickstart, dev); outside leaks no two nights in a row stay at
    0.1 L or more, so 0.2 L catches a toilet running at a fifth of that rate."""

    leak_persist_nights: int = 2
    """Consecutive continuous-flow nights (count) before a slow leak is acted on."""

    leak_technician_nights: int = 3
    """Consecutive continuous-flow nights (count) before a technician is requested."""

    leak_contractor_nights: int = 2
    """Further leak nights (count) after the technician was requested before an external
    plumber is ordered; the leak is then not fixed in time."""

    burst_night_volume_l: float = 250.0
    """Night-window volume (L) in one room that is a burst, counted only while every night
    interval flows; normal nights reach <= 180 L, but one bath alone draws up to 250 L."""

    gap_night_l_per_h: float = 4.0
    """Median night gap main minus rooms (L/h) above the run's own baseline at which a pipe leak
    is suspected. Normal nights reach at most 2.0 L/h (dev, 1.2 quickstart) over a baseline of
    0; the dev pipe leak ran at ~29 L/h."""

    gap_absolute_l_per_h: float = 15.0
    """Median night gap (L/h) that is a pipe leak whatever the baseline: a leak running from the
    first night is learnt as normal, so this net still catches one of half the dev leak's size."""

    gap_baseline_nights: int = 14
    """Past normal gap nights (count) the baseline is learnt from; nights in a leak run, blank
    nights and later nights never count, so a leak does not teach the baseline."""

    gap_baseline_min_nights: int = 2
    """Normal gap nights (count) needed before the baseline is used; before that only
    ``gap_absolute_l_per_h`` applies."""

    gap_baseline_quantile: float = 0.25
    """Quantile of the past normal nights taken as the baseline, floored at zero (meter noise
    makes quiet nights slightly negative). A low quantile keeps the baseline at the normal
    level when a leak starts early in the run, and one odd night cannot move it."""

    gap_min_coverage: float = 0.75
    """Share of a night's intervals with the main and every room meter needed for a gap verdict.
    With lone blank readings bridged, only gateway-outage nights fall below it (dev 6 of 365)."""

    burst_flow_l_per_15min: float = 10.0
    """Room volume (L per 15 min) that, held over ``burst_flow_intervals``, is a burst at any
    hour; the highest sustained room flow without a leak is 2.8 L per 15 min (dev)."""

    burst_flow_intervals: int = 12
    """Consecutive intervals (count) at ``burst_flow_l_per_15min`` or more for a burst; heavy
    guest use held at most 6 (dev), bursts run 20 h or more, so 3 h costs ~1 h of lead."""

    meter_reset_drop_l: float = 1000.0
    """A meter drop larger than this (L) is a reset or exchange; the interval is missing."""

    outage_min_intervals: int = 2
    """Consecutive blank intervals (count) on a whole floor that make a telemetry outage."""

    sensor_dead_h: float = 6.0
    """Hours one room's temperature is blank while its floor reports, for a dead sensor; dev
    has 19 such runs of 1 h or more and 18 of them last 6 h or more."""

    episode_quiet_h: float = 48.0
    """Hours without a new signal after which an episode is closed."""

    clean_room_min: float = 60.0
    """Minutes one human room cleaning is planned to take (audit p90; median 45)."""

    shift_lookahead_h: float = 24.0
    """Hours of cleaner shifts searched for one in which a room clean still fits."""

    robot_pass_min: float = 30.0
    """Minutes one robot floor pass takes (every dev pass took 30 min)."""

    technician_job_h: float = 24.0
    """Hours one dispatched repair holds the in-house technician; a second job in that time
    goes to a contractor."""

    oncall_travel_min: float = 45.0
    """Minutes until the on-call housekeeper is on site (building.json staff notes)."""

    urgent_lead_h: float = 3.0
    """Hours before the wanted time under which a turnaround request is high priority."""

    checkout_follow_h: float = 24.0
    """Hours after a request within which the room's actual checkout is still followed up."""

    day_plan_span_h: float = 16.0
    """Hours after the day plan whose cleaner shifts can still clean a room that day."""

    day_plan_hour: int = 7
    """Local hour of the daily turnaround check, after most sick notes are known."""

    day_clean_min: float = 45.0
    """Minutes per room for a whole-day workload (audit median); promises use p90."""

    day_load_high: float = 0.75
    """Turnover minutes over cleaner minutes before check-in at which the day is at risk."""

    day_load_help: float = 1.0
    """Load above which extra cleaning help is requested (approval; never assumed)."""

    surge_lookahead_d: int = 7
    """Days ahead the S05 event outlook judges, at ``day_plan_hour`` local."""

    surge_occupancy: int = 36
    """Bookings over the night (22:00 local), cancelled kept (no cancel time in the data), that
    make an event day (0.9 x 40 rooms)."""

    surge_early_arrivals: int = 11
    """Arrivals booked ``surge_early_days`` or more ahead that make an event day (p95 of
    normal dev days); with ``surge_occupancy`` it flags exactly the 3 dev event weekends."""

    surge_early_days: float = 14.0
    """Booking lead (days) from which an arrival counts as booked early."""

    checkout_estimate_local: str = "09:45"
    """Planned checkout of a departing room until its checkout event (dev p75; p50 08:45)."""

    snapshot_moved: tuple[tuple[str, float], ...] = (
        ("cop_ratio", 0.05),
        ("filter_dp_pa", 10.0),
        ("airflow_m3h", 300.0),
        ("main_flow_l", 10.0),
        ("soc_kwh", 10.0),
        ("pv_kw", 10.0),
        ("load_kw", 8.0),
        ("price_eur_kwh", 0.03),
        ("outdoor_c", 3.0),
        ("occupied", 2.0),
        ("dirty", 2.0),
        ("temperature_c", 1.0),
        ("setpoint_c", 1.0),
        ("night_water_l", 20.0),
    )
    """Change (in the metric's unit) beyond which a context snapshot lists a metric as moved
    against 15 min ago, 1 h ago or 24 h ago; metrics not listed are never reported."""

    snapshot_ahead_h: float = 6.0
    """Hours ahead a context snapshot lists booked arrivals and departures."""

    cop_ref_intercept: float = 2.78
    """Reference HP1 COP (-) at 0 degC: Theil-Sen fit on the dev year (audit section 8)."""

    cop_ref_slope: float = 0.068
    """Reference HP1 COP gain per degC outdoor (1/K), same fit."""

    cop_ratio_low: float = 0.88
    """24 h COP over reference below this is degradation; normal hours stay above 0.95."""

    cop_min_coverage: float = 0.8
    """Share of a 24 h window's intervals with heat and electricity needed for a verdict."""

    cop_window_h: int = 24
    """Hours of the rolling window over which the heat pump COP is judged."""

    outdoor_min_h: int = 12
    """Hours with an outdoor reading needed for the window's mean outdoor temperature."""

    comfort_deficit_k: float = 1.5
    """Shortfall below the active setpoint (K) for an occupied room to count as cold."""

    technician_horizon_h: float = 24.0
    """Hours ahead within which an on-shift technician is preferred over a contractor."""

    contractor_after_h: float = 24.0
    """Hours after the technician's start with the COP still low before a contractor is ordered."""

    notice_gap_h: float = 12.0
    """Minimum hours between two heating notices to guests in one episode."""

    logistics_rate_h: int = 96
    """Hours over which the waste fill or vending use rate is averaged; several days, since
    the container fills in jumps when floor bins are emptied into it."""

    logistics_buffer_h: float = 12.0
    """Hours beyond the order lead time within which an extra pickup or restock is ordered.
    On dev 96 h and 12 h give 21 of 23 shortages ordered 20 h ahead or more, 1 false order."""

    logistics_event_buffer_h: float = 18.0
    """Hours beyond the order lead time within which an extra restock is ordered when the
    weekend is an event weekend (S05); longer than ``logistics_buffer_h`` since a surge
    empties the machine faster than the trailing rate."""

    logistics_event_nights: int = 36
    """Rooms booked on a Friday or Saturday night, known at Thursday 13:00 local, at or above
    which the weekend counts as an event weekend. Both dev surge weekends had 40 of 40."""

    logistics_dump_pct: float = 35.0
    """Floor bin fill (%) at or above which housekeeping empties it at the daily dump; the
    split is exact in dev (emptied at most 34.2 to 35.0, not emptied at least 35.0)."""

    logistics_dump_local_h: float = 14.5
    """Local hour of the floor bin dump; the 15:00 row is the first to show it."""

    logistics_dump_ratio: float = 0.314
    """Container percent added per percent of floor bin emptied into it (240 L bin into the
    1100 L container, 1.43 times the nominal ratio; dev p10 to p90 1.40 to 1.49)."""

    logistics_pp_per_cleaning: float = 4.5
    """Floor bin percent added by one room cleaning (about 10.8 L; dev 4.5, quickstart 4.1)."""

    logistics_background_pp_per_h: float = 0.094
    """Container percent per hour that arrives without a floor bin dump (24.8 L a day)."""

    logistics_clean_first_h: int = 8
    """First local hour row in which room cleanings add waste; all of it arrives 07 to 16."""

    logistics_clean_hours: int = 8
    """Number of local hour rows over which a day's departures are cleaned."""

    logistics_cap_pct: float = 100.0
    """Container fill (%) that counts as overflowing."""

    logistics_hold_local_h: float = 13.5
    """Local hour on the overflow day at which the dump is held, one hour before the dump."""

    logistics_held_max_pct: float = 90.0
    """Fill (%) a held floor bin may reach before the pickup; above it the hold is unsafe and
    an extra pickup is ordered instead."""

    logistics_robot_start_local_h: float = 7.25
    """Local hour on the pickup day from which a robot carries held bins to the container."""

    logistics_robot_end_local_h: float = 11.0
    """Local hour by which the bin-runs end."""

    logistics_bin_run_min: int = 20
    """Minutes a robot needs to carry one floor bin (building.json: about 20 min)."""

    logistics_gap_h: int = 3
    """Consecutive blank hours of a logistics series before it is flagged."""

    logistics_jump_pp: float = 2.0
    """Hourly change (percent or units) with no event to explain it that counts as a jump."""

    logistics_stuck_h: int = 48
    """Hours a vending or container reading may stay exactly flat; the longest dev run is 32."""

    price_peaks: int = 3
    """Number of dearest published price hours named in the energy plan."""

    preheat_lead_h: float = 3.0
    """Hours (h) before a booked arrival that a vacant room below the comfort band is
    pre-heated (S06). Winter warm-up takes a median 2.75-3.25 h (dev)."""

    preheat_setpoint_c: float = 21.0
    """Setpoint (degC) a vacant room is pre-heated to before an arrival (S06), one kelvin
    above the bottom of the comfort band."""

    s03_backoff_h: float = 24.0
    """Hours (h) without an HP1 cop_low verdict before S06 resumes pre-heat and load shifting;
    also the gap that splits S03 episodes for the back-off notice."""

    tariff_due_local_h: float = 14.0
    """Local hour (h) by which tomorrow's tariff must be published (it comes at 13:00 local);
    later counts as a stale energy input (S06)."""

    forecast_stale_h: float = 30.0
    """Age (h) of the latest weather forecast at 07:00 local above which it is stale (S06);
    forecasts are issued daily at 06:00 local."""

    energy_balance_tol_kw: float = 1.0
    """Tolerance (kW) on the energy balance and the BAT1 limits before an energy.csv row
    counts as broken (S06); the dev balance residual is at most 0.001 kW."""

    repair_verify_h: float = 24.0
    """Hours (h) after a leak repair completes in which a new leak signal means the repair did
    not hold; a blocked room is released after it (verify.py). One night falls inside it."""

    promise_grace_min: float = 15.0
    """Minutes (min) after a promised ready time before a room that is not ready gets the
    guest a late notice (verify.py); one telemetry interval."""

    room_reading_h: float = 1.0
    """Look-back (h) for a room's latest temperature, setpoint and window reading, and for
    guests newly in comfort deficit (bus lane s03); readings come every 15 min."""

    cold_outlook_h: float = 24.0
    """Forecast horizon (h) the s03 lane scans for the coldest hour at each forecast issue."""

    room_heat_verify_h: float = 2.0
    """Hours (h) a boosted or flagged room has to reach the comfort band before the room's
    heating is inspected (s03 verification plan)."""

    hot_day_start_h: int = 8
    """Local wall-clock hour (h, Europe/Vienna, inclusive) from which a hot room's open window
    gets the advice to close it while it is hotter outside (bus lane s03). A design value,
    not fitted; at night the reply only says that opening the window helps."""

    hot_day_end_h: int = 20
    """Local wall-clock hour (h, Europe/Vienna, exclusive) at which the daytime window advice
    of ``hot_day_start_h`` ends."""

    preheat_forecast_c: float = -5.0
    """Coldest forecast hour (degC) in the next 24 h below which a degraded HP1 gets a
    pre-heat plan (bus lane s03). A design value, not fitted: every quickstart HP1 episode
    hour had a 24 h mean outdoor below it."""

    battery_history_d: int = 14
    """Days of history whose same-time-of-day mean is the PV and load forecast of the S06
    battery day plan (UC1). A plain, defensible forecast: no weather model."""

    battery_min_history_d: int = 7
    """Days of energy history needed before a battery day plan is made; before it, none."""

    battery_soc_step_kwh: float = 1.0
    """SOC grid step (kWh) of the battery day plan's dynamic programme; every action moves the
    SOC by whole steps, at the AC power that move needs, up to the BAT1 power limits."""

    battery_terminal_share: float = 0.9
    """A kWh left in BAT1 at the end of the plan is worth this share of the round-trip
    efficiency times the median horizon price (EUR per kWh); below 1, so the plan does not
    hoard energy it may never use."""

    battery_night_start_h: int = 22
    """Local hour from which a battery charge counts as a night charge."""

    battery_night_end_h: int = 6
    """Local hour (exclusive) at which the night of ``battery_night_start_h`` ends."""

    ahu_fan_dp_slope_pa_per_kw: float = 119.2
    """Slope (Pa per kW) of the AHU1 fan-implied filter dp, a least-squares fit on the dev year in
    day mode with the fan below 4.6 kW and airflow at least 4000 m3/h (R2 0.984, residual sd
    4.7 Pa). Frozen: never refit on the window judged (bus lane s04)."""

    ahu_fan_dp_offset_pa: float = -295.9
    """Offset (Pa) of the same fit: dp = slope * fan_kw + offset."""

    ahu_fan_ceiling_kw: float = 5.3
    """Fan power (kW) from which the AHU1 fan is at its ceiling (5.4 kW): the fan-implied dp no
    longer holds and a drop in airflow shows the loss instead."""

    ahu_day_start_hour: int = 6
    """First local hour (Europe/Vienna) of AHU1 day mode; dp and airflow switch at local 06."""

    ahu_day_end_hour: int = 22
    """Last local hour of AHU1 day mode, inclusive; they switch back at local 23. Night and
    summer flush modes are excluded by the clock, never by fan power."""

    ahu_window_h: int = 24
    """Hours of each of the two consecutive windows whose day medians give the loading rate."""

    ahu_min_day_readings: int = 8
    """Day-mode readings (count) needed in each rate window; fewer is no verdict."""

    ahu_rate_fast_pa_per_day: float = 10.0
    """Loading rate (Pa per 24 h) from which the AHU1 filter is clogging: about three times the
    normal maximum (3.6 on dp, 4.8 on the fan) and a third of the clog rate (28-33)."""

    ahu_due_dp_pa: float = 190.0
    """24 h median day dp (Pa) at which a preventive filter change is planned; humans ordered at
    180 and 215 Pa and the airflow knee is 245 Pa."""

    ahu_due_lead_d: float = 7.0
    """Days from the plan to the due date of a preventive filter change; all preventive work
    orders are scheduled exactly 168 h after their creation."""

    ahu_reset_dp_pa: float = 120.0
    """Day median dp (Pa) below which, with the fan at most ``ahu_reset_fan_kw``, a filter change
    is observed; resets reach 88-92 Pa. A blank dp is never a reset."""

    ahu_reset_fan_kw: float = 3.4
    """Day median fan power (kW) at or below which a reset is observed."""

    ahu_reset_window_h: float = 3.0
    """Hours (h) over which the reset is observed; completed work orders reset dp exactly at
    their scheduled time."""

    ahu_reset_min_readings: int = 4
    """Day-mode dp readings (count) needed in the reset window."""

    ahu_wo_grace_h: float = 3.0
    """Hours (h) after a work order's scheduled time at which a missing reset means the order was
    postponed; completed orders finish 1.5-3.5 h after it."""

    ahu_wo_wait_h: float = 24.0
    """Hours (h) after the grace period a postponement check waits for enough day readings."""

    ahu_tech_job_min: int = 240
    """Minutes (min) the filter job holds TECH_1; the two in-house filter jobs took 3.5 h and
    3.75 h."""

    ahu_tech_horizon_h: float = 24.0
    """Hours ahead within which TECH_1's believed shift is used for a clog; else a contractor."""

    ahu_tech_search_d: float = 14.0
    """Days after the due date within which a believed TECH_1 shift is searched for a preventive
    change; with none, a contractor is ordered."""

    ahu_s03_guard_h: float = 48.0
    """Hours (h) after an s03 ``cop_low`` check during which s04 claims no technician."""

    ahu_escalate_after_h: float = 24.0
    """Hours (h) after TECH_1's start with no reset before the contractor is ordered."""

    ahu_airflow_low_m3h: float = 3900.0
    """Day airflow (m3/h) below which, with the fan at its ceiling for a full hour, airflow is
    lost; the normal day minimum is 4099."""

    ahu_gap_h: float = 4.0
    """Hours (h) a dp, airflow or HP1 reading is blank before it is a gap; every dev blank run is
    at least 2 h."""

    ahu_gap_inspect_h: float = 24.0
    """Hours (h) of a gap that is neither degradation nor recovery before the sensor is
    inspected."""

    ahu_fault_residual_pa: float = 25.0
    """Gap (Pa) between dp and the fan-implied dp that, held for ``ahu_fault_h``, is an implausible
    dp; the dev maximum is 20.6 Pa."""

    ahu_fault_h: float = 2.0
    """Hours (h) the residual is held before an implausible dp is flagged."""

    ahu_stuck_intervals: int = 8
    """Identical dp readings (count) in a row, while the fan changes, that are a stuck dp; the
    longest run in dev is 2."""

    ahu_alarm_quiet_h: float = 24.0
    """Hours (h) before a raised AHU1 BMS alarm within which an s04 episode must have opened,
    else the alarm is unmatched."""


@dataclass(frozen=True)
class Settings:
    """Run configuration. Construct it from the CLI; there are no path defaults."""

    data_root: Path
    out_dir: Path
    local_tz: str = "Europe/Vienna"
    interval_minutes: int = 15
    night: NightWindow = field(default_factory=NightWindow)
    thresholds: AuditThresholds = field(default_factory=AuditThresholds)
    detect: DetectorSettings = field(default_factory=DetectorSettings)
    figure_dpi: int = 110
    llm_cache: Path | None = None
    lanes: tuple[str, ...] = ("s03",)
    follow_ups: bool = True
    supervisor: bool = True

    @property
    def figures_dir(self) -> Path:
        """Directory for generated figures, inside the output directory."""
        return self.out_dir / "figures"
