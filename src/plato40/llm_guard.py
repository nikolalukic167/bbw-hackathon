"""Design by contract around every LLM call (ADR-001: the LLM phrases, the rules decide).

A ``Task`` holds everything the LLM may use. Its ``say`` map holds every number, time, date
and ID the text may contain, already rendered by the rules ("14:30", "R108"). The LLM only
chooses the words around them.

- Preconditions (on the task, before any call): known language; ``facts`` serialisable and
  built only from bus rows known by ``t`` (``known_by`` <= ``t``; read them with
  ``bus.read(path, until=t)``); the template contains every ``must_say`` key.
- Postconditions (on the output): schema; same language; every number and ID in the text is
  a ``say`` value; every ``must_say`` value appears; length limit; no banned claim.
- On any postcondition failure the template text is used and the failure is returned for
  the trace. A failed precondition raises: it is a bug in the caller, not in the LLM.

Determinism: the API takes no temperature for these models and two calls can differ, so every
checked output is cached under a hash of model, prompt version and the full input. A run
reads the cache first. With ``PLATO40_LLM=off`` (the default) nothing is called and a cache
miss falls back to the template, so a run never needs the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from plato40.bus import LANGUAGES

PROMPT_VERSION = "guest_message/2"
DEFAULT_MODEL = "claude-haiku-5-5"
MAX_CHARS = 400
NUMBER = re.compile(r"\d+(?:[:.,]\d+)*")
ID = re.compile(r"\b(?:R\d{3}|BK\d+|GR\d+|WO\d+|EP-\S+|[A-Z]+_\d+)\b")
ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?Z$")
BANNED = ("guarantee", "garantiert", "confirmed", "bestätigt", "free of charge", "kostenlos")
SYSTEM = (
    "You write one short message to a hotel guest in the language given. Use only the facts "
    "given. Copy every time, date, room number and ID exactly as it appears in SAY; write no "
    "other number, time, price, ID or promise. Never use the words guarantee or confirmed, "
    "in any language: an offer pending approval is only offered. "
    "Answer with the message tool."
)
TOOL: Any = {
    "name": "message",
    "description": "The guest message.",
    "input_schema": {
        "type": "object",
        "properties": {"language": {"enum": list(LANGUAGES)}, "text": {"type": "string"}},
        "required": ["language", "text"],
        "additionalProperties": False,
    },
}

Call = Callable[[str, str], str]


@dataclass(frozen=True)
class Task:
    """One phrasing job: what to say, in which language, with which fixed tokens."""

    lane: str
    t: str
    known_by: str
    language: str
    intent: str
    facts: Mapping[str, Any]
    say: Mapping[str, str]
    must_say: tuple[str, ...]
    template: str


@dataclass
class Result:
    """The text used, where it came from (cache, llm, template), and why the LLM text was not."""

    text: str
    source: str
    errors: list[str] = field(default_factory=list)


def _norm(token: str) -> str:
    return re.sub(r"[.,]", ":", token)


def preconditions(task: Task) -> list[str]:
    """Return caller bugs: unknown language, non-JSON facts, facts known after t, bad template."""
    errors = []
    if task.language not in LANGUAGES:
        errors.append(f"language {task.language!r} not in {LANGUAGES}")
    try:
        json.dumps(task.facts, sort_keys=True)
    except TypeError as exc:
        errors.append(f"facts not JSON: {exc}")
    if not ISO.match(task.t):
        errors.append(f"t {task.t!r} is not ISO 8601 UTC ending in Z")
    elif not ISO.match(task.known_by) or pd.Timestamp(task.known_by) > pd.Timestamp(task.t):
        errors.append(f"facts known by {task.known_by}, after the decision time {task.t}")
    unsaid = [k for k in task.must_say if k not in task.say]
    if unsaid:
        errors.append(f"must_say {unsaid} not in say")
    missing = [k for k in task.must_say if "{" + k + "}" not in task.template]
    if missing:
        errors.append(f"template lacks {missing}")
    try:
        task.template.format(**task.say)
    except (KeyError, IndexError, ValueError) as exc:
        errors.append(f"template does not format with say: {exc!r}")
    return errors


def postconditions(task: Task, raw: str) -> list[str]:
    """Return the contract violations of one LLM answer."""
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        return ["answer is not JSON"]
    if not isinstance(out, dict) or set(out) != {"language", "text"}:
        return ["answer is not {language, text}"]
    text = out["text"]
    errors = []
    if out["language"] != task.language:
        errors.append(f"language {out['language']!r} is not {task.language!r}")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_CHARS:
        return [*errors, f"text empty or over {MAX_CHARS} chars"]
    allowed_ids = set(task.say.values())
    # Digits inside an ID (BK12, R108) do not license that number on its own ("12 Euro").
    allowed_numbers = {_norm(n) for v in task.say.values() for n in NUMBER.findall(ID.sub(" ", v))}
    ids = ID.findall(text)
    numbers = NUMBER.findall(ID.sub(" ", text))
    errors += [f"invented number {n!r}" for n in numbers if _norm(n) not in allowed_numbers]
    errors += [f"invented id {i!r}" for i in ids if i not in allowed_ids]
    errors += [f"missing {k}={task.say[k]!r}" for k in task.must_say if task.say[k] not in text]
    errors += [f"banned claim {b!r}" for b in BANNED if b in text.lower()]
    return errors


def cache_key(task: Task, model: str) -> str:
    """Return the hash of everything that can change the answer."""
    blob = json.dumps(
        {"model": model, "prompt": PROMPT_VERSION, "system": SYSTEM, "task": asdict(task)},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def user_prompt(task: Task) -> str:
    """Return the user turn: intent, language, SAY tokens and facts, nothing else."""
    return json.dumps(
        {"intent": task.intent, "language": task.language, "SAY": task.say, "facts": task.facts},
        ensure_ascii=False,
        sort_keys=True,
    )


def phrase(task: Task, cache_dir: Path, call: Call | None = None, model: str = "") -> Result:
    """Return checked LLM text from cache or ``call``; on any violation, the template text."""
    errors = preconditions(task)
    if errors:
        raise ValueError(f"LLM task precondition: {errors}")
    fallback = task.template.format(**task.say)
    model = model or os.environ.get("PLATO40_LLM_MODEL", DEFAULT_MODEL)
    path = cache_dir / f"{cache_key(task, model)}.json"
    if path.exists():
        raw, source = json.loads(path.read_text("utf-8"))["answer"], "cache"
    elif call is None or os.environ.get("PLATO40_LLM", "off") != "on":
        return Result(fallback, "template", ["no cached answer; LLM off"])
    else:
        try:
            raw, source = call(SYSTEM, user_prompt(task)), "llm"
        except Exception as exc:  # network, quota, timeout: never block a run
            return Result(fallback, "template", [f"LLM call failed: {exc!r}"])
    errors = postconditions(task, raw)
    if source == "llm":
        # Only a passing answer becomes the cached answer; a reject is kept for the trace only.
        where = cache_dir / "rejected" if errors else cache_dir
        where.mkdir(parents=True, exist_ok=True)
        record = {
            "model": model,
            "prompt": PROMPT_VERSION,
            "task": asdict(task),
            "answer": raw,
            "violations": errors,
        }
        (where / path.name).write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True, indent=1), "utf-8"
        )
    if errors:
        return Result(fallback, "template", errors)
    return Result(json.loads(raw)["text"], source)


def anthropic_call(model: str) -> Call:
    """Return a ``Call`` on the Anthropic API, with the output forced through TOOL."""
    import anthropic  # type: ignore[import-not-found,unused-ignore]  # optional; only with PLATO40_LLM=on

    client = anthropic.Anthropic()

    def call(system: str, user: str) -> str:
        msg = client.messages.create(
            model=model,
            max_tokens=400,
            system=system,
            tools=[TOOL],
            tool_choice={"type": "tool", "name": "message"},
            messages=[{"role": "user", "content": user}],
        )
        block = next(b for b in msg.content if b.type == "tool_use")
        return json.dumps(block.input, ensure_ascii=False)

    return call
