from pathlib import Path

import pandas as pd

from plato40.load.known_at import as_of, lead_time_hours
from plato40.load.reader import ParseIssue, read_csv
from plato40.load.schema import schema_by_name

SCHEMAS = schema_by_name()


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_blank_is_missing_not_zero(tmp_path: Path) -> None:
    csv = _write(
        tmp_path / "water_main.csv",
        "timestamp_utc,meter_total_l\n2026-01-01T00:00:00Z,10.0\n2026-01-01T00:15:00Z,\n",
    )
    issues: list[ParseIssue] = []
    frame = read_csv(csv, SCHEMAS["water_main.csv"], issues)
    assert frame["meter_total_l"].isna().tolist() == [False, True]
    assert issues == []


def test_timestamps_are_utc_aware(tmp_path: Path) -> None:
    csv = _write(
        tmp_path / "water_main.csv",
        "timestamp_utc,meter_total_l\n2026-03-29T01:00:00Z,1.0\n",
    )
    frame = read_csv(csv, SCHEMAS["water_main.csv"], [])
    assert str(frame["timestamp_utc"].dt.tz) == "UTC"


def test_unparseable_value_is_reported(tmp_path: Path) -> None:
    csv = _write(
        tmp_path / "water_main.csv",
        "timestamp_utc,meter_total_l\n2026-01-01T00:00:00Z,abc\n",
    )
    issues: list[ParseIssue] = []
    frame = read_csv(csv, SCHEMAS["water_main.csv"], issues)
    assert frame["meter_total_l"].isna().all()
    assert issues == [ParseIssue("water_main.csv", "meter_total_l", "not parseable as float", 1)]


def test_header_mismatch_is_reported(tmp_path: Path) -> None:
    csv = _write(tmp_path / "water_main.csv", "time,meter\n2026-01-01T00:00:00Z,1\n")
    issues: list[ParseIssue] = []
    read_csv(csv, SCHEMAS["water_main.csv"], issues)
    assert issues and issues[0].column == "*"


def test_as_of_excludes_rows_published_later() -> None:
    schema = SCHEMAS["tariff.csv"]
    frame = pd.DataFrame(
        {
            "timestamp_utc": pd.to_datetime(["2026-01-02T00:00Z", "2026-01-03T00:00Z"]),
            "price_eur_kwh": [0.2, 0.3],
            "published_utc": pd.to_datetime(["2026-01-01T12:00Z", "2026-01-02T12:00Z"]),
        }
    )
    known = as_of(frame, schema, pd.Timestamp("2026-01-02T06:00Z"))
    assert known["price_eur_kwh"].tolist() == [0.2]
    assert lead_time_hours(frame, schema).tolist() == [12.0, 12.0]
