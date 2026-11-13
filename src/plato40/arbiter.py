"""The arbiter (docs/BUS.md): turns lane proposals into decisions next to the pipeline's own.

Proposals are judged in time order, and within one minute by priority. A proposal is
rejected, with the reason in its verdict, when it breaks the bus contract or known-at, when
a claim overlaps a resource already granted, when the policy forbids it, or when the decision
built from it breaks the decision contract. An accepted proposal is copied unchanged into a
decision (``event_id`` = proposal id); the policy then sets autonomy and the approval flags.

The shared ledger starts with the pipeline's own staffed decisions, each held for its
setting's duration from its timestamp. For a lane listed in ``replace``, the pipeline's
decisions of that issue type are dropped, so the lane's proposals stand in for them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pandas as pd

from plato40 import bus, contract, pipeline, policy
from plato40.contract import Decision
from plato40.load.reader import Dataset
from plato40.settings import Settings
from plato40.state import RoomStates

COPIED = (
    "resource_id",
    "guest_request_ids",
    "confidence",
    "expected_outcome",
    "verification_plan",
)
RANK = {p: n for n, p in enumerate(contract.PRIORITIES)}


class Ledger:
    """Granted resource time per resource ID, as half-open intervals."""

    def __init__(self) -> None:
        """Start with nothing held."""
        self.held: dict[str, list[tuple[pd.Timestamp, pd.Timestamp, str]]] = {}

    def clash(self, rid: str, start: pd.Timestamp, end: pd.Timestamp) -> str | None:
        """Return the holder of an interval on ``rid`` that overlaps [start, end), if any."""
        return next((who for a, z, who in self.held.get(rid, []) if a < end and start < z), None)

    def grant(self, rid: str, start: pd.Timestamp, end: pd.Timestamp, who: str) -> None:
        """Hold [start, end) on ``rid`` for ``who``."""
        self.held.setdefault(rid, []).append((start, end, who))


def _held_for(d: Mapping[str, Any], settings: Settings) -> pd.Timedelta | None:
    """Return how long a pipeline decision holds its resource, or None if it holds none."""
    th = settings.detect
    minutes = {
        "schedule_cleaning": th.clean_room_min,
        "dispatch_robot": th.robot_pass_min,
        "dispatch_technician": th.technician_job_h * 60,
    }.get(d["action"])
    if minutes is None or "resource_id" not in d:
        return None
    return pd.Timedelta(minutes=minutes)


def _decision(p: Mapping[str, Any]) -> Decision:
    """Return the decision an accepted proposal becomes, before the policy sets its flags."""
    reason = p["reason"]
    if "message" in p:
        reason += f' Message ({p["message"]["language"]}): "{p["message"]["text"]}"'
    d: Decision = {
        "event_id": p["id"],
        "timestamp_utc": p["t"],
        "affected_assets": list(p["assets"]),
        "issue_type": bus.LANES[p["lane"]]["issue_type"],
        "action": p["action"],
        "priority": p["priority"],
        "reason": reason,
        "requires_human_approval": False,
        "episode_id": p["episode_id"],
        "evidence": list(p["evidence"]),
    }
    d.update({k: p[k] for k in COPIED if k in p})
    return d


def arbitrate(
    ds: Dataset,
    settings: Settings,
    base: list[Decision],
    rows: Iterable[Mapping[str, Any]],
    replace: Iterable[str] = (),
) -> tuple[list[Decision], list[dict[str, Any]]]:
    """Return the merged decisions and one verdict row per proposal."""
    rows = list(rows)
    ids = contract.known_asset_ids(ds.building)
    req = ds.table("guest_requests.csv")
    asked = dict(zip(req["request_id"].astype(str), req["timestamp_utc"], strict=True))
    broken: dict[int, list[str]] = {}
    for e in bus.check_file(rows, ids) + bus.check_known_at(rows, asked):
        line, _, problem = e.partition(": ")
        broken.setdefault(int(line.removeprefix("line ")), []).append(problem)
    dropped = {bus.LANES[lane]["issue_type"] for lane in replace}
    decisions = [d for d in base if d["issue_type"] not in dropped]
    ledger = Ledger()
    for own in decisions:
        span = _held_for(own, settings)
        if span is not None:
            t = pd.Timestamp(own["timestamp_utc"])
            ledger.grant(own["resource_id"], t, t + span, own["event_id"])
    states = RoomStates(ds)
    proposals = [
        (n, r)
        for n, r in enumerate(rows, start=1)
        if isinstance(r, Mapping) and r.get("row") == "proposal"
    ]
    proposals.sort(key=lambda x: (str(x[1].get("t")), RANK.get(str(x[1].get("priority")), 9), x[0]))
    verdicts = []
    for n, p in proposals:
        why = broken.get(n, [])
        if not why:
            for c in p.get("claims", []):
                start = pd.Timestamp(c["from"])
                holder = ledger.clash(c["id"], start, start + pd.Timedelta(minutes=c["min"]))
                if holder is not None:
                    why.append(f"claim on {c['id']} from {c['from']} overlaps {holder}")
        d: Decision | None = _decision(p) if not why else None
        rule = ""
        if d is not None:
            try:
                ctx = pipeline.context_at(ds, states, pd.Timestamp(d["timestamp_utc"]))
                rule = policy.apply(d, ctx)
            except ValueError as exc:
                why.append(str(exc))
            why += contract.check_decision(d, ids)
        verdict: dict[str, Any] = {
            "v": bus.VERSION,
            "row": "verdict",
            "lane": p.get("lane", ""),
            "id": f"{p.get('id')}-v",
            "t": p.get("t", ""),
            "proposal_id": str(p.get("id")),
            "accepted": not why,
            "why": "; ".join(why) if why else rule,
        }
        if d is not None and not why:
            for c in p.get("claims", []):
                start = pd.Timestamp(c["from"])
                ledger.grant(c["id"], start, start + pd.Timedelta(minutes=c["min"]), p["id"])
            decisions.append(d)
            verdict["event_id"] = d["event_id"]
        verdicts.append(verdict)
    decisions.sort(key=lambda x: (x["timestamp_utc"], x["event_id"]))
    errors = pipeline.check(ds, states, decisions)
    if errors:
        raise RuntimeError("merged decisions violate contract or policy:\n" + "\n".join(errors))
    return decisions, verdicts
