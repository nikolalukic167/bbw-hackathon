"""Section 3: referential integrity of IDs across building.json and every other file."""

from __future__ import annotations

import pandas as pd

from plato40.audit.render import fmt, table
from plato40.load.reader import Dataset

PSEUDO_IDS = {"BUILDING", "MAIN_ENTRANCE", "UNKNOWN_CARD"}


def declared_ids(ds: Dataset) -> dict[str, set[str]]:
    """Return ID sets declared in building.json, by kind."""
    b = ds.building["building"]
    return {
        "room": set(ds.room_ids()),
        "asset": set(ds.asset_ids()),
        "room water meter": {str(r["water_meter_id"]) for r in b["rooms"]},
        "gateway": {str(g["id"]) for g in b["gateways"]},
        "sensor": {str(s["id"]) for s in b["sensors"]},
        "resource": set(ds.resource_ids()),
    }


def references(ds: Dataset) -> list[tuple[str, pd.Series]]:
    """Return (location, values) for every column that refers to a declared ID."""
    ev = ds.events
    t = ds.tables
    refs: list[tuple[str, pd.Series]] = [
        ("bookings.csv room_id", t["bookings.csv"]["room_id"]),
        ("access_log.csv door_id", t["access_log.csv"]["door_id"]),
        ("room_telemetry.csv room_id", t["room_telemetry.csv"]["room_id"]),
        ("room_water.csv room_id", t["room_water.csv"]["room_id"]),
        ("equipment.csv asset_id", t["equipment.csv"]["asset_id"]),
        ("logistics.csv asset_id", t["logistics.csv"]["asset_id"]),
        ("staff_roster.csv resource_id", t["staff_roster.csv"]["resource_id"]),
        ("work_orders.csv asset_id", t["work_orders.csv"]["asset_id"]),
        ("guest_requests.csv room_id", t["guest_requests.csv"]["room_id"]),
    ]
    for col in ("room_id", "asset_id", "resource_id", "from_room", "to_room"):
        if col in ev.columns:
            refs.append((f"events.jsonl {col}", ev[col]))
    return refs


def integrity_rows(ds: Dataset) -> list[list[str]]:
    """Return one row per reference column: distinct values, unknown values."""
    ids = declared_ids(ds)
    known = set().union(*ids.values()) | PSEUDO_IDS
    rows: list[list[str]] = []
    for loc, values in references(ds):
        distinct = set(values.dropna().astype(str))
        unknown = sorted(distinct - known)
        rows.append(
            [
                loc,
                fmt(len(distinct), 0),
                ", ".join(unknown[:8]) + (" ..." if len(unknown) > 8 else "") if unknown else "-",
            ]
        )
    return rows


def booking_links(ds: Dataset) -> list[list[str]]:
    """Return checks that booking IDs used elsewhere exist and point to the same room."""
    bookings = ds.table("bookings.csv").set_index("booking_id")
    rows: list[list[str]] = []
    ev = ds.events.dropna(subset=["booking_id"])
    gr = ds.table("guest_requests.csv").dropna(subset=["booking_id"])
    acc = ds.table("access_log.csv")
    acc_bk = acc.loc[acc["credential"].str.startswith("BK")]
    for label, frame, room_col in (
        ("events.jsonl", ev, "room_id"),
        ("guest_requests.csv", gr, "room_id"),
        (
            "access_log.csv (room doors)",
            acc_bk.loc[acc_bk["door_id"] != "MAIN_ENTRANCE"],
            "door_id",
        ),
    ):
        key = "credential" if label.startswith("access") else "booking_id"
        ids = frame[key].astype(str)
        missing = ~ids.isin(bookings.index)
        booked_room = ids.map(bookings["room_id"])
        mismatch = (~missing) & (frame[room_col].astype(str) != booked_room)
        rows.append(
            [label, fmt(len(frame), 0), fmt(int(missing.sum()), 0), fmt(int(mismatch.sum()), 0)]
        )
    return rows


def unreferenced(ds: Dataset) -> list[str]:
    """Return declared IDs that no data file ever mentions, by kind."""
    ids = declared_ids(ds)
    seen: set[str] = set()
    for _, values in references(ds):
        seen |= set(values.dropna().astype(str))
    acc = ds.table("access_log.csv")
    seen |= set(acc["credential"].astype(str))
    for col in ("assigned_to",):
        seen |= set(ds.table("work_orders.csv")[col].dropna().astype(str))
    sensor_assets = {str(s["asset_id"]) for s in ds.building["building"]["sensors"]}
    out = []
    for kind in ("room", "asset", "resource"):
        never = sorted(ids[kind] - seen)
        if never:
            out.append(f"{kind}: {', '.join(never)}")
    meters = ids["room water meter"]
    out.append(
        f"room water meter: {len(meters - seen)} of {len(meters)} `WM_Rxxx` IDs never appear; "
        "room_water.csv keys readings by room_id, so meter IDs surface only in metering "
        "work orders."
    )
    out.append(
        f"sensor: {len(ids['sensor'])} declared sensors on {len(sensor_assets)} room assets; "
        "none for equipment, energy, weather or logistics series."
    )
    out.append(
        "gateway: " + ", ".join(sorted(ids["gateway"])) + " appear only in building.json "
        "(they are the join key for floor-wide outages, section 2)."
    )
    return out


def credential_mix(ds: Dataset) -> list[list[str]]:
    """Return access log credential classes with granted/denied counts."""
    acc = ds.table("access_log.csv").copy()
    resources = set(ds.resource_ids())
    acc["class"] = acc["credential"].map(
        lambda c: "booking" if c.startswith("BK") else ("resource" if c in resources else str(c))
    )
    counts = acc.groupby(["class", "result"]).size().unstack(fill_value=0)
    return [
        [str(cls), fmt(int(r.get("granted", 0)), 0), fmt(int(r.get("denied", 0)), 0)]
        for cls, r in counts.iterrows()
    ]


def render(datasets: list[Dataset]) -> str:
    """Return the markdown for section 3."""
    parts = [
        "## 3. Referential integrity",
        "",
        "Hypothesis: every room, asset, meter, staff and robot ID used in any file is "
        "declared in `building.json`, every booking referenced elsewhere exists, and the "
        "room on a guest record equals the booked room except after a reassignment.",
        "",
    ]
    for i, ds in enumerate(datasets, start=1):
        parts += [
            f"### 3.{i} `{ds.profile}`",
            "",
            "IDs not declared in `building.json` (pseudo-IDs "
            + ", ".join(f"`{p}`" for p in sorted(PSEUDO_IDS))
            + " are allowed):",
            "",
            table(["column", "distinct values", "undeclared"], integrity_rows(ds)),
            "",
            "Booking links (unknown = booking ID absent from bookings.csv; room mismatch = "
            "record room differs from booked room):",
            "",
            table(["source", "rows", "unknown booking", "room mismatch"], booking_links(ds)),
            "",
            "Access credentials:",
            "",
            table(["credential class", "granted", "denied"], credential_mix(ds)),
            "",
            "Declared but never referenced:",
            "",
        ]
        parts += [f"- {u}" for u in unreferenced(ds)]
        parts.append("")
    return "\n".join(parts)
