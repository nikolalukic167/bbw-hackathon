"""Sections 8 and 9: detector design hypotheses tied to evidence, and open questions.

The prose is ours; every number in it is recomputed here from the same functions the
earlier sections use, so the text cannot drift from the data.
"""

from __future__ import annotations

import pandas as pd

from plato40.audit import baselines, integrity, scenarios, seasonality, timing
from plato40.audit import candidates as cand
from plato40.audit.render import fmt
from plato40.load import derive
from plato40.load.reader import Dataset
from plato40.settings import Settings


def _by_profile(datasets: list[Dataset], profile: str) -> Dataset | None:
    return next((ds for ds in datasets if ds.profile == profile), None)


def _count(ds: Dataset, settings: Settings, scenario: str, kind: str) -> int:
    return sum(
        1 for c in cand.all_candidates(ds, settings) if c.scenario == scenario and c.kind == kind
    )


def _scenario_of(kind: str) -> str:
    return {
        "persistent_room_night_flow": "S02",
        "loud_room_night_volume": "S02",
        "night_gap_main_minus_rooms": "S02",
        "cop_below_outdoor_fit": "S03",
        "filter_dp_high": "S04",
    }[kind]


def cross_fit_ratio(
    train: Dataset, test: Dataset, settings: Settings
) -> tuple[float, float, float]:
    """Fit COP on outdoor in ``train``; return (slope, intercept, min COP ratio in ``test``)."""
    tr = derive.daily_cop(train, settings.local_tz)
    slope, intercept = cand.theil_sen(tr["outdoor_c"].to_numpy(), tr["cop"].to_numpy())
    te = derive.daily_cop(test, settings.local_tz)
    ratio = te["cop"] / (intercept + slope * te["outdoor_c"])
    return slope, intercept, float(ratio.min())


def normal_night_gap(ds: Dataset, settings: Settings) -> float:
    """Return the p95 median-night gap (L/h) over nights outside gap candidates."""
    gap = cand.night_gap(ds, settings)
    for c in cand.all_candidates(ds, settings):
        if c.kind == "night_gap_main_minus_rooms":
            days = pd.date_range(
                c.start.tz_convert(settings.local_tz).normalize(),
                c.end.tz_convert(settings.local_tz),
                freq="D",
            ).date
            gap = gap.loc[~gap.index.isin(days)]
    return float(gap.quantile(0.95))


