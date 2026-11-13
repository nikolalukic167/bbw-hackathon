"""Declared schema of every PLATO-40 data file, taken from the dataset README.

The schema is the contract the loader checks against. A file whose header differs
from its declaration fails loudly; the audit then reports the difference instead of
guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    """Logical column type after loading."""

    TIMESTAMP = "timestamp_utc"
    FLOAT = "float"
    INT = "int"
    TEXT = "text"
    FLAG = "flag_0_1"
    MIXED = "mixed"


@dataclass(frozen=True)
class FileSchema:
    """Declared layout of one tabular file.

    Attributes:
        name: File name inside the dataset directory.
        columns: Column name to logical kind, in file order.
        primary_key: Columns that should identify a row uniquely.
        time_column: Column holding the effective (observed or scheduled) time.
        known_at_column: Column stating when the row became known, if the file has one.
        cadence_minutes: Expected spacing of ``time_column`` per key, if regular.

    """

    name: str
    columns: dict[str, Kind]
    primary_key: tuple[str, ...]
    time_column: str
    known_at_column: str | None = None
    cadence_minutes: int | None = None


TS = Kind.TIMESTAMP

CSV_SCHEMAS: tuple[FileSchema, ...] = (
    FileSchema(
        "bookings.csv",
        {
            "booking_id": Kind.TEXT,
            "room_id": Kind.TEXT,
            "created_utc": TS,
            "arrival_utc": TS,
            "departure_utc": TS,
            "guest_count": Kind.INT,
            "channel": Kind.TEXT,
            "status": Kind.TEXT,
        },
        ("booking_id",),
        "arrival_utc",
        known_at_column="created_utc",
    ),
    FileSchema(
        "access_log.csv",
        {"timestamp_utc": TS, "door_id": Kind.TEXT, "credential": Kind.TEXT, "result": Kind.TEXT},
        ("timestamp_utc", "door_id", "credential"),
        "timestamp_utc",
    ),
    FileSchema(
        "room_telemetry.csv",
        {
            "timestamp_utc": TS,
            "room_id": Kind.TEXT,
            "temperature_c": Kind.FLOAT,
            "setpoint_c": Kind.FLOAT,
            "humidity_pct": Kind.FLOAT,
            "co2_ppm": Kind.FLOAT,
            "presence": Kind.FLAG,
            "window_open": Kind.FLAG,
            "housekeeping_state": Kind.TEXT,
        },
        ("timestamp_utc", "room_id"),
        "timestamp_utc",
        cadence_minutes=15,
    ),
    FileSchema(
        "room_water.csv",
        {"timestamp_utc": TS, "room_id": Kind.TEXT, "meter_total_l": Kind.FLOAT},
        ("timestamp_utc", "room_id"),
        "timestamp_utc",
        cadence_minutes=15,
    ),
    FileSchema(
        "water_main.csv",
        {"timestamp_utc": TS, "meter_total_l": Kind.FLOAT},
        ("timestamp_utc",),
        "timestamp_utc",
        cadence_minutes=15,
    ),
    FileSchema(
        "energy.csv",
        {
            "timestamp_utc": TS,
            "building_load_kw": Kind.FLOAT,
            "hvac_electric_kw": Kind.FLOAT,
            "pv_kw": Kind.FLOAT,
            "battery_discharge_kw_signed": Kind.FLOAT,
            "battery_soc_kwh": Kind.FLOAT,
            "grid_import_kw": Kind.FLOAT,
            "grid_export_kw": Kind.FLOAT,
        },
        ("timestamp_utc",),
        "timestamp_utc",
        cadence_minutes=15,
    ),
    FileSchema(
        "weather.csv",
        {"timestamp_utc": TS, "outdoor_c": Kind.FLOAT, "cloud_cover_pct": Kind.FLOAT},
        ("timestamp_utc",),
        "timestamp_utc",
        cadence_minutes=15,
    ),
    FileSchema(
        "weather_forecast.csv",
        {"issued_utc": TS, "target_utc": TS, "outdoor_c_forecast": Kind.FLOAT},
        ("issued_utc", "target_utc"),
        "target_utc",
        known_at_column="issued_utc",
    ),
    FileSchema(
        "tariff.csv",
        {"timestamp_utc": TS, "price_eur_kwh": Kind.FLOAT, "published_utc": TS},
        ("timestamp_utc",),
        "timestamp_utc",
        known_at_column="published_utc",
        cadence_minutes=60,
    ),
    FileSchema(
        "equipment.csv",
        {"timestamp_utc": TS, "asset_id": Kind.TEXT, "metric": Kind.TEXT, "value": Kind.MIXED},
        ("timestamp_utc", "asset_id", "metric"),
        "timestamp_utc",
        cadence_minutes=15,
    ),
    FileSchema(
        "logistics.csv",
        {"timestamp_utc": TS, "asset_id": Kind.TEXT, "metric": Kind.TEXT, "value": Kind.FLOAT},
        ("timestamp_utc", "asset_id", "metric"),
        "timestamp_utc",
        cadence_minutes=60,
    ),
    FileSchema(
        "staff_roster.csv",
        {
            "shift_id": Kind.TEXT,
            "resource_id": Kind.TEXT,
            "role": Kind.TEXT,
            "start_utc": TS,
            "end_utc": TS,
            "status": Kind.TEXT,
            "status_known_utc": TS,
        },
        ("shift_id",),
        "start_utc",
        known_at_column="status_known_utc",
    ),
    FileSchema(
        "work_orders.csv",
        {
            "work_order_id": Kind.TEXT,
            "created_utc": TS,
            "asset_id": Kind.TEXT,
            "type": Kind.TEXT,
            "status": Kind.TEXT,
            "scheduled_utc": TS,
            "completed_utc": TS,
            "assigned_to": Kind.TEXT,
            "summary": Kind.TEXT,
        },
        ("work_order_id",),
        "scheduled_utc",
        known_at_column="created_utc",
    ),
    FileSchema(
        "guest_requests.csv",
        {
            "request_id": Kind.TEXT,
            "timestamp_utc": TS,
            "room_id": Kind.TEXT,
            "booking_id": Kind.TEXT,
            "channel": Kind.TEXT,
            "language": Kind.TEXT,
            "request_type": Kind.TEXT,
            "text": Kind.TEXT,
            "priority": Kind.TEXT,
            "response_due_utc": TS,
            "requires_guest_consent": Kind.TEXT,
        },
        ("request_id",),
        "timestamp_utc",
    ),
)

EVENTS_FILE = "events.jsonl"
BUILDING_FILE = "building.json"
MANIFEST_FILE = "manifest.json"
DOC_FILES = ("README.md", "DECISION_CONTRACT.md", "CHALLENGE.md")
SAMPLE_FILES = ("decisions_sample.jsonl", "insights_sample.jsonl")


def schema_by_name() -> dict[str, FileSchema]:
    """Return the CSV schemas keyed by file name."""
    return {s.name: s for s in CSV_SCHEMAS}
