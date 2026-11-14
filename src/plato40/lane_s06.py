"""S06 energy lane on the bus (docs/BUS.md): pre-heat arrivals, back off for S03, guard inputs.

- ``arrival_due`` (UC2): ``preheat_lead_h`` before a booked arrival, the room is vacant, has a
  reading, its window is closed, it is below the comfort band and HP1 is ``running``: raise
  the setpoint to ``preheat_setpoint_c`` so the guest does not enter a cold room. Rooms due at
  the same time share one decision (one a day for the standard check-in): in dev, 96 % of
  guests checking in before 14:00 local entered a room below the band, 5 % after 18:00, and
  the check-in time is not known ahead. Skipped
  while S03 is active (HP1 ``cop_low`` in the last ``s03_backoff_h``). A cancellation counts
  as known at the arrival time (ADR-002), so a cancelled booking is pre-heated too.
- ``s03_active`` (UC5): the first ``cop_low`` verdict of an HP1 episode pauses heat pump load
  shifting and pre-heat with one ``defer_action``; the battery continues.
- ``input_stale`` (UC6): tomorrow's tariff not published by ``tariff_due_local_h``, the latest
  weather forecast older than ``forecast_stale_h`` at 07:00 local, or an ``energy.csv`` row
  that breaks the energy balance or the BAT1 limits in ``building.json``: one
  ``flag_anomaly`` per episode, and the rule in the data stays in charge.

- ``tariff_published`` (UC1): at the first publication of tomorrow's prices, a dynamic
  programme over BAT1's SOC plans charge and discharge to the end of the published prices,
  against forecast PV and load (the same-time-of-day mean of the last days). One
  ``optimize_energy`` per day; the saving is against no battery and simulated. The S03
  back-off does not pause it.

S06 claims no staff and no rooms, and never messages guests. Every effect is simulated.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from plato40 import llm_guard
from plato40.bus import VERSION
from plato40.detect import heating
from plato40.detect.grid import local_time, stamp
from plato40.episodes import group
from plato40.load.derive import telemetry_wide
from plato40.load.reader import Dataset
from plato40.load.units import BOUNDS
from plato40.settings import Settings
from plato40.state import RoomStates

LANE = "s06"
ISSUE = "energy_optimization"
Row = dict[str, Any]
Floats = npt.NDArray[np.float64]


class _Board:
    """Signal and proposal rows of the lane, with IDs numbered per episode."""

    def __init__(self) -> None:
        self.rows: list[Row] = []
        self.seq: Counter[str] = Counter()

    def _id(self, eid: str, kind: str) -> str:
        self.seq[f"{eid}/{kind}"] += 1
        return f"{LANE}-{eid[3:]}-{kind}{self.seq[f'{eid}/{kind}']:02d}"

    def signal(
        self,
        eid: str,
        t: pd.Timestamp,
        kind: str,
        assets: list[str],
        evidence: list[str],
        detail: str,
        facts: dict[str, Any],
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
            "assets": assets,
            "evidence": evidence,
            "detail": detail,
            "facts": facts,
            "cause": cause,
        }
        self.rows.append(row)
        return row

    def propose(
        self,
        eid: str,
        sig: Row,
        intent: str,
        action: str,
        priority: str,
        reason: str,
        outcome: str,
        verify: str,
        confidence: float,
    ) -> None:
        self.rows.append(
            {
                "v": VERSION,
                "row": "proposal",
                "lane": LANE,
                "id": self._id(eid, "prop"),
                "t": sig["t_known"],
                "source": "rules",
                "episode_id": eid,
                "signal_ids": [sig["id"]],
                "intent": intent,
                "action": action,
                "assets": sig["assets"],
                "priority": priority,
                "evidence": sig["evidence"],
                "reason": reason,
                "cause": sig["cause"],
                "expected_outcome": outcome,
                "verification_plan": verify,
                "confidence": confidence,
            }
        )


def _eid(asset: str, t: pd.Timestamp) -> str:
    return f"EP-{ISSUE}-{asset}-{t:%Y%m%dT%H%M}Z"


def _latest(frame: pd.DataFrame, room: str, t: pd.Timestamp, since: pd.Timestamp) -> float | None:
    if room not in frame.columns:
        return None
    col = frame[room]
    recent = col.loc[(col.index > since) & (col.index <= t)].dropna()
    return float(recent.iloc[-1]) if len(recent) else None


def _preheat(ds: Dataset, settings: Settings, board: _Board, degraded: list[pd.Timestamp]) -> None:
    """UC2: one setpoint raise per arrival into a vacant room below the comfort band."""
    th, tz = settings.detect, settings.local_tz
    lo = float(ds.building["constraints"]["occupied_comfort_c"][0])
    bound = BOUNDS[("room_telemetry.csv", "temperature_c")]
    temp = telemetry_wide(ds, "temperature_c")
    temp = temp.where((temp >= bound.low) & (temp <= bound.high))
    setpoint = telemetry_wide(ds, "setpoint_c")
    window = telemetry_wide(ds, "window_open")
    eq = ds.table("equipment.csv")
    hp = eq.loc[(eq["asset_id"] == "HP1") & (eq["metric"] == "status")].sort_values(
        "timestamp_utc", kind="stable"
    )
    hp_t = pd.DatetimeIndex(hp["timestamp_utc"])
    hp_v = hp["value"].astype(str).to_numpy()
    states = RoomStates(ds)
    recent = pd.Timedelta(hours=th.room_reading_h)
    lead = pd.Timedelta(hours=th.preheat_lead_h)
    backoff = pd.Timedelta(hours=th.s03_backoff_h)
    step = f"{settings.interval_minutes}min"
    bk = ds.table("bookings.csv").sort_values(["arrival_utc", "booking_id"], kind="stable")
    due: dict[pd.Timestamp, list[tuple[str, str, pd.Timestamp, float, str]]] = {}
    for _, b in bk.iterrows():
        arrival, room, booking = (
            pd.Timestamp(b["arrival_utc"]),
            str(b["room_id"]),
            str(b["booking_id"]),
        )
        t = max(arrival - lead, pd.Timestamp(b["created_utc"])).ceil(step)
        if t >= arrival or states.at(room, t) == "occupied":
            continue
        now = _latest(temp, room, t, t - recent)
        sp = _latest(setpoint, room, t, t - recent)
        if now is None or now >= lo or (sp is not None and sp >= th.preheat_setpoint_c):
            continue
        if _latest(window, room, t, t - recent) == 1.0:
            continue
        i = int(hp_t.searchsorted(t, side="right")) - 1
        if i < 0 or hp_v[i] != "running":
            continue
        if any(t - backoff < d <= t for d in degraded):
            continue
        # A cancellation is known only at arrival (ADR-002), so a rebooked room can come twice.
        if any(r == room for r, *_ in due.get(t, [])):
            continue
        due.setdefault(t, []).append((room, booking, arrival, now, stamp(hp_t[i])))
    for t, rooms in sorted(due.items()):
        rooms.sort()
        names = [r for r, _, _, _, _ in rooms]
        first = min(a for _, _, a, _, _ in rooms)
        eid = _eid(names[0] if len(names) == 1 else "BUILDING", t)
        listing = ", ".join(f"{r} {now:.1f} degC" for r, _, _, now, _ in rooms)
        sig = board.signal(
            eid,
            t,
            "arrival_due",
            names,
            [
                *(f"bookings.csv:{b}" for _, b, _, _, _ in rooms),
                f"room_telemetry.csv:{','.join(names)} temperature_c,setpoint_c,window_open:"
                f"{stamp(t - recent)}..{stamp(t)}",
                f"equipment.csv:HP1 status:{rooms[0][4]}",
            ],
            f"{len(rooms)} vacant arrival rooms below {lo:.0f} degC, first arrival "
            f"{local_time(first, t, tz)}: {listing}",
            {
                "bookings": [b for _, b, _, _, _ in rooms],
                "arrivals": [stamp(a) for _, _, a, _, _ in rooms],
                "temperatures": [round(now, 2) for _, _, _, now, _ in rooms],
            },
            "arrival",
        )
        board.propose(
            eid,
            sig,
            "preheat_arrival",
            "adjust_room_setpoint",
            "low",
            f"Pre-heat the vacant arrival rooms to {th.preheat_setpoint_c:.0f} degC now, "
            f"{th.preheat_lead_h:.0f} h before the first arrival at {local_time(first, t, tz)}: "
            f"{listing}. Guests who check in early otherwise enter a cold room.",
            f"Each room at or above {lo:.0f} degC at its arrival (simulated).",
            f"Each room at or above {lo:.0f} degC at check-in and no temperature_cold request in "
            "the first 2 h of the stay.",
            0.6,
        )


def _backoff(settings: Settings, board: _Board, cop: list[Any]) -> None:
    """UC5: one ``defer_action`` per HP1 degradation episode."""
    th = settings.detect
    for ep in group(cop, th.s03_backoff_h):
        s = ep.signals[0]
        eid = _eid("HP1", s.time)
        sig = board.signal(
            eid,
            s.time,
            "s03_active",
            ["HP1"],
            list(s.evidence),
            f"S03 has HP1 degraded: {s.detail}",
            {"cop_low_checks": len(ep.signals)},
            "s03_backoff",
        )
        board.propose(
            eid,
            sig,
            "back_off_for_comfort",
            "defer_action",
            "low",
            "Pause heat pump load shifting and arrival pre-heat while HP1 is degraded: S03 owns "
            f"comfort. The battery plan continues. Resume after {th.s03_backoff_h:.0f} h "
            "without a cop_low verdict.",
            "No energy action competes with S03 for HP1 (simulated).",
            f"No cop_low verdict for {th.s03_backoff_h:.0f} h before pre-heat resumes.",
            0.8,
        )


def _stale(ds: Dataset, settings: Settings, board: _Board) -> None:
    """UC6: one ``flag_anomaly`` per run of days with a late or broken energy input."""
    th, tz = settings.detect, settings.local_tz
    m = ds.manifest
    start, end = pd.Timestamp(m["start_utc"]), pd.Timestamp(m["end_utc"])
    days = pd.date_range(start.tz_convert(tz).normalize(), end.tz_convert(tz).normalize(), freq="D")
    tariff = ds.table("tariff.csv")
    fc = ds.table("weather_forecast.csv")
    found: list[tuple[pd.Timestamp, str, str, list[str]]] = []
    for day in days:
        due = day + pd.Timedelta(hours=th.tariff_due_local_h)
        tomorrow = day + pd.Timedelta(days=1)
        if (
            tomorrow.tz_convert("UTC") + pd.Timedelta(days=1) <= end
            and due.tz_convert("UTC") >= start
        ):
            have = tariff.loc[
                (tariff["published_utc"] <= due)
                & (tariff["timestamp_utc"] >= tomorrow)
                & (tariff["timestamp_utc"] < tomorrow + pd.Timedelta(days=1))
            ]
            if have.empty:
                found.append(
                    (
                        due.tz_convert("UTC"),
                        "tariff",
                        f"no tariff for {tomorrow:%Y-%m-%d} "
                        f"published by {th.tariff_due_local_h:.0f}:00 local",
                        ["tariff.csv:published_utc"],
                    )
                )
        check = (day + pd.Timedelta(hours=7)).tz_convert("UTC")
        issued = fc.loc[fc["issued_utc"] <= check, "issued_utc"]
        if check >= start + pd.Timedelta(hours=th.forecast_stale_h):
            age = (check - issued.max()).total_seconds() / 3600 if len(issued) else float("inf")
            if age > th.forecast_stale_h:
                found.append(
                    (
                        check,
                        "forecast",
                        f"latest weather forecast is {age:.0f} h old",
                        ["weather_forecast.csv:issued_utc"],
                    )
                )
    found += _energy_breaks(ds, settings)
    found.sort(key=lambda x: (x[0], x[1]))
    last: dict[str, pd.Timestamp] = {}
    gap = pd.Timedelta(hours=th.episode_quiet_h)
    for t, what, detail, evidence in found:
        if what in last and t - last[what] <= gap:
            last[what] = t
            continue
        last[what] = t
        broken = what == "energy"
        eid = _eid("BUILDING" if not broken else "BAT1", t)
        sig = board.signal(
            eid,
            t,
            "input_stale",
            ["BUILDING"] if not broken else ["BAT1", "PV1"],
            evidence,
            detail,
            {"input": what},
            "stale_input",
        )
        board.propose(
            eid,
            sig,
            "flag_energy_data_gap",
            "flag_anomaly",
            "high" if broken else "medium",
            f"Energy input not usable: {detail}. Keep the battery and heating on the rule in "
            "the data; no forecast-based energy action until it is back.",
            "Operations know the energy plan runs on the fallback rule (simulated).",
            "The input is back within limits; then planning resumes.",
            0.7,
        )


def _energy_breaks(
    ds: Dataset, settings: Settings
) -> list[tuple[pd.Timestamp, str, str, list[str]]]:
    """Return energy rows breaking the balance or the BAT1 limits from building.json."""
    th = settings.detect
    bat = next(a for a in ds.building["building"]["assets"] if a["type"] == "battery")
    lo, hi = (float(x) for x in bat["soc_bounds_kwh"])
    p_max = float(max(bat["max_charge_kw"], bat["max_discharge_kw"]))
    e = ds.table("energy.csv")
    residual = (
        e["grid_import_kw"]
        + e["pv_kw"]
        + e["battery_discharge_kw_signed"]
        - e["building_load_kw"]
        - e["grid_export_kw"]
    ).abs()
    tol = th.energy_balance_tol_kw
    bad = (
        (residual > tol)
        | (e["battery_soc_kwh"] < lo - tol)
        | (e["battery_soc_kwh"] > hi + tol)
        | (e["battery_discharge_kw_signed"].abs() > p_max + tol)
    )
    out = []
    hits = bad.fillna(False).to_numpy(dtype=bool)
    for t_raw, res, soc in zip(
        e["timestamp_utc"].to_numpy()[hits],
        residual.to_numpy()[hits],
        e["battery_soc_kwh"].to_numpy()[hits],
        strict=True,
    ):
        t = (
            pd.Timestamp(t_raw).tz_localize("UTC")
            if pd.Timestamp(t_raw).tzinfo is None
            else pd.Timestamp(t_raw)
        )
        out.append(
            (
                t,
                "energy",
                f"energy row at {stamp(t)} breaks the balance or the BAT1 limits "
                f"(residual {float(res):.1f} kW, SOC {float(soc):.1f} kWh)",
                [f"energy.csv:{stamp(t)}"],
            )
        )
    return out


def _runs(
    times: pd.DatetimeIndex, mask: npt.NDArray[np.bool_], ref: pd.Timestamp, tz: str
) -> list[str]:
    """Return the runs of ``mask`` as local windows, e.g. ``Fri 16 Jan 02:00 to 05:30``."""
    out, start = [], None
    step = pd.Timedelta(minutes=15)
    for k, on in enumerate([*mask.tolist(), False]):
        if on and start is None:
            start = k
        elif not on and start is not None:
            end = times[k - 1] + step
            out.append(f"{local_time(times[start], ref, tz)} to {end.tz_convert(tz):%H:%M}")
            start = None
    return out


def _dp(
    price: Floats,
    net: Floats,
    soc0: float,
    bounds: tuple[float, float],
    powers: Floats,
    moves: npt.NDArray[np.int_],
    soc_step: float,
    terminal: float,
) -> tuple[npt.NDArray[np.int_], Floats]:
    """Return the cheapest action index per step and the SOC path, by backward induction.

    The SOC lives on a grid of ``soc_step`` kWh and each action moves it by a whole number of
    grid steps (``moves``) at the AC power ``powers``, so no rounding creates or loses energy.
    Cost is the grid import price only, export is worth nothing, and the SOC left at the end
    is worth ``terminal`` EUR per kWh. Ties take the first action, and 0 kW comes first.
    """
    dt = 0.25
    lo, hi = bounds
    grid = np.arange(lo, hi + soc_step / 2, soc_step)
    ns, n = len(grid), len(price)
    nxt = np.arange(ns)[:, None] + moves[None, :]
    ok = (nxt >= 0) & (nxt < ns)
    nxt = np.clip(nxt, 0, ns - 1)
    value = -terminal * grid
    policy = np.zeros((n, ns), dtype=int)
    for k in range(n - 1, -1, -1):
        cost = price[k] * np.maximum(net[k] + powers, 0.0) * dt
        total = np.where(ok, cost[None, :] + value[nxt], np.inf)
        best = np.argmin(total, axis=1)
        policy[k] = best
        value = total[np.arange(ns), best]
    s = int(np.clip(np.rint((soc0 - lo) / soc_step), 0, ns - 1))
    actions = np.zeros(n, dtype=int)
    path = np.zeros(n + 1)
    path[0] = grid[s]
    for k in range(n):
        actions[k] = policy[k, s]
        s = int(nxt[s, actions[k]])
        path[k + 1] = grid[s]
    return actions, path


def _battery(ds: Dataset, settings: Settings, board: _Board) -> None:
    """UC1: one day-ahead BAT1 plan at each first tariff publication for the next day."""
    th, tz = settings.detect, settings.local_tz
    bat = next(a for a in ds.building["building"]["assets"] if a["type"] == "battery")
    lo, hi = (float(x) for x in bat["soc_bounds_kwh"])
    eta = float(bat["round_trip_efficiency"]) ** 0.5
    unit = th.battery_soc_step_kwh
    up = int(float(bat["max_charge_kw"]) * 0.25 * eta / unit)
    down = int(float(bat["max_discharge_kw"]) * 0.25 / eta / unit)
    moves = np.array(
        [0] + [m for k in range(1, max(up, down) + 1) for m in (-k, k) if -down <= m <= up]
    )
    powers = np.where(moves > 0, moves * unit / (0.25 * eta), moves * unit * eta / 0.25)
    e = ds.table("energy.csv").sort_values("timestamp_utc", kind="stable")
    et = pd.DatetimeIndex(e["timestamp_utc"])
    tariff = ds.table("tariff.csv")
    day = pd.DatetimeIndex(tariff["timestamp_utc"]).tz_convert(tz).normalize()
    first = tariff.groupby(day)["published_utc"].min()
    step = pd.Timedelta(minutes=15)
    history = pd.Timedelta(days=th.battery_history_d)
    for t in sorted(set(pd.DatetimeIndex(first.to_numpy()).tz_convert("UTC"))):
        t = pd.Timestamp(t)
        if len(et) == 0 or t - et[0] < pd.Timedelta(days=th.battery_min_history_d) or t > et[-1]:
            continue
        prices = tariff.loc[tariff["published_utc"] <= t].sort_values(
            ["timestamp_utc", "published_utc"], kind="stable"
        )
        prices = prices.drop_duplicates("timestamp_utc", keep="last").set_index("timestamp_utc")
        start = t.ceil("15min")
        end = pd.Timestamp(prices.index.max()) + pd.Timedelta(hours=1)
        times = pd.date_range(start, end, freq=step, inclusive="left")
        hour = times.floor("h")
        if len(times) == 0 or not hour.isin(prices.index).all():
            continue
        price = prices["price_eur_kwh"].reindex(hour).to_numpy(dtype=float)
        known = e.loc[(et <= t) & (et > t - history)]
        clock = pd.DatetimeIndex(known["timestamp_utc"]).tz_convert(tz)
        slot = clock.hour * 60 + clock.minute
        mean = known[["pv_kw", "building_load_kw"]].groupby(np.asarray(slot)).mean()
        want = times.tz_convert(tz)
        keys = np.asarray(want.hour * 60 + want.minute)
        if not np.isin(keys, mean.index).all():
            continue
        pv = mean["pv_kw"].reindex(keys).to_numpy(dtype=float)
        load = mean["building_load_kw"].reindex(keys).to_numpy(dtype=float)
        net = load - pv
        last = e.loc[et <= t].iloc[-1]
        soc0 = float(np.clip(last["battery_soc_kwh"], lo, hi))
        terminal = th.battery_terminal_share * eta * eta * float(np.median(price))
        actions, path = _dp(price, net, soc0, (lo, hi), powers, moves, unit, terminal)
        p = powers[actions]
        planned = np.maximum(net + p, 0.0) * 0.25
        base = np.maximum(net, 0.0) * 0.25
        cost, base_cost = float(planned @ price), float(base @ price)
        night = (want.hour >= th.battery_night_start_h) | (want.hour < th.battery_night_end_h)
        grid_charge = (p > 0) & (net + p > 0) & np.asarray(night)
        tomorrow = np.asarray(want.normalize() > t.tz_convert(tz).normalize())
        surplus = float(np.maximum(pv - load, 0.0)[tomorrow].sum() * 0.25)
        room = hi - soc0
        why = f"forecast PV surplus tomorrow {surplus:.1f} kWh against headroom {room:.1f} kWh" + (
            ", so PV alone can fill the battery" if surplus > room else ""
        )
        charge = _runs(times, p > 0, t, tz)
        discharge = _runs(times, p < 0, t, tz)
        saving = base_cost - cost
        eid = _eid("BAT1", t)
        stamp_last = stamp(pd.Timestamp(last["timestamp_utc"]))
        sig = board.signal(
            eid,
            t,
            "tariff_published",
            ["BAT1", "PV1"],
            [f"tariff.csv:{stamp(t)}", f"energy.csv:{stamp_last}", "building.json:BAT1"],
            f"Prices to {local_time(end, t, tz)} published; BAT1 at {soc0:.1f} kWh",
            {
                "charge_windows": charge,
                "discharge_windows": discharge,
                "planned_import_kwh": round(float(planned.sum()), 1),
                "baseline_import_kwh": round(float(base.sum()), 1),
                "plan_cost_eur": round(cost, 2),
                "baseline_cost_eur": round(base_cost, 2),
                "saving_eur": round(saving, 2),
                "forecast_pv_kwh": round(float(pv.sum() * 0.25), 1),
                "soc_now_kwh": round(soc0, 1),
                "soc_min_kwh": round(float(path.min()), 1),
                "soc_max_kwh": round(float(path.max()), 1),
                "night_grid_charge": bool(grid_charge.any()),
                "night_rule": why,
            },
            "tariff",
        )
        board.propose(
            eid,
            sig,
            "battery_day_plan",
            "optimize_energy",
            "low",
            f"BAT1 plan to {local_time(end, t, tz)}: charge "
            f"{'; '.join(charge) or 'never'}; discharge {'; '.join(discharge) or 'never'}. "
            f"Forecast PV {float(pv.sum() * 0.25):.1f} kWh and load are the "
            f"{th.battery_history_d}-day mean at the same time of day. Grid import "
            f"{float(planned.sum()):.1f} kWh against {float(base.sum()):.1f} kWh with no "
            f"battery: EUR {cost:.1f} against EUR {base_cost:.1f}, saving EUR {saving:.1f} "
            f"against no battery (simulated). Night grid charge "
            f"{'planned' if grid_charge.any() else 'not planned'}: {why}.",
            f"Grid import cost EUR {cost:.1f} over the plan (simulated).",
            "SOC follows the plan within 10 kWh at each window boundary; replan if import "
            "exceeds the plan by 20 %.",
            0.6,
        )


def rows(
    ds: Dataset, settings: Settings, cache: Path, call: llm_guard.Call | None = None
) -> tuple[list[Row], Counter[str]]:
    """Return the s06 signal and proposal rows in time order; S06 sends no guest messages."""
    del cache, call
    board = _Board()
    cop = [s for s in heating.signals(ds, settings) if s.kind == "cop_low"]
    _preheat(ds, settings, board, [s.time for s in cop])
    _backoff(settings, board, cop)
    _stale(ds, settings, board)
    _battery(ds, settings, board)
    order = {"signal": 0, "proposal": 1}
    board.rows.sort(key=lambda r: (r.get("t_known") or r["t"], order[r["row"]], r["id"]))
    return board.rows, Counter()
