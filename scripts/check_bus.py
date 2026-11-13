"""Check a bus file against the contract, known-at, and the decisions the arbiter wrote.

usage: python scripts/check_bus.py <data_dir> <lane.bus.jsonl> <lane.decisions.jsonl>
Exit 1 on any violation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from plato40 import bus, contract
from plato40.load.reader import load_dataset

SAME = (
    ("action", "action"),
    ("assets", "affected_assets"),
    ("priority", "priority"),
    ("evidence", "evidence"),
    ("resource_id", "resource_id"),
)


def main(data: Path, rows_path: Path, dec_path: Path) -> int:
    """Print the violations of one lane file and return the exit code."""
    ds = load_dataset(data)
    ids = contract.known_asset_ids(ds.building)
    rows = bus.read(rows_path)
    req = ds.table("guest_requests.csv")
    asked = {
        str(k): pd.Timestamp(v)
        for k, v in zip(req["request_id"], req["timestamp_utc"], strict=True)
    }
    errors = bus.check_file(rows, ids) + bus.check_known_at(rows, asked)
    decisions = {d["event_id"]: d for d in contract.read_decisions(dec_path)}
    errors += contract.check_file(list(decisions.values()), ids)
    props = {r["id"]: r for r in rows if r["row"] == "proposal"}
    judged = set()
    for r in (r for r in rows if r["row"] == "verdict"):
        p = props.get(r["proposal_id"])
        if p is None:
            errors.append(f"{r['id']}: verdict on unknown proposal {r['proposal_id']}")
            continue
        judged.add(p["id"])
        if not r["accepted"]:
            continue
        d = decisions.get(r.get("event_id", ""))
        if d is None:
            errors.append(f"{r['id']}: accepted but no decision {r.get('event_id')}")
            continue
        errors += [
            f"{r['id']}: decision {d['event_id']} changed {a}: {p.get(a)!r} -> {d.get(b)!r}"
            for a, b in SAME
            if p.get(a) != d.get(b)
        ]
        if d["timestamp_utc"] != p["t"]:
            errors.append(
                f"{r['id']}: decision time {d['timestamp_utc']} is not proposal time {p['t']}"
            )
    errors += [f"{i}: proposal without a verdict" for i in sorted(set(props) - judged)]
    n = {k: sum(r["row"] == k for r in rows) for k in bus.ROWS}
    print(f"{rows_path.name}: {n}, {len(decisions)} decisions, {len(errors)} violations")
    for e in errors:
        print("  " + e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])))
