"""Autonomy policy (CHALLENGE.md section 7): which approval a decision needs.

The required level depends on the action and its target, not on the verb alone:

- Entering an occupied room (cleaning, inspection, technician) needs approval and guest
  consent. A robot never enters an occupied room (building.json); that is forbidden.
- Technician, room block, room move and contractor services need approval. Waste pickup
  and vending restock are named autonomous in section 7; calling the on-call housekeeper
  is "costly" in building.json and needs approval.
- A room is released only when it is clean (vacant, human cleaning completed). No
  inspection is ever required in the data and no room is blocked, so ``clean`` suffices.
- Everything else is autonomous.

No euro amounts are used: the data has no service costs, so "notable cost" is the fixed
list above.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

ENTERS_ROOM = frozenset({"schedule_cleaning", "request_inspection", "dispatch_technician"})
ALWAYS_APPROVAL = frozenset(
    {"dispatch_technician", "block_room", "reassign_room", "request_human_approval"}
)
LOGISTICS_TYPES = frozenset({"waste_bin", "waste_container", "vending_machine"})


class Level(IntEnum):
    """Required autonomy level, ordered from least to most restrictive."""

    AUTONOMOUS = 0
    HUMAN_APPROVAL = 1
    APPROVAL_AND_CONSENT = 2
    FORBIDDEN = 3


CONTRACT_NAMES = {
    Level.AUTONOMOUS: "autonomous",
    Level.HUMAN_APPROVAL: "human_approval",
    Level.APPROVAL_AND_CONSENT: "approval_and_consent",
}


@dataclass(frozen=True)
class Context:
    """What the policy must know at decision time.

    Attributes:
        resource_ids: Staff and robot IDs; they carry out an action, they are not its target.
        logistics_ids: Waste bins, the container and the vending machine.
        on_call_ids: Resource IDs of the on-call housekeeper.
        occupied_rooms: Rooms with a guest stay in progress at the decision time.
        clean_rooms: Vacant rooms whose human cleaning completed, at the decision time.

    """

    resource_ids: frozenset[str]
    logistics_ids: frozenset[str]
    on_call_ids: frozenset[str]
    occupied_rooms: frozenset[str]
    clean_rooms: frozenset[str]

    @classmethod
    def from_building(
        cls,
        building: Mapping[str, Any],
        occupied_rooms: frozenset[str],
        clean_rooms: frozenset[str],
    ) -> Context:
        """Build a context from building.json and the room states at decision time."""
        staff = building["staff"]["resources"]
        return cls(
            resource_ids=frozenset(str(r["resource_id"]) for r in staff),
            logistics_ids=frozenset(
                str(a["id"]) for a in building["building"]["assets"] if a["type"] in LOGISTICS_TYPES
            ),
            on_call_ids=frozenset(
                str(r["resource_id"]) for r in staff if r["role"] == "on_call_housekeeping"
            ),
            occupied_rooms=occupied_rooms,
            clean_rooms=clean_rooms,
        )


def required_level(decision: Mapping[str, Any], ctx: Context) -> Level:
    """Return the most restrictive level any rule assigns to ``decision``."""
    action = decision["action"]
    named = {str(a) for a in decision["affected_assets"]}
    resources = (named & ctx.resource_ids) | {str(decision.get("resource_id", ""))}
    assets = named - ctx.resource_ids
    occupied = assets & ctx.occupied_rooms
    level = Level.AUTONOMOUS
    if action == "dispatch_robot" and occupied:
        return Level.FORBIDDEN
    if action == "release_room" and not assets <= ctx.clean_rooms:
        return Level.FORBIDDEN
    if action in ENTERS_ROOM and occupied:
        level = max(level, Level.APPROVAL_AND_CONSENT)
    if action in ALWAYS_APPROVAL:
        level = max(level, Level.HUMAN_APPROVAL)
    if action == "order_service" and not assets <= ctx.logistics_ids:
        level = max(level, Level.HUMAN_APPROVAL)
    if action == "schedule_cleaning" and resources & ctx.on_call_ids:
        level = max(level, Level.HUMAN_APPROVAL)
    return level


def violations(decision: Mapping[str, Any], ctx: Context) -> list[str]:
    """Return policy violations: forbidden actions and flags weaker than required."""
    need = required_level(decision, ctx)
    if need is Level.FORBIDDEN:
        return [f"{decision['action']} is forbidden on {sorted(decision['affected_assets'])}"]
    errors = []
    if need >= Level.HUMAN_APPROVAL and decision.get("requires_human_approval") is not True:
        errors.append(f"needs requires_human_approval=true ({CONTRACT_NAMES[need]})")
    if need >= Level.APPROVAL_AND_CONSENT and decision.get("requires_guest_consent") is not True:
        errors.append("needs requires_guest_consent=true (occupied room)")
    declared = decision.get("autonomy")
    if declared is not None:
        rank = {v: k for k, v in CONTRACT_NAMES.items()}.get(declared)
        if rank is not None and rank < need:
            errors.append(f"autonomy {declared} weaker than {CONTRACT_NAMES[need]}")
    return errors


def apply(decision: dict[str, Any], ctx: Context) -> None:
    """Set ``autonomy`` and both approval flags from the required level; refuse forbidden."""
    need = required_level(decision, ctx)
    if need is Level.FORBIDDEN:
        raise ValueError(f"{decision['event_id']}: {decision['action']} is forbidden")
    decision["requires_human_approval"] = need >= Level.HUMAN_APPROVAL
    decision["requires_guest_consent"] = need >= Level.APPROVAL_AND_CONSENT
    decision["autonomy"] = CONTRACT_NAMES[need]