def render(datasets: list[Dataset], settings: Settings) -> str:
    """Return the markdown for sections 8 and 9."""
    qs = _by_profile(datasets, "quickstart")
    dev = _by_profile(datasets, "dev")
    ref = dev or datasets[0]
    th = settings.thresholds
    nb = baselines.night_water_baseline(ref, settings)
    cl = baselines.cleaning_durations(ref)
    room_clean = cl.loc[cl["kind"] == "room", "minutes"]
    matches = timing.match_alarms(ref, settings)
    room_delays = pd.Series(
        [
            m.delay_h
            for m in matches
            if m.code in ("ROOM_TEMP_LOW", "ROOM_TEMP_HIGH", "SENSOR_COMM_LOSS")
            and m.delay_h is not None
        ],
        dtype=float,
    )
    unsupported = sum(1 for m in matches if m.delay_h is None)
    unalarmed = {r[0]: (r[1], r[2]) for r in timing.unalarmed_episodes(ref, 1.0, settings)}
    pers = scenarios.followed_by_order(ref, settings, "persistent_room_night_flow")
    loud = scenarios.followed_by_order(ref, settings, "loud_room_night_volume")
    gap_cands = [
        c for c in cand.all_candidates(ref, settings) if c.kind == "night_gap_main_minus_rooms"
    ]
    night_gap = cand.night_gap(ref, settings)
    gap_level = (
        float(
            pd.concat(
                [
                    night_gap.loc[
                        c.start.tz_convert(settings.local_tz).date() : c.end.tz_convert(
                            settings.local_tz
                        ).date()
                    ]
                    for c in gap_cands
                ]
            ).median()
        )
        if gap_cands
        else float("nan")
    )
    eq = derive.equipment_numeric(ref)
    dp = eq["AHU1/filter_dp_pa"]
    mode_day = eq["AHU1/fan_kw"] >= timing.AHU_DAY_MODE_FAN_KW
    dp_ratio = dp[~mode_day].median() / dp[mode_day].median()
    roster = ref.table("staff_roster.csv")
    sick = roster.loc[roster["status"] == "sick"]
    sick_notice = (sick["start_utc"] - sick["status_known_utc"]).dt.total_seconds() / 3600.0
    oos = roster.loc[roster["status"] == "out_of_service"]
    oos_late = int((oos["status_known_utc"] > oos["start_utc"]).sum())
    bk = ref.table("bookings.csv")
    bk_lead = (bk["arrival_utc"] - bk["created_utc"]).dt.total_seconds() / 3600.0 / 24.0
    kinds = [
        "persistent_room_night_flow",
        "loud_room_night_volume",
        "night_gap_main_minus_rooms",
        "cop_below_outdoor_fit",
        "filter_dp_high",
    ]
    per_kind = [_count(ref, settings, _scenario_of(k), k) for k in kinds]
    max_temp = max(float(d.table("room_telemetry.csv")["temperature_c"].max()) for d in datasets)
    lines = [
        "## 8. Implications for detector design",
        "",
        f"Each item is a design hypothesis for the detectors, with the evidence above that "
        f"supports it. Numbers refer to `{ref.profile}` unless stated.",
        "",
        f"1. **Room leak = persistence, not magnitude.** Outside S02 candidates, the median "
        f"room has a zero 15-minute night interval on {fmt(nb['zero_floor_pct'].median())} % "
        f"of nights (lowest room {fmt(nb['zero_floor_pct'].min())} %). A night with flow in "
        f"every interval is therefore abnormal on its own; two such nights in a row gave "
        f"{pers[1]} persistent candidates, {pers[0]} of them followed within 14 d by a "
        f"corrective order on the same room ({'; '.join(pers[2]) or 'none'}). Evidence: "
        "sections 5 and 6.",
        f"2. **Burst = volume in a vacant or sleeping room.** A night volume of "
        f">= {fmt(th.burst_night_volume_l, 0)} L found "
        f"{loud[1]} candidates, {loud[0]} of them followed by a corrective order on the same "
        f"room ({'; '.join(loud[2]) or 'none'}). A daytime burst would escape a night-only "
        "window, so "
        "the detector should also test sustained high flow in a vacant room at any hour.",
        f"3. **Leaks outside rooms show only in the main-minus-rooms gap at night.** The p95 "
        f"of the median night gap on normal nights is {fmt(normal_night_gap(ref, settings))} "
        f"L/h; on the {len(gap_cands)} gap candidate(s) the median night gap is "
        f"{fmt(gap_level)} L/h. Daily gap totals are confounded by the every-other-day common-area "
        "draw (section 5) and must not be used.",
    ]
    if qs is not None and dev is not None:
        slope, intercept, qs_ratio = cross_fit_ratio(dev, qs, settings)
        lines.append(
            f"4. **Heat pump degradation needs a reference curve from outside the window.** "
            f"On dev, COP = {fmt(intercept, 2)} + {fmt(slope, 3)} x outdoor degC (Theil-Sen) "
            f"and {_count(dev, settings, 'S03', 'cop_below_outdoor_fit')} low-COP candidates "
            "precede refrigerant-leak work orders. On quickstart the same screen, fitted on "
            f"its own 21 days, finds {_count(qs, settings, 'S03', 'cop_below_outdoor_fit')} "
            "because the cold spell and the degradation coincide; against the dev curve the "
            f"quickstart minimum daily COP ratio is {fmt(qs_ratio, 2)}. A detector should "
            "carry a fixed reference curve plus the comfort-deficit and lockout signals, and "
            "must not refit on the window it judges."
        )
    lines += [
        f"5. **Comfort is judged against the setpoint and occupancy, not the band alone.** "
        "Vacant setback rooms and guests who chose a low setpoint are below 20 degC by "
        f"design; requiring a {fmt(th.comfort_deficit_k)} K shortfall in an occupied room "
        "with the window closed isolates plant problems (section 5, S03).",
        "6. **Filter degradation: day-mode only, fan power as the second signal.** Night "
        f"(setback) filter dp is {fmt(dp_ratio, 2)} times the day-mode median and dp has "
        "multi-day gaps; fan power keeps reporting "
        "and rises through the gap until it hits its ceiling, then airflow falls (section 5, "
        "S04). A dp gap is reported as `data_quality`, never as recovery.",
        f"7. **BMS alarms corroborate; they never trigger alone.** Room and comm-loss alarms "
        f"lag their condition by a median {fmt(room_delays.median(), 2)} h; "
        f"{unsupported} raised alarms have no supporting telemetry; "
        f"{fmt(unalarmed.get('ROOM_TEMP_LOW', ('-', '-'))[1], 0)} of "
        f"{fmt(unalarmed.get('ROOM_TEMP_LOW', ('-', '-'))[0], 0)} low-temperature episodes "
        "of 1 h or more raise no alarm (section 4).",
        "8. **Gateway outages are one data-quality episode per floor**, not ten room "
        "incidents: whole-floor blanks are the signature (section 2). Rooms in an outage get "
        "lower confidence and deferred verdicts, never a zero reading.",
        f"9. **Turnaround readiness is predictable from the queue.** A human room cleaning "
        f"takes a median {fmt(room_clean.median(), 0)} min (p90 "
        f"{fmt(room_clean.quantile(0.9), 0)} min); waits cluster on weekends with near-full "
        "occupancy and a sick cleaner (sections 5 and 6). Capacity is cleaners present times "
        "shift length over the per-room time; robots shorten it but never release a room.",
        f"10. **Plan S05 at notice time.** Bookings are created a median "
        f"{fmt(bk_lead.median())} d before arrival and sick notes arrive "
        f"{fmt(sick_notice.min())} to {fmt(sick_notice.max())} h before the shift "
        f"(section 4); {oos_late} of {len(oos)} robot out-of-service rows became known "
        "only after the shift had started. The weekend plan is re-run whenever a roster "
        "row becomes known, not when guests start waiting.",
        "11. **Local time everywhere.** Arrivals, tariffs, forecasts and shifts follow "
        "Europe/Vienna across DST (section 2); all windows (night, check-in 15:00, shift "
        "07:00-15:30) are computed in local time from UTC.",
        "",
        "Risks for the holdout:",
        "",
        f"- Few instances per class (section 7): leak, heat pump and filter screens rest on "
        f"{min(per_kind)} to {max(per_kind)} {ref.profile} episodes per kind. Prefer rules "
        "with physical meaning (zero night floor, setpoint shortfall) over fitted cut-offs.",
        "- A short holdout window, or one inside a cold spell, breaks any baseline fitted on "
        "the holdout itself (item 4).",
        "- New codes (HP1 status, alarm codes, resource IDs) and documented-but-absent values "
        "(`inspection_required`) can appear; the loader must accept and report them, not "
        "crash.",
        "- Data-quality traps seen here will recur in new places: duplicates, a frozen "
        f"sensor, an isolated {fmt(max_temp, 0)} degC room temperature, meter exchange with "
        "jitter, multi-day dp gaps.",
        "- Screens in this report use hindsight; a detector restricted to information known "
        "at decision time will fire later than the candidate starts listed here.",
        "",
    ]
    lines += open_questions(datasets, settings, room_delays.median())
    return "\n".join(lines)


