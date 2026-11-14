"""Local copilot server for the console page: serves the page and answers questions from a run.

``uv run python -m plato40.ui.copilot <page.html> <decisions.jsonl> <trace.jsonl>``

The model reads the run's output rows through four read-only tools, and every tool cuts the rows
at the page clock (``as_of``): nothing a decision or signal after that time is visible. It never
writes or changes a decision. An answer is kept only if every id in square brackets is one a tool
returned and every number in it appears in the tool results or the question; otherwise the model
gets one retry with the list of problems, then the answer is withheld. Answers are cached under a
hash of model, prompt version, question, clock, episode and the decisions file, so a rehearsed
demo question replays without the network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from plato40.ui import payload

MODEL = "claude-sonnet-5-5"
PROMPT_VERSION = "copilot/3"
MAX_WORDS = 70
TZ = ZoneInfo("Europe/Vienna")
PRIO = {"low": 0, "medium": 1, "high": 2, "critical": 3}
LIMIT = 30

SYSTEM = """You are the OSAM copilot for the facility manager of the PLATO-40 building.
OSAM is the operations system that detected the episodes and proposed the decisions you read.
Answer only from the results of your tools. They hold the rows OSAM wrote, cut at the page clock.
Cite every episode or decision you mention with its id in square brackets, for example
[EP-water_leak-R204-20260314T0215Z] or [s02-water_leak-R204-20260314T0215Z-prop01].
Never count or add up rows yourself: take every number from a tool result (use overview for
totals). Write times as the tools give them (Europe/Vienna local).
You cannot take, approve or change decisions; you may say what the manager should look at first.
If the tools do not hold the answer, say so in one sentence.
Be brief: the manager reads this between two tasks. Start with one sentence that answers the
question. Then at most 3 "- " lines, one item each, most urgent first, each with its id and
what to do or check. At most 70 words in total. No headings, no emojis, no closing summary."""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "overview",
        "description": "Counts of episodes and decisions in a window, by scenario, autonomy and "
        "priority. Use it for every total.",
        "input_schema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "local time, YYYY-MM-DDTHH:MM"},
                "until": {"type": "string", "description": "local time; default the clock"},
            },
            "required": ["since"],
        },
    },
    {
        "name": "find_episodes",
        "description": "Episodes first seen in a window, highest priority first (at most 30).",
        "input_schema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "local time, YYYY-MM-DDTHH:MM"},
                "until": {"type": "string", "description": "local time; default the clock"},
                "issue_type": {"type": "string", "description": "e.g. water_leak, hvac_fault"},
                "asset": {"type": "string", "description": "room or asset id, e.g. R204"},
                "min_priority": {"type": "string", "enum": list(PRIO)},
            },
            "required": ["since"],
        },
    },
    {
        "name": "episode",
        "description": "One episode: its signals (evidence) and decisions with the autonomy rule, "
        "expected outcome and verification plan.",
        "input_schema": {
            "type": "object",
            "properties": {"episode_id": {"type": "string"}},
            "required": ["episode_id"],
        },
    },
    {
        "name": "needs_human",
        "description": "Decisions in a window that need a human approval or guest consent, "
        "highest priority first (at most 30).",
        "input_schema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "local time, YYYY-MM-DDTHH:MM"},
                "until": {"type": "string", "description": "local time; default the clock"},
            },
            "required": ["since"],
        },
    },
]


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _local(t: datetime) -> str:
    return t.astimezone(TZ).strftime("%a %d %b %Y %H:%M")


class Rows:
    """The run's episodes, signals and decisions, read once, queried at a clock."""

    def __init__(self, decisions: Path, trace: Path) -> None:
        """Join decisions and trace into episodes (the page's own join)."""
        self.episodes = payload(decisions, trace)["episodes"]
        for e in self.episodes:
            times = [_utc(s["time"]) for s in e["signals"]]
            times += [_utc(d["timestamp_utc"]) for d in e["decisions"]]
            e["_t0"] = min(times)
        self.by_id = {e["id"]: e for e in self.episodes}
        self.digest = hashlib.sha256(decisions.read_bytes()).hexdigest()

    @staticmethod
    def _window(args: dict[str, Any], as_of: datetime) -> tuple[datetime, datetime]:
        def parse(s: str) -> datetime:
            t = datetime.fromisoformat(s[:16])
            return t.replace(tzinfo=TZ) if t.tzinfo is None else t

        since = parse(args["since"]) if args.get("since") else as_of - timedelta(hours=12)
        until = min(parse(args["until"]), as_of) if args.get("until") else as_of
        return since, until

    @staticmethod
    def _known(e: dict[str, Any], as_of: datetime) -> dict[str, Any]:
        sig = [s for s in e["signals"] if _utc(s["time"]) <= as_of]
        dec = [d for d in e["decisions"] if _utc(d["timestamp_utc"]) <= as_of]
        return {**e, "signals": sig, "decisions": dec}

    @staticmethod
    def _top(e: dict[str, Any]) -> str | None:
        p = [d.get("priority", "low") for d in e["decisions"]]
        return max(p, key=lambda x: PRIO.get(x, 0)) if p else None

    def _in(self, since: datetime, until: datetime, as_of: datetime) -> list[dict[str, Any]]:
        return [self._known(e, as_of) for e in self.episodes if since <= e["_t0"] <= until]

    def overview(self, args: dict[str, Any], as_of: datetime) -> dict[str, Any]:
        """Count episodes and decisions in the window."""
        since, until = self._window(args, as_of)
        eps = self._in(since, until, as_of)
        dec = [d for e in eps for d in e["decisions"]]

        def by(key: str) -> dict[str, int]:
            out: dict[str, int] = {}
            for d in dec:
                out[d.get(key) or "none"] = out.get(d.get(key) or "none", 0) + 1
            return dict(sorted(out.items(), key=lambda kv: -kv[1]))

        return {
            "window": [_local(since), _local(until)],
            "episodes": len(eps),
            "episodes_with_decisions": sum(1 for e in eps if e["decisions"]),
            "decisions": len(dec),
            "by_scenario": by("issue_type"),
            "by_autonomy": by("autonomy"),
            "by_priority": by("priority"),
        }

    def find_episodes(self, args: dict[str, Any], as_of: datetime) -> dict[str, Any]:
        """List episodes first seen in the window, filtered, highest priority first."""
        since, until = self._window(args, as_of)
        eps = [e for e in self._in(since, until, as_of) if e["decisions"]]
        if args.get("issue_type"):
            eps = [e for e in eps if e["issue"] == args["issue_type"]]
        if args.get("asset"):
            a = str(args["asset"]).upper()
            eps = [e for e in eps if a in [x.upper() for x in e["assets"]]]
        rank = {e["id"]: PRIO.get(self._top(e) or "low", 0) for e in eps}
        if args.get("min_priority") in PRIO:
            eps = [e for e in eps if rank[e["id"]] >= PRIO[args["min_priority"]]]
        eps.sort(key=lambda e: (-rank[e["id"]], e["_t0"]))
        rows = [
            {
                "episode_id": e["id"],
                "scenario": e["issue"],
                "assets": e["assets"][:6],
                "first_seen": _local(e["_t0"]),
                "top_priority": self._top(e),
                "decisions": len(e["decisions"]),
                "need_human": sum(1 for d in e["decisions"] if d.get("autonomy") != "autonomous"),
            }
            for e in eps[:LIMIT]
        ]
        return {"window": [_local(since), _local(until)], "matching": len(eps), "shown": rows}

    def episode(self, args: dict[str, Any], as_of: datetime) -> dict[str, Any]:
        """Return one episode's evidence and decisions known at the clock."""
        e = self.by_id.get(str(args.get("episode_id", "")))
        if e is None or e["_t0"] > as_of:
            return {"error": "no such episode at this clock"}
        k = self._known(e, as_of)
        return {
            "episode_id": e["id"],
            "scenario": e["issue"],
            "assets": e["assets"],
            "signals": [
                {"time": _local(_utc(s["time"])), "kind": s["kind"], "detail": s["detail"]}
                for s in k["signals"][:15]
            ],
            "decisions": [
                {
                    "event_id": d["event_id"],
                    "time": _local(_utc(d["timestamp_utc"])),
                    "action": d["action"],
                    "priority": d.get("priority"),
                    "autonomy": d.get("autonomy"),
                    "autonomy_rule": d.get("rule"),
                    "reason": d.get("reason"),
                    "expected_outcome": d.get("expected_outcome"),
                    "verification_plan": d.get("verification_plan"),
                    "follows": d.get("dependencies") or [],
                }
                for d in k["decisions"][:20]
            ],
        }

    def needs_human(self, args: dict[str, Any], as_of: datetime) -> dict[str, Any]:
        """List decisions in the window that wait on a human."""
        since, until = self._window(args, as_of)
        dec = [
            (d, e)
            for e in self.episodes
            for d in e["decisions"]
            if d.get("autonomy") != "autonomous" and since <= _utc(d["timestamp_utc"]) <= until
        ]
        dec.sort(key=lambda x: (-PRIO.get(x[0].get("priority", "low"), 0), x[0]["timestamp_utc"]))
        rows = [
            {
                "event_id": d["event_id"],
                "episode_id": e["id"],
                "time": _local(_utc(d["timestamp_utc"])),
                "action": d["action"],
                "assets": (d.get("affected_assets") or [])[:6],
                "priority": d.get("priority"),
                "autonomy": d.get("autonomy"),
                "reason": d.get("reason"),
            }
            for d, e in dec[:LIMIT]
        ]
        return {"window": [_local(since), _local(until)], "matching": len(dec), "shown": rows}


