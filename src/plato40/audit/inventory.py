"""Section 1: inventory of files, schema conformance and documentation cross-checks."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pandas as pd

from plato40.audit.render import fmt, iso, table
from plato40.load.reader import Dataset
from plato40.load.schema import CSV_SCHEMAS, DOC_FILES, EVENTS_FILE, FileSchema

README_EVENT_TYPES = (
    "checkin",
    "checkin_early",
    "checkin_after_wait",
    "arrival_waiting_room",
    "room_reassigned",
    "checkout",
    "checkout_early",
    "no_show",
    "stay_in_progress",
    "cleaning_started",
    "cleaning_completed",
    "robot_floor_cleaning_started",
    "robot_floor_cleaning_completed",
    "oncall_housekeeping_called",
    "floor_bin_emptied",
    "waste_pickup",
    "waste_bin_full",
    "vending_restocked",
    "vending_out_of_stock",
    "bms_alarm",
)
README_EQUIPMENT_METRICS = {
    "HP1": ("heat_output_kw", "electric_kw", "supply_temp_c", "status"),
    "AHU1": ("airflow_m3h", "filter_dp_pa", "fan_kw", "supply_air_c"),
}


def _median_step_minutes(frame: pd.DataFrame, schema: FileSchema) -> str:
    """Median spacing of the time column within each key series, in minutes."""
    if schema.cadence_minutes is None:
        return "irregular"
    group_cols = [c for c in schema.primary_key if c != schema.time_column]
    ts = frame[[*group_cols, schema.time_column]].drop_duplicates()
    if group_cols:
        steps = ts.sort_values(schema.time_column).groupby(group_cols)[schema.time_column].diff()
    else:
        steps = ts[schema.time_column].sort_values().diff()
    return fmt(pd.Timedelta(steps.dropna().median()).total_seconds() / 60.0, 0)


def file_inventory(ds: Dataset) -> list[list[str]]:
    """Return one inventory row per CSV file plus events.jsonl."""
    rows: list[list[str]] = []
    for schema in CSV_SCHEMAS:
        frame = ds.table(schema.name)
        times = frame[schema.time_column]
        dup = int(frame.duplicated(list(schema.primary_key)).sum())
        rows.append(
            [
                f"`{schema.name}`",
                fmt(len(frame), 0),
                str(frame.shape[1]),
                f"{iso(times.min())} .. {iso(times.max())}",
                _median_step_minutes(frame, schema),
                ", ".join(schema.primary_key),
                fmt(dup, 0),
            ]
        )
    ev = ds.events
    rows.append(
        [
            f"`{EVENTS_FILE}`",
            fmt(len(ev), 0),
            str(ev.shape[1]),
            f"{iso(ev['timestamp_utc'].min())} .. {iso(ev['timestamp_utc'].max())}",
            "irregular",
            "event_id",
            fmt(int(ev["event_id"].duplicated().sum()), 0),
        ]
    )
    return rows


def dtype_table(ds: Dataset) -> list[list[str]]:
    """Return file, column, declared kind and loaded pandas dtype for every column."""
    rows: list[list[str]] = []
    for schema in CSV_SCHEMAS:
        frame = ds.table(schema.name)
        for col, kind in schema.columns.items():
            dtype = str(frame[col].dtype) if col in frame.columns else "absent"
            rows.append([f"`{schema.name}`", f"`{col}`", kind.value, dtype])
    return rows


def doc_mismatches(ds: Dataset) -> list[str]:
    """Return statements where the data differs from README.md or building.json."""
    out: list[str] = []
    span_days = (pd.Timestamp(ds.manifest["end_utc"]) - pd.Timestamp(ds.manifest["start_utc"])).days
    if span_days != ds.manifest["days"]:
        out.append(f"manifest `days`={ds.manifest['days']} but start..end spans {span_days} d.")
    observed_types = set(ds.events["type"].dropna().unique())
    missing = sorted(set(README_EVENT_TYPES) - observed_types)
    extra = sorted(observed_types - set(README_EVENT_TYPES))
    if missing:
        out.append(f"README event types absent from this dataset: {', '.join(missing)}.")
    if extra:
        out.append(f"Event types not in the README: {', '.join(extra)}.")
    eq = ds.table("equipment.csv")
    for asset in ds.building["building"]["assets"]:
        if asset["id"] not in README_EQUIPMENT_METRICS:
            continue
        declared = set(asset["metrics"])
        observed = set(eq.loc[eq["asset_id"] == asset["id"], "metric"].unique())
        if declared - observed:
            out.append(
                f"building.json lists {asset['id']} metric(s) "
                f"{', '.join(sorted(declared - observed))} that never appear in equipment.csv."
            )
        if observed - declared:
            out.append(
                f"equipment.csv has {asset['id']} metric(s) "
                f"{', '.join(sorted(observed - declared))} not declared in building.json."
            )
    status = eq.loc[(eq["asset_id"] == "HP1") & (eq["metric"] == "status"), "value"]
    undocumented = sorted(set(status.dropna()) - {"running", "standby", "defrost"})
    if undocumented:
        out.append(
            f"HP1 status codes beyond the documented running/standby/defrost: "
            f"{', '.join(undocumented)} (README allows other codes)."
        )
    for asset in ds.building["building"]["assets"]:
        if asset["id"] == "BAT1":
            soc = ds.table("energy.csv")["battery_soc_kwh"]
            lo, hi = asset["soc_bounds_kwh"]
            out.append(
                f"BAT1 SOC observed {fmt(soc.min())}..{fmt(soc.max())} kWh "
                f"vs declared bounds {lo}..{hi} kWh."
            )
    return out


def contract_fields(path: Path) -> tuple[list[str], list[str], list[str]]:
    """Parse DECISION_CONTRACT.md into required fields, optional fields and actions."""
    text = path.read_text(encoding="utf-8")
    sections = re.split(r"^## ", text, flags=re.MULTILINE)

    def fields(title: str) -> list[str]:
        body = next((s for s in sections if s.startswith(title)), "")
        return re.findall(r"^\| `([a-z_]+)` \|", body, flags=re.MULTILINE)

    vocab_body = next((s for s in sections if s.startswith("Action vocabulary")), "")
    actions = re.findall(r"`([a-z_]+)`", vocab_body.split("Other actions")[0])
    return fields("Required fields"), fields("Optional fields"), actions


def doc_hashes(datasets: list[Dataset]) -> list[list[str]]:
    """Return the SHA-256 prefix of each documentation file per dataset."""
    rows: list[list[str]] = []
    for name in DOC_FILES:
        digests = []
        for ds in datasets:
            p = ds.path / name
            digests.append(hashlib.sha256(p.read_bytes()).hexdigest()[:12] if p.is_file() else "-")
        rows.append([f"`{name}`", *digests])
    return rows


def render(datasets: list[Dataset]) -> str:
    """Return the markdown for section 1."""
    parts = ["## 1. Inventory", ""]
    parts.append(
        "Hypothesis: every file matches the column list in the dataset README, telemetry is "
        "on a 15-minute grid, hourly series (tariff, logistics) on a 60-minute grid, and "
        "each file has a natural primary key with no duplicates."
    )
    for ds in datasets:
        parts += [
            "",
            f"### 1.{datasets.index(ds) + 1} `{ds.profile}` ({ds.path.name})",
            "",
            f"Manifest: {ds.manifest['start_utc']} to {ds.manifest['end_utc']} "
            f"({ds.manifest['days']} d), interval {ds.manifest['interval_seconds']} s, "
            f"dataset version {ds.manifest['dataset_version']}.",
            "",
            table(
                [
                    "file",
                    "rows",
                    "cols",
                    "time range (UTC)",
                    "median step (min)",
                    "key",
                    "duplicate keys",
                ],
                file_inventory(ds),
            ),
            "",
        ]
        if ds.issues:
            parts.append(
                table(
                    ["file", "column", "problem", "count"],
                    [[i.file, i.column, i.problem, fmt(i.count, 0)] for i in ds.issues],
                )
            )
        else:
            parts.append("All headers match the declared schema; every non-empty value parsed.")
        mismatches = doc_mismatches(ds)
        parts += ["", "Differences between data and documentation:", ""]
        parts += [f"- {m}" for m in mismatches] or ["- none found."]
    parts += [
        "",
        "Column types after loading (identical for all datasets; blank = missing):",
        "",
        "<details><summary>Column table</summary>",
        "",
        table(["file", "column", "declared", "loaded dtype"], dtype_table(datasets[0])),
        "",
        "</details>",
        "",
        "Units (from `manifest.json`): "
        + "; ".join(f"{k} = {v}" for k, v in datasets[0].manifest["units"].items())
        + ".",
        "",
        "Known limitations stated by `manifest.json`:",
        "",
    ]
    parts += [f"- {lim}" for lim in datasets[0].manifest["limitations"]]
    parts += [
        "",
        "Documentation files are byte-identical across datasets when the digests match:",
        "",
        table(["file", *[ds.profile for ds in datasets]], doc_hashes(datasets)),
        "",
    ]
    contract = datasets[0].path / "DECISION_CONTRACT.md"
    required, optional, actions = contract_fields(contract)
    parts += [
        f"### 1.{len(datasets) + 1} Decision contract",
        "",
        f"`DECISION_CONTRACT.md` is shipped inside every dataset directory "
        f"(`<data_dir>/DECISION_CONTRACT.md`). It defines {len(required)} required fields: "
        + ", ".join(f"`{f}`" for f in required)
        + f". It recommends {len(optional)} optional fields: "
        + ", ".join(f"`{f}`" for f in optional)
        + f". The action vocabulary has {len(actions)} verbs: "
        + ", ".join(f"`{a}`" for a in actions)
        + ". Other verbs are allowed if documented in our README. Two rules matter for "
        "design: the decision `timestamp_utc` must only use information available at that "
        "time, and a consent requirement or a proposed message is never evidence of "
        "consent or delivery.",
        "",
    ]
    return "\n".join(parts)
