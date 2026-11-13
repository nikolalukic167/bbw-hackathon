"""S04 trigger lane on the bus (docs/BUS.md): the AHU1 filter cascade and equipment data gaps.

Lane ``s04``, issue ``ventilation_maintenance``. Every row is timed when its inputs were
known (ADR-002): the lane replays the dataset in hourly checks and each check reads
equipment rows with ``timestamp_utc <= t`` only. Day mode is the local clock, never the
fan power.

The loading rate is the day median dp of the last 24 h minus that of the 24 h before, on
dp, or on the fan-implied dp (``dp = slope * fan_kw + offset``, frozen dev fit) when dp is
blank. One open AHU1 loading episode at a time, closed only on an observed reset (day
median dp and fan low for ``ahu_reset_window_h``), never by a BMS ``cleared``.

- ``dp_fast_rise`` (``filter_clogging``): rate at or above ``ahu_rate_fast_pa_per_day``. One
  flag plus a service: TECH_1 with a claim if a believed shift of his starts within
  ``ahu_tech_horizon_h`` and s03 has no ``cop_low`` in the last ``ahu_s03_guard_h``, else the
  contractor (``order_service``); a known open AHU1 work order defers instead. Once, after
  ``ahu_escalate_after_h`` from TECH_1's start with no reset: the contractor.
- ``airflow_loss``: for a full day-mode hour the fan is at its ceiling and airflow low. One
  flag in the open episode; it opens one (with its service) if none is open.
- ``dp_due`` (``filter_loading``): 24 h median day dp at or above ``ahu_due_dp_pa`` with no
  episode open. A flag names the due date; TECH_1 is dispatched at the start of his first
  believed shift on or after it (a claim from that time), else the contractor.
- ``work_order_missed`` (``work_order_postponed``): no reset ``ahu_wo_grace_h`` after an AHU1
  work order's scheduled time. One flag, then a preventive change is planned again.
- ``sensor_gap`` (``sensor_gap``): AHU1 dp or airflow, or an HP1 metric, blank for
  ``ahu_gap_h``. A gap with loading or airflow loss as evidence joins the loading episode
  and writes no row; otherwise one flag, and one inspection after ``ahu_gap_inspect_h``.
- ``sensor_fault`` (``sensor_fault``): dp far from the fan-implied dp, or a stuck dp.
- ``bms_unmatched`` (``bms_unconfirmed``): a raised AHU1 alarm with no s04 episode in the last
  ``ahu_alarm_quiet_h``. Other BMS alarms are only cited as evidence.

HP1 appears only in gap flags: S03 owns HP1 service. The contractor is named in the reason
only (``EXTERNAL_HVAC`` is not a building.json ID). Proposals never carry ``autonomy`` or
approval flags; the arbiter's policy sets them.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from plato40 import llm_guard
from plato40.bus import LANES, VERSION
from plato40.detect import heating
from plato40.detect.grid import grid, local_time, stamp
from plato40.load.derive import equipment_numeric
from plato40.load.reader import Dataset
from plato40.settings import Settings
from plato40.staff import Roster

LANE = "s04"
ISSUE = str(LANES[LANE]["issue_type"])
Row = dict[str, Any]
Floats = npt.NDArray[np.float64]
HOUR = pd.Timedelta(hours=1)
SIM = "(simulated)"
CONTRACTOR = "EXTERNAL_HVAC"
HP1_SERIES = ("HP1/heat_output_kw", "HP1/electric_kw", "HP1/supply_temp_c")


def _json(x: Any) -> Any:
    """Return ``x`` as JSON: UTC times as ``...Z``, numpy scalars as Python, floats rounded."""
    if x is pd.NaT or (isinstance(x, float) and np.isnan(x)):
        return None
    if isinstance(x, pd.Timestamp):
        return stamp(x)
    if isinstance(x, Mapping):
        return {str(k): _json(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_json(v) for v in x]
    if isinstance(x, np.generic):
        return _json(x.item())
    if isinstance(x, float):
        return round(x, 4)
    return x


def _tag(t: pd.Timestamp) -> str:
    return f"{t.tz_convert('UTC'):%Y%m%dT%H%M}Z"


@dataclass
class _Board:
    """The rows this lane writes, with per-episode sequence numbers for their IDs."""

    rows: list[Row] = field(default_factory=list)
    seq: Counter[str] = field(default_factory=Counter)

    def _id(self, eid: str, what: str) -> str:
        self.seq[f"{eid}/{what}"] += 1
        return f"{LANE}-{eid.removeprefix('EP-')}-{what}{self.seq[f'{eid}/{what}']:02d}"

    def signal(
        self,
        eid: str,
        t: pd.Timestamp,
        kind: str,
        assets: Iterable[str],
        evidence: Iterable[str],
        detail: str,
        facts: Mapping[str, Any],
        cause: str,
    ) -> Row:
        row: Row = {
            "v": VERSION,
            "row": "signal",
            "lane": LANE,
            "id": self._id(eid, "sig"),
            "t_known": stamp(t),
            "issue_type": ISSUE,
            "kind": kind,
            "assets": list(assets),
            "evidence": list(evidence),
            "detail": detail,
            "facts": _json(facts),
            "cause": cause,
        }
        self.rows.append(row)
        return row

    def propose(
        self,
        eid: str,
        t: pd.Timestamp,
        sig: Row,
        intent: str,
        action: str,
        assets: Iterable[str],
        priority: str,
        reason: str,
        **extra: Any,
    ) -> Row:
        row: Row = {
            "v": VERSION,
            "row": "proposal",
            "lane": LANE,
            "id": self._id(eid, "prop"),
            "t": stamp(t),
            "source": "rules",
            "episode_id": eid,
            "signal_ids": [sig["id"]],
            "intent": intent,
            "action": action,
            "assets": list(assets),
            "priority": priority,
            "evidence": list(sig["evidence"]),
            "reason": reason,
            "cause": sig["cause"],
        }
        row.update({k: v for k, v in extra.items() if v is not None})
        self.rows.append(row)
        return row


@dataclass
class _Episode:
    """One open AHU1 loading episode: a preventive plan or a clog."""

    eid: str
    kind: str
    opened: pd.Timestamp
    sig: Row
    deadline: pd.Timestamp | None = None
    escalated: bool = False
    air_flagged: bool = False
    due: pd.Timestamp | None = None
    dispatch_at: pd.Timestamp | None = None
    deferred_wo: str | None = None
    contractor: bool = False


@dataclass
class _Gap:
    """One blank run of a series: whether it was flagged and whether it was inspected."""

    eid: str
    sig: Row | None
    asset: str
    series: str
    inspected: bool = False


def _median(a: Floats) -> tuple[float, int]:
    """Return the median of the non-missing values and their count (NaN for none)."""
    ok = a[~np.isnan(a)]
    return (float(np.median(ok)) if len(ok) else float("nan")), len(ok)


class _Lane:
    """Builds the s04 rows of one dataset in time order."""

    def __init__(self, ds: Dataset, settings: Settings) -> None:
        self.ds, self.settings, self.th = ds, settings, settings.detect
        self.board = _Board()
        self.roster = Roster(ds)
        self.tz = settings.local_tz
        eq = equipment_numeric(ds)
        self.idx = grid(ds, settings)
        eq = eq.reindex(self.idx)
        th = self.th
        local = self.idx.tz_convert(self.tz)
        self.day = np.asarray(
            (local.hour >= th.ahu_day_start_hour) & (local.hour <= th.ahu_day_end_hour)
        )
        self.dp: Floats = eq["AHU1/filter_dp_pa"].to_numpy(dtype=float)
        self.fan: Floats = eq["AHU1/fan_kw"].to_numpy(dtype=float)
        self.air: Floats = eq["AHU1/airflow_m3h"].to_numpy(dtype=float)
        self.hp1 = {s: eq[s].to_numpy(dtype=float) for s in HP1_SERIES}
        self.dp_day = np.where(self.day, self.dp, np.nan)
        implied = th.ahu_fan_dp_slope_pa_per_kw * self.fan + th.ahu_fan_dp_offset_pa
        self.dp_fan = np.where(self.day & (self.fan < th.ahu_fan_ceiling_kw), implied, np.nan)
        self.implied = implied
        self.per_h = 60 // settings.interval_minutes
        self.wo = ds.table("work_orders.csv")
        self.wo = self.wo.loc[self.wo["asset_id"] == "AHU1"].sort_values(
            "created_utc", kind="stable"
        )
        ev = ds.events
        al = ev.loc[
            (ev["type"] == "bms_alarm") & (ev["asset_id"] == "AHU1") & (ev["state"] == "raised")
        ]
        self.alarms = [
            (pd.Timestamp(r["timestamp_utc"]), str(r["event_id"]), str(r["alarm_code"]))
            for _, r in al.sort_values(["timestamp_utc", "event_id"]).iterrows()
        ]
        self.alarm_seen = 0
        self.ep: _Episode | None = None
        self.gaps: dict[tuple[str, int], _Gap] = {}
        self.cop_hours: set[pd.Timestamp] | None = None
        self.last_active: pd.Timestamp | None = None
        self.fault_last: pd.Timestamp | None = None
        self.wo_done: set[str] = set()
        self.last_reset: pd.Timestamp | None = None
        self.sent: list[pd.Timestamp] = []
        self.wo_pending: dict[str, pd.Timestamp] = {}

    # ---------------------------------------------------------------- facts known at t

    def _loc(self, t: pd.Timestamp, ref: pd.Timestamp) -> str:
        return local_time(t, ref, self.tz)

    def _windows(self, a: Floats, k: int) -> tuple[Floats, Floats]:
        n = self.th.ahu_window_h * self.per_h
        return a[k - n + 1 : k + 1], a[k - 2 * n + 1 : k - n + 1]

    def _level(self, k: int) -> tuple[float, str] | None:
        """Return the 24 h day median dp (or fan-implied dp when dp is blank) and its source."""
        for src, a in (("dp", self.dp_day), ("fan", self.dp_fan)):
            m, n = _median(self._windows(a, k)[0])
            if n >= self.th.ahu_min_day_readings:
                return m, src
        return None

    def _rate(self, k: int) -> tuple[float, str] | None:
        """Return the loading rate (Pa per 24 h) on dp, else on the fan-implied dp."""
        for src, a in (("dp", self.dp_day), ("fan", self.dp_fan)):
            now, before = self._windows(a, k)
            m1, n1 = _median(now)
            m0, n0 = _median(before)
            if min(n1, n0) >= self.th.ahu_min_day_readings:
                return m1 - m0, src
        return None

    def _reset(self, k: int, since: int | None = None) -> bool | None:
        """Return whether a filter change is observed in the last window; None if unreadable."""
        th = self.th
        n = int(th.ahu_reset_window_h * self.per_h)
        lo = k - n + 1 if since is None else max(k - n + 1, since)
        dp, dp_n = _median(self.dp_day[lo : k + 1])
        fan, _ = _median(np.where(self.day, self.fan, np.nan)[lo : k + 1])
        if dp_n < th.ahu_reset_min_readings or np.isnan(fan):
            return None
        return dp < th.ahu_reset_dp_pa and fan <= th.ahu_reset_fan_kw

    def _ceiling(self, k: int) -> bool:
        """Whether the last full hour is day mode with the fan at its ceiling and airflow low."""
        s = slice(k - self.per_h + 1, k + 1)
        th = self.th
        ok = (
            self.day[s]
            & (self.fan[s] >= th.ahu_fan_ceiling_kw)
            & (self.air[s] < th.ahu_airflow_low_m3h)
        )
        return bool(ok.all())

    def _blank_run(self, a: Floats, k: int) -> int:
        """Return the number of missing readings in a row ending at ``k``."""
        n = 0
        while k - n >= 0 and np.isnan(a[k - n]):
            n += 1
        return n

    def _cop_low_near(self, t: pd.Timestamp) -> bool:
        """Whether s03's heat pump check was ``cop_low`` in the last ``ahu_s03_guard_h``."""
        if self.cop_hours is None:
            self.cop_hours = {s.time for s in heating.signals(self.ds, self.settings)}
        lo = t - pd.Timedelta(hours=self.th.ahu_s03_guard_h)
        return any(lo < h <= t for h in self.cop_hours)

    def _open_wo(self, t: pd.Timestamp) -> pd.Series | None:
        """Return the AHU1 work order known at ``t`` and not yet past its grace period."""
        grace = pd.Timedelta(hours=self.th.ahu_wo_grace_h)
        wo = self.wo
        hit = wo.loc[(wo["created_utc"] <= t) & (wo["scheduled_utc"] + grace > t)]
        return None if hit.empty else hit.iloc[0]

    def _alarm_refs(self, t: pd.Timestamp, since: pd.Timestamp) -> list[str]:
        return [f"events.jsonl:{e}" for a, e, _ in self.alarms if since <= a <= t][-3:]

    def _slot(
        self, t: pd.Timestamp, start: pd.Timestamp, days: float, known: pd.Timestamp
    ) -> tuple[str, pd.Timestamp] | None:
        """Return the earliest believed technician shift from ``start`` that fits the job."""
        th = self.th
        span = pd.Timedelta(days=days)
        job = pd.Timedelta(minutes=th.ahu_tech_job_min)
        shifts = self.roster.shifts(
            "technician", start, start + span + pd.Timedelta(hours=12), known
        )
        fits = [(a, rid) for rid, a, z in shifts if z - a >= job and a - start <= span]
        if not fits:
            return None
        a, rid = min(fits)
        return rid, a

    def _unrepaired(self, t: pd.Timestamp) -> pd.Timestamp | None:
        """Return the time of our last technician dispatch with no AHU1 repair completed since."""
        done = self.wo.loc[self.wo["completed_utc"] <= t, "completed_utc"]
        open_ = [a for a in self.sent if a <= t and not (done > a).any()]
        return open_[-1] if open_ else None

    def _contractor_note(self, t: pd.Timestamp) -> str:
        wo = self.ds.table("work_orders.csv")
        done = wo.loc[(wo["assigned_to"] == CONTRACTOR) & (wo["completed_utc"] <= t)]
        if done.empty:
            return f"contractor {CONTRACTOR} (not a building.json resource)"
        lead = (done["scheduled_utc"] - done["created_utc"]).dt.total_seconds() / 3600
        return (
            f"contractor {CONTRACTOR} (not a building.json resource); its {len(done)} earlier "
            f"orders were visited {lead.min():.1f}-{lead.max():.1f} h after they were placed"
        )

    def _evidence(
        self, t: pd.Timestamp, series: str = "filter_dp_pa,fan_kw,airflow_m3h"
    ) -> list[str]:
        span = pd.Timedelta(hours=2 * self.th.ahu_window_h)
        return [f"equipment.csv:AHU1 {series}:{stamp(t - span)}..{stamp(t)}"]

    # ---------------------------------------------------------------- services

    def _service(self, t: pd.Timestamp, eid: str, sig: Row, ep: _Episode, why: str) -> None:
        """Propose the clog service: a deferral, TECH_1 with a claim, or the contractor."""
        th = self.th
        open_wo = self._open_wo(t)
        if open_wo is not None:
            ep.deferred_wo = str(open_wo["work_order_id"])
            self._defer(eid, t, sig, open_wo, "high")
            return
        blocked = self._unrepaired(t)
        slot = (
            None
            if blocked is not None or self._cop_low_near(t)
            else self._slot(t, t, th.ahu_tech_horizon_h / 24, t)
        )
        if slot is not None:
            who, start = slot
            start = max(start, t)
            when = "now" if start <= t else "from " + self._loc(start, t)
            ep.deadline = start + pd.Timedelta(hours=th.ahu_escalate_after_h)
            self.sent.append(t)
            self.board.propose(
                eid,
                t,
                sig,
                "service_filter",
                "dispatch_technician",
                ["AHU1"],
                "high",
                f"Change the AHU1 filters: {why}; technician {who} on shift {when}, job "
                f"{th.ahu_tech_job_min} min.",
                resource_id=who,
                claims=[
                    {
                        "res": "technician",
                        "id": who,
                        "from": stamp(start),
                        "min": th.ahu_tech_job_min,
                    }
                ],
                confidence=0.8,
                expected_outcome=f"Filters changed, dp and fan back to normal {SIM}.",
                verification_plan=self._verify(),
            )
            return
        if blocked is not None:
            reason = f"the technician was sent {self._loc(blocked, t)} and no repair is completed"
        elif self._cop_low_near(t):
            reason = f"s03 has a heat pump episode in the last {th.ahu_s03_guard_h:.0f} h"
        else:
            reason = f"no technician shift starts within {th.ahu_tech_horizon_h:.0f} h"
        self.board.propose(
            eid,
            t,
            sig,
            "service_filter",
            "order_service",
            ["AHU1"],
            "high",
            f"Order a filter change by a {self._contractor_note(t)}: {why}; {reason}.",
            confidence=0.7,
            expected_outcome=f"Filters changed by the contractor, dp back to normal {SIM}.",
            verification_plan=self._verify(),
        )
        ep.contractor = True

    def _verify(self) -> str:
        th = self.th
        return (
            f"Day median dp below {th.ahu_reset_dp_pa:.0f} Pa and fan at most "
            f"{th.ahu_reset_fan_kw:.1f} kW within {th.ahu_reset_window_h:.0f} h of the service "
            "start; otherwise the contractor is ordered."
        )

    def _defer(self, eid: str, t: pd.Timestamp, sig: Row, wo: pd.Series, prio: str) -> None:
        self.board.propose(
            eid,
            t,
            sig,
            "defer_to_work_order",
            "defer_action",
            ["AHU1"],
            prio,
            f"No second order: work order {wo['work_order_id']} for AHU1 is open, scheduled "
            f"{self._loc(pd.Timestamp(wo['scheduled_utc']), t)}.",
            confidence=0.8,
            expected_outcome=f"The open work order changes the filters {SIM}.",
            verification_plan=(
                "Reset at the work order's scheduled time; a flag follows "
                f"{self.th.ahu_wo_grace_h:.0f} h after it with no reset."
            ),
        )

    # ---------------------------------------------------------------- loading episodes

    def _clog(self, t: pd.Timestamp, k: int, trigger: str, rate: tuple[float, str] | None) -> None:
        """Open a clog episode: the signal, the flag and the service."""
        th = self.th
        eid = f"EP-{ISSUE}-AHU1-{_tag(t)}"
        level = self._level(k)
        alarms = self._alarm_refs(t, t - pd.Timedelta(hours=2 * th.ahu_window_h))
        dp_blank = self._blank_run(self.dp, k) / self.per_h
        if trigger == "rate" and rate is not None:
            kind = "dp_fast_rise"
            src = "dp" if rate[1] == "dp" else "the fan-implied dp (dp is blank)"
            detail = (
                f"AHU1 filter loading {rate[0]:.1f} Pa per 24 h on {src}, from "
                f"{th.ahu_rate_fast_pa_per_day:.0f}: clogging, not a sensor gap"
            )
            why = f"loading {rate[0]:.1f} Pa per 24 h"
            intent = "flag_filter_clogging"
            facts: dict[str, Any] = {"rate": rate[0], "source": rate[1]}
        else:
            kind = "airflow_loss"
            detail = (
                f"AHU1 fan at its ceiling ({th.ahu_fan_ceiling_kw:.1f} kW) with day airflow "
                f"below {th.ahu_airflow_low_m3h:.0f} m3/h for a full hour"
            )
            why = f"airflow lost, below {th.ahu_airflow_low_m3h:.0f} m3/h at the fan ceiling"
            intent = "flag_airflow_loss"
            facts = {"airflow": float(self.air[k]), "fan_kw": float(self.fan[k])}
        facts |= {
            "level": None if level is None else level[0],
            "dp_blank_h": dp_blank,
            "alarms": alarms,
        }
        sig = self.board.signal(
            eid, t, kind, ("AHU1",), self._evidence(t) + alarms, detail, facts, "filter_clogging"
        )
        ep = _Episode(eid, "clog", t, sig)
        ep.air_flagged = kind == "airflow_loss"
        self.ep = ep
        gap = (
            f" The dp transmitter has been blank for {dp_blank:.1f} h; the fan shows the loading."
            if dp_blank >= th.ahu_gap_h
            else ""
        )
        self.board.propose(
            eid,
            t,
            sig,
            intent,
            "flag_anomaly",
            ["AHU1"],
            "high",
            f"AHU1 filters are clogging: {detail}.{gap} The BMS dp alarm is blind while dp is "
            "blank.",
            confidence=0.8,
            expected_outcome=f"Operations see the clogging before the airflow drops {SIM}.",
            verification_plan=self._verify(),
        )
        self._service(t, eid, sig, ep, why)

    def _airflow(self, t: pd.Timestamp, k: int, ep: _Episode) -> None:
        th = self.th
        alarms = self._alarm_refs(t, ep.opened)
        sig = self.board.signal(
            ep.eid,
            t,
            "airflow_loss",
            ("AHU1",),
            self._evidence(t) + alarms,
            f"AHU1 fan at its ceiling ({th.ahu_fan_ceiling_kw:.1f} kW) with day airflow below "
            f"{th.ahu_airflow_low_m3h:.0f} m3/h for a full hour",
            {"airflow": float(self.air[k]), "alarms": alarms},
            "filter_clogging",
        )
        ep.air_flagged = True
        self.board.propose(
            ep.eid,
            t,
            sig,
            "flag_airflow_loss",
            "flag_anomaly",
            ["AHU1"],
            "high",
            f"AHU1 airflow is lost: {sig['detail']}; the filters are the cause in this episode.",
            confidence=0.8,
            expected_outcome=f"Operations see the airflow loss {SIM}.",
            verification_plan=(
                f"Day airflow back above {th.ahu_airflow_low_m3h:.0f} m3/h within "
                f"{th.ahu_reset_window_h:.0f} h of the service."
            ),
        )

    def _escalate(self, t: pd.Timestamp, k: int, ep: _Episode) -> None:
        th = self.th
        ep.escalated = True
        open_wo = self._open_wo(t)
        alarms = self._alarm_refs(t, ep.opened)
        level = self._level(k)
        shown = "no reading" if level is None else f"day dp {level[0]:.0f} Pa"
        sig = self.board.signal(
            ep.eid,
            t,
            "dp_fast_rise",
            ("AHU1",),
            self._evidence(t) + alarms,
            f"no reset {th.ahu_escalate_after_h:.0f} h after the technician's start; {shown}",
            {"level": None if level is None else level[0], "alarms": alarms},
            "filter_clogging",
        )
        if open_wo is not None:
            self._defer(ep.eid, t, sig, open_wo, "high")
            return
        self.board.propose(
            ep.eid,
            t,
            sig,
            "escalate_contractor",
            "order_service",
            ["AHU1"],
            "high",
            f"Order a filter change by a {self._contractor_note(t)}: the filters are not changed "
            f"{th.ahu_escalate_after_h:.0f} h after the technician's start ({shown}).",
            confidence=0.7,
            expected_outcome=f"Filters changed by the contractor {SIM}.",
            verification_plan=self._verify(),
        )

    def _plan(self, t: pd.Timestamp, k: int, level: tuple[float, str]) -> None:
        """Plan a preventive change: the flag now, TECH_1's dispatch at his first shift."""
        th = self.th
        eid = f"EP-{ISSUE}-AHU1-{_tag(t)}"
        due = t + pd.Timedelta(days=th.ahu_due_lead_d)
        src = "dp" if level[1] == "dp" else "the fan-implied dp"
        sig = self.board.signal(
            eid,
            t,
            "dp_due",
            ("AHU1",),
            self._evidence(t),
            f"24 h day median {src} {level[0]:.0f} Pa, from {th.ahu_due_dp_pa:.0f} Pa",
            {"level": level[0], "source": level[1], "due": due},
            "filter_loading",
        )
        ep = _Episode(eid, "plan", t, sig, due=due)
        self.ep = ep
        open_wo = self._open_wo(t)
        if open_wo is not None:
            ep.deferred_wo = str(open_wo["work_order_id"])
            self._defer(eid, t, sig, open_wo, "medium")
            return
        self.board.propose(
            eid,
            t,
            sig,
            "plan_filter_change",
            "flag_anomaly",
            ["AHU1"],
            "medium",
            f"AHU1 filters are loaded ({sig['detail']}); change them by {self._loc(due, t)}.",
            confidence=0.7,
            expected_outcome=f"A planned change before airflow drops {SIM}.",
            verification_plan=self._verify(),
        )
        self._schedule(ep, t)

    def _schedule(self, ep: _Episode, t: pd.Timestamp) -> None:
        """Set when the preventive dispatch is proposed: TECH_1's first believed shift."""
        th = self.th
        assert ep.due is not None
        slot = self._slot(t, ep.due, th.ahu_tech_search_d, t)
        ep.dispatch_at = ep.due if slot is None else slot[1]
        ep.contractor = slot is None

    def _dispatch(self, t: pd.Timestamp, ep: _Episode) -> None:
        """Propose the preventive change at the planned time, re-reading shifts known at ``t``."""
        th = self.th
        assert ep.due is not None
        ep.dispatch_at = None
        open_wo = self._open_wo(t)
        if open_wo is not None:
            if ep.deferred_wo != str(open_wo["work_order_id"]):
                ep.deferred_wo = str(open_wo["work_order_id"])
                self._defer(ep.eid, t, ep.sig, open_wo, "medium")
            return
        slot = None
        if not ep.contractor and self._unrepaired(t) is None and not self._cop_low_near(t):
            slot = self._slot(t, t, (ep.due - t) / pd.Timedelta(days=1) + th.ahu_tech_search_d, t)
        if slot is not None and slot[1] > t:
            ep.dispatch_at = slot[1]
            return
        if slot is None:
            self.board.propose(
                ep.eid,
                t,
                ep.sig,
                "change_filter",
                "order_service",
                ["AHU1"],
                "medium",
                f"Order the planned filter change from a {self._contractor_note(t)}: no technician "
                f"shift fits within {th.ahu_tech_search_d:.0f} d of the due date.",
                confidence=0.7,
                expected_outcome=f"Filters changed by the contractor {SIM}.",
                verification_plan=self._verify(),
            )
            return
        who = slot[0]
        self.sent.append(t)
        self.board.propose(
            ep.eid,
            t,
            ep.sig,
            "change_filter",
            "dispatch_technician",
            ["AHU1"],
            "medium",
            f"Change the AHU1 filters as planned: technician {who} on shift now, job "
            f"{th.ahu_tech_job_min} min.",
            resource_id=who,
            claims=[{"res": "technician", "id": who, "from": stamp(t), "min": th.ahu_tech_job_min}],
            confidence=0.8,
            expected_outcome=f"Filters changed, dp and fan back to normal {SIM}.",
            verification_plan=self._verify(),
        )

    # ---------------------------------------------------------------- work orders

    def _work_orders(self, t: pd.Timestamp, k: int) -> None:
        th = self.th
        grace = pd.Timedelta(hours=th.ahu_wo_grace_h)
        wait = pd.Timedelta(hours=th.ahu_wo_wait_h)
        for _, w in self.wo.iterrows():
            wid, due = str(w["work_order_id"]), pd.Timestamp(w["scheduled_utc"])
            if wid in self.wo_done or w["created_utc"] > t or due + grace > t:
                continue
            if t > due + grace + wait:
                self.wo_done.add(wid)
                continue
            since = int(self.idx.searchsorted(due, side="right"))
            reset = self._reset(k, since)
            if reset is None:
                continue
            self.wo_done.add(wid)
            if reset:
                continue
            self._missed(t, w, due)

    def _missed(self, t: pd.Timestamp, w: pd.Series, due: pd.Timestamp) -> None:
        th = self.th
        wid = str(w["work_order_id"])
        eid = f"EP-{ISSUE}-AHU1-{_tag(t)}"
        replan = self.ep is None or self.ep.kind == "plan"
        sig = self.board.signal(
            eid,
            t,
            "work_order_missed",
            ("AHU1",),
            [f"work_orders.csv:{wid}", *self._evidence(t)],
            f"work order {wid} was scheduled {self._loc(due, t)} and the dp did not reset "
            f"{th.ahu_wo_grace_h:.0f} h after it",
            {"work_order": wid, "scheduled": due},
            "work_order_postponed",
        )
        new = pd.Timedelta(days=th.ahu_due_lead_d)
        tail = f" A new change is planned by {self._loc(t + new, t)}." if replan else ""
        self.board.propose(
            eid,
            t,
            sig,
            "flag_missed_work_order",
            "flag_anomaly",
            ["AHU1"],
            "medium",
            f"AHU1 work order {wid} was not carried out: {sig['detail']}.{tail}",
            confidence=0.8,
            expected_outcome=f"Operations see the missed work order {SIM}.",
            verification_plan=self._verify(),
        )
        if replan:
            ep = _Episode(eid, "plan", t, sig, due=t + new)
            self.ep = ep
            self._schedule(ep, t)

    # ---------------------------------------------------------------- gaps

    def _gaps(self, t: pd.Timestamp, k: int) -> None:
        th = self.th
        need = int(th.ahu_gap_h * self.per_h)
        inspect_after = int(th.ahu_gap_inspect_h * self.per_h)
        series = [("AHU1", "filter_dp_pa", self.dp), ("AHU1", "airflow_m3h", self.air)]
        series += [("HP1", s.split("/")[1], self.hp1[s]) for s in HP1_SERIES]
        for asset, name, a in series:
            run = self._blank_run(a, k)
            key = (f"{asset}/{name}", k - run + 1)
            if run < need:
                continue
            gap = self.gaps.get(key)
            if gap is None:
                gap = self.gaps[key] = self._open_gap(t, k, asset, name, run)
            if (
                gap.sig is not None
                and run >= inspect_after
                and not gap.inspected
                and asset == "AHU1"
            ):
                gap.inspected = True
                self.board.propose(
                    gap.eid,
                    t,
                    gap.sig,
                    "inspect_dp_sensor",
                    "request_inspection",
                    ["AHU1"],
                    "medium",
                    f"Inspect the AHU1 {name} sensor: blank for {run / self.per_h:.0f} h, still "
                    "neither degradation nor recovery.",
                    confidence=0.7,
                    expected_outcome=f"The sensor is checked {SIM}.",
                    verification_plan=self._gap_verify(),
                )

    def _gap_verify(self) -> str:
        return (
            "The series returns and, on return, dp is within 15 Pa of the fan-implied dp; "
            "an episode is opened if loading or airflow loss shows."
        )

    def _open_gap(self, t: pd.Timestamp, k: int, asset: str, name: str, run: int) -> _Gap:
        th = self.th
        hours = run / self.per_h
        series = f"{asset}/{name}"
        rate = self._rate(k)
        loading = rate is not None and rate[0] >= th.ahu_rate_fast_pa_per_day
        joined = asset == "AHU1" and (
            (self.ep is not None and self.ep.kind == "clog") or loading or self._ceiling(k)
        )
        if joined:
            self.last_active = t
            return _Gap("", None, asset, series)
        eid = f"EP-{ISSUE}-{asset}-{_tag(t)}"
        shown = "no fan-implied loading" if asset == "AHU1" else "heat pump gap only"
        sig = self.board.signal(
            eid,
            t,
            "sensor_gap",
            (asset,),
            self._evidence(t, name)
            if asset == "AHU1"
            else [
                f"equipment.csv:{asset} {name}:{stamp(t - pd.Timedelta(hours=hours))}..{stamp(t)}"
            ],
            f"{asset} {name} blank for {hours:.2f} h ({shown})",
            {"series": series, "blank_h": hours, "rate": None if rate is None else rate[0]},
            "sensor_gap",
        )
        self.last_active = t
        self.board.propose(
            eid,
            t,
            sig,
            "flag_sensor_gap",
            "flag_anomaly",
            [asset],
            "medium",
            f"{asset} {name} has no readings for {hours:.2f} h. This is a data gap, neither "
            "degradation nor recovery; no service is ordered on it.",
            confidence=0.8,
            expected_outcome=f"Operations see the gap and are not misled by it {SIM}.",
            verification_plan=self._gap_verify(),
        )
        return _Gap(eid, sig, asset, series)

    # ---------------------------------------------------------------- sensor fault

    def _fault(self, t: pd.Timestamp, k: int) -> None:
        th = self.th
        n = int(th.ahu_fault_h * self.per_h)
        s = slice(k - n + 1, k + 1)
        res = np.abs(self.dp[s] - self.implied[s])
        far = bool(
            (
                self.day[s]
                & (self.fan[s] < th.ahu_fan_ceiling_kw)
                & (res > th.ahu_fault_residual_pa)
            ).all()
        )
        run = slice(k - th.ahu_stuck_intervals + 1, k + 1)
        d, f = self.dp[run], self.fan[run]
        stuck = bool(
            not np.isnan(d).any()
            and not np.isnan(f).any()
            and (d == d[0]).all()
            and f.max() > f.min()
        )
        if not (far or stuck):
            return
        last, self.fault_last = self.fault_last, t
        if last is not None and t - last <= pd.Timedelta(hours=th.ahu_window_h):
            return
        eid = f"EP-{ISSUE}-AHU1-{_tag(t)}"
        what = (
            f"dp {self.dp[k]:.0f} Pa is {abs(self.dp[k] - self.implied[k]):.0f} Pa from the "
            f"fan-implied dp for {th.ahu_fault_h:.0f} h"
            if far
            else f"dp has read {self.dp[k]:.1f} Pa for {th.ahu_stuck_intervals} readings while the "
            "fan changes"
        )
        sig = self.board.signal(
            eid,
            t,
            "sensor_fault",
            ("AHU1",),
            self._evidence(t, "filter_dp_pa,fan_kw"),
            f"AHU1 {what}",
            {"dp": float(self.dp[k]), "fan_kw": float(self.fan[k])},
            "sensor_fault",
        )
        self.last_active = t
        self.board.propose(
            eid,
            t,
            sig,
            "flag_sensor_fault",
            "flag_anomaly",
            ["AHU1"],
            "medium",
            f"The AHU1 dp reading is implausible: {what}. The fan is trusted for loading until "
            "the transmitter is checked.",
            confidence=0.7,
            expected_outcome=f"Operations see the suspect transmitter {SIM}.",
            verification_plan=(
                f"The residual stays within 15 Pa for {th.ahu_window_h} h after the transmitter "
                "check."
            ),
        )

    # ---------------------------------------------------------------- BMS alarms

    def _bms(self, t: pd.Timestamp) -> None:
        th = self.th
        while self.alarm_seen < len(self.alarms) and self.alarms[self.alarm_seen][0] <= t:
            at, event, code = self.alarms[self.alarm_seen]
            self.alarm_seen += 1
            quiet = pd.Timedelta(hours=th.ahu_alarm_quiet_h)
            if self.ep is not None or (
                self.last_active is not None and at - self.last_active <= quiet
            ):
                continue
            eid = f"EP-{ISSUE}-AHU1-{_tag(t)}"
            sig = self.board.signal(
                eid,
                t,
                "bms_unmatched",
                ("AHU1",),
                [f"events.jsonl:{event}"],
                f"BMS alarm {code} raised {stamp(at)} with no s04 episode in the last "
                f"{th.ahu_alarm_quiet_h:.0f} h",
                {"alarm": code, "raised": at},
                "bms_unconfirmed",
            )
            self.last_active = t
            self.board.propose(
                eid,
                t,
                sig,
                "flag_unconfirmed_alarm",
                "flag_anomaly",
                ["AHU1"],
                "medium",
                f"BMS alarm {code} on AHU1 is not confirmed by the loading, fan or airflow data.",
                confidence=0.6,
                expected_outcome=f"Operations check the alarm against the data {SIM}.",
                verification_plan=(
                    "The alarm clears with no change in dp, fan or airflow, or a service follows."
                ),
            )

    # ---------------------------------------------------------------- run

    def _step(self, t: pd.Timestamp, k: int) -> None:
        th = self.th
        ep = self.ep
        if self._reset(k):
            self.last_reset = t
            self.ep = ep = None
        if ep is not None:
            self.last_active = t
        self._work_orders(t, k)
        ep = self.ep
        rate = self._rate(k)
        fast = rate is not None and rate[0] >= th.ahu_rate_fast_pa_per_day
        ceiling = self._ceiling(k)
        if (ep is None or ep.kind == "plan") and (fast or ceiling):
            self._clog(t, k, "rate" if fast else "air", rate)
        elif ep is not None and ep.kind == "clog":
            if ceiling and not ep.air_flagged:
                self._airflow(t, k, ep)
            if ep.deadline is not None and t >= ep.deadline and not ep.escalated:
                self._escalate(t, k, ep)
        elif ep is None:
            level = self._level(k)
            settled = self.last_reset is None or t - self.last_reset > pd.Timedelta(
                hours=th.ahu_window_h
            )
            if settled and level is not None and level[0] >= th.ahu_due_dp_pa:
                self._plan(t, k, level)
        ep = self.ep
        if (
            ep is not None
            and ep.kind == "plan"
            and ep.dispatch_at is not None
            and t >= ep.dispatch_at
        ):
            self._dispatch(t, ep)
        self._gaps(t, k)
        self._fault(t, k)
        self._bms(t)

    def run(self) -> list[Row]:
        first = 2 * self.th.ahu_window_h * self.per_h - 1
        for k in range(first, len(self.idx)):
            t = self.idx[k]
            if t.minute == 0:
                self._step(pd.Timestamp(t), k)
        return self.board.rows


def rows(
    ds: Dataset, settings: Settings, cache: Path, call: llm_guard.Call | None = None
) -> tuple[list[Row], Counter[str]]:
    """Return the s04 signal and proposal rows of ``ds`` in time order.

    The lane phrases no guest message, so ``cache`` and ``call`` are unused and the message
    count is empty.
    """
    del cache, call
    return _Lane(ds, settings).run(), Counter()