def open_questions(datasets: list[Dataset], settings: Settings, lag_h: float) -> list[str]:
    """Return section 9: numbered questions for the case mentor."""
    ds = datasets[-1]
    dev = _by_profile(datasets, "dev")
    if dev is not None:
        m = seasonality.monthly_table(dev, settings)
        warm = m.loc[[5, 6, 7, 8, 9], "comfort_deficit_room_h"]
        top = sorted(int(k) for k in warm.nlargest(3).index)
        months = ", ".join(pd.Timestamp(2026, k, 1).strftime("%B") for k in top)
    else:
        months = "warm months"
    undeclared = sorted(
        {
            u.strip()
            for row in integrity.integrity_rows(ds)
            if row[2] != "-"
            for u in row[2].split(",")
        }
    )
    return [
        "## 9. Open questions for the case mentor",
        "",
        "1. Code freeze: `CHALLENGE.md` section 10 says Saturday 14:00 sharp; other "
        "channels say 15:00. Which is binding for the repository and for "
        "`decisions_holdout.jsonl`?",
        "2. Cancellations: `bookings.csv` has `created_utc` but no time at which a "
        "cancellation became known. May we treat a cancellation as known from "
        "`created_utc`, or must we treat it as unknown until the arrival time?",
        "3. Scoring: how does the automated check match a decision to an incident (asset, "
        "time window, `issue_type` string, `episode_id`)? Do routine autonomous decisions "
        "(battery schedule, setpoints) count toward alert spam?",
        "4. Holdout: same `building.json`, same length and season as dev, or a shorter "
        "window? This decides whether detectors may fit any baseline on the holdout itself.",
        f"5. BMS alarms: are the rule thresholds and the {fmt(lag_h * 60, 0)}-minute lag "
        "intended? Are "
        "`SENSOR_COMM_LOSS` alarms without missing telemetry deliberate false alarms?",
        f"6. Shoulder-season comfort: occupied rooms fall well below their setpoint mostly in "
        f"{months} (section 7). Is heating switched off or limited by a "
        "season or night-flushing rule in the simulator, and is that an intended S03 "
        "instance?",
        "7. `building.json` declares HP1 metric `cop`, which never appears in "
        "`equipment.csv`. Is COP meant to be derived (heat output over electric input)?",
        "8. Room release: the rules require `inspection_passed_if_required`, but "
        "`inspection_required` never occurs and no inspection event exists. Is `clean` "
        "after `cleaning_completed` sufficient to release, and who may inspect?",
        "9. IDs outside `building.json` appear in events: "
        + ", ".join(f"`{u}`" for u in undeclared)
        + ". May decisions reference them in `affected_assets` or `resource_id`?",
        "10. Robots carrying bins are marked 'not used in current operations'. May our "
        "planner dispatch them for bins, and is the 20-minute carry time binding?",
        "11. On-call housekeeper `ONCALL_1` has no roster rows. Is it always available "
        "15:30-22:00 local, and how is its cost to be represented (approval needed)?",
        "12. Attribution: the dataset README writes 'Proximata Hackathon '26 \u2013 Rhomberg "
        "track'. Should the README attribution reproduce this string exactly?",
        "",
    ]
