"""Show the LLM contract on the real R108 signal (quickstart, 24 Jan 13:40Z) with scripted answers.

usage: PLATO40_LLM=on python scripts/demo_llm_guard.py <s01.bus.jsonl> <cache_dir>
No network: each case plays one LLM answer. Prints which text was used and why.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pandas as pd

from plato40 import bus
from plato40.llm_guard import Task, phrase

SIGNAL = "s01-room_turnaround-R108-20260124T1300Z-sig02"
TZ = "Europe/Vienna"


def local(t: str) -> str:
    """Return a UTC time as local HH:MM, the way the rules render it."""
    return f"{pd.Timestamp(t).tz_convert(TZ):%H:%M}"


def main(path: Path, cache: Path) -> None:
    """Run every scripted case through the guard and print the outcome."""
    t = pd.Timestamp("2026-01-24T13:41Z")
    rows = bus.read(path, until=t)
    s = next(r for r in rows if r["id"] == SIGNAL)
    f = s["facts"]
    task = Task(
        lane="s01",
        t="2026-01-24T13:41Z",
        known_by=max(r.get("t_known") or r["t"] for r in rows),
        language=f["language"],
        intent="promise_ready_time",
        facts={k: f[k] for k in ("booking", "ready_prio", "wanted", "waiting")},
        say={"room": s["assets"][0], "ready": local(f["ready_prio"])},
        must_say=("room", "ready"),
        template="Your room {room} is expected to be ready at {ready} local time.",
    )
    print("say (rendered by the rules from the signal facts):", task.say)
    shutil.rmtree(cache, ignore_errors=True)

    def answer(text: str, lang: str = task.language):
        return lambda system, user: json.dumps({"language": lang, "text": text})

    def down(system: str, user: str) -> str:
        raise TimeoutError("API timeout")

    cases = [
        (
            "invented time and discount",
            answer("Room R108 will be ready at 15:00 and you get 20% off."),
        ),
        ("wrong room id", answer("Your room R109 will be ready at 15:15.")),
        ("wrong language", answer("Ihr Zimmer R108 ist um 15:15 bereit.", "de")),
        ("drops the ready time", answer("Your room R108 will be ready soon.")),
        ("promise word", answer("R108 is guaranteed ready at 15:15.")),
        ("API down", down),
        ("good", answer("Sorry for the wait: your room R108 should be ready at 15:15 local time.")),
    ]
    for name, call in cases:
        r = phrase(task, cache, call, model="demo")
        print(f"- {name:28} -> {r.source:8} {r.text!r}  {r.errors}")
    r = phrase(task, cache, None, model="demo")
    print(f"- {'rerun, no LLM (cache)':28} -> {r.source:8} {r.text!r}")
    late = Task(**{**task.__dict__, "known_by": "2026-01-24T14:00Z"})
    try:
        phrase(late, cache, None, model="demo")
    except ValueError as exc:
        print(f"- {'facts from the future':28} -> raises: {exc}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