def _ids(obj: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("episode_id", "event_id") and isinstance(v, str):
                out.add(v)
            out |= _ids(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _ids(v)
    return out


def check(answer: str, results: list[Any], question: str) -> tuple[list[str], int, int]:
    """Return the problems with an answer, and how many citations and numbers it holds."""
    known = set().union(*(_ids(r) for r in results)) if results else set()
    cited = [x.strip() for b in re.findall(r"\[([^\]]+)\]", answer) for x in re.split(r"[,;]", b)]
    problems = [f"id {c} is in no tool result" for c in cited if c and c not in known]
    corpus = json.dumps(results, ensure_ascii=False).replace(",", "") + question
    plain = re.sub(r"\[[^\]]*\]", " ", answer)
    nums = re.findall(r"\d+(?:[.:]\d+)*", plain.replace(",", ""))
    problems += [f"number {n} is in no tool result" for n in nums if n not in corpus]
    return problems, len(cited), len(nums)


class Copilot:
    """The model with the four tools, the grounding check and the answer cache."""

    def __init__(self, rows: Rows, cache: Path, model: str = MODEL) -> None:
        """Hold the rows and the cache folder; create the client only when a call is needed."""
        self.rows, self.cache, self.model = rows, cache, model
        self.client: Any = None

    def _key(self, question: str, as_of: str, since: str, episode: str) -> str:
        blob = json.dumps(
            [
                self.model,
                PROMPT_VERSION,
                SYSTEM,
                TOOLS,
                question,
                as_of,
                since,
                episode,
                self.rows.digest,
            ],
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _run(self, messages: list[dict[str, Any]], as_of: datetime, results: list[Any]) -> str:
        for _ in range(6):
            msg = self.client.messages.create(
                model=self.model, max_tokens=900, system=SYSTEM, tools=TOOLS, messages=messages
            )
            messages.append({"role": "assistant", "content": msg.content})
            uses = [b for b in msg.content if b.type == "tool_use"]
            if not uses:
                return "".join(b.text for b in msg.content if b.type == "text").strip()
            replies = []
            for u in uses:
                fn = getattr(self.rows, u.name, None)
                out = (
                    fn(dict(u.input), as_of)
                    if u.name in {t["name"] for t in TOOLS} and fn
                    else {"error": "unknown tool"}
                )
                results.append(out)
                replies.append(
                    {"type": "tool_result", "tool_use_id": u.id, "content": json.dumps(out)}
                )
            messages.append({"role": "user", "content": replies})
        raise RuntimeError("the model did not finish within six tool rounds")

    def ask(
        self, question: str, as_of_utc: str, since: str = "", episode: str = ""
    ) -> dict[str, Any]:
        """Answer one question at the clock; cached, checked, never writes a decision."""
        key = self._key(question, as_of_utc, since, episode)
        path = self.cache / f"{key}.json"
        if path.exists():
            return {**json.loads(path.read_text("utf-8")), "cached": True}
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return {"ok": False, "reason": "no ANTHROPIC_API_KEY and no cached answer"}
        if self.client is None:
            import anthropic  # type: ignore[import-not-found,unused-ignore]  # optional extra

            self.client = anthropic.Anthropic()
        as_of = _utc(as_of_utc)
        context = f"Page clock: {_local(as_of)} (Europe/Vienna)."
        if since:
            context += f" The manager's window starts {since[:16]} local; use it unless asked."
        if episode:
            context += f" The manager has episode {episode} open."
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": f"{context}\n\nQuestion: {question}"}
        ]
        results: list[Any] = []
        answer = self._run(messages, as_of, results)
        problems, n_cited, n_nums = check(answer, results, context + question)
        retried = False
        words = len(answer.split())
        if problems or words > MAX_WORDS:
            retried = True
            fix = (
                "Your answer failed the grounding check: "
                + "; ".join(problems[:10])
                + ". Answer again, citing only ids from tool results and taking every "
                "number from them."
                if problems
                else ""
            )
            if words > MAX_WORDS:
                fix += (
                    f" Your answer has {words} words. Answer again in at most {MAX_WORDS} "
                    "words: one sentence, then at most 3 short '- ' lines."
                )
            messages.append({"role": "user", "content": fix.strip()})
            answer = self._run(messages, as_of, results)
            problems, n_cited, n_nums = check(answer, results, context + question)
        out: dict[str, Any] = {
            "ok": not problems,
            "answer": answer if not problems else None,
            "reason": "; ".join(problems[:5]) if problems else None,
            "citations": n_cited,
            "numbers": n_nums,
            "tool_calls": len(results),
            "retried": retried,
            "model": self.model,
            "prompt": PROMPT_VERSION,
        }
        self.cache.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
        return {**out, "cached": False}


def serve(page: Path, copilot: Copilot, port: int) -> None:
    """Serve the page at / and the copilot at /api on localhost until interrupted."""
    html = page.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send(code, body, "application/json")

        def do_GET(self) -> None:
            if self.path.split("?")[0] in ("/", "/index.html"):
                self._send(200, html, "text/html; charset=utf-8")
            elif self.path == "/api/health":
                live = bool(os.environ.get("ANTHROPIC_API_KEY"))
                self._json(200, {"model": copilot.model, "prompt": PROMPT_VERSION, "live": live})
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            if self.path != "/api/ask":
                self._send(404, b"not found", "text/plain")
                return
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                q = str(req.get("question", "")).strip()[:500]
                if not q:
                    self._json(400, {"ok": False, "reason": "empty question"})
                    return
                as_of = str(req.get("as_of") or datetime.now(UTC).isoformat())
                since, episode = str(req.get("since") or ""), str(req.get("episode") or "")
                self._json(200, copilot.ask(q, as_of, since, episode))
            except Exception as exc:
                self._json(500, {"ok": False, "reason": f"{type(exc).__name__}: {exc}"})

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"OSAM copilot on http://127.0.0.1:{port}/#view=ask  (model {copilot.model})")
    srv.serve_forever()


def main() -> None:
    """Parse the paths and serve."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("page", type=Path, help="the console page written by plato40 ui")
    ap.add_argument("decisions", type=Path)
    ap.add_argument("trace", type=Path)
    ap.add_argument("--cache", type=Path, default=Path("llm_cache_copilot"))
    ap.add_argument("--port", type=int, default=8040)
    ap.add_argument("--ask", help="answer one question on the command line and exit")
    ap.add_argument("--as-of", help="clock for --ask, UTC ISO time; default the end of the run")
    a = ap.parse_args()
    rows = Rows(a.decisions, a.trace)
    cp = Copilot(rows, a.cache)
    if a.ask:
        end = max(e["_t0"] for e in rows.episodes) + timedelta(days=1)
        print(json.dumps(cp.ask(a.ask, a.as_of or end.isoformat()), indent=1, ensure_ascii=False))
        return
    serve(a.page, cp, a.port)


if __name__ == "__main__":
    main()
