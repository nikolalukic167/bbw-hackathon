"""Read a PLATO-40 dataset directory into typed pandas frames.

Rules (ADR-002): an empty field is missing (NaN/NaT/<NA>), never zero; every
timestamp is parsed as timezone-aware UTC; nothing is clipped, filled, deduplicated
or sorted here. The audit reports those properties; later stages decide what to do.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from plato40.load.schema import (
    BUILDING_FILE,
    CSV_SCHEMAS,
    EVENTS_FILE,
    MANIFEST_FILE,
    FileSchema,
    Kind,
)


@dataclass(frozen=True)
class ParseIssue:
    """A value or header that did not match its declared schema."""

    file: str
    column: str
    problem: str
    count: int


@dataclass
class Dataset:
    """One loaded dataset (quickstart, dev or holdout).

    Attributes:
        path: Dataset directory.
        profile: ``manifest.json`` profile, e.g. ``quickstart`` or ``dev``.
        manifest: Parsed ``manifest.json``.
        building: Parsed ``building.json``.
        tables: CSV file name to frame, columns typed per schema.
        events: ``events.jsonl`` as a frame; fields absent for a type are missing.
        issues: Schema and parse problems found while loading.

    """

    path: Path
    profile: str
    manifest: dict[str, Any]
    building: dict[str, Any]
    tables: dict[str, pd.DataFrame]
    events: pd.DataFrame
    issues: list[ParseIssue] = field(default_factory=list)

    def table(self, name: str) -> pd.DataFrame:
        """Return one CSV table by file name."""
        return self.tables[name]

    def room_ids(self) -> list[str]:
        """Return room IDs declared in building.json, in declared order."""
        return [str(r["id"]) for r in self.building["building"]["rooms"]]

    def asset_ids(self) -> list[str]:
        """Return non-room asset IDs declared in building.json."""
        return [str(a["id"]) for a in self.building["building"]["assets"]]

    def resource_ids(self) -> list[str]:
        """Return staff and robot resource IDs declared in building.json."""
        return [str(r["resource_id"]) for r in self.building["staff"]["resources"]]


def _parse_column(
    raw: pd.Series, kind: Kind, file: str, column: str, issues: list[ParseIssue]
) -> pd.Series:
    """Convert a string column to its declared kind and record unparsable values."""
    present = raw.notna()
    parsed: pd.Series
    if kind is Kind.TIMESTAMP:
        parsed = pd.to_datetime(raw, utc=True, format="ISO8601", errors="coerce")
    elif kind in (Kind.FLOAT, Kind.FLAG):
        parsed = pd.to_numeric(raw, errors="coerce").astype("float64")
    elif kind is Kind.INT:
        parsed = pd.to_numeric(raw, errors="coerce").astype("Int64")
    else:
        return raw
    bad = int((present & parsed.isna()).sum())
    if bad:
        issues.append(ParseIssue(file, column, f"not parseable as {kind.value}", bad))
    return parsed


def read_csv(path: Path, schema: FileSchema, issues: list[ParseIssue]) -> pd.DataFrame:
    """Read one CSV with blank-as-missing semantics and type it per ``schema``."""
    declared = list(schema.columns)
    if not path.is_file():
        issues.append(ParseIssue(schema.name, "*", "file missing; read as empty", 1))
        raw = pd.DataFrame({c: pd.Series(dtype=object) for c in declared})
    else:
        raw = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    if list(raw.columns) != declared:
        issues.append(
            ParseIssue(
                schema.name,
                "*",
                f"header {list(raw.columns)} differs from declared {declared}",
                1,
            )
        )
    missing = [c for c in declared if c not in raw.columns]
    if missing:
        # A later stage reads a missing column as all-missing rather than crashing on it.
        issues.append(ParseIssue(schema.name, ",".join(missing), "column missing; all missing", 1))
        raw = raw.assign(**{c: pd.Series(pd.NA, index=raw.index, dtype=object) for c in missing})
    out = pd.DataFrame(index=raw.index)
    for column in raw.columns:
        kind = schema.columns.get(column, Kind.TEXT)
        out[column] = _parse_column(raw[column], kind, schema.name, column, issues)
    return out


def read_events(path: Path, issues: list[ParseIssue]) -> pd.DataFrame:
    """Read ``events.jsonl``; each line is one JSON object."""
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(json.loads(stripped))
            except json.JSONDecodeError:
                issues.append(ParseIssue(EVENTS_FILE, f"line {line_no}", "invalid JSON", 1))
    frame = pd.DataFrame.from_records(records)
    frame["timestamp_utc"] = pd.to_datetime(
        frame["timestamp_utc"], utc=True, format="ISO8601", errors="coerce"
    )
    return frame


def load_dataset(path: Path) -> Dataset:
    """Load every file of one dataset directory."""
    issues: list[ParseIssue] = []
    manifest = json.loads((path / MANIFEST_FILE).read_text(encoding="utf-8"))
    building = json.loads((path / BUILDING_FILE).read_text(encoding="utf-8"))
    tables = {s.name: read_csv(path / s.name, s, issues) for s in CSV_SCHEMAS}
    events = read_events(path / EVENTS_FILE, issues)
    return Dataset(
        path=path,
        profile=str(manifest["profile"]),
        manifest=manifest,
        building=building,
        tables=tables,
        events=events,
        issues=issues,
    )


def discover_datasets(root: Path) -> list[Path]:
    """Return dataset directories under ``root`` (or ``root`` itself), quickstart first.

    A dataset directory is one that contains ``manifest.json``. Order: quickstart, dev,
    then any other profile, each group sorted by path.
    """
    candidates = [root] if (root / MANIFEST_FILE).is_file() else []
    candidates += sorted(p for p in root.iterdir() if p.is_dir() and (p / MANIFEST_FILE).is_file())
    rank = {"quickstart": 0, "dev": 1}

    def key(p: Path) -> tuple[int, str]:
        profile = json.loads((p / MANIFEST_FILE).read_text(encoding="utf-8")).get("profile", "")
        return (rank.get(str(profile), 2), str(p))

    return sorted(candidates, key=key)
