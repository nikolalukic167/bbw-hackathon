"""Print the Day view's bookmarks of a generated page, one line per step, for docs/DEMO.md.

usage: uv run python scripts/demo_steps.py <page.html> [YYYY-MM-DD ...]

The presenter steps through a day with the arrow keys; step n is bookmark n. Run this after
every regeneration and compare it with the step numbers in docs/DEMO.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


def main(page: Path, days: list[str]) -> int:
    """Print the bookmarks of ``days`` (all extracted days if none given)."""
    html = page.read_text(encoding="utf-8")
    start = html.index("const DATA = ") + len("const DATA = ")
    data, _ = json.JSONDecoder().raw_decode(html[start:])
    tz = ZoneInfo(data.get("tz", "Europe/Vienna"))
    for day in days or list(data.get("days", {})):
        marks = data["days"][day]["bookmarks"]
        print(f"{day}: {len(marks)} steps  (#view=day&day={day}&step=N)")
        for n, b in enumerate(marks):
            local = pd.Timestamp(b["t"]).tz_convert(tz).strftime("%H:%M")
            print(f"  {n:3d}  {local}  {b['kind']:10}  {b['label']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]), sys.argv[2:]))
