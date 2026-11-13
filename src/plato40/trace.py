"""Run trace: why the pipeline did (or did not) act. Written by ``run``, read by ``explain``.

One JSON object per line: ``episode`` (key and counts), ``signal`` (every detector
observation, including those that led to no decision) and ``decision`` (action, level
and the policy rule that set it).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from plato40.detect.grid import stamp
from plato40.load.reader import Dataset
from plato40.pipeline import Run


def write(result: Run, path: Path) -> None:
    """Write the trace of ``result`` to ``path``."""
    per_episode = Counter(d["episode_id"] for d in result.decisions)
    lines: list[dict[str, Any]] = []
    for ep in result.episodes:
        lines.append(
            {
                "record": "episode",
                "episode_id": ep.episode_id,
                "issue_type": ep.issue_type,
                "assets": list(ep.assets),
                "signals": len(ep.signals),
                "decisions": per_episode[ep.episode_id],
            }
        )
        lines += [
            {
                "record": "signal",
                "episode_id": ep.episode_id,
                "time": stamp(s.time),
                "kind": s.kind,
                "detail": s.detail,
                "evidence": list(s.evidence),
            }
            for s in ep.signals
        ]
    lines += [
        {
            "record": "decision",
            "episode_id": d["episode_id"],
            "event_id": d["event_id"],
            "time": d["timestamp_utc"],
            "action": d["action"],
            "priority": d["priority"],
            "autonomy": d["autonomy"],
            "rule": result.rules[d["event_id"]],
            "resource_id": d.get("resource_id"),
            "assets": d["affected_assets"],
        }
        for d in result.decisions
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), "utf-8")


def _counts(counter: Counter[str]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(counter.items())) or "none"


def summary(ds: Dataset, result: Run) -> str:
    """Return the funnel of one run: data, signals, episodes, decisions, checks."""
    by_issue = Counter(s.issue_type for s in result.signals)
    kinds: dict[str, Counter[str]] = {}
    for s in result.signals:
        kinds.setdefault(s.issue_type, Counter())[s.kind] += 1
    acted = {d["episode_id"] for d in result.decisions}
    silent = Counter(ep.issue_type for ep in result.episodes if ep.episode_id not in acted)
    m = ds.manifest
    rows = [
        f"data       {ds.profile}  {m['start_utc'][:10]}..{m['end_utc'][:10]}  "
        f"{len(ds.tables) + 1} files, {len(ds.issues)} parse issues",
        "signals    "
        + "  ".join(f"{k} {by_issue[k]} ({_counts(kinds[k])})" for k in sorted(by_issue)),
        f"episodes   {len(result.episodes)}  acted on {len(acted)}  "
        f"no action {sum(silent.values())} ({_counts(silent)})",
        f"decisions  {len(result.decisions)}  "
        + _counts(Counter(d["autonomy"] for d in result.decisions)),
        "checks     contract and policy: 0 violations",
    ]
    return "\n".join(rows)


def explain(path: Path, episode: str | None) -> str:
    """Return the episode list, or the full story of the episodes matching ``episode``."""
    records = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]
    episodes = [r for r in records if r["record"] == "episode"]
    if episode is None:
        return "\n".join(
            f"{r['episode_id']:<48} signals {r['signals']:>4}  decisions {r['decisions']:>2}"
            for r in episodes
        )
    out: list[str] = []
    for ep in (r for r in episodes if episode in r["episode_id"]):
        eid = ep["episode_id"]
        sig = [r for r in records if r["record"] == "signal" and r["episode_id"] == eid]
        dec = [r for r in records if r["record"] == "decision" and r["episode_id"] == eid]
        out.append(f"{eid}  ({ep['issue_type']}, {', '.join(ep['assets'][:3])})")
        out.append(f"  signals ({len(sig)})")
        shown = sig if len(sig) <= 8 else [*sig[:5], None, *sig[-2:]]
        for s in shown:
            if s is None:
                out.append(f"    ... {len(sig) - 7} more")
            else:
                out.append(f"    {s['time']}  {s['kind']:<12} {s['detail']}")
        out.append(f"  evidence  {'; '.join(sig[0]['evidence'])}")
        out.append(f"  decisions ({len(dec)})" if dec else "  decisions: none (no rule fired)")
        for d in dec:
            who = f" -> {d['resource_id']}" if d["resource_id"] else ""
            out.append(
                f"    {d['event_id'][-2:]} {d['time']} {d['action']:<20} {d['priority']:<8} "
                f"{d['autonomy']:<21} rule: {d['rule']}{who}"
            )
        out.append("")
    return "\n".join(out) or f"no episode matches {episode!r}"
