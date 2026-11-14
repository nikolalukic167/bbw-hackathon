"""Operations console and demo page: one self-contained HTML from a run's decisions and trace.

The page has four views: Year (the jury dashboard), Day (the replay of one day), Episode
(evidence, risk, decisions, verification, with a mock executor kept in the browser) and Trail
(one decision's chain). Views live in ``ui_*.js`` next to ``ui.html`` in this folder and are
inlined here, with the IBM Plex fonts in ``fonts/`` (SIL Open Font License, ``fonts/OFL.txt``).
The folder holds only page code: the pipeline never imports it.

Without ``--data`` the page shows the episodes only. With it, the pipeline runs once more on the
dataset (same settings, same cache) and the page gets the KPIs, misses, the year strip, hourly
series and the day extracts from that run, so every number on the page is computed from the
same source as ``insights.md``. The data path is always an argument.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from plato40 import contract, lanes, pipeline
from plato40.load.reader import load_dataset
from plato40.settings import Settings
from plato40.ui import data as ui_data

TEMPLATE = Path(__file__).with_name("ui.html")
PLACEHOLDER = "/*__DATA__*/null"
VIEWS = "/*__VIEWS__*/"
FONTS = "/*__FONTS__*/"


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
    issues = {e["issue"] for e in episodes.values()} | {d["issue_type"] for d in full.values()}
    return {
        "source": decisions_path.name,
        "tz": "Europe/Vienna",
        "lanes": ui_data.lanes(issues),
        "episodes": sorted(
            episodes.values(),
            key=lambda e: (e["signals"][0]["time"] if e["signals"] else "", e["id"]),
        ),
    }


def _views() -> str:
    """Return the view scripts (``ui_*.js``) in name order, inlined after the shell."""
    return "\n".join(p.read_text("utf-8") for p in sorted(TEMPLATE.parent.glob("ui_*.js")))


def _fonts() -> str:
    """Return @font-face rules with the IBM Plex files in ``fonts/`` inlined (no network)."""
    rules = []
    for p in sorted(TEMPLATE.parent.glob("fonts/*.woff2")):
        family = "IBM Plex Mono" if "-mono-" in p.name else "IBM Plex Sans"
        weight = p.stem.split("-")[-2]
        src = base64.b64encode(p.read_bytes()).decode("ascii")
        rules.append(
            f"@font-face {{ font-family: '{family}'; font-weight: {weight}; font-style: normal;"
            f" font-display: swap; src: url(data:font/woff2;base64,{src}) format('woff2'); }}"
        )
    return "\n".join(rules)


def render(
    decisions_path: Path,
    trace_path: Path,
    out: Path,
    data: Path | None = None,
    llm_cache: Path | None = None,
    days: Sequence[str] = (),
    supervisor: Path | None = None,
    rerun: Path | None = None,
) -> dict[str, Any]:
    """Write the page to ``out``; return what it holds (episodes, views, size, warnings)."""
    page = payload(decisions_path, trace_path)
    info: dict[str, Any] = {"episodes": len(page["episodes"]), "warnings": []}
    if data is not None:
        ds = load_dataset(data)
        settings = Settings(
            data_root=data, out_dir=out.parent, llm_cache=llm_cache, lanes=tuple(lanes.registry())
        )
        run = pipeline.run(ds, settings)
        info["warnings"] += ui_data.decisions_match(run, contract.read_decisions(decisions_path))
        log = None
        if supervisor is not None and supervisor.exists():
            log = [json.loads(x) for x in supervisor.read_text("utf-8").splitlines() if x.strip()]
        page["dataset"] = data.name
        page["tz"] = settings.local_tz
        page |= ui_data.demo_data(ds, settings, run, decisions_path, days, log, rerun)
    else:
        page["dataset"] = decisions_path.stem
    blob = json.dumps(page, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = (
        TEMPLATE.read_text("utf-8")
        .replace(PLACEHOLDER, blob)
        .replace(VIEWS, _views())
        .replace(FONTS, _fonts())
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, "utf-8")
    info["views"] = sorted(p.stem.removeprefix("ui_") for p in TEMPLATE.parent.glob("ui_*.js"))
    info["kb"] = len(html.encode("utf-8")) // 1024
    info["days"] = sorted(page.get("days", {}))
    return info
