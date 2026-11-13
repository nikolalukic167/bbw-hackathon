"""S07 cleaning and logistics lane on the bus (docs/BUS.md): vending, container, floor bins.

- ``drinks_low`` / ``snacks_low`` (UC1): the vending stock, at the trailing use rate that
  leaves out hours at zero stock and at the cap, runs out before the next regular restock
  and an extra restock ordered now lands before it: one ``order_service`` per window.
- ``container_overflow_risk`` (UC2, UC3): the container is simulated to the next regular
  pickup. It fills in steps at the 14:30 floor bin dump, not linearly. If it would reach the
  cap, S07 first holds the dump of the bins that cause it and has a robot carry them after
  the pickup; an extra pickup is ordered only if the hold is unsafe.
- ``event_weekend_window`` (UC4) is a cause and a wider vending buffer, not a row of its own.
- ``sensor_gap`` / ``sensor_jump`` (UC5): a blank run, a step no event explains, or a
  series stuck flat. One ``flag_anomaly`` per episode; the step stays out of every rate.

Floor bins fill only from room cleanings and never overflow, so a robot has no floor bin
to empty before a pickup: a bin-run happens after it. Every effect is simulated.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from plato40 import llm_guard
from plato40.bus import VERSION
from plato40.detect.grid import local_time, stamp
from plato40.detect.logistics import _next, _schedule
from plato40.load.derive import logistics_wide
from plato40.load.reader import Dataset
from plato40.settings import Settings
from plato40.staff import Roster

LANE = "s07"
ISSUE = "logistics"
Row = dict[str, Any]
Floats = npt.NDArray[np.float64]
HOUR = pd.Timedelta(hours=1)
SIM = "(simulated)"
FLOORS = (1, 2, 3, 4)
VEND = ("VEND1/stock_drinks", "VEND1/stock_snacks")


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
class _Proj:
    """The container projected to the next regular pickup."""

    overflow: pd.Timestamp | None = None
    background: bool = False
    held: list[int] = field(default_factory=list)
    held_max: float = 0.0
    level: float = 0.0


@dataclass
class _Plan:
    """A window with a projected overflow that is handled by a hold on the overflow day."""

    eid: str
    sig: Row
    day: date
    event: bool
    held: list[int] = field(default_factory=list)


def _eid(asset: str, t: pd.Timestamp) -> str:
    return f"EP-{ISSUE}-{asset}-{t.tz_convert('UTC'):%Y%m%dT%H%M}Z"


class _Lane:
    """One replay of the logistics series on the hourly grid, using only data known at ``t``."""

    def __init__(self, ds: Dataset, settings: Settings) -> None:
        self.ds, self.settings = ds, settings
        self.th, self.tz = settings.detect, settings.local_tz
        self.board = _Board()
        self.roster = Roster(ds)
        lg = logistics_wide(ds)
        lg.index = pd.DatetimeIndex(lg.index)
        self.lg = lg
        self.idx: pd.DatetimeIndex = lg.index
        loc = self.idx.tz_convert(self.tz)
        self.lhour: npt.NDArray[np.int_] = np.asarray(loc.hour)
        self.ldate: list[date] = list(loc.date)
        assets = {a["id"]: a for a in ds.building["building"]["assets"]}
        self.sched = {
            "VEND1": _schedule(assets["VEND1"]["restock"]),
            "WASTE_MAIN": _schedule(assets["WASTE_MAIN"]["pickup"]),
        }
        self.rooms = sum(1 for a in assets.values() if a["type"] == "room")
        ev = ds.events
        self.ev = ev
        self._events: dict[tuple[str, str], set[pd.Timestamp]] = {}
        self._bookings()
        self._cleanings()
        self._vending_rates()
        self.window_done: set[tuple[str, pd.Timestamp]] = set()
        self.plans: dict[pd.Timestamp, _Plan] = {}
        self.flagged: dict[str, pd.Timestamp] = {}
        self.post_pickup = self._post_pickup_levels()

    # ------------------------------------------------------------------ inputs

    def _hours(self, kind: str, asset: str = "") -> set[pd.Timestamp]:
        """Return the hour rows that show an event of ``kind`` (on ``asset``)."""
        key = (kind, asset)
        if key not in self._events:
            e = self.ev.loc[self.ev["type"] == kind]
            if asset:
                e = e.loc[e["asset_id"] == asset]
            self._events[key] = set(pd.DatetimeIndex(e["timestamp_utc"]).ceil("h"))
        return self._events[key]

    def _bookings(self) -> None:
        bk = self.ds.table("bookings.csv")
        bk = bk.loc[bk["status"] == "confirmed"]
        self.b_created = pd.DatetimeIndex(bk["created_utc"]).tz_convert("UTC").tz_localize(None)
        self.b_arrival = pd.DatetimeIndex(bk["arrival_utc"]).tz_convert("UTC").tz_localize(None)
        self.b_depart = pd.DatetimeIndex(bk["departure_utc"]).tz_convert("UTC").tz_localize(None)
        by: dict[tuple[date, int], list[pd.Timestamp]] = {}
        dep_day = pd.DatetimeIndex(bk["departure_utc"]).tz_convert(self.tz).date
        for d, room, created in zip(dep_day, bk["room_id"], bk["created_utc"], strict=True):
            by.setdefault((d, int(str(room)[1])), []).append(pd.Timestamp(created))
        self.departures = {
            k: pd.DatetimeIndex(sorted(v)).tz_convert("UTC").tz_localize(None).to_numpy()
            for k, v in by.items()
        }

    def _cleanings(self) -> None:
        ev = self.ev.loc[self.ev["type"] == "cleaning_completed"]
        floor = ev["room_id"].astype(str).str[1].astype(int)
        self.cleaned = {
            fl: np.sort(
                pd.DatetimeIndex(ev.loc[floor == fl, "timestamp_utc"])
                .tz_convert("UTC")
                .tz_localize(None)
                .to_numpy()
            )
            for fl in FLOORS
        }

    def _vending_rates(self) -> None:
        """Precompute the censor-aware use rate and the censoring flag of each product."""
        th = self.th
        restock = self._hours("vending_restocked", "VEND1")
        self.rate: dict[str, Floats] = {}
        self.censored: dict[str, npt.NDArray[np.bool_]] = {}
        self.jump_rows: dict[str, set[pd.Timestamp]] = {}
        for col in VEND:
            s = self.lg[col]
            prev = s.shift(1)
            step = prev - s
            explained = pd.Series([t in restock for t in self.idx], index=self.idx)
            rise = step < -th.logistics_jump_pp
            self.jump_rows[col] = set(self.idx[(rise & ~explained).to_numpy()])
            ok = (prev > 0) & (step >= 0)
            self.rate[col] = (
                step.where(ok).rolling(th.logistics_rate_h, min_periods=1).mean().to_numpy()
            )
            self.censored[col] = (
                (prev <= 0).astype(float).rolling(th.logistics_rate_h, min_periods=1).max() > 0
            ).to_numpy()

    def _post_pickup_levels(self) -> pd.Series:
        """Return the container fill in the row of each pickup, indexed by pickup row."""
        rows = sorted(self._hours("waste_pickup", "WASTE_MAIN"))
        w = self.lg["WASTE_MAIN/fill_pct"]
        return pd.Series([w.get(r, np.nan) for r in rows], index=pd.DatetimeIndex(rows))

    def _after_pickup(self, t: pd.Timestamp) -> float:
        """Return the median container fill right after a pickup, from pickups known at ``t``."""
        known = self.post_pickup.loc[self.post_pickup.index <= t].dropna()
        return float(known.median()) if len(known) else 0.0

    def _regular(self, asset: str, t: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timedelta]:
        days, at, lead = self.sched[asset]
        return _next(t, days, at, self.tz), lead

    # ------------------------------------------------------------------ weekend

    def _booked(self, night: date, t: pd.Timestamp) -> int:
        """Return rooms booked for ``night`` by bookings created at or before ``t``."""
        cut = pd.Timestamp(f"{night} 22:00").tz_localize(self.tz).tz_convert("UTC")
        cut_n, t_n = cut.tz_localize(None), t.tz_localize(None)
        m = (self.b_created <= t_n) & (self.b_arrival <= cut_n) & (self.b_depart > cut_n)
        return int(m.sum())

    def _event(self, t: pd.Timestamp, reg: pd.Timestamp) -> bool:
        """Return whether ``reg`` follows a weekend booked at or above the event level."""
        rl = reg.tz_convert(self.tz)
        if rl.weekday() not in (0, 1):
            return False
        friday = rl.date() - timedelta(days=3 if rl.weekday() == 0 else 4)
        known = pd.Timestamp(f"{friday - timedelta(days=1)} 13:00").tz_localize(self.tz)
        if t < known.tz_convert("UTC"):
            return False
        n = max(self._booked(friday, t), self._booked(friday + timedelta(days=1), t))
        return n >= self.th.logistics_event_nights

    # ------------------------------------------------------------------ UC1

    def _vending(self, i: int, t: pd.Timestamp) -> None:
        th = self.th
        reg, lead = self._regular("VEND1", t)
        if ("VEND1", reg) in self.window_done:
            return
        event = self._event(t, reg)
        buffer = pd.Timedelta(hours=th.logistics_event_buffer_h if event else th.logistics_buffer_h)
        for col in VEND:
            stock, rate = float(self.lg[col].iloc[i]), float(self.rate[col][i])
            if np.isnan(stock) or np.isnan(rate) or rate <= 0:
                continue
            empty = t + pd.Timedelta(hours=stock / rate)
            if not (empty < reg and t + lead < reg and empty - t <= lead + buffer):
                continue
            product = col.split("/")[1].removeprefix("stock_")
            cause = (
                "event_weekend"
                if event
                else "post_stockout"
                if self.censored[col][i]
                else "demand_peak"
            )
            eid = _eid("VEND1", t)
            window = t - pd.Timedelta(hours=th.logistics_rate_h)
            sig = self.board.signal(
                eid,
                t,
                f"{product}_low",
                ["VEND1"],
                [f"logistics.csv:{col}:{stamp(window)}..{stamp(t)}"],
                f"VEND1 {product} {stock:.0f} now, {rate:.2f} per hour with zero-stock and cap "
                f"hours left out: out at about {local_time(empty, t, self.tz)}, before the "
                f"regular restock {local_time(reg, t, self.tz)}",
                {"stock": stock, "rate_per_h": rate, "empty": empty, "regular": reg},
                cause,
            )
            eta = t + lead
            self.board.propose(
                eid,
                t,
                sig,
                "order_restock",
                "order_service",
                ["VEND1"],
                "medium",
                f"Order an extra VEND1 restock now ({lead / HOUR:.0f} h lead, arriving about "
                f"{local_time(eta, t, self.tz)}): {product} runs out at about "
                f"{local_time(empty, t, self.tz)}, before the regular restock "
                f"{local_time(reg, t, self.tz)}" + (" on an event weekend." if event else "."),
                confidence=0.8,
                expected_outcome=f"VEND1 restocked before {product} runs out {SIM}.",
                verification_plan=(
                    f"A vending_restocked event by {stamp(eta)} and no vending_out_of_stock "
                    "before it; otherwise escalate."
                ),
            )
            self.window_done.add(("VEND1", reg))
            return

    # ------------------------------------------------------------------ UC2, UC3

    def _count(self, arr: npt.NDArray[Any], hi: pd.Timestamp, lo: pd.Timestamp | None) -> int:
        top = int(np.searchsorted(arr, np.datetime64(hi.tz_localize(None)), side="right"))
        low = (
            0
            if lo is None
            else int(np.searchsorted(arr, np.datetime64(lo.tz_localize(None)), side="left"))
        )
        return top - low

    def _project(self, i: int, t: pd.Timestamp, reg: pd.Timestamp, hold: bool) -> _Proj | None:
        """Simulate the container hourly from ``t`` to ``reg``; ``hold`` keeps big dumps back."""
        th, tz = self.th, self.tz
        c = float(self.lg["WASTE_MAIN/fill_pct"].iloc[i])
        f = {fl: float(self.lg[f"WASTE_F{fl}/fill_pct"].iloc[i]) for fl in FLOORS}
        if np.isnan(c) or any(np.isnan(v) for v in f.values()) or c >= th.logistics_cap_pct:
            return None
        lt = t.tz_convert(tz)
        day0 = lt.normalize().tz_convert("UTC")
        dump_row = int(np.ceil(th.logistics_dump_local_h))
        first, last = (
            th.logistics_clean_first_h,
            th.logistics_clean_first_h + th.logistics_clean_hours,
        )
        out = _Proj()
        h = t
        while h < reg:
            h += HOUR
            lh = h.tz_convert(tz)
            c += th.logistics_background_pp_per_h
            if first <= lh.hour < last:
                day = lh.date()
                for fl in FLOORS:
                    arr = self.departures.get((day, fl))
                    n = 0 if arr is None else self._count(arr, t, None)
                    if day == lt.date():
                        n = max(n - self._count(self.cleaned[fl], t, day0), 0)
                        left_h = max(last - max(lt.hour + 1, first), 1)
                        f[fl] += th.logistics_pp_per_cleaning * n / left_h
                    else:
                        f[fl] += th.logistics_pp_per_cleaning * n / th.logistics_clean_hours
            if lh.hour == dump_row:
                for fl in FLOORS:
                    if f[fl] < th.logistics_dump_pct:
                        continue
                    add = th.logistics_dump_ratio * f[fl]
                    if hold and c + add >= th.logistics_cap_pct:
                        if fl not in out.held:
                            out.held.append(fl)
                        continue
                    c += add
                    f[fl] = 0.0
            if out.held:
                out.held_max = max(out.held_max, *(f[fl] for fl in out.held))
            if c >= th.logistics_cap_pct:
                if hold:
                    out.background = True
                else:
                    out.overflow = h - pd.Timedelta(minutes=30)
                out.level = c
                return out
        out.level = c
        return out

    def _container(self, i: int, t: pd.Timestamp) -> None:
        th = self.th
        reg, lead = self._regular("WASTE_MAIN", t)
        if ("WASTE_MAIN", reg) in self.window_done:
            return
        proj = self._project(i, t, reg, hold=False)
        if proj is None or proj.overflow is None:
            return
        buffer = pd.Timedelta(hours=th.logistics_buffer_h)
        if not (proj.overflow < reg and proj.overflow - t <= lead + buffer):
            return
        event = self._event(t, reg)
        safe = self._project(i, t, reg, hold=True)
        assert safe is not None
        feasible = not safe.background and safe.held_max < th.logistics_held_max_pct
        eid = _eid("WASTE_MAIN", t)
        cols = ",".join(f"{c}/fill_pct" for c in ("WASTE_MAIN", *(f"WASTE_F{n}" for n in FLOORS)))
        sig = self.board.signal(
            eid,
            t,
            "container_overflow_risk",
            ["WASTE_MAIN"],
            [
                f"logistics.csv:{cols}:{stamp(t - 24 * HOUR)}..{stamp(t)}",
                f"bookings.csv:departures known at {stamp(t)}",
            ],
            f"WASTE_MAIN reaches {th.logistics_cap_pct:.0f} % at the floor bin dump at about "
            f"{local_time(proj.overflow, t, self.tz)}, before the regular pickup "
            f"{local_time(reg, t, self.tz)}",
            {"overflow": proj.overflow, "regular": reg, "hold_feasible": feasible},
            "event_weekend" if event else "turnover_peak",
        )
        self.window_done.add(("WASTE_MAIN", reg))
        day = proj.overflow.tz_convert(self.tz).date()
        if feasible:
            self.plans[reg] = _Plan(eid, sig, day, event)
            return
        if t + lead >= reg:
            return
        eta = t + lead
        reason = (
            f"a held floor bin would pass {th.logistics_held_max_pct:.0f} % before the pickup"
            if not safe.background
            else "the container reaches the cap without any floor bin dump"
        )
        self.board.propose(
            eid,
            t,
            sig,
            "order_pickup",
            "order_service",
            ["WASTE_MAIN"],
            "medium",
            f"Order an extra WASTE_MAIN pickup now (arriving about "
            f"{local_time(eta, t, self.tz)}): the container reaches the cap at about "
            f"{local_time(proj.overflow, t, self.tz)} and holding the dump is unsafe, {reason}.",
            confidence=0.7,
            expected_outcome=f"WASTE_MAIN emptied before it overflows {SIM}.",
            verification_plan=(
                f"A waste_pickup event by {stamp(eta)} and no waste_bin_full before it; the "
                "container stays below the cap at the next dump."
            ),
        )

    def _hold(self, i: int, t: pd.Timestamp, reg: pd.Timestamp, plan: _Plan) -> None:
        """UC3 part 1: hold the dump of the bins that would overflow the container."""
        th = self.th
        proj = self._project(i, t, reg, hold=True)
        if proj is None or not proj.held or proj.background:
            return
        plan.held = sorted(proj.held)
        names = [f"WASTE_F{n}" for n in plan.held]
        when = t + pd.Timedelta(hours=th.logistics_hold_local_h - t.tz_convert(self.tz).hour)
        self.board.propose(
            plan.eid,
            when,
            plan.sig,
            "hold_floor_dump",
            "defer_action",
            names,
            "medium",
            f"Hold the {th.logistics_dump_local_h:.1f} h floor bin dump of {', '.join(names)}: "
            f"emptying them would fill WASTE_MAIN to the cap before the pickup "
            f"{local_time(reg, when, self.tz)}. A robot carries them after the pickup.",
            confidence=0.8,
            expected_outcome=f"WASTE_MAIN stays below the cap until the pickup {SIM}.",
            verification_plan=(
                f"Held bins stay below {th.logistics_held_max_pct:.0f} %, WASTE_MAIN stays below "
                f"{th.logistics_cap_pct:.0f} % until the pickup, and the bins drop to 0 after "
                "the bin-run."
            ),
        )

    def _robots(self, i: int, t: pd.Timestamp, plan: _Plan) -> None:
        """UC3 part 2: robots carry the held bins right after the pickup."""
        th = self.th
        when = t + pd.Timedelta(
            minutes=round((th.logistics_robot_start_local_h - t.tz_convert(self.tz).hour) * 60)
        )
        robots = self.roster.on_shift("robot", when, when)
        end = when + pd.Timedelta(
            hours=th.logistics_robot_end_local_h - th.logistics_robot_start_local_h
        )
        level = self._after_pickup(when)
        bins: list[int] = []
        for fl in plan.held:
            level += th.logistics_dump_ratio * float(self.lg[f"WASTE_F{fl}/fill_pct"].iloc[i])
            if level < th.logistics_cap_pct:
                bins.append(fl)
        room = int((end - when) / pd.Timedelta(minutes=th.logistics_bin_run_min))
        bins = bins[: room * len(robots)] if robots else []
        for r, robot in enumerate(robots):
            mine = bins[r :: len(robots)]
            if not mine:
                continue
            names = [f"WASTE_F{n}" for n in mine]
            minutes = th.logistics_bin_run_min * len(mine)
            self.board.propose(
                plan.eid,
                when,
                plan.sig,
                "robot_bin_run",
                "dispatch_robot",
                [robot, *names],
                "low",
                f"{robot} carries {', '.join(names)} to WASTE_MAIN from "
                f"{local_time(when, when, self.tz)}, right after the pickup: {minutes} min.",
                resource_id=robot,
                claims=[{"res": "robot", "id": robot, "from": stamp(when), "min": minutes}],
                confidence=0.7,
                expected_outcome=f"Held bins emptied, WASTE_MAIN below the cap {SIM}.",
                verification_plan=(
                    "The held bins drop to 0 (floor_bin_emptied or a fill drop) and "
                    f"WASTE_MAIN stays below {th.logistics_cap_pct:.0f} % until the next pickup."
                ),
            )

    # ------------------------------------------------------------------ UC5

    def _flag(self, series: str, t: pd.Timestamp, kind: str, detail: str, cause: str) -> None:
        """One ``flag_anomaly`` per series episode; later hits within the quiet time join it."""
        quiet = pd.Timedelta(hours=self.th.episode_quiet_h)
        last = self.flagged.get(series)
        self.flagged[series] = t
        if last is not None and t - last <= quiet:
            return
        asset = series.split("/")[0]
        eid = _eid(asset, t)
        sig = self.board.signal(
            eid,
            t,
            kind,
            [asset],
            [f"logistics.csv:{series}:{stamp(t - 24 * HOUR)}..{stamp(t)}"],
            detail,
            {"series": series},
            cause,
        )
        self.board.propose(
            eid,
            t,
            sig,
            "flag_logistics_data",
            "flag_anomaly",
            [asset],
            "low",
            f"{detail}. No projection is made on {series} until it reports normally again; "
            "the censored or unexplained values stay out of every rate.",
            confidence=0.7,
            expected_outcome=f"Operations know {series} is not trusted {SIM}.",
            verification_plan="The series reports again, or its steps match events again.",
        )

    def _sensors(self, i: int, t: pd.Timestamp) -> None:
        th = self.th
        for col in self.lg.columns:
            v = self.lg[col].to_numpy()
            asset, metric = col.split("/")
            if (
                i + 1 >= th.logistics_gap_h
                and np.isnan(v[i - th.logistics_gap_h + 1 : i + 1]).all()
            ):
                self._flag(
                    col,
                    t,
                    "sensor_gap",
                    f"{col} has been blank for {th.logistics_gap_h} h",
                    "sensor_gap",
                )
                continue
            if i == 0 or np.isnan(v[i]) or np.isnan(v[i - 1]):
                continue
            step = v[i] - v[i - 1]
            unexplained = False
            if asset == "WASTE_MAIN":
                if step > th.logistics_jump_pp:
                    unexplained = not (t in self._hours("floor_bin_emptied") or self.lhour[i] == 15)
                elif step < -th.logistics_jump_pp:
                    unexplained = t not in self._hours("waste_pickup", asset)
            elif asset == "VEND1":
                unexplained = t in self.jump_rows[col]
            elif step < -th.logistics_jump_pp:
                unexplained = t not in self._hours("floor_bin_emptied", asset)
            if unexplained:
                self._flag(
                    col,
                    t,
                    "sensor_jump",
                    f"{col} changed by {step:+.1f} at {stamp(t)} and no event explains it",
                    "sensor_jump",
                )
                continue
            if asset != "WASTE_MAIN" and asset != "VEND1":
                self._stuck_bin(i, t, col, asset)
            elif i >= th.logistics_stuck_h:
                window = v[i - th.logistics_stuck_h : i + 1]
                if not np.isnan(window).any() and (window == window[0]).all():
                    self._flag(
                        col,
                        t,
                        "sensor_gap",
                        f"{col} has not changed for {th.logistics_stuck_h} h",
                        "sensor_gap",
                    )
            _ = metric

    def _stuck_bin(self, i: int, t: pd.Timestamp, col: str, asset: str) -> None:
        """Flag a floor bin that stayed flat over a day on which its floor was cleaned."""
        th = self.th
        if self.lhour[i] != th.logistics_clean_first_h + th.logistics_clean_hours + 1:
            return
        start = i - (self.lhour[i] - th.logistics_clean_first_h + 1)
        if start < 0 or self.ldate[start] != self.ldate[i]:
            return
        fl = int(asset[-1])
        day0 = t.tz_convert(self.tz).normalize().tz_convert("UTC")
        if self._count(self.cleaned[fl], t, day0) == 0:
            return
        window = self.lg[col].to_numpy()[start : i + 1]
        if not np.isnan(window).any() and (window == window[0]).all():
            self._flag(
                col, t, "sensor_gap", f"{col} did not move on a day with cleanings", "sensor_gap"
            )

    # ------------------------------------------------------------------ replay

    def run(self) -> list[Row]:
        th = self.th
        for i, t in enumerate(self.idx):
            self._sensors(i, t)
            self._vending(i, t)
            self._container(i, t)
            ld = self.ldate[i]
            for reg, plan in sorted(self.plans.items()):
                if ld == plan.day and self.lhour[i] == int(th.logistics_hold_local_h):
                    self._hold(i, t, reg, plan)
                rl = reg.tz_convert(self.tz)
                if (
                    plan.held
                    and ld == rl.date()
                    and self.lhour[i] == int(th.logistics_robot_start_local_h)
                ):
                    self._robots(i, t, plan)
                    plan.held = []
        order = {"signal": 0, "proposal": 1}
        self.board.rows.sort(key=lambda r: (r.get("t_known") or r["t"], order[r["row"]], r["id"]))
        return self.board.rows


def rows(
    ds: Dataset, settings: Settings, cache: Path, call: llm_guard.Call | None = None
) -> tuple[list[Row], Counter[str]]:
    """Return the s07 signal and proposal rows in time order; S07 sends no guest messages."""
    del cache, call
    return _Lane(ds, settings).run(), Counter()
