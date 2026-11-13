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

    leak_floor_l_per_15min: float = 0.5
    """A night whose every 15-min room volume (L) is at least this has continuous flow."""

    leak_persist_nights: int = 2
    """Consecutive continuous-flow nights (count) before a slow leak is acted on."""

    leak_technician_nights: int = 3
    """Consecutive continuous-flow nights (count) before a technician is requested."""

    burst_night_volume_l: float = 250.0
    """Night-window volume (L) in one room that is a burst; normal nights reach <= 180 L."""

    gap_night_l_per_h: float = 10.0
    """Median night gap main minus rooms (L/h) above which a pipe leak is suspected."""

    gap_min_coverage: float = 0.75
    """Share of a night's intervals with the main and every room meter needed for a gap verdict."""

    burst_flow_l_per_15min: float = 10.0
    """Room volume (L per 15 min) that, held over ``burst_flow_intervals``, is a burst at any
    hour; the highest 2 h sustained room flow without a leak is 2.8 L (dev)."""

    burst_flow_intervals: int = 8
    """Consecutive intervals (count) at ``burst_flow_l_per_15min`` or more for a burst."""

    next_night_h: float = 26.0
    """Hours between two night verdicts still counted as consecutive nights (DST nights)."""

    meter_reset_drop_l: float = 1000.0
    """A meter drop larger than this (L) is a reset or exchange; the interval is missing."""

    outage_min_intervals: int = 2
    """Consecutive blank intervals (count) on a whole floor that make a telemetry outage."""

    episode_quiet_h: float = 48.0
    """Hours without a new signal after which an episode is closed."""

    clean_room_min: float = 60.0
    """Minutes one human room cleaning is planned to take (audit p90; median 45)."""

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

    price_peaks: int = 3
    """Number of dearest published price hours named in the energy plan."""


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

    @property
    def figures_dir(self) -> Path:
        """Directory for generated figures, inside the output directory."""
        return self.out_dir / "figures"
