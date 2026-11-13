"""Operations console: one self-contained HTML page from a run's decisions and trace.

The page shows, per episode, the evidence (signals), the risk (priority), every decision
with its autonomy level and the policy rule behind it, and the verification plan. It needs
no server: the data is embedded, and approvals clicked on the page are a mock executor
kept in the viewer's browser only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from plato40 import contract

TEMPLATE = Path(__file__).with_name("ui.html")
PLACEHOLDER = "/*__DATA__*/null"


def payload(decisions_path: Path, trace_path: Path) -> dict[str, Any]:
    """Join the decisions file with the trace into episodes with signals and decisions."""
    full = {d["event_id"]: d for d in contract.read_decisions(decisions_path)}
    records = [json.loads(x) for x in trace_path.read_text("utf-8").splitlines() if x]
    episodes: dict[str, dict[str, Any]] = {}
    for r in records:
        if r["record"] == "episode":
            episodes[r["episode_id"]] = {
                "id": r["episode_id"],
                "issue": r["issue_type"],
                "assets": r["assets"],
                "signals": [],
                "decisions": [],
            }
    for r in records:
        if r["record"] == "signal":
            episodes[r["episode_id"]]["signals"].append(
                {k: r[k] for k in ("time", "kind", "detail", "evidence")}
            )
        elif r["record"] == "decision":
            d = dict(full.get(r["event_id"], {}))
            d["rule"] = r["rule"]
            episodes[r["episode_id"]]["decisions"].append(d)
    return {
        "source": decisions_path.name,
        "episodes": sorted(
            episodes.values(),
            key=lambda e: (e["signals"][0]["time"] if e["signals"] else "", e["id"]),
        ),
    }


def render(decisions_path: Path, trace_path: Path, out: Path) -> int:
    """Write the console to ``out``; return the number of episodes shown."""
    data = payload(decisions_path, trace_path)
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = TEMPLATE.read_text("utf-8").replace(PLACEHOLDER, blob)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, "utf-8")
    return len(data["episodes"])
