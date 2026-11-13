"""Guest messages in the guest's language, phrased by the LLM under ``llm_guard``.

The rules write every guest message in English, with the guest's language tag:
``Tell the guest (de): R108 expected ready at 15:15 local (proposed message; ...).``
Here every ID and number in that text becomes a fixed token (``say``), so the LLM may only
reword and translate around them. A checked answer is appended to the reason as the message
to send; any failure keeps the reason unchanged. Messages to several rooms at once carry no
single language and stay English.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from plato40.contract import Decision
from plato40.llm_guard import (
    DEFAULT_MODEL,
    ID,
    NUMBER,
    Call,
    Result,
    Task,
    anthropic_call,
    phrase,
    prefetch,
)

ONE_GUEST = re.compile(
    r"^Tell the (?:waiting )?guest \((?P<lang>\w\w)\): (?P<text>.*?)"
    r" \(proposed message; delivery not confirmed\)\.$"
)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DATE = rf"(?P<date>\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (?P<day>\d\d) (?P<mon>{'|'.join(MONTHS)})\b)"
TOKEN = re.compile(f"{DATE}|{ID.pattern}|{NUMBER.pattern}")


def task_for(d: Decision) -> Task | None:
    """Return the phrasing task of a one-guest notify decision, or None for any other."""
    m = ONE_GUEST.match(d["reason"]) if d["action"] == "notify_guest" else None
    if m is None:
        return None
    say: dict[str, str] = {}

    def slot(t: re.Match[str]) -> str:
        value = t[0]
        if t["date"]:
            # One token per date without the weekday, numeric outside English, so the model
            # need not translate it and may still write the weekday itself.
            month = MONTHS.index(t["mon"]) + 1
            value = f"{t['day']} {t['mon']}" if m["lang"] == "en" else f"{t['day']}.{month:02d}."
        key = next((k for k, v in say.items() if v == value), f"t{len(say) + 1}")
        say[key] = value
        return "{" + key + "}"

    template = TOKEN.sub(slot, m["text"].replace("{", "{{").replace("}", "}}"))
    return Task(
        lane="s01",
        t=d["timestamp_utc"],
        known_by=d["timestamp_utc"],
        language=m["lang"],
        intent="guest_message",
        facts={"english": m["text"]},
        say=say,
        must_say=tuple(say),
        template=template,
        hedged="expected" in m["text"],
    )


def localise(decisions: list[Decision], cache_dir: Path) -> dict[str, Result]:
    """Append the checked guest-language message to each one-guest notify; return the results.

    With ``PLATO40_LLM=on`` a cache miss calls the model; otherwise only cached answers
    are used, so a rerun from a filled cache needs no network and gives the same file.
    """
    model = os.environ.get("PLATO40_LLM_MODEL", DEFAULT_MODEL)
    call: Call | None = anthropic_call(model) if os.environ.get("PLATO40_LLM") == "on" else None
    results = {}
    tasks = {d["event_id"]: t for d in decisions if (t := task_for(d)) is not None}
    done = prefetch(tasks.values(), cache_dir, call, model)
    for d in decisions:
        task = tasks.get(d["event_id"])
        if task is None:
            continue
        r = phrase(task, cache_dir, call, model, done)
        results[d["event_id"]] = r
        if r.source != "template":
            d["reason"] += f' Message ({task.language}): "{r.text}"'
    return results
